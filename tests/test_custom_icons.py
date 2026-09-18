import unittest

from swarm2.configuration import encode_host_action_icon_rgba
from swarm2.custom_icons import (
    CustomIconUpload, custom_icon_reference, plan_custom_icons,
    referenced_custom_icon_indices, upload_custom_icons,
)
from swarm2.host_actions import encode_host_action_record
from swarm2.image_commands import CUSTOM_ICON_RGBA_BYTES
from swarm2.lcd_commands import decode_lcd_response
from swarm2.screen_key_commands import decode_screen_key_responses
from swarm2.transport import DeviceError
from tests.test_settings import (
    CAPTURES, EMPTY_SCREEN_KEY, screen_key_capture, with_lcd_page,
    with_screen_key_records,
)


def lcd_with_app():
    raw = with_lcd_page(
        bytes.fromhex(CAPTURES["lcd"]), 0,
        (b"\x05\x03", b"\xfe\x00", b"\xfe\x00", b"\xfe\x00"),
    )
    return decode_lcd_response(raw, 0)


def key_state(profile, records=()):
    raw = screen_key_capture(profile)
    for page, slot, record in records:
        existing = decode_screen_key_responses(raw, profile).pages[page].records
        values = list(existing)
        values[slot] = record
        raw = with_screen_key_records(raw, page, values)
    return decode_screen_key_responses(raw, profile)


def matrices(icon=None, target="/opt/Demo"):
    pages = [["open_application", "empty", "empty", "empty"],
             ["empty"] * 4, ["empty"] * 4]
    targets = [[target, None, None, None], [None] * 4, [None] * 4]
    icons = [[icon, None, None, None], [None] * 4, [None] * 4]
    return pages, targets, icons


class CustomIconAllocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rgba = bytes((20, 40, 60, 255)) * (CUSTOM_ICON_RGBA_BYTES // 4)
        cls.icon = encode_host_action_icon_rgba(cls.rgba)

    def all_states(self, current):
        return [current] + [key_state(profile) for profile in range(1, 5)]

    def test_reference_scan_requires_all_profiles_and_reserves_valid_records(self):
        unknown = bytes.fromhex("1122334407000000000000")
        current = key_state(0, [
            (0, 0, encode_host_action_record(
                "open_application", "/opt/Demo", icon_index=19)),
            (0, 1, unknown),
        ])
        states = self.all_states(current)
        self.assertEqual(referenced_custom_icon_indices(states), frozenset((6, 19)))
        self.assertEqual(custom_icon_reference(
            encode_host_action_record("open_application", "/opt/Demo", icon_index=0)), 0)
        self.assertIsNone(custom_icon_reference(unknown))
        self.assertIsNone(custom_icon_reference(bytes(11)))
        with self.assertRaisesRegex(DeviceError, "all five"):
            referenced_custom_icon_indices(states[:4])
        with self.assertRaisesRegex(DeviceError, "distinct"):
            referenced_custom_icon_indices([states[0]] * 5)

    def test_unchanged_locally_known_pixels_keep_referenced_slot(self):
        record = encode_host_action_record(
            "open_application", "/opt/Demo", icon_index=3)
        current = key_state(0, [(0, 0, record)])
        pages, targets, icons = matrices(self.icon)
        plan = plan_custom_icons(
            lcd_with_app(), current, self.all_states(current),
            pages, targets, icons, previous_icons=icons,
            previous_icon_indices=[
                [3, None, None, None], [None] * 4, [None] * 4])
        self.assertEqual(plan.icon_indices[0][0], 3)
        self.assertEqual(plan.uploads, ())
        self.assertEqual(plan.referenced_before, frozenset((3,)))

    def test_changed_pixels_use_unreferenced_store_and_duplicate_pixels_share_it(self):
        record = encode_host_action_record(
            "open_application", "/opt/Demo", icon_index=0)
        current = key_state(0, [(0, 0, record)])
        pages, targets, icons = matrices(self.icon)
        pages[0][1] = "open_application"
        targets[0][1] = "/opt/Other"
        icons[0][1] = self.icon
        old_icon = encode_host_action_icon_rgba(bytes(CUSTOM_ICON_RGBA_BYTES))
        previous = [[old_icon, None, None, None], [None] * 4, [None] * 4]
        plan = plan_custom_icons(
            lcd_with_app(), current, self.all_states(current), pages, targets,
            icons, previous_icons=previous)
        self.assertEqual(plan.icon_indices[0][:2], (1, 1))
        self.assertEqual(plan.uploads, (CustomIconUpload(1, self.rgba),))
        self.assertNotIn(plan.uploads[0].icon_index, plan.referenced_before)

    def test_allocator_never_overwrites_any_referenced_global_store(self):
        states = []
        for profile in range(5):
            records = []
            for coordinate in range(12):
                icon_index = profile * 4 + coordinate % 4
                if icon_index < 20:
                    records.append((coordinate // 4, coordinate % 4,
                                    encode_host_action_record(
                                        "open_application", f"/opt/A{icon_index}",
                                        icon_index=icon_index)))
            states.append(key_state(profile, records))
        pages, targets, icons = matrices(self.icon)
        with self.assertRaisesRegex(DeviceError, "All 20"):
            plan_custom_icons(
                lcd_with_app(), states[0], states, pages, targets, icons)

    def test_unresolved_existing_app_tile_requires_no_allocation(self):
        current = key_state(0, [(0, 0, encode_host_action_record(
            "open_application", "/opt/Demo", icon_index=2))])
        pages, targets, icons = matrices(None, target=None)
        plan = plan_custom_icons(
            lcd_with_app(), current, self.all_states(current), pages, targets, icons)
        self.assertEqual(plan.uploads, ())
        self.assertIsNone(plan.icon_indices[0][0])


class CustomIconUploadTests(unittest.TestCase):
    class Transport:
        def __init__(self, fail_select=False):
            self.sent = []
            self.exchanged = []
            self.fail_select = fail_select

        def send(self, report):
            self.sent.append(report)

        def get_feature(self, selector):
            self.assert_selector = selector
            return bytes.fromhex(CAPTURES["status"])

        def exchange_image(self, report, *, delay_ms):
            self.exchanged.append((report, delay_ms))
            command = report[2]
            if self.fail_select and len(self.exchanged) == 1:
                command = 0
            return bytes((0x10, 0xA5, command))

    def test_acknowledged_upload_reports_no_pixel_readback(self):
        transport = self.Transport()
        result = upload_custom_icons(
            [CustomIconUpload(0, bytes(CUSTOM_ICON_RGBA_BYTES))], transport)
        self.assertEqual(len(transport.exchanged), 213)
        self.assertEqual(transport.exchanged[0][0][:3], bytes.fromhex("10a569"))
        self.assertEqual(result[0]["icon_index"], 0)
        self.assertEqual(result[0]["completed_packets"], 213)
        self.assertFalse(result[0]["pixel_readback"])

    def test_select_echo_mismatch_stops_before_first_data_block(self):
        transport = self.Transport(fail_select=True)
        with self.assertRaisesRegex(DeviceError, "stopped after 0/213"):
            upload_custom_icons(
                [CustomIconUpload(0, bytes(CUSTOM_ICON_RGBA_BYTES))], transport)
        self.assertEqual(len(transport.exchanged), 1)


if __name__ == "__main__":
    unittest.main()
