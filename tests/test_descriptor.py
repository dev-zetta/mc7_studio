import unittest

from swarm2.descriptor import DescriptorError, MAX_DESCRIPTOR_BYTES, Usage, parse_descriptor


# Captured from the connected 10f5:502c interface 2 on 2026-09-15.
# This describes report envelopes; it does not establish their vendor meanings.
MC7_DESCRIPTOR = bytes.fromhex(
    "060bff0a0401a101150026ff007508953c852a09608202010961920201"
    "0962b20201852b0965b20201170000008027ffffff7f75209504852c19"
    "6629698102852d198a298d8102198e29919102c00602ff0901a1018514"
    "0970150026ff007508953c810285140971150026ff0075089514b102c0"
    "0600ff0901a1018511190029ff150026ff0075089514b102c00601ff09"
    "01a1018510190029ff150026ff007508953fb102c0"
)


class DescriptorTests(unittest.TestCase):
    def test_captured_mc7_configuration_interface_envelopes(self):
        summary = parse_descriptor(MC7_DESCRIPTOR)
        reports = {(report.kind, report.report_id): report.report_bytes for report in summary.reports}
        self.assertEqual(reports, {
            ("input", 0x14): 61, ("input", 0x2A): 61,
            ("input", 0x2C): 17, ("input", 0x2D): 17,
            ("output", 0x2A): 61, ("output", 0x2D): 17,
            ("feature", 0x10): 64, ("feature", 0x11): 21,
            ("feature", 0x14): 21, ("feature", 0x2A): 61, ("feature", 0x2B): 61,
        })
        self.assertEqual(summary.warnings, [])
        self.assertIn(Usage(0xFF0B, 0x0104), summary.application_usages)

    def test_push_pop_restores_id_size_count_and_page(self):
        descriptor = bytes.fromhex("050175019503850109308102a4050275089502850209328102b409318102")
        first, second = parse_descriptor(descriptor).reports
        self.assertEqual((first.report_id, first.bits, first.report_bytes), (1, 6, 2))
        self.assertEqual(first.usages, [Usage(1, 0x30), Usage(1, 0x31)])
        self.assertEqual((second.report_id, second.bits, second.report_bytes), (2, 16, 3))
        self.assertEqual(second.usages, [Usage(2, 0x32)])

    def test_local_usages_reset_after_main_item(self):
        summary = parse_descriptor(bytes.fromhex("05017508950185010930810285028102"))
        self.assertEqual(summary.reports[0].usages, [Usage(1, 0x30)])
        self.assertEqual(summary.reports[1].usages, [])

    def test_unnumbered_bit_fields_are_rounded_once(self):
        report = parse_descriptor(bytes.fromhex("75019505810295038103")).reports[0]
        self.assertEqual((report.bits, report.payload_bytes, report.report_bytes), (8, 1, 1))

    def test_extended_usage_overrides_current_page(self):
        report = parse_descriptor(bytes.fromhex("0501750895010b341202ff8102")).reports[0]
        self.assertEqual(report.usages, [Usage(0xFF02, 0x1234)])

    def test_ranges_do_not_expand(self):
        report = parse_descriptor(bytes.fromhex("050719002affff750895018102")).reports[0]
        self.assertEqual(report.usages, [])
        self.assertEqual(report.usage_ranges, [{"minimum": Usage(7, 0), "maximum": Usage(7, 65535)}])

    def test_long_item_is_bounded_and_reported(self):
        summary = parse_descriptor(bytes.fromhex("fe0301aabbcc750895018102"))
        self.assertEqual(summary.reports[0].report_bytes, 1)
        self.assertEqual(len(summary.warnings), 1)

    def test_invalid_descriptors_rejected(self):
        samples = {
            "empty": b"", "truncated short": bytes.fromhex("27ff"),
            "truncated long header": bytes.fromhex("fe01"),
            "truncated long data": bytes.fromhex("fe0201ff"),
            "pop underflow": bytes.fromhex("b4"), "collection underflow": bytes.fromhex("c0"),
            "unclosed collection": bytes.fromhex("a101"), "reserved ID": bytes.fromhex("8500"),
            "missing size": bytes.fromhex("95018102"),
            "reversed range": bytes.fromhex("19092901750895018102"),
            "missing range endpoint": bytes.fromhex("1909750895018102"),
            "overflow report": bytes.fromhex("77ffffffff97ffffffff8102"),
            "mixed ID presence": bytes.fromhex("75089501810285018102"),
            "oversized descriptor": b"\x00" * (MAX_DESCRIPTOR_BYTES + 1),
        }
        for name, descriptor in samples.items():
            with self.subTest(name=name), self.assertRaises(DescriptorError):
                parse_descriptor(descriptor)


if __name__ == "__main__":
    unittest.main()
