"""Macro selectors and exact native reassembly, with no hardware access."""

from dataclasses import replace
import hashlib
import unittest

from swarm2.macro_commands import (
    MACRO_READ_CHUNKS, KeyboardEvent, MouseEvent, MacroReadAssembler, TimingAdjustment,
    build_keyboard_macro_image, build_macro_get_buffer, build_macro_read_request,
    build_vendor_macro_packets, compile_keyboard_macro, decode_keyboard_macro,
    decode_macro_chunk, macro_slot, plan_keyboard_macro_upload,
)
from swarm2.protocol import ProtocolError


def response(index, size=61):
    return b"\x10\x1d\0" + bytes((index,)) * size


class MacroReadTests(unittest.TestCase):
    def test_actual_erased_slot_capture_is_retained_without_claiming_valid_events(self):
        assembler = MacroReadAssembler(1, 5)
        # Native profile1/logical5, all seventeen GETs captured 2026-09-15.
        for index in range(17):
            assembler.append(bytes.fromhex("101d00") + b"\xff" * 61, index)
        result = assembler.finish()
        self.assertEqual(result.raw, b"\xff" * 1037)
        self.assertEqual(result.native_lengths, (64,) * 17)
        with self.assertRaises(ProtocolError):
            decode_keyboard_macro(result.raw[76:1036])

    def test_read_selector_golden_vectors_and_layer_slot_mapping(self):
        self.assertEqual(build_macro_read_request(1, 5, 0), bytes.fromhex("101c0301050035") + bytes(57))
        self.assertEqual(build_macro_read_request(4, 11, 16, "easy_shift"),
                         bytes.fromhex("101c03041a105d") + bytes(57))
        self.assertEqual(macro_slot(5, "easy_shift"), 20)
        self.assertEqual(
            tuple(macro_slot(cell, "lcd") for cell in range(12)),
            (33, 32, 31, 30, 37, 36, 35, 34, 41, 40, 39, 38),
        )
        self.assertEqual(build_macro_read_request(0, 0, 0, "lcd"),
                         bytes.fromhex("101c0300210050") + bytes(57))
        self.assertEqual(build_macro_read_request(4, 11, 16, "lcd"),
                         bytes.fromhex("101c0304261069") + bytes(57))
        self.assertEqual(build_macro_get_buffer(), bytes.fromhex("101d") + bytes(62))
        for chunk in range(MACRO_READ_CHUNKS):
            packet = build_macro_read_request(0, 0, chunk)
            self.assertEqual(packet[2], 3)  # Never selects the macro write subcommand.
            self.assertEqual(packet[6], sum(packet[:6]) & 255)
            self.assertEqual(packet[7:], bytes(57))

    def test_selector_rejects_hidden_slots_types_layers_and_untraced_chunks(self):
        for value in (-1, 5, True, 1.0, "1"):
            with self.subTest(profile=value), self.assertRaises(ProtocolError):
                build_macro_read_request(value, 5, 0)
        for value in (-1, 9, 12, 15, True, "5"):
            with self.subTest(slot=value), self.assertRaises(ProtocolError):
                build_macro_read_request(0, value, 0)
        for value in (-1, 17, True, 0.0, "0"):
            with self.subTest(chunk=value), self.assertRaises(ProtocolError):
                build_macro_read_request(0, 5, value)
        for layer in (None, 1, "secondary", "PRIMARY"):
            with self.subTest(layer=layer), self.assertRaises(ProtocolError):
                macro_slot(5, layer)
        for value in (-1, 12, 33, True, "0"):
            with self.subTest(lcd_cell=value), self.assertRaises(ProtocolError):
                macro_slot(value, "lcd")

    def test_decoder_preserves_actual_native_length_and_does_not_invent_checksum(self):
        for size in (1, 30, 58, 61):
            raw = response(5, size)
            result = decode_macro_chunk(bytearray(raw), 5)
            self.assertEqual(result.raw, raw)
            self.assertEqual(result.payload, bytes((5,)) * size)
            self.assertEqual(result.chunk_index, 5)

    def test_invalid_response_header_status_and_length_rejected(self):
        for raw in (None, [0x10, 0x1D, 0, 0], b"", b"\x10\x1d\0",
                    response(0) + b"\0", b"\x11\x1d\0\0", b"\x10\x10\0\0",
                    b"\x10\x1d\x01\0", b"\x10\x1d\xff\0"):
            with self.subTest(raw=raw), self.assertRaises(ProtocolError):
                decode_macro_chunk(raw, 0)

    def test_seventeen_full_chunks_assemble_to_1037_bytes_without_zero_extension(self):
        assembler = MacroReadAssembler(1, 5)
        for index in range(17):
            self.assertEqual(assembler.read_request(), build_macro_read_request(1, 5, index))
            assembler.append(response(index), index)
        self.assertTrue(assembler.transfer_complete)
        self.assertEqual(assembler.received_bytes, 1037)
        result = assembler.finish()
        self.assertEqual(result.raw, b"".join(bytes((i,)) * 61 for i in range(17)))
        self.assertEqual(result.native_lengths, (64,) * 17)
        self.assertEqual((result.profile_index, result.macro_slot, result.layer), (1, 5, "primary"))
        self.assertEqual(result.raw[-1], 16)
        for operation in (assembler.read_request, lambda: assembler.append(response(17))):
            with self.assertRaises(ProtocolError):
                operation()

    def test_short_final_chunk_is_retained_without_padding(self):
        assembler = MacroReadAssembler(4, 11, "easy_shift")
        for index in range(16):
            assembler.append(response(index))
        assembler.append(response(16, 30))
        result = assembler.finish()
        self.assertEqual(result.native_lengths, (64,) * 16 + (33,))
        self.assertEqual(len(result.raw), 1006)
        self.assertEqual(result.macro_slot, 26)

    def test_missing_duplicate_reordered_and_short_ordinary_chunk_never_advance(self):
        assembler = MacroReadAssembler(0, 5)
        with self.assertRaisesRegex(ProtocolError, "incomplete"):
            assembler.finish()
        assembler.append(response(0), 0)
        for index, raw in ((0, response(0)), (2, response(2)), (1, response(1, 60)),
                           (1, b"\x10\x1d\x01\0")):
            with self.subTest(index=index, raw=raw), self.assertRaises(ProtocolError):
                assembler.append(raw, index)
            self.assertEqual(assembler.next_chunk_index, 1)
            self.assertEqual(assembler.received_bytes, 61)
        with self.assertRaisesRegex(ProtocolError, "1 of seventeen"):
            assembler.finish()
        assembler.append(response(1), 1)
        self.assertEqual(assembler.next_chunk_index, 2)


class KeyboardMacroTests(unittest.TestCase):
    def test_bundled_w_press_release_vector_and_exact_roundtrip(self):
        events = (KeyboardEvent(0x1A, True, 50), KeyboardEvent(0x1A, False))
        compiled = compile_keyboard_macro(events)
        self.assertEqual(compiled.encoded, bytes.fromhex("011ab21a0000"))
        self.assertEqual(compiled.events, events)
        self.assertEqual(compiled.timing_adjustments, ())
        self.assertEqual(decode_keyboard_macro(compiled.encoded), compiled)

    def test_extended_and_zero_delays_report_every_timing_normalization(self):
        for requested, expected, hex_value in ((0, 1, "011a811a0000"),
                                               (127, 127, "011aff1a0000"),
                                               (128, 128, "011a00010600881a0000"),
                                               (500, 501, "011a00011900811a0000")):
            events = (KeyboardEvent(0x1A, True, requested), KeyboardEvent(0x1A, False))
            with self.subTest(delay=requested):
                compiled = compile_keyboard_macro(events)
                self.assertEqual(compiled.encoded.hex(), hex_value)
                self.assertEqual(compiled.events[0].delay_after_ticks, expected)
                changes = () if requested == expected else (TimingAdjustment(0, requested, expected),)
                self.assertEqual(compiled.timing_adjustments, changes)
                self.assertEqual(compile_keyboard_macro(compiled.events).encoded, compiled.encoded)

    def test_maximum_requested_delay_has_a_bounded_explicit_extra_tick(self):
        compiled = compile_keyboard_macro((KeyboardEvent(4, True, 60000), KeyboardEvent(4, False)))
        self.assertEqual(compiled.events[0].delay_after_ticks, 60001)
        self.assertEqual(compiled.timing_adjustments, (TimingAdjustment(0, 60000, 60001),))

    def test_decoder_supports_all_three_extension_scales_and_retains_trailer(self):
        for scale, expected in ((1, 41), (2, 101), (3, 201)):
            stream = bytes((1, 4, 0, scale, 2, 0, 0x81, 4, 0, 0))
            decoded = decode_keyboard_macro(stream + b"\xaa\xbb")
            self.assertEqual(decoded.events, (KeyboardEvent(4, True, expected), KeyboardEvent(4, False)))
            self.assertEqual(decoded.encoded, stream)
            self.assertEqual(decoded.trailing_bytes, b"\xaa\xbb")

    def test_chord_and_right_side_modifier_remain_physical_hid_keys(self):
        events = (KeyboardEvent(0xE7, True, 1), KeyboardEvent(6, True, 20),
                  KeyboardEvent(6, False, 1), KeyboardEvent(0xE7, False))
        compiled = compile_keyboard_macro(events)
        self.assertEqual(compiled.events, events)
        self.assertEqual(compiled.encoded, bytes.fromhex("01e70106940681e70000"))

    def test_stop_marker_and_bounded_delay_records_are_required(self):
        for raw in (b"", b"\0", bytes.fromhex("0104"), bytes.fromhex("01040001"),
                    bytes.fromhex("000101000000"), bytes.fromhex("01040004010081040000"),
                    bytes.fromhex("01040001ffff81040000"), b"\0" * 961):
            with self.subTest(raw=raw), self.assertRaises(ProtocolError):
                decode_keyboard_macro(raw)
        self.assertEqual(decode_keyboard_macro(b"\0\0").events, ())

    def test_unbalanced_unknown_and_trailing_delay_events_are_rejected(self):
        invalid = [[], [KeyboardEvent(4, True)], [KeyboardEvent(4, False)],
                   [KeyboardEvent(4, True), KeyboardEvent(4, True), KeyboardEvent(4, False)],
                   [KeyboardEvent(4, True), KeyboardEvent(4, False, 50)],
                   [KeyboardEvent(0xF0, True), KeyboardEvent(0xF0, False)],
                   [KeyboardEvent(4, 1), KeyboardEvent(4, False)],
                   [KeyboardEvent(4, True, True), KeyboardEvent(4, False)],
                   [KeyboardEvent(4, True, 60001), KeyboardEvent(4, False)],
                   [{"key_code": 4, "command": "example"}], "A"]
        for events in invalid:
            with self.subTest(events=events), self.assertRaises(ProtocolError):
                compile_keyboard_macro(events)
        for raw in (bytes.fromhex("01040000"), bytes.fromhex("81040000"),
                    bytes.fromhex("01f581f50000")):
            with self.subTest(raw=raw), self.assertRaises(ProtocolError):
                decode_keyboard_macro(raw)

    def test_encoded_capacity_reserves_stop_even_when_extensions_use_extra_bytes(self):
        # 239 complete taps use956 event bytes and two stop bytes.
        events = [event for _ in range(239) for event in (KeyboardEvent(4, True, 1), KeyboardEvent(4, False, 1))]
        events[-1] = KeyboardEvent(4, False)
        self.assertEqual(len(compile_keyboard_macro(events).encoded), 958)
        events[0] = KeyboardEvent(4, True, 128)
        with self.assertRaisesRegex(ProtocolError, "960-byte"):
            compile_keyboard_macro(events)


class MacroUploadPlanTests(unittest.TestCase):
    def test_lcd_plan_uses_the_vendor_touch_macro_slot_namespace(self):
        plan = plan_keyboard_macro_upload(
            (KeyboardEvent(4, True, 50), KeyboardEvent(4, False)),
            profile_index=2, logical_slot=8, layer="lcd", name="LCD tap",
            repeat_count=3, playback="repeat")
        self.assertEqual(plan.packets[0][:7], bytes.fromhex("101c0202290059"))
        self.assertEqual(plan.expected_read_payload[74:76], b"\x03\0")
        assembler = MacroReadAssembler(2, 8, "lcd")
        for index in range(17):
            assembler.append(b"\x10\x1d\0" + plan.expected_read_payload[index*61:(index+1)*61])
        self.assertEqual(assembler.finish().macro_slot, 41)

    def test_every_packet_matches_offline_wine_vendor_writer_capture(self):
        # Harness input pattern and metadata; SHA256 is over all34 captured
        # packets, captured by the actual DLL with HID entry points blocked.
        image = bytearray(index % 251 + 1 for index in range(1038))
        image[72:76] = bytes.fromhex("01000100")
        image[1036:] = bytes.fromhex("3412")
        packets = build_vendor_macro_packets(image, 1, 5)
        self.assertEqual(len(packets), 34)
        self.assertEqual(hashlib.sha256(b"".join(packets)).hexdigest(),
                         "ffec0e16115221186c7973451470a3fee48967ef61843a753c3bb3eb757cb754")
        self.assertEqual(packets[0], bytes.fromhex("101c0201050034") + bytes(57))
        self.assertEqual(packets[-1], b"\x10\x1d" + image[992:1022] + bytes(32))
        for index, packet in enumerate(packets):
            self.assertEqual(len(packet), 64)
            self.assertEqual(packet[1], 0x1C if index % 2 == 0 else 0x1D)

    def test_short_keyboard_plan_tracks_metadata_source_checksum_and_wire_padding(self):
        plan = plan_keyboard_macro_upload((KeyboardEvent(0x1A, True, 50), KeyboardEvent(0x1A, False)),
                                          profile_index=1, logical_slot=5, name="Readback test", group="Swarm2")
        image = plan.source_image
        self.assertEqual(len(image), 1038)
        self.assertEqual(image[:40], b"Swarm2" + bytes(34))
        self.assertEqual(image[40:72], b"Readback test" + bytes(19))
        self.assertEqual(image[72:76], bytes.fromhex("01000100"))
        self.assertEqual(image[76:82], bytes.fromhex("011ab21a0000"))
        self.assertEqual(int.from_bytes(image[-2:], "big"), sum(image[:-2]) & 65535)
        self.assertEqual(plan.transmitted_payload, image[:1022] + bytes(32))
        self.assertEqual(plan.expected_read_payload, image[:1022] + bytes(15))
        self.assertEqual(len(plan.transmitted_payload), 1054)
        self.assertEqual(len(plan.expected_read_payload), 1037)
        self.assertEqual(plan.omitted_source_bytes, image[1022:])
        self.assertTrue(all(packet[1] not in (0x15, 0x16) for packet in plan.packets))

    def test_predicted_readback_reassembles_exactly_without_manufacturing_source_checksum(self):
        plan = plan_keyboard_macro_upload((KeyboardEvent(4, True, 128), KeyboardEvent(4, False)),
                                          profile_index=2, logical_slot=8, layer="easy_shift", repeat_count=7)
        assembler = MacroReadAssembler(2, 8, "easy_shift")
        for index in range(17):
            assembler.append(b"\x10\x1d\0" + plan.expected_read_payload[index*61:(index+1)*61])
        readback = assembler.finish()
        self.assertEqual(readback.raw, plan.expected_read_payload)
        self.assertEqual(readback.macro_slot, 23)
        self.assertEqual(readback.raw[74:76], b"\x07\0")
        self.assertEqual(readback.raw[1022:], bytes(15))
        self.assertEqual(decode_keyboard_macro(readback.raw[76:1036]).events, plan.keyboard_macro.events)

    def test_invalid_metadata_and_oversize_event_tail_never_produce_a_plan(self):
        macro = compile_keyboard_macro((KeyboardEvent(4, True, 50), KeyboardEvent(4, False)))
        for kwargs in ({"name": ""}, {"name": "a"*32}, {"name": "newline\n"},
                       {"group": "a"*40}, {"group": "emoji🙂"}, {"repeat_count": 0},
                       {"repeat_count": 1000}, {"repeat_count": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ProtocolError):
                build_keyboard_macro_image(macro, **kwargs)
        with self.assertRaises(ProtocolError):
            build_keyboard_macro_image(replace(macro, events=()))
        events = [event for _ in range(237) for event in (KeyboardEvent(4, True, 1), KeyboardEvent(4, False, 1))]
        events[-1] = KeyboardEvent(4, False)
        # 950 bytes fits compact960 area, but source76+950 is beyond copy1022.
        self.assertEqual(len(compile_keyboard_macro(events).encoded), 950)
        with self.assertRaisesRegex(ProtocolError, "truncated"):
            plan_keyboard_macro_upload(events, profile_index=1, logical_slot=5)
        for bad in (bytes(1037), bytes(1039), "0"*1038):
            with self.subTest(image=type(bad)), self.assertRaises(ProtocolError):
                build_vendor_macro_packets(bad, 1, 5)

    def test_macro_writer_rejects_unsafe_device_selectors(self):
        image = bytes(1038)
        for profile in (-1, 5, 25, True):
            with self.subTest(profile=profile), self.assertRaises(ProtocolError):
                build_vendor_macro_packets(image, profile, 5)
        for logical_slot in (-1, 9, 12, 25, True):
            with self.subTest(logical_slot=logical_slot), self.assertRaises(ProtocolError):
                build_vendor_macro_packets(image, 0, logical_slot)
        for layer in (None, "secondary", "PRIMARY"):
            with self.subTest(layer=layer), self.assertRaises(ProtocolError):
                build_vendor_macro_packets(image, 0, 5, layer)

    def test_maximum_upload_event_stream_retains_stop_at_last_copied_source_bytes(self):
        events = [event for _ in range(236)
                  for event in (KeyboardEvent(4, True, 1), KeyboardEvent(4, False, 1))]
        events[-1] = KeyboardEvent(4, False)
        plan = plan_keyboard_macro_upload(events, profile_index=1, logical_slot=5)
        self.assertEqual(len(plan.keyboard_macro.encoded), 946)
        self.assertEqual(plan.source_image[1020:1022], bytes(2))
        self.assertEqual(plan.expected_read_payload[76:1022], plan.keyboard_macro.encoded)
        self.assertEqual(decode_keyboard_macro(plan.expected_read_payload[76:1036]).events,
                         tuple(events))


class MouseMacroTests(unittest.TestCase):
    def test_source_mapped_mouse_click_vectors_roundtrip(self):
        # EXE140118330 sets f0/f1/f2 and transitions1/2; MC7 DLL18020d630
        # copies the private identifier and sets bit80 for transition2. MC7
        # firmware 5.09 executor0010b604 maps f3 to the ordinary Forward
        # action's mask and f4 to Back. All five vectors also match the actual
        # converter under offline Wine with all HID entry points blocked.
        for button, code in (("left", 0xF0), ("right", 0xF1), ("middle", 0xF2),
                             ("forward", 0xF3), ("back", 0xF4)):
            with self.subTest(button=button):
                events = (MouseEvent(button, True, 50), MouseEvent(button, False))
                expected = bytes((1, code, 0xB2, code, 0, 0))
                compiled = compile_keyboard_macro(events)
                self.assertEqual(compiled.encoded, expected)
                self.assertEqual(compiled.events, events)
                self.assertEqual(decode_keyboard_macro(expected).events, events)

    def test_mixed_modifier_mouse_chord_tracks_inputs_independently(self):
        events = (KeyboardEvent(0xE1, True, 1), MouseEvent("left", True, 50),
                  MouseEvent("left", False, 1), KeyboardEvent(0xE1, False))
        encoded = bytes.fromhex("01e101f0b2f081e10000")
        self.assertEqual(compile_keyboard_macro(events).encoded, encoded)
        self.assertEqual(decode_keyboard_macro(encoded).events, events)
        plan = plan_keyboard_macro_upload(events, profile_index=1, logical_slot=5)
        self.assertEqual(plan.expected_read_payload[76:86], encoded)
        self.assertEqual(decode_keyboard_macro(plan.expected_read_payload[76:1036]).events, events)

    def test_mouse_extensions_and_timing_normalization_match_keyboard_semantics(self):
        compiled = compile_keyboard_macro((MouseEvent("middle", True, 500), MouseEvent("middle", False)))
        self.assertEqual(compiled.encoded, bytes.fromhex("01f20001190081f20000"))
        self.assertEqual(compiled.events[0], MouseEvent("middle", True, 501))
        self.assertEqual(compiled.timing_adjustments, (TimingAdjustment(0, 500, 501),))
        self.assertEqual(compile_keyboard_macro(compiled.events).encoded, compiled.encoded)

    def test_mixed_vector_matches_actual_vendor_converter_under_offline_wine(self):
        events = (KeyboardEvent(0x68, True, 1), MouseEvent("middle", True, 500),
                  MouseEvent("middle", False, 1), KeyboardEvent(0x68, False))
        captured = bytes.fromhex("016801f20001190081f281680000")
        compiled = compile_keyboard_macro(events)
        self.assertEqual(compiled.encoded, captured)
        self.assertEqual(compiled.timing_adjustments, (TimingAdjustment(1, 500, 501),))
        self.assertEqual(decode_keyboard_macro(captured).events, compiled.events)

    def test_unknown_mouse_codes_and_unbalanced_inputs_are_rejected(self):
        for button in ("scroll_up", "scroll_down", "tilt_left", "Left", 0, []):
            with self.subTest(button=button), self.assertRaises(ProtocolError):
                compile_keyboard_macro((MouseEvent(button, True, 50), MouseEvent(button, False)))
        for code in (0xF5, 0xFF):
            with self.subTest(code=code), self.assertRaises(ProtocolError):
                decode_keyboard_macro(bytes((1, code, 0x81, code, 0, 0)))
        invalid = (
            (MouseEvent("left", True),), (MouseEvent("left", False),),
            (MouseEvent("left", True), MouseEvent("right", False)),
            (MouseEvent("left", True), MouseEvent("left", True), MouseEvent("left", False)),
            (KeyboardEvent(4, True), MouseEvent("left", False)),
            (MouseEvent("left", True), KeyboardEvent(4, False)),
            (MouseEvent("left", True), MouseEvent("left", False, 1)),
            (MouseEvent("left", 1), MouseEvent("left", False)),
            (MouseEvent("left", True, True), MouseEvent("left", False)),
            (MouseEvent("left", True, 60001), MouseEvent("left", False)),
        )
        for events in invalid:
            with self.subTest(events=events), self.assertRaises(ProtocolError):
                compile_keyboard_macro(events)
        with self.assertRaises(ProtocolError):
            # Explicit event types keep private mouse identifiers distinct
            # from the HID keyboard namespace.
            compile_keyboard_macro((KeyboardEvent(0xF0, True), KeyboardEvent(0xF0, False)))


if __name__ == "__main__":
    unittest.main()
