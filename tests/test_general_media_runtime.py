"""General Media listener integration uses fixtures and never opens USB."""

from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import Mock, patch

from swarm2.countdown import validate_countdown_request
from swarm2.countdown_runtime import (
    CountdownBinding,
    GeneralMediaBinding,
    LcdActionRuntime,
)
from swarm2.host_actions import HostActionBinding
from swarm2.host_media import (
    MediaAction,
    MediaDispatch,
    MediaLoopStatus,
    MediaPlaybackState,
    NoMediaPlayer,
)
from swarm2.transport import DeviceError
from tests.test_settings import CAPTURES, with_lcd_page


GENERAL_MEDIA_LCD = with_lcd_page(
    bytes.fromhex(CAPTURES["lcd"]),
    0,
    (b"\x45\x00", b"\x00\x00", b"\x00\x00", b"\x64\x00"),
)
GENERAL_MEDIA_LCD_SLOT_1 = with_lcd_page(
    bytes.fromhex(CAPTURES["lcd"]),
    0,
    (b"\x64\x00", b"\x45\x00", b"\x00\x00", b"\x00\x00"),
)
PRESS_TIMER_SLOT_3 = bytes.fromhex("1033104600000100")


def media_event(subtype, *, page=0, slot=0, pressed=True):
    packed = ((page + 1) << 4) | (3 - slot)
    return bytes((0x10, 0x33, packed, 0x45, subtype, 1,
                  int(pressed), 0))


class Provider:
    def __init__(self):
        self.actions = []
        self.failure = None
        self.state_failure = None
        self.state_calls = 0
        self.state = MediaPlaybackState(
            False, False, MediaLoopStatus.NONE,
            "fixture-media", "fixture-player")

    def perform(self, action):
        self.actions.append(action)
        if self.failure is not None:
            raise self.failure
        return MediaDispatch(action, "fixture-media", "fixture-player", "Playing")

    def read_state(self):
        self.state_calls += 1
        if self.state_failure is not None:
            raise self.state_failure
        return self.state


class BlockingProvider(Provider):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.active = 0
        self.maximum_active = 0

    def perform(self, action):
        self.actions.append(action)
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        self.entered.set()
        try:
            if len(self.actions) == 1 and not self.release.wait(1.0):
                raise NoMediaPlayer("Fixture provider timed out")
            return MediaDispatch(
                action, "fixture-media", "fixture-player", "Playing")
        finally:
            self.active -= 1


class GeneralMediaRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.messages = []
        self.provider = Provider()
        self.guards = Mock()
        self.transport = SimpleNamespace(send=Mock())
        self.runtime = LcdActionRuntime(
            [], [], self.transport, self.messages.append,
            general_media_bindings=[GeneralMediaBinding(0, 0)],
            guard=self.guards, media_provider=self.provider,
        )
        self.runtime.start()
        self.pump_until(lambda: self.transport.send.call_count == 1)
        self.initial_report = self.transport.send.call_args.args[0]
        self.transport.send.reset_mock()
        self.guards.reset_mock()

    def tearDown(self):
        if self.runtime.started:
            try:
                self.runtime.stop()
            except DeviceError:
                pass

    def pump_until(self, predicate, *, timeout=1.0, now=None):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.runtime.advance(now=now)
            time.sleep(0.001)
        self.assertTrue(predicate(), "Media worker did not finish in time")

    def assert_worker_error(self, message):
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                self.runtime.advance()
            except DeviceError as error:
                self.assertRegex(str(error), message)
                return
            time.sleep(0.001)
        self.fail("Media worker did not report its terminal error")

    def test_media_only_start_and_stop_guard_and_report_one_panel(self):
        self.assertEqual(self.messages, [{
            "type": "ready", "timers": 0, "positions": 0,
            "media_panels": 1,
        }])
        self.runtime.stop()
        self.assertEqual(self.guards.call_count, 1)
        self.assertEqual(self.messages[-1], {"type": "stopped"})

    def test_initial_state_uses_scan_coordinates_and_exact_a3_flags(self):
        self.assertEqual(
            self.initial_report,
            bytes.fromhex("10a3000101000f000000000000") + bytes(51),
        )

    def test_poll_writes_only_changed_state_and_keeps_usb_on_main_thread(self):
        self.runtime.stop()
        current = [100.0]
        main_thread = threading.get_ident()
        send_threads = []
        transport = SimpleNamespace(
            send=lambda report: send_threads.append((threading.get_ident(), report)))
        provider = Provider()
        runtime = LcdActionRuntime(
            [], [], transport, lambda _message: None,
            general_media_bindings=[GeneralMediaBinding(1, 1)],
            guard=Mock(), media_provider=provider, clock=lambda: current[0],
        )
        self.runtime = runtime
        runtime.start()
        self.pump_until(lambda: len(send_threads) == 1, now=100.0)

        current[0] = 102.0
        before = provider.state_calls
        runtime.advance(now=102.0)
        self.pump_until(
            lambda: provider.state_calls > before
                    and not runtime._media_refresh_pending,
            now=102.0,
        )
        self.assertEqual(len(send_threads), 1)

        provider.state = MediaPlaybackState(
            True, True, MediaLoopStatus.TRACK,
            "fixture-media", "fixture-player")
        current[0] = 104.0
        runtime.advance(now=104.0)
        self.pump_until(lambda: len(send_threads) == 2, now=104.0)
        thread_id, report = send_threads[-1]
        self.assertEqual(thread_id, main_thread)
        self.assertEqual(
            report,
            bytes.fromhex("10a3000102010f010001000001") + bytes(51),
        )

    def test_action_refresh_uses_touch_coordinate_and_real_provider_state(self):
        self.provider.state = MediaPlaybackState(
            True, False, MediaLoopStatus.PLAYLIST,
            "fixture-media", "fixture-player")
        self.assertTrue(self.runtime.observe(media_event(4)))
        self.pump_until(lambda: self.transport.send.call_count == 1)
        self.assertEqual(
            self.transport.send.call_args.args[0],
            bytes.fromhex("10a3000101030f010001000000") + bytes(51),
        )
        self.assertEqual(self.messages[-1]["action"], "repeat")

    def test_state_read_failure_never_writes_fabricated_flags(self):
        self.provider.state_failure = NoMediaPlayer("Fixture player closed")
        self.assertTrue(self.runtime.observe(media_event(0)))
        before = len(self.messages)
        self.pump_until(lambda: len(self.messages) > before)
        self.assertEqual(self.messages[-1]["event"], "dispatched")
        self.assertEqual(self.transport.send.call_count, 0)

    def test_malformed_or_mismatched_state_is_terminal(self):
        self.runtime.stop()
        provider = Provider()
        provider.state = object()
        self.runtime = LcdActionRuntime(
            [], [], SimpleNamespace(send=Mock()), lambda _message: None,
            general_media_bindings=[GeneralMediaBinding(0, 0)],
            guard=Mock(), media_provider=provider,
        )
        self.runtime.start()
        self.assert_worker_error("invalid state")
        self.runtime.stop()

        provider = Provider()
        transport = SimpleNamespace(send=Mock())
        self.runtime = LcdActionRuntime(
            [], [], transport, lambda _message: None,
            general_media_bindings=[GeneralMediaBinding(0, 0)],
            guard=Mock(), media_provider=provider,
        )
        self.runtime.start()
        self.pump_until(lambda: transport.send.call_count == 1)
        provider.state = MediaPlaybackState(
            False, False, MediaLoopStatus.NONE,
            "fixture-media", "different-player")
        self.runtime.observe(media_event(1))
        self.assert_worker_error("invalid state")

    def test_all_five_subtypes_dispatch_once_and_refresh_the_panel(self):
        expected = (
            (0, MediaAction.SHUFFLE),
            (1, MediaAction.NEXT),
            (2, MediaAction.PLAY_PAUSE),
            (3, MediaAction.PREVIOUS),
            (4, MediaAction.REPEAT),
        )
        for subtype, action in expected:
            with self.subTest(subtype=subtype):
                before = len(self.messages)
                self.assertTrue(self.runtime.observe(media_event(subtype)))
                self.pump_until(lambda: len(self.messages) > before)
                self.assertEqual(self.provider.actions[-1], action)
                self.assertEqual(self.messages[-1], {
                    "type": "media_action", "event": "dispatched",
                    "action": action.value, "page_index": 0, "slot_index": 0,
                })
        self.assertEqual(self.guards.call_count, 15)
        self.assertEqual(self.transport.send.call_count, 5)

    def test_known_provider_failure_is_reported_and_listener_continues(self):
        self.provider.failure = NoMediaPlayer("No fixture player is running")
        self.assertTrue(self.runtime.observe(media_event(1)))
        self.pump_until(lambda: self.messages[-1].get("event") == "failed")
        self.assertEqual(self.messages[-1], {
            "type": "media_action", "event": "failed", "action": "next",
            "page_index": 0, "slot_index": 0,
            "error": "No fixture player is running",
        })
        self.provider.failure = None
        self.assertTrue(self.runtime.observe(media_event(2)))
        self.pump_until(lambda: self.messages[-1].get("event") == "dispatched")
        self.assertEqual(self.messages[-1]["event"], "dispatched")

    def test_unbounded_or_nonprintable_provider_errors_are_terminal(self):
        for detail in ("", "bad\nerror", "x" * 4097):
            with self.subTest(detail=detail[:20]):
                self.runtime.stop()
                self.messages = []
                self.provider = Provider()
                self.transport = SimpleNamespace(send=Mock())
                self.runtime = LcdActionRuntime(
                    [], [], self.transport, self.messages.append,
                    general_media_bindings=[GeneralMediaBinding(0, 0)],
                    guard=Mock(), media_provider=self.provider,
                )
                self.runtime.start()
                self.pump_until(lambda: self.transport.send.call_count == 1)
                self.provider.failure = NoMediaPlayer(detail)
                self.assertTrue(self.runtime.observe(media_event(1)))
                self.assert_worker_error("invalid error")

    def test_unbound_press_and_release_are_ignored_without_provider_or_guard(self):
        before = self.guards.call_count
        self.assertFalse(self.runtime.observe(media_event(2, page=1, slot=0)))
        self.assertFalse(self.runtime.observe(media_event(2, pressed=False)))
        self.assertEqual(self.provider.actions, [])
        self.assertEqual(self.guards.call_count, before)

    def test_slot_one_wide_panel_uses_its_anchor_only(self):
        self.runtime.stop()
        self.messages = []
        self.provider = Provider()
        self.runtime = LcdActionRuntime(
            [], [], SimpleNamespace(send=Mock()), self.messages.append,
            general_media_bindings=[GeneralMediaBinding(0, 1)],
            guard=Mock(), media_provider=self.provider,
        )
        self.runtime.start()
        self.pump_until(lambda: self.provider.state_calls >= 1)
        self.assertFalse(self.runtime.observe(media_event(1, slot=0)))
        self.assertFalse(self.runtime.observe(media_event(1, slot=2)))
        self.assertTrue(self.runtime.observe(media_event(1, slot=1)))
        self.pump_until(lambda: self.messages[-1].get("event") == "dispatched")
        self.assertEqual(self.provider.actions, [MediaAction.NEXT])

    def test_invalid_dispatch_and_duplicate_positions_fail_closed(self):
        self.provider.perform = Mock(return_value=object())
        self.assertTrue(self.runtime.observe(media_event(2)))
        self.assert_worker_error("invalid dispatch")
        with self.assertRaisesRegex(ValueError, "configured once"):
            LcdActionRuntime(
                [CountdownBinding("timer", 0, 1, 1)], [],
                SimpleNamespace(send=Mock()),
                general_media_bindings=[GeneralMediaBinding(0, 0)],
                media_provider=Provider())
        with self.assertRaisesRegex(ValueError, "configured once"):
            LcdActionRuntime(
                [], [HostActionBinding(
                    "open_website", 0, 2, "https://example.com")],
                SimpleNamespace(send=Mock()),
                general_media_bindings=[GeneralMediaBinding(0, 0)],
                media_provider=Provider())

    def test_guard_runs_on_main_pump_before_provider_and_failure_blocks_dispatch(self):
        self.runtime.stop()
        order = []
        main_thread = threading.get_ident()
        self.provider = Provider()

        def perform(action):
            order.append(("provider", threading.get_ident()))
            return MediaDispatch(
                action, "fixture-media", "fixture-player", "Playing")

        self.provider.perform = perform
        self.guards = Mock(side_effect=lambda: order.append(
            ("guard", threading.get_ident())))
        self.messages = []
        transport = SimpleNamespace(send=Mock())
        self.runtime = LcdActionRuntime(
            [], [], transport, self.messages.append,
            general_media_bindings=[GeneralMediaBinding(0, 0)],
            guard=self.guards, media_provider=self.provider,
        )
        self.runtime.start()
        self.pump_until(lambda: transport.send.call_count == 1)
        order.clear()
        self.assertTrue(self.runtime.observe(media_event(1)))
        self.assertEqual(order, [])
        self.pump_until(lambda: self.messages[-1].get("event") == "dispatched")
        self.assertEqual(
            [item[0] for item in order],
            ["guard", "provider", "guard", "guard"],
        )
        self.assertEqual(order[0][1], main_thread)
        self.assertNotEqual(order[1][1], main_thread)
        self.assertTrue(all(item[1] == main_thread for item in order[2:]))

        self.runtime.stop()
        self.provider = Provider()
        self.guards = Mock()
        self.messages = []
        transport = SimpleNamespace(send=Mock())
        self.runtime = LcdActionRuntime(
            [], [], transport, self.messages.append,
            general_media_bindings=[GeneralMediaBinding(0, 0)],
            guard=self.guards, media_provider=self.provider,
        )
        self.runtime.start()
        self.pump_until(lambda: transport.send.call_count == 1)
        self.guards.side_effect = DeviceError("LCD layout drift")
        self.assertTrue(self.runtime.observe(media_event(1)))
        self.assert_worker_error("LCD layout drift")
        self.assertEqual(self.provider.actions, [])

    def test_blocked_provider_does_not_block_countdown_or_hid_processing(self):
        self.runtime.stop()
        self.messages = []
        self.provider = BlockingProvider()
        transport = SimpleNamespace(send=Mock())
        self.runtime = LcdActionRuntime(
            [CountdownBinding("timer", 0, 3, 4)], [], transport,
            self.messages.append,
            general_media_bindings=[GeneralMediaBinding(0, 0)],
            guard=Mock(), clock=lambda: 100.0, media_provider=self.provider,
        )
        self.runtime.start()
        self.pump_until(lambda: self.provider.state_calls >= 1)
        self.assertTrue(self.runtime.observe(media_event(1)))
        self.pump_until(self.provider.entered.is_set, now=100.0)

        started = time.monotonic()
        self.assertTrue(self.runtime.observe(PRESS_TIMER_SLOT_3, now=100.0))
        self.assertEqual(self.runtime.advance(now=101.0), 1)
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual(self.runtime.running_timer_ids, ("timer",))
        self.assertTrue(any(
            message.get("type") == "timer" and message.get("event") == "tick"
            for message in self.messages))
        self.assertFalse(self.provider.release.is_set())

        self.provider.release.set()
        self.pump_until(lambda: any(
            message.get("type") == "media_action"
            and message.get("event") == "dispatched"
            for message in self.messages), now=101.0)
        self.assertEqual(self.provider.maximum_active, 1)

    def test_worker_is_fifo_queue_is_bounded_and_stop_cancels_waiting_actions(self):
        self.runtime.stop()
        self.messages = []
        self.provider = BlockingProvider()
        self.runtime = LcdActionRuntime(
            [], [], SimpleNamespace(send=Mock()), self.messages.append,
            general_media_bindings=[GeneralMediaBinding(0, 0)],
            guard=Mock(), media_provider=self.provider,
        )
        self.runtime.start()
        self.runtime.observe(media_event(1))
        self.pump_until(self.provider.entered.is_set)
        for subtype in (2, 3, 1, 2):
            self.assertTrue(self.runtime.observe(media_event(subtype)))
        self.assertTrue(self.runtime.observe(media_event(3)))
        self.assertEqual(self.messages[-1], {
            "type": "media_action", "event": "failed", "action": "previous",
            "page_index": 0, "slot_index": 0,
            "error": "Host media action queue is full",
        })

        self.provider.release.set()
        self.pump_until(lambda: sum(
            message.get("event") == "dispatched" for message in self.messages
        ) == 5)
        self.assertEqual(self.provider.actions, [
            MediaAction.NEXT, MediaAction.PLAY_PAUSE, MediaAction.PREVIOUS,
            MediaAction.NEXT, MediaAction.PLAY_PAUSE,
        ])
        self.assertEqual(self.provider.maximum_active, 1)

        self.runtime.stop()
        self.messages = []
        self.provider = BlockingProvider()
        self.runtime = LcdActionRuntime(
            [], [], SimpleNamespace(send=Mock()), self.messages.append,
            general_media_bindings=[GeneralMediaBinding(0, 0)],
            guard=Mock(), media_provider=self.provider,
        )
        self.runtime.start()
        self.runtime.observe(media_event(1))
        self.pump_until(self.provider.entered.is_set)
        self.runtime.observe(media_event(2))
        release = threading.Timer(0.05, self.provider.release.set)
        release.start()
        try:
            self.runtime.stop()
        finally:
            release.join()
        self.assertEqual(self.provider.actions, [MediaAction.NEXT])
        self.assertEqual(self.messages[-1], {"type": "stopped"})

    def test_stop_rechecks_worker_after_slow_completed_result_service(self):
        self.runtime.stop()

        class FinishedDuringService:
            def __init__(self):
                self.checks = iter((True, False))

            def is_alive(self):
                return next(self.checks)

            def join(self, _timeout):  # pragma: no cover - must not be reached
                raise AssertionError("A dead worker must not be joined")

        self.runtime._media_worker = FinishedDuringService()
        self.runtime._begin_media_shutdown = Mock()
        self.runtime._cancel_media_guard_requests = Mock()
        self.runtime._service_media_worker = Mock()
        # The result service consumes more than the nominal deadline. Once it
        # returns the worker is already dead, so no worker timeout is valid.
        with patch(
                "swarm2.countdown_runtime.time.monotonic",
                side_effect=(100.0, 105.0)):
            self.runtime._stop_media_worker()
        self.assertEqual(self.runtime._service_media_worker.call_count, 2)


class GeneralMediaRequestTests(unittest.TestCase):
    def request(self, binding=None, *, lcd=GENERAL_MEDIA_LCD):
        return {
            "command": "start", "device_id": "fixture", "profile_slot": 1,
            "baseline": {
                "device_id": "fixture", "profile_slot": 1,
                "transport_identity": "fixture-port",
                "settings": {
                    "profile": CAPTURES["profile"],
                    "lcd": lcd.hex(),
                },
            },
            "bindings": [], "host_action_bindings": [],
            "general_media_bindings": [
                {"page_index": 0, "slot_index": 0}
                if binding is None else binding],
        }

    def test_media_only_request_requires_exact_stored_tile(self):
        prepared = validate_countdown_request(self.request())
        self.assertEqual(prepared.bindings, ())
        self.assertEqual(prepared.host_action_bindings, ())
        self.assertEqual(
            prepared.general_media_bindings, (GeneralMediaBinding(0, 0),))

        for binding, message in (
            ({"page_index": 0, "slot_index": 1}, "stored LCD tile"),
            ({"page_index": 1, "slot_index": 0}, "stored LCD tile"),
            ({"page_index": 0, "slot_index": 2}, "start in slot"),
            ({"page_index": 0, "slot_index": 0, "action": "next"},
             "missing or unknown"),
        ):
            with self.subTest(binding=binding), self.assertRaisesRegex(
                    DeviceError, message):
                validate_countdown_request(self.request(binding))

    def test_slot_one_wide_panel_request_matches_its_stored_anchor(self):
        prepared = validate_countdown_request(self.request(
            {"page_index": 0, "slot_index": 1},
            lcd=GENERAL_MEDIA_LCD_SLOT_1,
        ))
        self.assertEqual(
            prepared.general_media_bindings, (GeneralMediaBinding(0, 1),))

    def test_old_requests_default_to_no_media_bindings(self):
        request = self.request()
        request.pop("general_media_bindings")
        request["bindings"] = [{
            "timer_id": "timer", "page_index": 0, "slot_index": 3,
            "duration_seconds": 3,
        }]
        # The stored slot is DPI, so validation reaches and rejects the old
        # binding normally instead of requiring the new optional field.
        with self.assertRaisesRegex(DeviceError, "stored LCD tile"):
            validate_countdown_request(request)

    def test_preferred_player_is_optional_bounded_and_requires_media(self):
        player = "org.mpris.MediaPlayer2.vlc"
        request = self.request()
        request["preferred_media_player"] = player
        prepared = validate_countdown_request(request)
        self.assertEqual(prepared.preferred_media_player, player)

        for invalid in ("vlc", "org.mpris.MediaPlayer2.2bad", 4, False):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                    DeviceError, "Media player"):
                changed = self.request()
                changed["preferred_media_player"] = invalid
                validate_countdown_request(changed)

        without_media = self.request()
        without_media["general_media_bindings"] = []
        without_media["bindings"] = [{
            "timer_id": "timer", "page_index": 0, "slot_index": 3,
            "duration_seconds": 3,
        }]
        without_media["preferred_media_player"] = player
        with self.assertRaisesRegex(DeviceError, "requires a General Media"):
            validate_countdown_request(without_media)


if __name__ == "__main__":
    unittest.main()
