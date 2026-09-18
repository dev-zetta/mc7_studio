"""Desktop discovery, device snapshots and configuration through isolated USB I/O."""

from datetime import datetime, timezone
import base64
import copy
import hashlib
import json
import platform
import subprocess

from .configuration import Action, Configuration, DPIStage, MAX_MACROS
from .devices import enumerate_devices
from .protocol import decode_sensor_response
from .runtime import helper_command
from .transport import DeviceError, has_report, physical_id

CAPABILITIES = ["read_sensor", "read_settings", "read_active_profile", "apply_sensor", "apply_buttons",
                "apply_lighting", "apply_display", "apply_power", "activate_display", "switch_profile",
                "read_status", "upload_background", "apply_macros", "host_lcd", "countdown",
                "calibrate_lift_off"]


class DeviceService:
    def discover(self) -> list[dict]:
        result = []
        enumeration = enumerate_devices()
        if platform.system() != "Linux":
            native = {"Darwin": ("mc7-macos", "macOS"),
                      "Windows": ("mc7-windows", "Windows")}.get(platform.system())
            if native and any(d.product_id == 0x502C for d in enumeration.devices):
                device_id, label = native
                result.append({"id": device_id, "label": f"Command Series MC7 · {label}",
                               "product_id": 0x502C, "connected": True,
                               "capabilities": CAPABILITIES[:],
                               "detail": f"Experimental {label} USB transport; hardware validation is pending. Connect exactly one mouse directly by USB."})
            return result
        for device in enumeration.devices:
            if device.interface_number != 2 or not has_report(device, "feature", 64):
                continue
            mouse = device.product_id == 0x502C
            events = [d for d in enumeration.devices if d.product_id == device.product_id
                      and physical_id(d) == physical_id(device) and d.interface_number == 1
                      and has_report(d, "input", 8)]
            accessible = (device.device_node_writable and len(events) == 1
                          and events[0].device_node_readable)
            result.append({
                "id": physical_id(device), "label": f"Command Series MC7 · {'USB mouse' if mouse else 'transmitter'}",
                "product_id": device.product_id, "connected": True,
                "capabilities": CAPABILITIES[:] if mouse and accessible else [],
                "detail": ("Native sensor, buttons, lighting, display and power settings" if accessible and mouse else
                           "Connect the mouse directly by USB to configure it" if not mouse else
                           "Raw HID access is unavailable; open Device > Host integration and install the MC7 udev rule"),
            })
        return sorted(result, key=lambda d: (d["product_id"], d["id"]))

    @staticmethod
    def _call(request: dict) -> dict:
        if platform.system() not in ("Linux", "Darwin", "Windows"):
            raise DeviceError("Device transactions require Linux, macOS or Windows with the mouse connected by USB")
        uncertainty = " The mouse may have changed; read it again." if request["operation"] not in ("read", "read_settings", "read_active_profile", "read_status") else ""
        deadline = {"upload_background": 120, "apply_settings": 90,
                    "read_settings": 60}.get(request["operation"], 20)
        if (request.get("operation") == "apply_settings"
                and request.get("section") == "display"):
            pages = request.get("configuration", {}).get("display", {}).get(
                "pages", [])
            application_tiles = sum(
                widget == "open_application" for page in pages for widget in page)
            if application_tiles:
                deadline = min(360, max(deadline, 45 + 25 * application_tiles))
        try:
            process = subprocess.run(helper_command("swarm2.hardware"),
                                     input=json.dumps(request), text=True, capture_output=True,
                                     timeout=deadline)
        except subprocess.TimeoutExpired as error:
            raise DeviceError(f"MC7 operation exceeded its {deadline}-second deadline." + uncertainty) from error
        try:
            payload = json.loads(process.stdout)
        except ValueError as error:
            raise DeviceError("The MC7 helper stopped without a valid response." + uncertainty) from error
        if not isinstance(payload, dict):
            raise DeviceError("The MC7 helper returned an invalid response." + uncertainty)
        if "error" in payload:
            message = str(payload["error"])
            raise DeviceError(message + (uncertainty if "may have changed" not in message else ""))
        if process.returncode != 0 or "result" not in payload:
            raise DeviceError("The MC7 helper failed." + uncertainty)
        return payload["result"]

    @staticmethod
    def _snapshot(device_id: str, profile_slot: int, result: dict,
                  draft: Configuration | None = None) -> dict:
        state = decode_sensor_response(bytes.fromhex(result["raw"]), profile_slot - 1)
        configuration = Configuration.from_dict(draft.to_dict()) if draft else Configuration()
        configuration.profile_slot = profile_slot
        configuration.sensor.current_stage = state.current_stage
        configuration.sensor.stages = [
            DPIStage(value, enabled, "#" + bytes(color).hex().upper())
            for value, enabled, color in zip(state.dpi, state.enabled, state.colors)]
        configuration.sensor.dpi_indicator_enabled = state.indicator_enabled
        verified = ["sensor.stages", "sensor.current_stage", "sensor.dpi_indicator_enabled"]
        active_profile = None
        device_status = None
        known_host_action_icon_bindings = []
        known_host_action_icon_indices = []
        errors = dict(result.get("errors", {}))
        extra = result.get("settings", {})
        if extra:
            from .button_commands import BUTTON_SLOTS, decode_action
            from .settings import decode
            states = {name: decode(name, bytes.fromhex(raw), profile_slot-1) for name, raw in extra.items()}
            def field(section, name, value):
                setattr(getattr(configuration, section), name, value)
                verified.append(f"{section}.{name}")
            advanced = states["sensor"]
            for attr, value in (("polling_rate", advanced.polling_rate_usb), ("motion_sync", advanced.motion_sync),
                                ("angle_snapping", advanced.angle_snapping), ("angle_tuning", advanced.angle_tuning),
                                ("angle_tuning_enabled", advanced.angle_tuning_enabled),
                                ("lift_off_distance", {0:"very_low",0x85:"low"}.get(advanced.lift_off_raw,"custom"))):
                field("sensor", attr, value)
            if "debounce" in states:
                field("sensor", "debounce_ms", states["debounce"].debounce_ms)
            if all(layer in states for layer in ("primary", "easy_shift")):
                for binding in configuration.buttons:
                    for layer in ("primary", "easy_shift"):
                        raw = states[layer].records[BUTTON_SLOTS[binding.button_id-1]]
                        setattr(binding, layer, decode_action(raw) or Action("device", raw.hex()))
                verified.append("buttons")
            if "lighting" in states:
                light = states["lighting"]
                if light.effect is not None:
                    field("lighting", "effect", light.effect)
                field("lighting", "brightness", light.brightness_percent)
                if 1 <= light.speed <= 10:
                    field("lighting", "speed", light.speed*10)
                field("lighting", "color", "#"+bytes(light.color).hex().upper())
                if 0 <= light.led_timeout_raw <= 30:
                    field("power", "led_timeout_value", light.led_timeout_raw)
            if "screen" in states:
                field("display", "brightness", states["screen"].brightness)
                field("display", "timeout_value", states["screen"].timeout_value)
            if "background" in states:
                field("display", "background_index", states["background"].background_index)
            if "lcd" in states:
                lcd = states["lcd"]
                prior_pages = copy.deepcopy(configuration.display.pages)
                prior_timer_bindings = copy.deepcopy(
                    configuration.display.timer_bindings)
                prior_host_action_bindings = copy.deepcopy(
                    configuration.display.host_action_bindings)
                prior_host_action_icon_bindings = copy.deepcopy(
                    configuration.display.host_action_icon_bindings)
                pages = [[None if w is None else w.key for w in page.slots]
                         for page in lcd.pages[:min(3, lcd.page_count)]]
                field("display", "pages", pages)
                # Command 0x25 exposes only the 46/00 countdown tile. TimerID
                # and duration remain local, so retain them only when the same
                # coordinate was already a countdown tile in the supplied
                # draft. A raw read represents every other tile as unresolved.
                timer_bindings = [[None] * 4 for _ in pages]
                timer_ids = {timer.id for timer in configuration.countdown_timers}
                for page_index, page in enumerate(pages):
                    for cell, widget in enumerate(page):
                        if (widget == "countdown"
                                and page_index < len(prior_pages)
                                and cell < len(prior_pages[page_index])
                                and prior_pages[page_index][cell] == "countdown"
                                and page_index < len(prior_timer_bindings)
                                and cell < len(prior_timer_bindings[page_index])
                                and prior_timer_bindings[page_index][cell] in timer_ids):
                            timer_bindings[page_index][cell] = (
                                prior_timer_bindings[page_index][cell])
                configuration.display.timer_bindings = timer_bindings
                # Website and filesystem targets never live on the mouse. Keep
                # a supplied target only while the same host-action tile stays
                # at the same coordinate. When command 0x29 was read, also
                # require its short trigger record to match the local target.
                from .lcd_commands import LCD_HOST_ACTION_WIDGETS
                from .host_actions import (
                    encode_host_action_record, same_host_action_record,
                )
                host_action_bindings = [[None] * 4 for _ in pages]
                host_action_icon_bindings = [[None] * 4 for _ in pages]
                known_host_action_icon_bindings = [[None] * 4 for _ in pages]
                known_host_action_icon_indices = [[None] * 4 for _ in pages]
                reported_icon_indices = result.get("custom_icon_indices")
                if reported_icon_indices is not None:
                    if (not isinstance(reported_icon_indices, list)
                            or len(reported_icon_indices) != 3
                            or any(
                                not isinstance(row, list) or len(row) != 4
                                or any(index is not None and (
                                    type(index) is not int or not 0 <= index <= 19)
                                    for index in row)
                                for row in reported_icon_indices)):
                        raise DeviceError(
                            "The MC7 helper returned invalid custom-icon store metadata.")
                for page_index, page in enumerate(pages):
                    for cell, widget in enumerate(page):
                        if (widget not in LCD_HOST_ACTION_WIDGETS
                                or page_index >= len(prior_pages)
                                or cell >= len(prior_pages[page_index])
                                or prior_pages[page_index][cell] != widget
                                or page_index >= len(prior_host_action_bindings)
                                or cell >= len(prior_host_action_bindings[page_index])):
                            continue
                        target = prior_host_action_bindings[page_index][cell]
                        if target is None:
                            continue
                        icon = None
                        icon_index = None
                        if widget == "open_application":
                            if (page_index >= len(prior_host_action_icon_bindings)
                                    or cell >= len(
                                        prior_host_action_icon_bindings[page_index])):
                                continue
                            icon = prior_host_action_icon_bindings[page_index][cell]
                            if icon is None or "screen_keys" not in states:
                                continue
                        if "screen_keys" in states:
                            record = states["screen_keys"].pages[page_index].records[cell]
                            if widget == "open_application":
                                icon_index = record[4] - 1 if 1 <= record[4] <= 20 else None
                            try:
                                requested = encode_host_action_record(
                                    widget, target, icon_index=icon_index)
                            except (TypeError, ValueError):
                                continue
                            if not same_host_action_record(record, requested, widget):
                                continue
                        host_action_bindings[page_index][cell] = target
                        host_action_icon_bindings[page_index][cell] = icon
                        if (widget == "open_application"
                                and reported_icon_indices is not None
                                and reported_icon_indices[page_index][cell]
                                == icon_index):
                            known_host_action_icon_bindings[page_index][cell] = icon
                            known_host_action_icon_indices[page_index][cell] = icon_index
                configuration.display.host_action_bindings = (
                    host_action_bindings
                    if any(widget in LCD_HOST_ACTION_WIDGETS
                    for page in pages for widget in page)
                    else [])
                configuration.display.host_action_icon_bindings = (
                    host_action_icon_bindings
                    if configuration.display.host_action_bindings else [])
                configuration.display.macro_bindings = [
                    [None] * 4 for _ in configuration.display.pages]
                if "screen_keys" in states:
                    from .screen_key_commands import screen_key_bindings
                    bindings = screen_key_bindings(states["screen_keys"], lcd)
                    field("display", "key_bindings",
                          [list(page) for page in bindings[:len(configuration.display.pages)]])
            if result.get("macro_data"):
                from .macro_commands import (
                    NORMAL_MACRO_ASSIGNMENT, compile_keyboard_macro,
                    macro_playback_metadata,
                )
                from .macro_profiles import decode_device_macro, macro_to_keyboard_events
                from .screen_key_commands import lcd_macro_record_mode

                def same_macro(incoming, requested, *, normalized=False):
                    if incoming.name != requested.name:
                        return False
                    try:
                        if (macro_playback_metadata(incoming.playback, incoming.repeat)
                                != macro_playback_metadata(requested.playback, requested.repeat)):
                            return False
                        actual = macro_to_keyboard_events(incoming)
                        wanted = macro_to_keyboard_events(requested)
                        return actual == wanted or (normalized and actual == compile_keyboard_macro(wanted).events)
                    except ValueError:
                        # An unsupported local draft must not prevent a
                        # different supported device macro being imported.
                        return False

                namespace = hashlib.sha256(device_id.encode()).hexdigest()[:8]
                draft_library = {macro.id: macro for macro in draft.macros} if draft else {}
                imported_by_id = {}
                imported = False
                for key, payload in result["macro_data"].items():
                    try:
                        layer, logical_text = key.split(":")
                        logical = int(logical_text)
                        requested_id = None
                        if layer == "lcd":
                            page, cell = divmod(logical, 4)
                            if ("lcd" not in states or "screen_keys" not in states
                                    or page >= len(configuration.display.pages)
                                    or cell >= 4
                                    or configuration.display.pages[page][cell] != "macro"):
                                raise DeviceError("LCD macro data does not match an active Macro tile")
                            trigger_mode = lcd_macro_record_mode(
                                states["screen_keys"].pages[page].records[cell])
                            assignment = NORMAL_MACRO_ASSIGNMENT
                            if (draft is not None
                                    and page < len(draft.display.macro_bindings)
                                    and cell < len(draft.display.macro_bindings[page])):
                                requested_id = draft.display.macro_bindings[page][cell]
                        elif layer in ("primary", "easy_shift"):
                            binding = next(
                                b for b in configuration.buttons
                                if BUTTON_SLOTS[b.button_id - 1] == logical)
                            assignment = states[layer].records[logical]
                            if draft is not None:
                                prior = next(
                                    b for b in draft.buttons
                                    if b.button_id == binding.button_id)
                                requested = getattr(prior, layer)
                                if requested.kind == "macro":
                                    requested_id = requested.value
                        else:
                            raise DeviceError("Macro data uses an unsupported storage namespace")

                        macro = decode_device_macro(
                            bytes.fromhex(payload), profile_index=profile_slot - 1,
                            logical_slot=logical, layer=layer, assignment=assignment)
                        if (layer == "lcd"
                                and (trigger_mode == "toggle") != (macro.playback == "toggle")):
                            raise DeviceError(
                                "LCD macro trigger mode and stored loop count do not match")
                        device_macro_id = f"device_{namespace}_p{profile_slot}_{layer}_{logical}"
                        macro.id = device_macro_id
                        requested_macro = draft_library.get(requested_id)
                        reuse_requested = False
                        if (requested_macro is not None
                                and same_macro(macro, requested_macro, normalized=True)
                                and (requested_id not in imported_by_id
                                     or same_macro(macro, imported_by_id[requested_id]))):
                            macro.id = requested_id
                            reuse_requested = True
                        if not reuse_requested:
                            # A prior device import is also a saved local
                            # library entry. Keep it if this slot changed, and
                            # avoid collisions with user-chosen IDs.
                            existing = {old.id: old for old in configuration.macros}
                            suffix = 2
                            while macro.id in existing and not same_macro(macro, existing[macro.id]):
                                macro.id = f"{device_macro_id}_{suffix}"
                                suffix += 1
                        position = next(
                            (i for i, old in enumerate(configuration.macros)
                             if old.id == macro.id), None)
                        if position is None:
                            if len(configuration.macros) >= MAX_MACROS:
                                raise DeviceError(
                                    "The local macro library is full; the on-device assignment has been preserved")
                            configuration.macros.append(macro)
                        else:
                            configuration.macros[position] = macro
                        imported_by_id[macro.id] = macro
                        if layer == "lcd":
                            configuration.display.macro_bindings[page][cell] = macro.id
                        else:
                            setattr(binding, layer, Action("macro", macro.id))
                        imported = True
                    except (KeyError, StopIteration, ValueError, DeviceError) as error:
                        errors[f"macro/{key}"] = str(error)
                if imported:
                    verified.append("macros")
            if "lcd" in states:
                active_lcd_macros = {
                    page * 4 + cell
                    for page, row in enumerate(configuration.display.pages)
                    for cell, widget in enumerate(row)
                    if widget == "macro"
                }
                resolved_lcd_macros = {
                    page * 4 + cell
                    for page, row in enumerate(configuration.display.macro_bindings)
                    for cell, macro_id in enumerate(row)
                    if macro_id is not None
                }
                if active_lcd_macros <= resolved_lcd_macros:
                    verified.append("display.macro_bindings")
            if "haptic" in states:
                field("display", "haptic_intensity", ("off","low","medium","high")[states["haptic"].intensity])
            if "standby" in states:
                field("power", "standby_value", states["standby"].standby_value)
            if "eco" in states:
                field("power", "eco_mode", states["eco"].enabled)
            if "profile" in states:
                field("power", "energy_saving", states["profile"].energy_saving)
                active_profile = states["profile"].current_profile + 1
            if "status" in states:
                info = states["status"]
                device_status = {"firmware_version": info.firmware_version,
                                 "firmware_catalog_version": info.firmware_catalog_version,
                                 "firmware_numeric": info.firmware_numeric, "role": info.role,
                                 "battery_percent": info.battery_percent, "charging": info.charging}
        return {"device_id": device_id, "profile_slot": profile_slot,
                "configuration": configuration,
                "verified_fields": verified,
                "errors": errors,
                "summary": {"dpi": state.dpi[state.current_stage], "stage": state.current_stage + 1,
                            "profile": profile_slot, "active_profile": active_profile, "status": device_status,
                            "transport": ({"Darwin": "macOS HIDAPI · USB · experimental",
                                           "Windows": "Windows HIDAPI · USB · experimental"}.get(platform.system(), "Linux hidraw · USB")),
                            "read_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "changed": result["changed"],
                            "activation_acknowledged": result.get("activation_acknowledged", False),
                            "lcd_setup_verified": result.get("lcd_setup_verified", False)},
                "baseline": {"device_id": device_id, "profile_slot": profile_slot,
                             "raw": state.raw.hex(),
                             "transport_identity": result.get(
                                 "transport_identity", device_id),
                             "settings": extra,
                             "macro_data": result.get("macro_data", {}),
                             "host_action_icon_bindings": copy.deepcopy(
                                 known_host_action_icon_bindings),
                             "host_action_icon_indices": copy.deepcopy(
                                 known_host_action_icon_indices)},
                "macro_timing_adjustments": result.get("macro_timing_adjustments", [])}

    def read(self, device_id: str, profile_slot: int = 1,
             draft: Configuration | None = None) -> dict:
        result = self._call({"operation": "read_settings", "device_id": device_id, "profile_slot": profile_slot})
        return self._snapshot(device_id, profile_slot, result, draft)

    def read_status(self, device_id: str) -> dict:
        """Refresh telemetry without reading or replacing a settings baseline."""
        return self._call({"operation": "read_status", "device_id": device_id, "profile_slot": 1})

    def read_active_profile(self, device_id: str) -> dict:
        """Read only the global onboard profile state without changing it."""
        result = self._call({"operation": "read_active_profile", "device_id": device_id,
                             "profile_slot": 1})
        try:
            from .display_commands import decode_profile_response
            state = decode_profile_response(bytes.fromhex(result["raw"]))
            if (result["active_profile"], result["profile_count"],
                    result["energy_saving"], result["changed"]) != (
                    state.current_profile + 1, state.profile_count,
                    state.energy_saving, False):
                raise ValueError
        except (KeyError, TypeError, ValueError) as error:
            raise DeviceError("The MC7 helper returned invalid active-profile state.") from error
        return result

    @staticmethod
    def gpu_sources() -> list[dict]:
        """List host adapters for the GUI; this performs no mouse I/O."""

        from .host_metrics import available_gpu_sources
        return [{
            "id": source.identity,
            "label": source.label,
            "primary": source.primary,
            "load_available": source.load_available,
            "temperature_available": source.temperature_available,
        } for source in available_gpu_sources()]

    @staticmethod
    def media_players() -> list[dict]:
        """List supported running host players without opening the mouse."""

        from .host_media import available_media_players
        return [{
            "id": player.player_id,
            "label": player.label,
            "backend": player.backend,
        } for player in available_media_players()]

    def update_host_lcd(self, device_id: str, profile_slot: int, baseline: dict,
                        gpu_source: str | None = None) -> dict:
        from .host_metrics import sample_metrics
        metrics = sample_metrics(gpu_source=gpu_source)
        return self._call({"operation": "host_lcd", "device_id": device_id,
                           "profile_slot": profile_slot, "baseline": baseline,
                           "cpu_percent": metrics.cpu_percent, "ram_percent": metrics.ram_percent,
                           "gpu_percent": metrics.gpu_percent,
                           "cpu_temperature_c": metrics.cpu_temperature_c,
                           "gpu_temperature_c": metrics.gpu_temperature_c})

    def apply(self, device_id: str, configuration: Configuration,
              baseline: dict | None = None) -> dict:
        return self.apply_section(device_id, configuration, "sensor", baseline)

    def apply_section(self, device_id: str, configuration: Configuration, section: str,
                      baseline: dict | None = None) -> dict:
        configuration.validate()
        result = self._call({"operation": "apply_settings", "device_id": device_id,
                             "profile_slot": configuration.profile_slot, "baseline": baseline,
                             "section": section, "configuration": configuration.to_dict()})
        return self._snapshot(device_id, configuration.profile_slot, result, configuration)

    def activate(self, device_id: str, profile_slot: int = 1) -> dict:
        result = self._call({"operation": "activate", "device_id": device_id, "profile_slot": profile_slot})
        return self._snapshot(device_id, profile_slot, result)

    def switch_profile(self, device_id: str, profile_slot: int,
                       draft: Configuration | None = None) -> dict:
        result = self._call({"operation": "switch_profile", "device_id": device_id, "profile_slot": profile_slot})
        return self._snapshot(device_id, profile_slot, result, draft)

    def setup_display(self, device_id: str, profile_slot: int = 1) -> dict:
        result = self._call({"operation": "setup_display", "device_id": device_id, "profile_slot": profile_slot})
        return self._snapshot(device_id, profile_slot, result)

    def upload_background(self, device_id: str, rgba: bytes) -> dict:
        from .image_commands import BACKGROUND_RGBA_BYTES
        if not isinstance(rgba, bytes) or len(rgba) != BACKGROUND_RGBA_BYTES:
            raise DeviceError("Choose a 76 by 284 RGBA background image")
        return self._call({"operation": "upload_background", "device_id": device_id,
                           "profile_slot": 1, "rgba": base64.b64encode(rgba).decode("ascii")})
