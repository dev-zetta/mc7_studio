"""State-preserving MC7 setting transactions, executed in the USB helper."""

from .button_commands import (BUTTON_SLOTS, build_button_report, decode_action,
                              decode_button_response)
from .configuration import Configuration
from .display_commands import build_application_report, decode_profile_response
from .lighting_commands import (brightness_from_percent, build_lighting_report,
                                decode_lighting_response, expected_lighting_state)
from .lcd_commands import LCD_WIDGETS, build_lcd_report, decode_lcd_response
from .screen_key_commands import (
    build_screen_key_read_request, build_screen_key_reports,
    decode_screen_key_responses, screen_key_bindings,
)
from .protocol import build_physical_dpi_report, decode_sensor_response
from .sensor_commands import (POLLING_RATES, build_angle_report, build_debounce_report,
    build_eco_report, build_haptic_report, build_lift_off_reports, build_polling_report,
    build_screen_report, build_standby_report, decode_advanced_sensor_response,
    decode_debounce_response, decode_eco_response, decode_haptic_response,
    decode_screen_response, decode_standby_response)
from .transport import DeviceError
from .status_commands import decode_status_response
from .image_commands import decode_background_selection_response, build_background_selection_report

SELECTORS = {"sensor": 0x10, "profile": 0x12, "lighting": 0x2A, "primary": 0x15,
             "easy_shift": 0x16, "haptic": 0x24, "screen": 0x2B,
             "standby": 0x05, "debounce": 0x1A, "eco": 0x26, "lcd": 0x25, "status": 0x09,
             "screen_keys": 0x29, "background": 0x2C}
PER_PROFILE = {"sensor", "lighting", "primary", "easy_shift", "lcd", "screen_keys"}


def read_raw(transport, name, profile):
    selector = SELECTORS[name]
    if name == "screen_keys":
        responses = []
        for page in range(3):
            transport.send(build_screen_key_read_request(profile, page))
            responses.append(transport.get_feature(selector))
        return b"".join(responses)
    index = profile if name in PER_PROFILE else 0
    transport.send(bytes((0x10, 0x1C, 0, selector, index, 0, 0)) + bytes(57))
    return transport.get_feature(selector)


def decode(name, raw, profile):
    if name == "sensor":
        return decode_advanced_sensor_response(raw, profile)
    if name in ("primary", "easy_shift"):
        return decode_button_response(raw, profile, name)
    if name == "lighting":
        return decode_lighting_response(raw, profile)
    if name == "lcd":
        return decode_lcd_response(raw, profile)
    if name == "screen_keys":
        return decode_screen_key_responses(raw, profile)
    if name == "status":
        return decode_status_response(raw)
    if name == "background":
        return decode_background_selection_response(raw)
    return {"profile": decode_profile_response, "haptic": decode_haptic_response,
            "screen": decode_screen_response, "standby": decode_standby_response,
            "debounce": decode_debounce_response, "eco": decode_eco_response}[name](raw)


def stable(name, raw):
    """Compare known setting fields, excluding device-generated checksums."""
    if name in ("primary", "easy_shift"):
        return raw[:52]
    if name == "lcd":
        return decode_lcd_response(raw, raw[3]).signature
    if name == "screen_keys":
        return decode_screen_key_responses(raw, raw[6]).signature
    if name == "background":
        # Only the selection byte is classified; no trailer checksum is known.
        return decode_background_selection_response(raw).background_index
    if name == "profile":
        state = decode_profile_response(raw)
        # This firmware expands the outgoing energy bit to a 0xF0 read nibble.
        return state.current_profile, state.profile_count, state.energy_saving
    return raw[:-1]


def lcd_edits(state, pages):
    if len(pages) > min(3, state.page_count):
        raise DeviceError("Enabling additional LCD pages is not yet supported")
    edits = {}
    for index, keys in enumerate(pages):
        current = state.pages[index].slots
        if keys == [None if widget is None else widget.key for widget in current]:
            continue
        preserved = {widget.key: widget for widget in current if widget is not None}
        slots = []
        for key in keys:
            if key is None or key in LCD_WIDGETS:
                slots.append(key)
            elif key in preserved:
                slots.append(preserved[key])
            else:
                raise DeviceError("Unknown LCD widgets can only be preserved on their current page")
        edits[index] = slots
    return edits


def read_bundle(transport, profile):
    settings, errors = {}, {}
    for name in SELECTORS:
        try:
            raw = read_raw(transport, name, profile)
            state = decode(name, raw, profile)
            settings[name] = state.raw.hex()
        except (ValueError, OSError, DeviceError) as error:
            if name == "sensor":
                raise
            errors[name] = str(error)
    macro_data = {}
    from .macro_io import read_macro
    for layer in ("primary", "easy_shift"):
        if layer not in settings:
            continue
        state = decode_button_response(bytes.fromhex(settings[layer]), profile, layer)
        for logical in BUTTON_SLOTS:
            if state.records[logical][3] != 7:
                continue
            key = f"{layer}:{logical}"
            try:
                macro_data[key] = read_macro(transport, profile, logical, layer).hex()
            except (OSError, ValueError, DeviceError) as error:
                errors[f"macro/{key}"] = str(error)
    if "lcd" in settings:
        lcd = decode_lcd_response(bytes.fromhex(settings["lcd"]), profile)
        for page, current in enumerate(lcd.pages[:min(3, lcd.page_count)]):
            for cell, widget in enumerate(current.slots):
                if widget is None or widget.key != "macro":
                    continue
                logical = page * 4 + cell
                key = f"lcd:{logical}"
                try:
                    macro_data[key] = read_macro(transport, profile, logical, "lcd").hex()
                except (OSError, ValueError, DeviceError) as error:
                    errors[f"macro/{key}"] = str(error)
    return {"raw": settings["sensor"], "settings": settings, "errors": errors,
            "macro_data": macro_data, "changed": False}


def _plan(section, configuration, raw, profile, *, macro_plans=None, force_layers=(),
          force_lcd=False, host_action_icon_indices=None):
    """Return commands and predicted responses; never send while validating."""
    # Battery/charging telemetry may change independently during any update.
    expected = {name: bytearray(value) for name, value in raw.items() if name != "status"}
    commands = []
    def add(packet):
        commands.append(packet)
    sensor = configuration.sensor
    if section == "sensor":
        state = decode_advanced_sensor_response(raw["sensor"], profile)
        dpi = decode_sensor_response(raw["sensor"], profile)
        target = expected["sensor"]
        values = [s.value for s in sensor.stages]
        colors = [tuple(bytes.fromhex(s.color[1:])) for s in sensor.stages]
        enabled = [s.enabled for s in sensor.stages]
        packet = build_physical_dpi_report(profile_index=profile, current_stage=sensor.current_stage,
                    dpi=values, colors=colors, enabled=enabled, indicator_enabled=sensor.dpi_indicator_enabled)
        if (dpi.current_stage, dpi.dpi, dpi.colors, dpi.enabled, dpi.indicator_enabled) != (
                sensor.current_stage, tuple(values), tuple(colors), tuple(enabled), sensor.dpi_indicator_enabled):
            add(packet)
            target[9] = sensor.current_stage
            for i, stage in enumerate(sensor.stages):
                target[10+7*i] = int(stage.enabled)
                target[11+7*i:13+7*i] = (stage.value//50-1).to_bytes(2, "little")
                target[13+7*i:16+7*i] = bytes(colors[i])
                target[16+7*i] = int(sensor.dpi_indicator_enabled)
        if (sensor.polling_rate, sensor.motion_sync) != (state.polling_rate_usb, state.motion_sync):
            add(build_polling_report(profile_index=profile, polling_rate=sensor.polling_rate, motion_sync=sensor.motion_sync))
            target[4] = target[5] = POLLING_RATES.index(sensor.polling_rate)
            target[46] = int(sensor.motion_sync)
        if (sensor.angle_snapping, sensor.angle_tuning, sensor.angle_tuning_enabled) != state.angle_signature[1:]:
            add(build_angle_report(profile_index=profile, angle_snapping=sensor.angle_snapping,
                                   angle_tuning=sensor.angle_tuning, angle_tuning_enabled=sensor.angle_tuning_enabled))
            target[6:9] = bytes((int(sensor.angle_snapping), sensor.angle_tuning & 255, int(sensor.angle_tuning_enabled)))
        lod = {0: "very_low", 0x85: "low"}.get(state.lift_off_raw, "custom")
        if sensor.lift_off_distance != lod:
            commands.extend(build_lift_off_reports(sensor.lift_off_distance))
            target[45] = 0 if sensor.lift_off_distance == "very_low" else 0x85
        debounce = decode_debounce_response(raw["debounce"])
        if sensor.debounce_ms != debounce.debounce_ms:
            add(build_debounce_report(debounce_ms=sensor.debounce_ms))
            expected["debounce"][3:5] = bytes((sensor.debounce_ms, sensor.debounce_ms))
    elif section == "lighting":
        state = decode_lighting_response(raw["lighting"], profile)
        if state.effect is None or not 1 <= state.speed <= 10:
            raise DeviceError("Unknown device lighting effect or speed; existing settings have been preserved")
        lighting = configuration.lighting
        if lighting.speed not in range(10, 101, 10):
            raise DeviceError("Lighting speed must be 10..100 in steps of 10")
        desired_enabled = lighting.effect != "off"
        effect = None
        enabled = None
        semantic_changed = lighting.effect != state.effect
        if semantic_changed:
            if not state.extended_layout or desired_enabled:
                effect = lighting.effect
            if state.extended_layout and desired_enabled != bool(state.enabled_raw):
                enabled = desired_enabled
        packet = build_lighting_report(state,
            effect=effect,
            brightness_raw=brightness_from_percent(lighting.brightness, baseline=state),
            speed=lighting.speed//10 if lighting.speed != state.speed*10 else None,
            color=list(bytes.fromhex(lighting.color[1:])),
            enabled=enabled)
        predicted = expected_lighting_state(packet, state)
        if state.signature != predicted.signature:
            add(packet)
            expected["lighting"] = bytearray(predicted.raw)
    elif section == "buttons":
        for layer in ("primary", "easy_shift"):
            state = decode_button_response(raw[layer], profile, layer)
            edits = {}
            for binding in configuration.buttons:
                slot = BUTTON_SLOTS[binding.button_id-1]
                action = getattr(binding, layer)
                old = decode_action(state.records[slot])
                if action.kind == "macro":
                    if macro_plans is None or (layer, slot) not in macro_plans:
                        raise DeviceError("Compile and validate macro data before assigning it")
                elif action.kind == "device":
                    if action.value.lower() != state.records[slot].hex():
                        raise DeviceError("Unknown device actions can only be preserved, not imported as new assignments")
                elif action != old:
                    edits[slot] = action
            requested = [slot for (name, slot) in (macro_plans or {}) if name == layer]
            if edits or requested:
                packet = bytearray(build_button_report(state, edits))
                for slot in requested:
                    packet[3+4*slot:7+4*slot] = macro_plans[(layer, slot)].assignment
                if layer == "primary":
                    from .configuration import Action
                    for needed in (Action("mouse", "left"), Action("mouse", "right")):
                        if not any(decode_action(packet[3+4*i:7+4*i]) == needed for i in BUTTON_SLOTS):
                            raise DeviceError("Keep reachable left and right click assignments in the primary layer")
                packet = bytes(packet)
                if packet[3:51] == b"".join(state.records) and layer not in force_layers:
                    continue
                add(packet)
                expected[layer][4:52] = packet[3:51]
    elif section == "display":
        screen = decode_screen_response(raw["screen"])
        display = configuration.display
        if display.background_index is not None:
            if "background" not in raw:
                raise DeviceError("Read the background selection successfully before applying it")
            if display.background_index != decode_background_selection_response(raw["background"]).background_index:
                add(build_background_selection_report(display.background_index))
                expected["background"][3] = display.background_index
        if screen.signature != (display.brightness, display.timeout_value):
            add(build_screen_report(brightness=display.brightness, timeout_value=display.timeout_value))
            expected["screen"][3:5] = bytes((display.brightness, display.timeout_value))
        haptic = ("off", "low", "medium", "high").index(display.haptic_intensity)
        if haptic != decode_haptic_response(raw["haptic"]).intensity:
            add(build_haptic_report(intensity=haptic))
            expected["haptic"][3] = haptic
        if display.pages:
            if "lcd" not in raw:
                raise DeviceError("Read the LCD layout successfully before applying it")
            if "screen_keys" not in raw:
                raise DeviceError("Read the LCD key definitions successfully before applying the layout")
            state = decode_lcd_response(raw["lcd"], profile)
            key_state = decode_screen_key_responses(raw["screen_keys"], profile)
            current_pages = [
                [None if widget is None else widget.key for widget in page.slots]
                for page in state.pages[:3]
            ]
            desired_pages = [list(page) for page in current_pages]
            for index, page in enumerate(display.pages):
                desired_pages[index] = list(page)
            desired_bindings = [list(page) for page in screen_key_bindings(key_state, state)]
            for index in range(len(display.pages)):
                desired_bindings[index] = (list(display.key_bindings[index])
                                           if display.key_bindings else [None] * 4)
            macro_records = [[None] * 4 for _ in range(3)]
            host_action_targets = [[None] * 4 for _ in range(3)]
            for page, row in enumerate(display.host_action_bindings):
                host_action_targets[page] = list(row)
            library = {macro.id: macro for macro in configuration.macros}
            for page, row in enumerate(display.pages):
                for cell, widget in enumerate(row):
                    if widget != "macro":
                        continue
                    macro_id = (display.macro_bindings[page][cell]
                                if display.macro_bindings else None)
                    if macro_id is None:
                        continue
                    if macro_plans is None or ("lcd", page * 4 + cell) not in macro_plans:
                        raise DeviceError("Compile and validate LCD macro data before assigning its tile")
                    from .screen_key_commands import encode_lcd_macro_record
                    macro_records[page][cell] = encode_lcd_macro_record(
                        library[macro_id].name, library[macro_id].playback)
            key_reports, predicted_keys = build_screen_key_reports(
                key_state, state, desired_pages, desired_bindings, macro_records,
                host_action_targets, host_action_icon_indices)
            commands.extend(key_reports)
            expected["screen_keys"] = bytearray(predicted_keys.raw)
            edits = lcd_edits(state, display.pages)
            if edits or force_lcd:
                packet = build_lcd_report(state, pages=edits)
                add(packet)
                prediction = bytearray(packet[:61])
                prediction[2] = 0
                prediction[60] = -sum(prediction[2:60]) & 255
                expected["lcd"] = prediction
    elif section == "power":
        power = configuration.power
        lighting = decode_lighting_response(raw["lighting"], profile)
        # Unsupported device values are preserved. The configuration snapshot
        # deliberately leaves its safe default in place when it cannot express
        # the raw value, so an unrelated power edit must not overwrite it.
        if (0 <= lighting.led_timeout_raw <= 30
                and power.led_timeout_value != lighting.led_timeout_raw):
            packet = build_lighting_report(lighting, led_timeout_raw=power.led_timeout_value)
            add(packet)
            expected["lighting"] = bytearray(expected_lighting_state(packet, lighting).raw)
        if power.standby_value != decode_standby_response(raw["standby"]).standby_value:
            add(build_standby_report(standby_value=power.standby_value))
            expected["standby"][3] = power.standby_value
        if power.eco_mode != decode_eco_response(raw["eco"]).enabled:
            add(build_eco_report(enabled=power.eco_mode))
            expected["eco"][3] = int(power.eco_mode)
        state = decode_profile_response(raw["profile"])
        if power.energy_saving != state.energy_saving:
            add(build_application_report(state, True, energy_saving=power.energy_saving))
            expected["profile"][4] = state.profile_count | (int(power.energy_saving) << 4)
            expected["profile"][5] = -sum(expected["profile"][2:5]) & 255
    else:
        raise DeviceError("Unsupported settings section")
    return commands, expected


REQUIRED = {"sensor": ("sensor", "debounce"), "lighting": ("lighting",),
            "buttons": ("primary", "easy_shift"), "display": ("screen", "haptic"),
            "power": ("standby", "eco", "profile", "lighting")}


def transact_settings(request, transport):
    slot = request["profile_slot"]
    if type(slot) is not int or not 1 <= slot <= 5:
        raise DeviceError("Profile slot must be 1..5")
    profile, operation = slot-1, request["operation"]
    identity = str(getattr(transport, "location_id", request["device_id"]))
    if operation == "read_active_profile":
        state = decode_profile_response(read_raw(transport, "profile", profile))
        result = {"active_profile": state.current_profile + 1,
                  "profile_count": state.profile_count,
                  "energy_saving": state.energy_saving,
                  "raw": state.raw.hex(),
                  "changed": False}
    elif operation == "setup_display":
        before = decode_lcd_response(read_raw(transport, "lcd", profile), profile)
        keys_before = decode_screen_key_responses(
            read_raw(transport, "screen_keys", profile), profile)
        edits = {}
        for index, page in enumerate(before.pages[:min(3, before.page_count)]):
            slots = list(page.slots)
            for cell, widget in enumerate(slots):
                if widget is not None and widget.key == "download_swarm":
                    slots[cell:cell+3] = [LCD_WIDGETS[key] for key in ("next_track", "led_brightness", "play_pause")]
                elif widget is not None and widget.key == "polling_rate":
                    # Catalogued by the vendor, but this firmware does not render it.
                    slots[cell] = LCD_WIDGETS["next_track"]
            if slots != list(page.slots):
                edits[index] = slots
        if edits:
            from .lcd_commands import edited_lcd_signature
            try:
                pages = [list(page.slots) for page in before.pages[:3]]
                for index, slots in edits.items():
                    pages[index] = slots
                bindings = [list(row) for row in screen_key_bindings(keys_before, before)]
                key_reports, expected_keys = build_screen_key_reports(
                    keys_before, before, pages, bindings)
                for report in key_reports:
                    transport.send(report)
                transport.send(build_lcd_report(before, pages=edits))
                keys_after = decode_screen_key_responses(
                    read_raw(transport, "screen_keys", profile), profile)
                after = decode_lcd_response(read_raw(transport, "lcd", profile), profile)
                if keys_after.signature != expected_keys.signature:
                    raise DeviceError("LCD key-definition readback did not match")
                if after.signature != edited_lcd_signature(before, pages=edits):
                    raise DeviceError("LCD layout readback did not match")
            except (OSError, ValueError, DeviceError) as error:
                raise DeviceError(f"LCD setup could not be verified: {error}. Read the mouse again.") from error
        result = read_bundle(transport, profile)
        result["changed"] = bool(edits)
        result["lcd_setup_verified"] = True
    elif operation in ("activate", "switch_profile"):
        before = decode_profile_response(read_raw(transport, "profile", profile))
        selected = profile if operation == "switch_profile" else before.current_profile
        if selected != before.current_profile or operation == "activate":
            transport.send(build_application_report(before, True, profile_index=selected))
            after = decode_profile_response(read_raw(transport, "profile", profile))
        else:
            after = before
        if (after.current_profile, after.profile_count, after.energy_saving) != (selected, before.profile_count, before.energy_saving):
            raise DeviceError("Profile/application command readback failed; read the mouse again")
        result = read_bundle(transport, profile)
        result["activation_acknowledged"] = True
        result["changed"] = operation == "switch_profile" and before.current_profile != selected
    else:
        result = read_bundle(transport, profile)
        if operation == "apply_settings":
            section = request["section"]
            if section not in REQUIRED:
                raise DeviceError("Unsupported settings section")
            baseline = request.get("baseline") or {}
            if (baseline.get("device_id"), baseline.get("profile_slot"), baseline.get("transport_identity")) != (request["device_id"], slot, identity):
                raise DeviceError("Read this mouse/profile before applying settings")
            raw = {name: bytes.fromhex(value) for name, value in result["settings"].items()}
            required = REQUIRED[section] + (("lcd", "screen_keys")
                if section == "display" and request.get("configuration", {}).get("display", {}).get("pages") else ())
            if section == "display" and request.get("configuration", {}).get("display", {}).get("background_index") is not None:
                required += ("background",)
            for name in required:
                previous = baseline.get("settings", {}).get(name)
                if name not in raw or not previous:
                    raise DeviceError(f"Read the {name} settings successfully before applying")
                if stable(name, bytes.fromhex(previous)) != stable(name, raw[name]):
                    raise DeviceError(f"The {name} settings changed since the last read. Read again before applying.")
            config = Configuration.from_dict(request["configuration"])
            if config.profile_slot != slot:
                raise DeviceError("Draft and target profile do not match")
            macro_plans, macro_updates, disabling = {}, {}, {}
            custom_icon_plan = None
            if section == "buttons":
                from .macro_io import prepare_macro_updates
                macro_plans, macro_updates, disabling = prepare_macro_updates(
                    transport, config, raw, result.get("macro_data", {}), baseline.get("macro_data", {}))
            elif section == "display" and config.display.pages:
                from .macro_io import prepare_lcd_macro_updates
                macro_plans, macro_updates, disabling = prepare_lcd_macro_updates(
                    transport, config, raw["lcd"], result.get("macro_data", {}),
                    baseline.get("macro_data", {}))
                needs_custom_icons = any(
                    widget == "open_application"
                    and page_index < len(config.display.host_action_bindings)
                    and config.display.host_action_bindings[page_index][cell] is not None
                    for page_index, page in enumerate(config.display.pages)
                    for cell, widget in enumerate(page)
                )
                if needs_custom_icons:
                    from .custom_icons import plan_custom_icons
                    current_lcd = decode_lcd_response(raw["lcd"], profile)
                    current_keys = decode_screen_key_responses(
                        raw["screen_keys"], profile)
                    all_profile_keys = []
                    for other_profile in range(5):
                        if other_profile == profile:
                            all_profile_keys.append(current_keys)
                        else:
                            all_profile_keys.append(decode_screen_key_responses(
                                read_raw(transport, "screen_keys", other_profile),
                                other_profile))
                    custom_icon_plan = plan_custom_icons(
                        current_lcd, current_keys, all_profile_keys,
                        config.display.pages,
                        config.display.host_action_bindings,
                        config.display.host_action_icon_bindings,
                        previous_icons=baseline.get(
                            "host_action_icon_bindings", []),
                        previous_icon_indices=baseline.get(
                            "host_action_icon_indices", []),
                    )
            commands, expected = _plan(section, config, raw, profile,
                                      macro_plans=macro_plans, force_layers=disabling,
                                      force_lcd="lcd" in disabling,
                                      host_action_icon_indices=(
                                          custom_icon_plan.icon_indices
                                          if custom_icon_plan is not None else None))
            predictions = {name: stable(name, bytes(value)) for name, value in expected.items()}
            custom_icon_uploads = ()
            if (commands or macro_updates or
                    (custom_icon_plan is not None and custom_icon_plan.uploads)):
                try:
                    if custom_icon_plan is not None and custom_icon_plan.uploads:
                        from .custom_icons import upload_custom_icons
                        custom_icon_uploads = upload_custom_icons(
                            custom_icon_plan.uploads, transport)
                    if macro_updates:
                        from .macro_io import execute_macro_updates
                        execute_macro_updates(transport, macro_updates, disabling)
                    for command in commands:
                        transport.send(command)
                    after = read_bundle(transport, profile)
                    for name, prediction in predictions.items():
                        observed = after["settings"].get(name)
                        if not observed or stable(name, bytes.fromhex(observed)) != prediction:
                            raise DeviceError(f"The {name} readback does not match the planned update")
                    for (layer, logical), plan in macro_plans.items():
                        observed = after.get("macro_data", {}).get(f"{layer}:{logical}")
                        if observed != plan.expected_read_payload.hex():
                            raise DeviceError("The assigned macro's final readback does not match the planned update")
                    result = after
                    result["changed"] = True
                    result["custom_icon_uploads"] = custom_icon_uploads
                except (OSError, ValueError, DeviceError) as error:
                    macro_note = ""
                    if "lcd" in disabling:
                        macro_note = " A replaced LCD macro tile may remain disabled after an interrupted upload."
                    elif disabling:
                        macro_note = " A replaced macro button may remain disabled after an interrupted upload."
                    icon_note = (
                        " Uploaded icon pixels may remain in an unreferenced store; "
                        "no LCD tile was bound to an incomplete upload."
                        if custom_icon_plan is not None and custom_icon_plan.uploads
                        else "")
                    raise DeviceError(
                        f"Apply could not be verified: {error}. Some settings may have changed; "
                        "read again." + macro_note + icon_note) from error
            if custom_icon_plan is not None:
                result["custom_icon_indices"] = [
                    list(row) for row in custom_icon_plan.icon_indices]
            result["macro_timing_adjustments"] = [
                {"layer": layer, "logical_slot": logical, "event_index": a.event_index,
                 "requested_ticks": a.requested_ticks, "encoded_ticks": a.encoded_ticks}
                for (layer, logical), plan in macro_plans.items() for a in plan.timing_adjustments]
    result["transport_identity"] = identity
    result["acknowledgements"] = transport.acknowledgements
    return result
