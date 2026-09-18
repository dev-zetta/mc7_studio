import unittest
from dataclasses import replace

from swarm2.protocol import ProtocolError
from swarm2.status_commands import (
    build_status_get_buffer, build_status_read_request, compare_catalog_version,
    decode_status_response,
)


CAPTURE = bytes.fromhex("1009000405204019ff63011b")


def response_with(**fields):
    result = bytearray(CAPTURE)
    for offset, value in fields.items():
        result[int(offset)] = value
    result[-1] = (-sum(result[2:-1])) & 255
    return bytes(result)


class StatusCommandTests(unittest.TestCase):
    def test_live_capture_firmware_battery_and_charge(self):
        state = decode_status_response(CAPTURE)
        self.assertEqual(state.firmware_version, "5.04")
        self.assertEqual((state.firmware_major, state.firmware_minor), (5, 4))
        self.assertEqual(state.battery_percent, 99)
        self.assertIs(state.charging, True)
        self.assertEqual(state.charging_raw, 1)
        self.assertEqual(state.raw, CAPTURE)
        self.assertEqual(state.firmware_catalog_version, "5.4.0.0")
        self.assertEqual(state.firmware_numeric, 504)
        self.assertEqual(state.role, "mouse")

    def test_numeric_catalog_comparison_keeps_role_and_decimal_components(self):
        state = decode_status_response(CAPTURE)
        for version, expected in (("5.3.0.0", 1), ("5.4.0.0", 0), ("5.9.0.0", -1),
                                  ("5.10.0.0", -1), ("12.0.0.0", -1)):
            with self.subTest(version=version):
                self.assertEqual(compare_catalog_version(state, version, role="mouse", product_id=0x502C), expected)
        version12 = decode_status_response(response_with(**{"3": 0x29, "4": 0x12}))
        self.assertEqual(version12.firmware_catalog_version, "12.29.0.0")
        self.assertEqual(version12.firmware_numeric, 1229)

    def test_incompatible_role_component_or_unverified_status_cannot_be_compared(self):
        state = decode_status_response(CAPTURE)
        for role, pid in (("transmitter", 0x502E), ("mouse", 0x502E),
                          ("transmitter", 0x502C), ("mouse", "502C")):
            with self.subTest(role=role, pid=pid), self.assertRaises(ProtocolError):
                compare_catalog_version(state, "5.4.0.0", role=role, product_id=pid)
        for version in ("5.04", "5.04.0.0", "5.9.1.0", "5.9.0.1", "504", "100.0.0.0",
                        "0x48005071", "5.9.0.0\n", None):
            with self.subTest(version=version), self.assertRaises(ProtocolError):
                compare_catalog_version(state, version, role="mouse", product_id=0x502C)
        with self.assertRaises(ProtocolError):
            compare_catalog_version(replace(state, firmware_minor=9), "5.9.0.0", role="mouse", product_id=0x502C)

    def test_read_selector_is_not_firmware_update_command(self):
        self.assertEqual(build_status_read_request(), bytes.fromhex("101c0009000000") + bytes(57))
        self.assertEqual(build_status_get_buffer(), bytes.fromhex("1009") + bytes(62))

    def test_packed_decimal_and_zero_charge(self):
        state = decode_status_response(response_with(**{"3": 0x29, "4": 0x12, "10": 0}))
        self.assertEqual(state.firmware_version, "12.29")
        self.assertIs(state.charging, False)
        for offset in (3, 4):
            with self.subTest(offset=offset), self.assertRaises(ProtocolError):
                decode_status_response(response_with(**{str(offset): 0xFA}))

    def test_unknown_battery_and_charge_are_not_presented_as_valid(self):
        state = decode_status_response(response_with(**{"9": 0xFF, "10": 0xFF}))
        self.assertIsNone(state.battery_percent)
        self.assertIsNone(state.charging)
        self.assertEqual(state.charging_raw, 0xFF)

    def test_invalid_length_identity_status_and_checksum(self):
        for value in (CAPTURE[:-1], CAPTURE + bytes(52), "not bytes", b""):
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                decode_status_response(value)
        for offset, value in ((0, 4), (1, 8), (2, 1), (9, 98), (11, 0)):
            bad = bytearray(CAPTURE)
            bad[offset] = value
            with self.subTest(offset=offset), self.assertRaises(ProtocolError):
                decode_status_response(bad)


if __name__ == "__main__":
    unittest.main()
