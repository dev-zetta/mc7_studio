import unittest

from swarm2.host_lcd_commands import (
    HOST_LCD_WIDGET_TYPES, HostLcdUpdate, build_host_lcd_reports,
    decode_host_lcd_acknowledgement, host_lcd_icon_index,
)
from swarm2.protocol import AckStatus, ProtocolError


class HostLcdCommandsTests(unittest.TestCase):
    def test_cpu_and_ram_known_wire_fixture(self):
        reports = build_host_lcd_reports([
            HostLcdUpdate("cpu_load", 0, 0, 42),
            HostLcdUpdate("ram_usage", 0, 1, 73),
        ])
        expected = bytes.fromhex(
            "10a30004"
            "010304020000000000"  # CPU low icon, reversed slot3.
            "0103052a0000000000"  # CPU42 percent.
            "010204000000000000"  # RAM high icon, reversed slot2.
            "010205490000000000"  # RAM73 percent.
        )
        self.assertEqual(reports, (expected + bytes(64 - len(expected)),))
        self.assertEqual(dict(HOST_LCD_WIDGET_TYPES), {
            "gpu_temperature": 0x37, "gpu_load": 0x38,
            "cpu_temperature": 0x39, "cpu_load": 0x3A, "ram_usage": 0x41,
        })

    def test_gpu_and_temperature_values_use_the_same_native_records(self):
        reports = build_host_lcd_reports([
            HostLcdUpdate("gpu_load", 0, 0, 49),
            HostLcdUpdate("cpu_temperature", 0, 1, 50),
            HostLcdUpdate("gpu_temperature", 0, 2, 0xFFFF),
        ])
        expected = bytes.fromhex(
            "10a30006"
            "010304020000000000" "010305310000000000"
            "010204010000000000" "010205320000000000"
            "010104000000000000" "010105ffff00000000"
        )
        self.assertEqual(reports, (expected + bytes(64 - len(expected)),))

    def test_boundaries_use_vendor_icon_order(self):
        self.assertEqual([host_lcd_icon_index(n) for n in (0, 49, 50, 69, 70, 100, 0xFFFF)],
                         [2, 2, 1, 1, 0, 0, 0])

    def test_pages_are_separate_and_ordered(self):
        reports = build_host_lcd_reports([
            HostLcdUpdate("ram_usage", 2, 3, 100),
            HostLcdUpdate("cpu_load", 0, 2, 0),
        ])
        self.assertEqual(len(reports), 2)
        self.assertEqual(reports[0][:13], bytes.fromhex("10a30002010104020000000000"))
        self.assertEqual(reports[1][:13], bytes.fromhex("10a30002030004000000000000"))
        self.assertEqual(reports[1][13:22], bytes.fromhex("030005640000000000"))

    def test_full_page_splits_at_six_records_without_splitting_a_widget(self):
        reports = build_host_lcd_reports([
            HostLcdUpdate("cpu_load", 1, index, index) for index in range(4)
        ])
        self.assertEqual([len(item) for item in reports], [64, 64])
        self.assertEqual([item[3] for item in reports], [6, 2])
        self.assertEqual(reports[0][4:7], bytes((2, 3, 4)))
        self.assertEqual(reports[0][49:52], bytes((2, 1, 5)))
        self.assertEqual(reports[0][58:], bytes(6))
        self.assertEqual(reports[1][4:7], bytes((2, 0, 4)))
        self.assertEqual(reports[1][22:], bytes(42))

    def test_twelve_positions_bounded_to_six_reports(self):
        reports = build_host_lcd_reports([
            HostLcdUpdate("ram_usage", page, slot, 50)
            for page in range(3) for slot in range(4)
        ])
        self.assertEqual(len(reports), 6)
        self.assertTrue(all(len(report) == 64 for report in reports))

    def test_empty_and_duplicate_positions(self):
        self.assertEqual(build_host_lcd_reports([]), ())
        with self.assertRaises(ProtocolError):
            build_host_lcd_reports([HostLcdUpdate("cpu_load", 0, 0, 1),
                                    HostLcdUpdate("ram_usage", 0, 0, 2)])

    def test_invalid_measurements_positions_and_widget_types(self):
        for value in (-1, 101, 42.5, True, "50", None, float("nan")):
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                HostLcdUpdate("cpu_load", 0, 0, value)
        for value in (-1, 0x10000, 42.5, True, "50", None, float("nan")):
            with self.subTest(temperature=value), self.assertRaises(ProtocolError):
                HostLcdUpdate("cpu_temperature", 0, 0, value)
        for page, slot in ((-1, 0), (3, 0), (0, -1), (0, 4), (False, 0), (0, 1.0)):
            with self.subTest(page=page, slot=slot), self.assertRaises(ProtocolError):
                HostLcdUpdate("cpu_load", page, slot, 50)
        for widget in ("unknown", None, [], True):
            with self.subTest(widget=widget), self.assertRaises(ProtocolError):
                HostLcdUpdate(widget, 0, 0, 50)

    def test_bad_batch_rejects_before_encoding(self):
        for updates in ("cpu_load", b"", None, [object()], {},
                        [HostLcdUpdate("cpu_load", 0, 0, 1)] * 13):
            with self.subTest(updates=updates), self.assertRaises(ProtocolError):
                build_host_lcd_reports(updates)

    def test_ack_identity_and_nonzero_status(self):
        for code, state in ((0, AckStatus.ACCEPTED), (1, AckStatus.BUSY),
                            (2, AckStatus.BUSY), (3, AckStatus.UNKNOWN)):
            raw = bytes((0x10, 0, 0xF2, 0xA3, code, 0x11, 0x22, 0x33))
            decoded = decode_host_lcd_acknowledgement(raw)
            self.assertEqual(decoded.status, state)
            self.assertEqual(decoded.raw, raw)
        for raw in (bytes.fromhex("1000f22500000000"),
                    bytes.fromhex("100006a300000000"),
                    bytes.fromhex("1100f2a300000000"), b"", bytes(64)):
            with self.subTest(raw=raw), self.assertRaises(ProtocolError):
                decode_host_lcd_acknowledgement(raw)


if __name__ == "__main__":
    unittest.main()
