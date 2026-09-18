import unittest

from swarm2.protocol import (
    AckStatus, ProtocolError, build_dpi_report, build_sensor_get_buffer,
    build_sensor_read_request, decode_acknowledgement,
)


class ProtocolTests(unittest.TestCase):
    def dpi_arguments(self):
        return {
            "profile_index": 0xA, "current_dpi_index": 3, "packed_flags": 0x95,
            "raw_sensor_values": [0, 1, 0x1234, 0xABCD, 0xFFFF],
            "raw_color_triplets": [(0, 0xFF, 0x10), (0x20, 0x30, 0x40), (0x50, 0x60, 0x70),
                           (0x80, 0x90, 0xA0), (0xB0, 0xC0, 0xD0)],
        }

    def test_dpi_packet_matches_statically_derived_byte_vector(self):
        expected = bytes.fromhex(
            "10 14 a3 95 00 00 01 00 34 12 cd ab ff ff "
            "00 ff 10 20 30 40 50 60 70 80 90 a0 b0 c0 d0"
        ) + bytes(35)
        self.assertEqual(build_dpi_report(**self.dpi_arguments()), expected)

    def test_dpi_rejects_unrepresentable_fields_and_malformed_collections(self):
        invalid = [
            ("profile_index", -1), ("profile_index", 16), ("profile_index", True),
            ("current_dpi_index", 16), ("current_dpi_index", 1.0),
            ("packed_flags", -1), ("packed_flags", 256), ("packed_flags", False),
            ("raw_sensor_values", [0] * 4), ("raw_sensor_values", [0] * 6),
            ("raw_sensor_values", [0, 0, 0, 0, 65536]),
            ("raw_sensor_values", [0, 0, 0, 0, -1]),
            ("raw_sensor_values", [0, 0, 0, 0, True]),
            ("raw_sensor_values", [0, 0, 0, 0, "800"]),
            ("raw_color_triplets", [(0, 0, 0)] * 4), ("raw_color_triplets", [(0, 0, 0)] * 6),
            ("raw_color_triplets", [(0, 0)] * 5), ("raw_color_triplets", [(0, 0, 0, 0)] * 5),
            ("raw_color_triplets", [(0, 0, 256)] * 5), ("raw_color_triplets", [(0, 0, -1)] * 5),
            ("raw_color_triplets", [(0, 0, True)] * 5), ("raw_color_triplets", None),
        ]
        for field, value in invalid:
            with self.subTest(field=field, value=value), self.assertRaises(ProtocolError):
                arguments = self.dpi_arguments()
                arguments[field] = value
                build_dpi_report(**arguments)

    def test_sensor_request_and_get_buffer_have_distinct_static_layouts(self):
        self.assertEqual(build_sensor_read_request(0xAB), bytes.fromhex("10 1c 00 10 ab 00 00") + bytes(57))
        self.assertEqual(build_sensor_get_buffer(), bytes.fromhex("10 10") + bytes(62))
        for value in (-1, 256, True, "1", 1.0):
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                build_sensor_read_request(value)

    def test_ack_statuses_preserve_unknown_fields_without_command_correlation(self):
        vectors = [
            ("mouse", "10 78 f2 56 00 12 34 ab", AckStatus.ACCEPTED),
            ("mouse", "00 ff f2 10 01 20 30 40", AckStatus.BUSY),
            ("mouse", "01 23 f2 45 02 67 89 ab", AckStatus.BUSY),
            ("transmitter", "01 23 06 45 02 67 89 ab", AckStatus.UNKNOWN),
            ("transmitter", "00 00 06 00 ff 00 00 00", AckStatus.UNKNOWN),
        ]
        for target, raw_hex, expected in vectors:
            with self.subTest(target=target, raw_hex=raw_hex):
                raw = bytes.fromhex(raw_hex)
                acknowledgement = decode_acknowledgement(raw, target=target)
                self.assertEqual(acknowledgement.status, expected)
                self.assertEqual(acknowledgement.status_code, raw[4])
                self.assertEqual(acknowledgement.raw, raw)

    def test_ack_requires_exact_length_and_explicit_matching_target(self):
        raw = bytes.fromhex("10 00 f2 00 00 00 00 00")
        for invalid in (raw[:-1], raw + b"\x00", [0] * 8, "00000000"):
            with self.subTest(invalid=invalid), self.assertRaises(ProtocolError):
                decode_acknowledgement(invalid, target="mouse")
        for target in ("transmitter", "receiver", ""):
            with self.subTest(target=target), self.assertRaises(ProtocolError):
                decode_acknowledgement(raw, target=target)


if __name__ == "__main__":
    unittest.main()
