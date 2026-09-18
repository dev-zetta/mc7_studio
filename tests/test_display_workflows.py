import copy
import unittest
from unittest.mock import patch

from swarm2.configuration import Configuration, Macro, MacroEvent
from swarm2.hardware import transact
from swarm2.lcd_layout import move_page
from swarm2.macro_profiles import plan_macro_upload
from swarm2.screen_key_commands import encode_lcd_macro_record
from swarm2.service import DeviceService
from swarm2.transport import DeviceError
from tests.test_macro_integration import MacroTransport
from tests.test_settings import (EMPTY_SCREEN_KEY, SettingsTransport,
                                 with_lcd_page, with_screen_key_records)


class DisplayWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.transport = SettingsTransport()
        self.service = DeviceService()
        helper = patch.object(DeviceService, '_call', side_effect=lambda request: transact(request, lambda _: self.transport))
        helper.start()
        self.addCleanup(helper.stop)

    def read(self):
        return self.service.read('fixture-mc7', 1)

    def apply(self, snapshot):
        return self.service.apply_section('fixture-mc7', snapshot['configuration'], 'display', snapshot['baseline'])

    def test_selection_applies_without_image_upload_and_restores(self):
        snapshot = self.read()
        self.assertEqual(snapshot['configuration'].display.background_index, 0)
        before = dict(self.transport.records)
        snapshot['configuration'].display.background_index = 1
        result = self.apply(snapshot)
        self.assertEqual(result['configuration'].display.background_index, 1)
        self.assertEqual([p[:3] for p in self.transport.writes], [b'\x10\x2c\x01'])
        for name in before.keys() - {'background'}:
            self.assertEqual(before[name], self.transport.records[name])
        result['configuration'].display.background_index = 0
        self.apply(result)
        self.assertEqual(before, self.transport.records)

    def test_older_presets_keep_current_background(self):
        data = Configuration().to_dict()
        del data['display']['background_index']
        self.assertIsNone(Configuration.from_dict(data).display.background_index)
        snapshot = self.read()
        snapshot['configuration'].display.background_index = None
        snapshot['configuration'].display.brightness = 80
        self.apply(snapshot)
        self.assertEqual([p[1] for p in self.transport.writes], [0x2b])

    def test_selection_stale_missing_and_mismatch_fail(self):
        snapshot = self.read()
        snapshot['configuration'].display.background_index = 1
        self.transport.records['background'] = bytes.fromhex('102c000100000000')
        with self.assertRaisesRegex(DeviceError, 'changed since the last read'):
            self.apply(snapshot)
        self.assertEqual(self.transport.writes, [])
        self.transport = SettingsTransport()
        self.transport.read_errors['background'] = 'Unavailable'
        snapshot = self.read()
        snapshot['configuration'].display.background_index = 1
        with self.assertRaisesRegex(DeviceError, 'background.*successfully'):
            self.apply(snapshot)
        self.assertEqual(self.transport.writes, [])
        self.transport = SettingsTransport()
        snapshot = self.read()
        snapshot['configuration'].display.background_index = 1
        self.transport.ignore_writes = True
        with self.assertRaisesRegex(DeviceError, 'background readback'):
            self.apply(snapshot)

    def test_unknown_selection_can_be_preserved_but_not_created(self):
        self.transport.records['background'] = bytes.fromhex('102c000800112233')
        snapshot = self.read()
        self.assertEqual(snapshot['configuration'].display.background_index, 8)
        result = self.apply(snapshot)
        self.assertFalse(result['summary']['changed'])
        self.assertEqual(self.transport.writes, [])
        result['configuration'].display.background_index = 9
        with self.assertRaises(ValueError):
            self.apply(result)
        self.assertEqual(self.transport.writes, [])

    def test_move_page_keeps_wide_widget_and_preserves_unexposed_records(self):
        snapshot = self.read()
        original = copy.deepcopy(snapshot['configuration'].display.pages)
        raw = self.transport.records['lcd']
        snapshot['configuration'].display.pages = move_page(original, 0, 2)
        result = self.apply(snapshot)
        self.assertEqual(result['configuration'].display.pages, [original[1], original[2], original[0]])
        self.assertEqual(original[0], ['download_swarm', None, None, 'dpi'])
        self.assertEqual(self.transport.records['lcd'][38:60], raw[38:60])
        self.assertEqual([p[1] for p in self.transport.writes], [0x25])

    def test_unchanged_imported_lcd_macro_is_a_verified_noop(self):
        self.transport = MacroTransport()
        self.transport.records['lcd'] = with_lcd_page(
            self.transport.records['lcd'], 2,
            (b'\x05\x02', b'\x19\xff', b'\x1a\xff', b'\x1f\xff'))
        macro = Macro(id='lcd_macro', name='LCD macro', events=[
            MacroEvent('key_down', 'F13', 50), MacroEvent('key_up', 'F13', 0)])
        plan = plan_macro_upload(macro, profile_index=0, logical_slot=8, layer='lcd')
        self.transport.storage[('lcd', 8)] = bytearray(
            plan.expected_read_payload + bytes(1054-len(plan.expected_read_payload)))
        self.transport.records['screen_keys'] = with_screen_key_records(
            self.transport.records['screen_keys'], 2,
            (encode_lcd_macro_record(macro.name), EMPTY_SCREEN_KEY,
             EMPTY_SCREEN_KEY, EMPTY_SCREEN_KEY))
        snapshot = self.read()
        self.assertEqual(snapshot['configuration'].display.pages[2][0], 'macro')
        imported_id = snapshot['configuration'].display.macro_bindings[2][0]
        self.assertIsNotNone(imported_id)
        self.assertEqual(snapshot['configuration'].macros[0].name, macro.name)

        result = self.apply(snapshot)

        self.assertEqual(self.transport.writes, [])
        self.assertEqual(result['configuration'].display.macro_bindings[2][0], imported_id)

    def test_unknown_widgets_cannot_move_across_positions(self):
        pages = [['unknown_80_01', 'dpi', 'empty', 'empty'], ['cut', 'copy', 'paste', 'undo']]
        original = copy.deepcopy(pages)
        with self.assertRaisesRegex(ValueError, 'unrecognized widgets'):
            move_page(pages, 0, 1)
        self.assertEqual(pages, original)
        self.assertEqual(move_page(pages, 0, 0), pages)
        for index in (True, -1, 2):
            with self.assertRaises(ValueError):
                move_page(pages, index, 0)

    def test_unreadable_lcd_macro_cannot_move_to_a_different_raw_slot(self):
        pages = [['macro', 'copy', 'paste', 'undo'],
                 ['dpi', 'led_brightness', 'play_pause', 'stop']]
        bindings = [[None] * 4, [None] * 4]

        with self.assertRaisesRegex(ValueError, 'unreadable LCD macro'):
            move_page(pages, 0, 1, macro_bindings=bindings)

        self.assertEqual(move_page(
            pages, 0, 0, macro_bindings=bindings), pages)


class StatusRefreshTests(unittest.TestCase):
    def test_refresh_uses_only_status_selector_and_never_changes_settings(self):
        transport = SettingsTransport()
        before = dict(transport.records)
        with patch.object(DeviceService, '_call', side_effect=lambda r: transact(r, lambda _: transport)):
            result = DeviceService().read_status('fixture-mc7')
        self.assertEqual(result, {'firmware_version': '5.04', 'firmware_catalog_version': '5.4.0.0',
                                  'firmware_numeric': 504, 'role': 'mouse',
                                  'battery_percent': 99, 'charging': True})
        self.assertEqual(transport.sent, [bytes.fromhex('101c0009000000') + bytes(57)])
        self.assertEqual(transport.writes, [])
        self.assertEqual(transport.records, before)

    def test_status_failure_does_not_claim_configuration_changed(self):
        import subprocess
        with patch('swarm2.service.platform.system', return_value='Linux'), patch('swarm2.service.subprocess.run', side_effect=subprocess.TimeoutExpired('helper', 20)):
            with self.assertRaises(DeviceError) as caught:
                DeviceService().read_status('fixture')
        self.assertNotIn('may have changed', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
