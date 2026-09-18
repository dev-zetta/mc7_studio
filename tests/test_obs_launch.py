"""Protocol, host boundary and shared-listener tests for Launch OBS."""

from types import SimpleNamespace
import subprocess
import unittest
from unittest.mock import Mock

from swarm2.countdown import PreparedCountdownRequest, validate_countdown_request
from swarm2.countdown_runtime import LcdActionRuntime
from swarm2.lcd_commands import (
    LCD_WIDGETS,
    build_lcd_report,
    decode_lcd_response,
    edited_lcd_signature,
)
from swarm2.obs_actions import (
    OBS_FLATPAK_APP_ID,
    OBS_FLATPAK_LAUNCH_GRACE_SECONDS,
    OBS_FLATPAK_PROBE_TIMEOUT_SECONDS,
    ObsLaunchBinding,
    build_obs_launch_argv,
    execute_obs_launch,
)
from swarm2.obs_commands import decode_obs_launch_touch
from swarm2.protocol import ProtocolError
from swarm2.transport import DeviceError
from tests.test_settings import CAPTURES, with_lcd_page


OBS_PRESS_SLOT_0 = bytes.fromhex("1033134b00010100")
OBS_RELEASE_SLOT_0 = bytes.fromhex("1033134b00010000")


def obs_lcd(slot=0):
    signatures = [b"\xfe\x00"] * 4
    signatures[slot] = b"\x4b\x00"
    return with_lcd_page(
        bytes.fromhex(CAPTURES["lcd"]), 0, tuple(signatures)
    )


class ObsLaunchCodecTests(unittest.TestCase):
    def test_source_mapped_tile_and_exact_press_release_envelope(self):
        self.assertEqual(LCD_WIDGETS["launch_obs"].signature, (0x4B, 0))
        self.assertEqual(LCD_WIDGETS["launch_obs"].width, 1)

        state = decode_lcd_response(bytes.fromhex(CAPTURES["lcd"]), 0)
        edits = {0: ["launch_obs", "empty", "empty", "empty"]}
        report = build_lcd_report(state, pages=edits)
        self.assertEqual(
            report[5:16].hex(), "010000fe00fe00fe004b00"
        )
        response = bytearray(report[:61])
        response[2] = 0
        decoded = decode_lcd_response(response, 0)
        self.assertEqual(
            decoded.signature,
            edited_lcd_signature(state, pages=edits),
        )
        self.assertEqual(
            [widget.key for widget in decoded.pages[0].slots],
            ["launch_obs", "empty", "empty", "empty"],
        )

        press = decode_obs_launch_touch(OBS_PRESS_SLOT_0)
        release = decode_obs_launch_touch(OBS_RELEASE_SLOT_0)
        self.assertEqual(
            (press.page_index, press.slot_index, press.pressed),
            (0, 0, True),
        )
        self.assertEqual(
            (release.page_index, release.slot_index, release.pressed),
            (0, 0, False),
        )
        other = decode_obs_launch_touch(bytes.fromhex("1033214b00010100"))
        self.assertEqual((other.page_index, other.slot_index), (1, 2))

    def test_decoder_rejects_malformed_coordinates_and_ignores_other_events(self):
        for malformed in (
            bytes(7),
            bytes.fromhex("1033034b00010100"),
            bytes.fromhex("1033144b00010100"),
        ):
            with self.subTest(malformed=malformed), self.assertRaises(ProtocolError):
                decode_obs_launch_touch(malformed)
        for unrelated in (
            bytes.fromhex("1033134a00010100"),
            bytes.fromhex("1033134b01010100"),
            bytes.fromhex("1033134b00020100"),
            bytes.fromhex("1033134b00010200"),
            bytes.fromhex("1033134b00010101"),
        ):
            with self.subTest(unrelated=unrelated):
                self.assertIsNone(decode_obs_launch_touch(unrelated))


class ObsHostLauncherTests(unittest.TestCase):
    def test_fixed_platform_argv_and_missing_provider_boundary(self):
        finds = []

        def find(name):
            finds.append(name)
            return "/opt/obs/bin/obs"

        self.assertEqual(
            build_obs_launch_argv(system="Linux", find_executable=find),
            ("/opt/obs/bin/obs",),
        )
        self.assertEqual(finds, ["obs"])
        self.assertEqual(
            build_obs_launch_argv(system="Darwin", find_executable=find),
            ("/usr/bin/open", "-a", "OBS"),
        )
        self.assertEqual(finds, ["obs"])
        with self.assertRaisesRegex(DeviceError, "native obs.*Flatpak"):
            build_obs_launch_argv(
                system="Linux", find_executable=lambda _name: None
            )
        self.assertEqual(
            build_obs_launch_argv(
                system="Windows", find_executable=lambda name: "C:\\OBS\\obs64.exe" if name == "obs64.exe" else None),
            ("C:\\OBS\\obs64.exe",),
        )

    def test_flatpak_fallback_uses_exact_installed_app_and_no_shell(self):
        finds = []

        def find(name):
            finds.append(name)
            return "/usr/bin/flatpak" if name == "flatpak" else None

        probe = Mock(return_value=SimpleNamespace(returncode=0))
        self.assertEqual(
            build_obs_launch_argv(
                system="Linux",
                find_executable=find,
                probe_runner=probe,
            ),
            ("/usr/bin/flatpak", "run", OBS_FLATPAK_APP_ID),
        )
        self.assertEqual(finds, ["obs", "flatpak"])
        self.assertEqual(
            probe.call_args.args,
            ((
                "/usr/bin/flatpak",
                "info",
                "--show-ref",
                OBS_FLATPAK_APP_ID,
            ),),
        )
        self.assertFalse(probe.call_args.kwargs["shell"])
        self.assertEqual(probe.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(probe.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(probe.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertTrue(probe.call_args.kwargs["close_fds"])
        self.assertEqual(
            probe.call_args.kwargs["timeout"],
            OBS_FLATPAK_PROBE_TIMEOUT_SECONDS,
        )
        self.assertFalse(probe.call_args.kwargs["check"])

    def test_flatpak_probe_failures_and_invalid_paths_are_bounded(self):
        with self.assertRaisesRegex(DeviceError, "unavailable"):
            build_obs_launch_argv(
                system="Linux",
                find_executable=lambda name: (
                    "/usr/bin/flatpak" if name == "flatpak" else None
                ),
                probe_runner=Mock(
                    return_value=SimpleNamespace(returncode=1)
                ),
            )

        check_failures = (
            SimpleNamespace(returncode=True),
            object(),
            subprocess.TimeoutExpired(("flatpak", "info"), 2),
            OSError("fixture failure"),
        )
        for response in check_failures:
            probe = (
                Mock(side_effect=response)
                if isinstance(response, BaseException)
                else Mock(return_value=response)
            )
            with self.subTest(response=type(response).__name__):
                with self.assertRaisesRegex(DeviceError, "Could not check"):
                    build_obs_launch_argv(
                        system="Linux",
                        find_executable=lambda name: (
                            "/usr/bin/flatpak" if name == "flatpak" else None
                        ),
                        probe_runner=probe,
                    )

        for invalid in ("flatpak", "", "/bad\npath", "/" + "x" * 4096):
            probe = Mock(side_effect=AssertionError("probe must not run"))
            with self.subTest(invalid=invalid[:20]):
                with self.assertRaisesRegex(DeviceError, "unavailable"):
                    build_obs_launch_argv(
                        system="Linux",
                        find_executable=lambda name, value=invalid: (
                            value if name == "flatpak" else None
                        ),
                        probe_runner=probe,
                    )
                probe.assert_not_called()

    def test_native_obs_precedes_flatpak_without_running_probe(self):
        finds = []

        def find(name):
            finds.append(name)
            return "/usr/bin/obs" if name == "obs" else "/usr/bin/flatpak"

        probe = Mock(side_effect=AssertionError("probe must not run"))
        self.assertEqual(
            build_obs_launch_argv(
                system="Linux",
                find_executable=find,
                probe_runner=probe,
            ),
            ("/usr/bin/obs",),
        )
        self.assertEqual(finds, ["obs"])
        probe.assert_not_called()

    def test_execute_uses_no_shell_and_validates_binding_before_process(self):
        process = Mock()
        binding = ObsLaunchBinding(2, 3)
        execute_obs_launch(
            binding,
            system="Linux",
            find_executable=lambda _name: "/usr/bin/obs",
            process_factory=process,
        )
        self.assertEqual(process.call_args.args, (("/usr/bin/obs",),))
        self.assertFalse(process.call_args.kwargs["shell"])
        self.assertTrue(process.call_args.kwargs["close_fds"])
        self.assertTrue(process.call_args.kwargs["start_new_session"])

        process.reset_mock()
        probe = Mock(return_value=SimpleNamespace(returncode=0))
        process.return_value.wait.side_effect = subprocess.TimeoutExpired(
            ("flatpak", "run"), OBS_FLATPAK_LAUNCH_GRACE_SECONDS
        )
        execute_obs_launch(
            binding,
            system="Linux",
            find_executable=lambda name: (
                "/usr/bin/flatpak" if name == "flatpak" else None
            ),
            probe_runner=probe,
            process_factory=process,
        )
        self.assertEqual(
            process.call_args.args,
            (("/usr/bin/flatpak", "run", OBS_FLATPAK_APP_ID),),
        )
        self.assertFalse(process.call_args.kwargs["shell"])
        process.return_value.wait.assert_called_once_with(
            timeout=OBS_FLATPAK_LAUNCH_GRACE_SECONDS
        )

        process.reset_mock()
        with self.assertRaisesRegex(DeviceError, "validated"):
            execute_obs_launch(object(), process_factory=process)
        process.assert_not_called()

    def test_flatpak_launch_checks_immediate_wrapper_exit(self):
        binding = ObsLaunchBinding(0, 0)
        finder = lambda name: "/usr/bin/flatpak" if name == "flatpak" else None
        probe = Mock(return_value=SimpleNamespace(returncode=0))

        process_factory = Mock()
        process_factory.return_value.wait.return_value = 0
        self.assertIs(
            execute_obs_launch(
                binding,
                system="Linux",
                find_executable=finder,
                probe_runner=probe,
                process_factory=process_factory,
            ),
            process_factory.return_value,
        )

        for response, message in (
            (1, "could not be opened through Flatpak"),
            (None, "could not be opened through Flatpak"),
            (OSError("fixture wait failure"), "status could not be checked"),
        ):
            process_factory = Mock()
            if isinstance(response, BaseException):
                process_factory.return_value.wait.side_effect = response
            else:
                process_factory.return_value.wait.return_value = response
            with self.subTest(response=response), self.assertRaisesRegex(
                DeviceError, message
            ):
                execute_obs_launch(
                    binding,
                    system="Linux",
                    find_executable=finder,
                    probe_runner=probe,
                    process_factory=process_factory,
                )


class ObsRuntimeAndRequestTests(unittest.TestCase):
    def test_runtime_guards_launches_and_suppresses_repeat_until_release(self):
        guards = Mock()
        launches = Mock()
        messages = []
        runtime = LcdActionRuntime(
            [],
            [],
            SimpleNamespace(send=Mock()),
            messages.append,
            obs_launch_bindings=[ObsLaunchBinding(0, 0)],
            guard=guards,
            obs_launcher=launches,
        )
        runtime.start()
        self.assertEqual(messages[-1], {
            "type": "ready", "timers": 0, "positions": 0,
            "host_actions": 1,
        })
        self.assertTrue(runtime.observe(OBS_PRESS_SLOT_0))
        self.assertTrue(runtime.observe(OBS_PRESS_SLOT_0))
        self.assertEqual(launches.call_count, 1)
        self.assertTrue(runtime.observe(OBS_RELEASE_SLOT_0))
        self.assertTrue(runtime.observe(OBS_PRESS_SLOT_0))
        self.assertEqual(launches.call_count, 2)
        self.assertEqual(messages[-1], {
            "type": "host_action", "event": "opened",
            "widget": "launch_obs", "page_index": 0, "slot_index": 0,
        })
        runtime.stop()
        self.assertEqual(guards.call_count, 4)

    def test_runtime_reports_missing_obs_and_retries_only_after_release(self):
        messages = []
        launches = Mock(side_effect=DeviceError(
            "Install OBS Studio so its obs executable is on PATH"
        ))
        runtime = LcdActionRuntime(
            [],
            [],
            SimpleNamespace(send=Mock()),
            messages.append,
            obs_launch_bindings=[ObsLaunchBinding(0, 0)],
            obs_launcher=launches,
        )
        runtime.start()

        self.assertTrue(runtime.observe(OBS_PRESS_SLOT_0))
        self.assertEqual(messages[-1], {
            "type": "host_action", "event": "failed",
            "widget": "launch_obs", "page_index": 0, "slot_index": 0,
            "error": "Install OBS Studio so its obs executable is on PATH",
        })
        message_count = len(messages)
        self.assertTrue(runtime.observe(OBS_PRESS_SLOT_0))
        self.assertEqual(launches.call_count, 1)
        self.assertEqual(len(messages), message_count)

        self.assertTrue(runtime.observe(OBS_RELEASE_SLOT_0))
        self.assertTrue(runtime.observe(OBS_PRESS_SLOT_0))
        self.assertEqual(launches.call_count, 2)
        self.assertEqual(len(messages), message_count + 1)

    def test_guard_and_unexpected_launcher_failures_remain_terminal(self):
        launches = Mock()
        guard = Mock(side_effect=[None, DeviceError("Layout changed")])
        runtime = LcdActionRuntime(
            [], [], SimpleNamespace(send=Mock()),
            obs_launch_bindings=[ObsLaunchBinding(0, 0)],
            guard=guard, obs_launcher=launches,
        )
        runtime.start()
        with self.assertRaisesRegex(DeviceError, "Layout changed"):
            runtime.observe(OBS_PRESS_SLOT_0)
        launches.assert_not_called()

        launcher = Mock(side_effect=RuntimeError("unexpected"))
        runtime = LcdActionRuntime(
            [], [], SimpleNamespace(send=Mock()),
            obs_launch_bindings=[ObsLaunchBinding(0, 0)],
            obs_launcher=launcher,
        )
        runtime.start()
        with self.assertRaisesRegex(RuntimeError, "unexpected"):
            runtime.observe(OBS_PRESS_SLOT_0)
        launcher.side_effect = None
        self.assertTrue(runtime.observe(OBS_PRESS_SLOT_0))
        self.assertEqual(launcher.call_count, 2)

    def test_invalid_launcher_device_errors_remain_terminal(self):
        for detail in ("", "bad\nerror", "x" * 4097):
            with self.subTest(detail_length=len(detail)):
                runtime = LcdActionRuntime(
                    [], [], SimpleNamespace(send=Mock()),
                    obs_launch_bindings=[ObsLaunchBinding(0, 0)],
                    obs_launcher=Mock(side_effect=DeviceError(detail)),
                )
                runtime.start()
                with self.assertRaisesRegex(DeviceError, "invalid error"):
                    runtime.observe(OBS_PRESS_SLOT_0)

    def test_runtime_does_not_claim_unbound_obs_or_overlapping_positions(self):
        runtime = LcdActionRuntime(
            [], [], SimpleNamespace(send=Mock()),
            obs_launch_bindings=[ObsLaunchBinding(0, 1)],
            obs_launcher=Mock(),
        )
        runtime.start()
        self.assertFalse(runtime.observe(OBS_PRESS_SLOT_0))
        with self.assertRaisesRegex(ValueError, "configured once"):
            LcdActionRuntime(
                [], [], SimpleNamespace(send=Mock()),
                obs_launch_bindings=[
                    ObsLaunchBinding(0, 0), ObsLaunchBinding(0, 0)
                ],
            )

    def test_request_requires_exact_stored_obs_tile_without_screen_key_data(self):
        request = {
            "command": "start",
            "device_id": "fixture",
            "profile_slot": 1,
            "baseline": {
                "device_id": "fixture",
                "profile_slot": 1,
                "transport_identity": "fixture-port",
                "settings": {
                    "profile": CAPTURES["profile"],
                    "lcd": obs_lcd().hex(),
                },
            },
            "obs_launch_bindings": [
                {"page_index": 0, "slot_index": 0}
            ],
        }
        prepared = validate_countdown_request(request)
        self.assertEqual(
            prepared.obs_launch_bindings, (ObsLaunchBinding(0, 0),)
        )
        self.assertIsNone(prepared.expected_screen_keys)

        request["obs_launch_bindings"][0]["slot_index"] = 1
        with self.assertRaisesRegex(DeviceError, "stored LCD tile"):
            validate_countdown_request(request)

    def test_prepared_request_preserves_the_original_positional_field_order(self):
        screen_keys = object()
        prepared = PreparedCountdownRequest(
            "fixture", 0, "fixture-port", "profile", "lcd", (), (), (),
            screen_keys, "org.mpris.MediaPlayer2.vlc",
        )
        self.assertIs(prepared.expected_screen_keys, screen_keys)
        self.assertEqual(
            prepared.preferred_media_player, "org.mpris.MediaPlayer2.vlc"
        )
        self.assertEqual(prepared.obs_launch_bindings, ())


if __name__ == "__main__":
    unittest.main()
