import struct
import unittest

from swarm2.image_commands import (
    CUSTOM_ICON_FIRST_STORE_SELECTOR,
    CUSTOM_ICON_HEIGHT,
    CUSTOM_ICON_IMAGE_BYTES,
    CUSTOM_ICON_LAST_STORE_SELECTOR,
    CUSTOM_ICON_RGBA_BYTES,
    CUSTOM_ICON_SOURCE_HEIGHT,
    CUSTOM_ICON_SOURCE_WIDTH,
    CUSTOM_ICON_STORE_COUNT,
    CUSTOM_ICON_WIDTH,
    build_custom_icon_transfer,
    build_image_transfer,
    custom_icon_store_selector,
    decode_custom_icon_blob,
    decode_image_response,
    encode_custom_icon_rgba,
)
from swarm2.protocol import ProtocolError


class CustomIconImageCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rgba = bytes((0, 0, 0, 0)) * (CUSTOM_ICON_WIDTH * CUSTOM_ICON_HEIGHT)
        cls.blob = encode_custom_icon_rgba(cls.rgba)
        cls.plan = build_custom_icon_transfer(cls.blob, icon_index=0)

    def test_wire_dimensions_and_golden_container_planes(self):
        self.assertEqual((CUSTOM_ICON_SOURCE_WIDTH, CUSTOM_ICON_SOURCE_HEIGHT), (64, 62))
        self.assertEqual((CUSTOM_ICON_WIDTH, CUSTOM_ICON_HEIGHT), (62, 64))
        self.assertEqual(CUSTOM_ICON_RGBA_BYTES, 15_872)
        self.assertEqual(CUSTOM_ICON_IMAGE_BYTES, 11_916)

        rgba = bytearray(self.rgba)
        rgba[:16] = bytes((255, 0, 0, 0,
                           0, 255, 0, 64,
                           0, 0, 255, 128,
                           255, 255, 255, 255))
        blob = encode_custom_icon_rgba(rgba)
        self.assertEqual(blob[:12], bytes.fromhex("14f80008802e00000c000000"))
        state = decode_custom_icon_blob(blob)
        self.assertEqual((state.width, state.height), (62, 64))
        self.assertEqual(state.rgb565[:8].hex(), "00f8e0071f00ffff")
        self.assertEqual(state.alpha[:4], bytes((0, 64, 128, 255)))

    def test_custom_slot_mapping_is_bounded_to_twenty_stores(self):
        self.assertEqual(CUSTOM_ICON_STORE_COUNT, 20)
        self.assertEqual(CUSTOM_ICON_FIRST_STORE_SELECTOR, 105)
        self.assertEqual(CUSTOM_ICON_LAST_STORE_SELECTOR, 124)
        self.assertEqual(custom_icon_store_selector(0), 105)
        self.assertEqual(custom_icon_store_selector(19), 124)
        for index in (True, -1, 20, 105, "0"):
            with self.subTest(index=index), self.assertRaises(ProtocolError):
                custom_icon_store_selector(index)

    def test_plan_has_three_blocks_213_steps_and_source_timing(self):
        self.assertEqual(self.plan.image_selector, 105)
        self.assertEqual(self.plan.image_bytes, CUSTOM_ICON_IMAGE_BYTES)
        self.assertEqual(self.plan.block_count, 3)
        self.assertEqual(len(self.plan.steps), 213)
        self.assertEqual(self.plan.minimum_duration_ms, 15_250)
        self.assertEqual(self.plan.steps[0].report,
                         bytes.fromhex("10a569") + bytes(61))
        self.assertEqual(self.plan.steps[-1].report,
                         bytes.fromhex("10a5ff") + bytes(61))
        self.assertTrue(all(len(step.report) == 64 for step in self.plan.steps))

    def test_packet_boundaries_reconstruct_blob_and_final_padding(self):
        combined = bytearray()
        for block in range(3):
            steps = [step for step in self.plan.steps if step.block_index == block]
            self.assertEqual(steps[0].command, 0xF1)
            self.assertEqual(steps[-1].command, 0xF2)
            body = bytearray()
            for step in steps[1:-1]:
                self.assertIn(step.phase, ("data", "padding"))
                self.assertTrue(1 <= step.command <= 61)
                body.extend(step.report[3:3 + step.command])
                self.assertEqual(step.report[3 + step.command:], bytes(61 - step.command))
            self.assertEqual(len(body), 4096)
            combined.extend(body)
        self.assertEqual(combined[:CUSTOM_ICON_IMAGE_BYTES], self.blob)
        self.assertEqual(combined[CUSTOM_ICON_IMAGE_BYTES:], bytes(372))
        final_data = [step for step in self.plan.steps
                      if step.block_index == 2 and step.phase == "data"]
        final_padding = [step for step in self.plan.steps
                         if step.block_index == 2 and step.phase == "padding"]
        self.assertEqual((final_data[-1].command, final_padding[-1].command), (3, 6))

    def test_selection_response_must_echo_the_requested_custom_store(self):
        for response_selector in (0xA2, 0xA5, 0xFF):
            raw = bytes((0x10, response_selector, 105, 0x7F))
            with self.subTest(response_selector=response_selector):
                reply = decode_image_response(raw, expected_command=105)
                self.assertEqual(reply.raw, raw)
        for command in (104, 106, 124, 0xF1):
            with self.subTest(command=command), self.assertRaisesRegex(
                    ProtocolError, "command echo"):
                decode_image_response(bytes((0x10, 0xA5, command)), expected_command=105)

    def test_generic_planner_rejects_unknown_store_and_wrong_dimensions(self):
        for selector in (True, -1, 1, 61, 104, 125, 255, "105"):
            with self.subTest(selector=selector), self.assertRaises(ProtocolError):
                build_image_transfer(self.blob, image_selector=selector,
                                     width=CUSTOM_ICON_WIDTH, height=CUSTOM_ICON_HEIGHT)
        with self.assertRaises(ProtocolError):
            build_image_transfer(self.blob, image_selector=105,
                                 width=CUSTOM_ICON_HEIGHT, height=CUSTOM_ICON_WIDTH)
        with self.assertRaises(ProtocolError):
            build_image_transfer(self.blob, image_selector=0,
                                 width=CUSTOM_ICON_WIDTH, height=CUSTOM_ICON_HEIGHT)
        for data in (self.blob[:-1], self.blob + b"\0", b"", "not bytes"):
            with self.subTest(data_type=type(data).__name__), self.assertRaises(ProtocolError):
                build_custom_icon_transfer(data, icon_index=0)

        damaged = bytearray(self.blob)
        struct.pack_into("<I", damaged, 4, 1)
        with self.assertRaises(ProtocolError):
            decode_custom_icon_blob(damaged)

    def test_encoder_rejects_wrong_sized_or_typed_rgba(self):
        for rgba in (self.rgba[:-1], self.rgba + b"\0", b"", "not bytes"):
            with self.subTest(data_type=type(rgba).__name__), self.assertRaises(ProtocolError):
                encode_custom_icon_rgba(rgba)


if __name__ == "__main__":
    unittest.main()
