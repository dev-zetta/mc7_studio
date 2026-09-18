import struct
import unittest

from swarm2.image_commands import (
    BACKGROUND_HEIGHT, BACKGROUND_WIDTH, BACKGROUND_IMAGE_BYTES, BACKGROUND_RGBA_BYTES,
    build_background_transfer, build_image_get_buffer, decode_background_blob,
    decode_image_response, encode_background_rgba, rotate_background_rgba,
    build_background_selection_read_request, build_background_selection_get_buffer,
    build_background_selection_report, decode_background_selection_response,
)
from swarm2.protocol import ProtocolError


class ImageCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rgba = bytes((255, 0, 0, 255)) * (BACKGROUND_WIDTH * BACKGROUND_HEIGHT)
        cls.blob = encode_background_rgba(cls.rgba)
        cls.plan = build_background_transfer(cls.blob)

    def test_golden_header_and_separate_rgb565_alpha_planes(self):
        rgba = bytearray(self.rgba)
        rgba[:16] = bytes((255, 0, 0, 0, 0, 255, 0, 64, 0, 0, 255, 128, 255, 255, 255, 255))
        blob = encode_background_rgba(rgba)
        self.assertEqual(blob[:12], bytes.fromhex("14308123f0fc00000c000000"))
        state = decode_background_blob(blob)
        self.assertEqual((state.width, state.height), (76, 284))
        self.assertEqual(state.rgb565[:8].hex(), "00f8e0071f00ffff")
        self.assertEqual(state.alpha[:4], bytes((0, 64, 128, 255)))
        self.assertEqual(len(blob), 64764)

    def test_quarter_turn_maps_asymmetric_corners_without_resampling(self):
        rgba = bytearray(BACKGROUND_RGBA_BYTES)
        # Source is 284 wide, 76 high. Mark each corner with a distinct color.
        colors = (b"\x01\x02\x03\xff", b"\x04\x05\x06\xff",
                  b"\x07\x08\x09\xff", b"\x0a\x0b\x0c\xff")
        for offset, color in zip((0, 283 * 4, 75 * 284 * 4, (76 * 284 - 1) * 4), colors):
            rgba[offset:offset + 4] = color
        rotated = rotate_background_rgba(rgba)
        self.assertEqual(rotated[:4], colors[1])
        self.assertEqual(rotated[75 * 4:76 * 4], colors[3])
        self.assertEqual(rotated[283 * 76 * 4:283 * 76 * 4 + 4], colors[0])
        self.assertEqual(rotated[-4:], colors[2])

    def test_plan_selects_background_only_and_has_bounded_duration(self):
        self.assertEqual(self.plan.image_bytes, BACKGROUND_IMAGE_BYTES)
        self.assertEqual(self.plan.block_count, 16)
        self.assertEqual(len(self.plan.steps), 1122)
        self.assertEqual(self.plan.steps[0].report, bytes.fromhex("10a500") + bytes(61))
        self.assertEqual(self.plan.steps[-1].report, bytes.fromhex("10a5ff") + bytes(61))
        self.assertEqual(self.plan.minimum_duration_ms, 69790)
        self.assertTrue(all(len(step.report) == 64 for step in self.plan.steps))
        self.assertEqual(build_image_get_buffer(), bytes.fromhex("10a2") + bytes(62))

    def test_packet_boundaries_reconstruct_exact_blob_and_zero_padded_blocks(self):
        combined = bytearray()
        phases = []
        for block in range(16):
            steps = [s for s in self.plan.steps if s.block_index == block]
            self.assertEqual(steps[0].command, 0xF1)
            self.assertEqual(steps[-1].command, 0xF2)
            body = bytearray()
            for step in steps[1:-1]:
                self.assertIn(step.phase, ("data", "padding"))
                self.assertTrue(1 <= step.command <= 61)
                body.extend(step.report[3:3 + step.command])
                self.assertEqual(step.report[3 + step.command:], bytes(61 - step.command))
                phases.append(step.phase)
            self.assertEqual(len(body), 4096)
            combined.extend(body)
        self.assertEqual(combined[:64764], self.blob)
        self.assertEqual(combined[64764:], bytes(772))
        final_data = [s for s in self.plan.steps if s.block_index == 15 and s.phase == "data"]
        final_pad = [s for s in self.plan.steps if s.block_index == 15 and s.phase == "padding"]
        self.assertEqual((final_data[-1].command, final_pad[-1].command), (30, 40))

    def test_invalid_blobs_cannot_produce_upload_plan(self):
        for data in (self.blob[:-1], self.blob + b"\0", b"", "not bytes"):
            with self.subTest(data_type=type(data).__name__), self.assertRaises(ProtocolError):
                build_background_transfer(data)
        for offset, value in ((0, 4), (4, 1), (8, 16)):
            data = bytearray(self.blob)
            struct.pack_into("<I", data, offset, value)
            with self.subTest(offset=offset), self.assertRaises(ProtocolError):
                build_background_transfer(data)
        with self.assertRaises(ProtocolError):
            encode_background_rgba(self.rgba[:-1])
        with self.assertRaises(ProtocolError):
            rotate_background_rgba(bytes(4))

    def test_control_reply_requires_identity_ready_selector_and_exact_correlation(self):
        for command in (0, 0xF1, 0xF2, 0xFF):
            for selector in (0xA2, 0xA5, 0xFF):
                raw = bytes((0x10, selector, command, 0xB7))
                with self.subTest(command=command, selector=selector):
                    reply = decode_image_response(raw, expected_command=command)
                    self.assertEqual(reply.command, command)
                    self.assertEqual(reply.raw, raw)
        for raw in (b"", b"\x10\xa5", b"\x04\xa5\xf1", b"\x10\x00\xf1",
                    b"\x10\xa5\xf2", bytes(65)):
            with self.subTest(raw=raw), self.assertRaises(ProtocolError):
                decode_image_response(raw, expected_command=0xF1)
        for command in (True, 62, 0xA5, -1):
            with self.subTest(command=command), self.assertRaises(ProtocolError):
                decode_image_response(b"\x10\xa5\x00", expected_command=command)

    def test_data_and_padding_ignore_ff_filled_response_payload(self):
        raw = bytes((0x10,)) + bytes((0xFF,)) * 63
        for command in (1, 30, 61):
            with self.subTest(command=command):
                reply = decode_image_response(raw, expected_command=command)
                self.assertEqual(reply.selector, 0xFF)
                self.assertEqual(reply.command, 0xFF)
                self.assertEqual(reply.raw, raw)

    def test_background_selection_native_read_and_separate_write(self):
        native = bytes.fromhex("102c000000000000")
        state = decode_background_selection_response(native)
        self.assertEqual(state.background_index, 0)
        self.assertEqual(state.raw, native)
        self.assertEqual(build_background_selection_read_request(),
                         bytes.fromhex("101c002c000000") + bytes(57))
        self.assertEqual(build_background_selection_get_buffer(), bytes.fromhex("102c") + bytes(62))
        self.assertEqual(build_background_selection_report(1), bytes.fromhex("102c0100000000") + bytes(57))
        self.assertEqual(build_background_selection_report(0), bytes.fromhex("102c0000000000") + bytes(57))
        # Unknown future values are retained on read, never emitted on write.
        unknown = bytes.fromhex("102c0077aabbccdd")
        self.assertEqual(decode_background_selection_response(unknown).raw, unknown)

    def test_background_selection_rejects_other_response_and_untraced_write_indices(self):
        for raw in (b"", bytes.fromhex("102b000000000000"), bytes.fromhex("102c010000000000"),
                    bytes.fromhex("102c000000000000") + bytes(56)):
            with self.subTest(raw=raw), self.assertRaises(ProtocolError):
                decode_background_selection_response(raw)
        for index in (True, -1, 2, 105, "1"):
            with self.subTest(index=index), self.assertRaises(ProtocolError):
                build_background_selection_report(index)


if __name__ == "__main__":
    unittest.main()
