"""Byte-level tests for the host-managed MC7 countdown timer."""

import unittest

from swarm2.countdown_commands import (
    CountdownDisplayUpdate,
    build_countdown_reports,
    countdown_live_values,
    countdown_sync_values,
    decode_countdown_press,
)
from swarm2.protocol import ProtocolError


class CountdownCommandTests(unittest.TestCase):
    def test_live_start_tick_and_stop_use_the_native_asymmetric_values(self):
        self.assertEqual(countdown_live_values(0, 60, 60), (0, 60))
        self.assertEqual(countdown_live_values(1, 60, 60), (1, 60))
        self.assertEqual(countdown_live_values(1, 60, 45), (1, 45))
        self.assertEqual(countdown_live_values(1, 60, 44), (2, 44))
        self.assertEqual(countdown_live_values(1, 60, 30), (2, 30))
        self.assertEqual(countdown_live_values(1, 60, 29), (3, 29))
        self.assertEqual(countdown_live_values(1, 60, 15), (3, 15))
        self.assertEqual(countdown_live_values(1, 60, 14), (4, 14))
        self.assertEqual(countdown_live_values(1, 60, 0), (4, 0))
        self.assertEqual(countdown_live_values(2, 60, 0), (0, 60))

    def test_initial_layout_sync_uses_configured_duration_only(self):
        self.assertEqual(countdown_sync_values(0, 60, 60), (0, 60))
        for values in ((0, 60, 59), (1, 60, 44), (1, 60, 0),
                       (2, 60, 0)):
            with self.subTest(values=values), self.assertRaises(ProtocolError):
                countdown_sync_values(*values)

    def test_record_uses_page_reversed_slot_command_and_two_little_endian_values(self):
        update = CountdownDisplayUpdate.live(
            1, 2, state=1, total_seconds=600, remaining_seconds=511)
        self.assertEqual(update.record, bytes.fromhex("02010b0100ff010000"))

    def test_reports_sort_split_at_six_and_reject_duplicate_positions(self):
        updates = [CountdownDisplayUpdate(page, slot, 0, page * 4 + slot)
                   for page in range(2, -1, -1) for slot in range(3, -1, -1)]
        reports = build_countdown_reports(updates)
        self.assertEqual([report[3] for report in reports], [6, 6])
        self.assertTrue(all(len(report) == 64 and report[:2] == b"\x10\xa3"
                            for report in reports))
        self.assertEqual(reports[0][4:13], bytes.fromhex("01030b000000000000"))
        with self.assertRaises(ProtocolError):
            build_countdown_reports([updates[0], updates[0]])

    def test_empty_batch_and_invalid_updates(self):
        self.assertEqual(build_countdown_reports([]), ())
        for value in (None, b"", "x", {}, [object()],
                      [CountdownDisplayUpdate(0, 0, 0, 0)] * 13):
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                build_countdown_reports(value)
        for args in ((-1, 0, 0, 0), (3, 0, 0, 0), (0, 4, 0, 0),
                     (0, 0, -1, 0), (0, 0, 5, 0), (0, 0, 0, 601)):
            with self.subTest(args=args), self.assertRaises(ProtocolError):
                CountdownDisplayUpdate(*args)

    def test_timer_value_validation(self):
        for args in ((3, 60, 1), (0, 0, 0), (0, 601, 1),
                     (1, 60, -1), (1, 60, 61), (True, 60, 1)):
            with self.subTest(args=args), self.assertRaises(ProtocolError):
                countdown_live_values(*args)

    def test_touch_decoder_matches_native_coordinate_type_and_press_edge(self):
        raw = bytes.fromhex("1033214600aa01bb")
        touch = decode_countdown_press(raw)
        self.assertEqual((touch.page_index, touch.slot_index, touch.raw), (1, 2, raw))
        for other in (
            bytes.fromhex("1001214600aa01bb"),  # wrong outer discriminator
            bytes.fromhex("1033214500aa01bb"),  # another widget type
            bytes.fromhex("1033214601aa01bb"),  # another subtype
            bytes.fromhex("1033214600aa00bb"),  # release edge
            bytes.fromhex("1000f2a300000000"),  # ordinary acknowledgement
        ):
            with self.subTest(other=other):
                self.assertIsNone(decode_countdown_press(other))

    def test_touch_decoder_rejects_malformed_claimed_countdown_coordinates(self):
        for raw in (b"", bytes(7), bytes(9), "not bytes"):
            with self.subTest(raw=raw), self.assertRaises(ProtocolError):
                decode_countdown_press(raw)
        for coordinate in (0x00, 0x04, 0x40):
            raw = bytes((0x10, 0x33, coordinate, 0x46, 0, 0, 1, 0))
            with self.subTest(coordinate=coordinate), self.assertRaises(ProtocolError):
                decode_countdown_press(raw)


if __name__ == "__main__":
    unittest.main()
