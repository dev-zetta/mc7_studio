import json
import sys
import unittest
from unittest.mock import Mock

from swarm2.automatic_profiles import ApplicationIdentity
from swarm2.automatic_profile_monitor import (
    COMMAND_TIMEOUT_SECONDS,
    MAX_COMMAND_OUTPUT_BYTES,
    MAX_SWAY_TREE_OUTPUT_BYTES,
    MAX_SWAY_TREE_NODES,
    ForegroundApplicationMonitor,
    ForegroundApplicationSnapshot,
    ForegroundApplicationStatus,
    MonitorCommandResult,
    _CommandOutputLimit,
    _CommandTimedOut,
    _run_bounded_command,
)
from swarm2.gnome_extension import (
    GNOME_DBUS_INTERFACE,
    GNOME_DBUS_NAME,
    GNOME_DBUS_OBJECT_PATH,
    GNOME_EXTENSION_UUID,
)


class CommandFixture:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, arguments, timeout, maximum):
        self.calls.append((tuple(arguments), timeout, maximum))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class LinuxForegroundApplicationMonitorTests(unittest.TestCase):
    def monitor(self, runner, **changes):
        values = {
            "system": "Linux",
            "environment": {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
            "find_executable": lambda name: "/usr/bin/xprop",
            "run_command": runner,
            "readlink": lambda path: "/opt/example/bin/game",
        }
        values.update(changes)
        return ForegroundApplicationMonitor(**values)

    def test_active_x11_window_resolves_one_exact_executable(self):
        runner = CommandFixture(
            [
                MonitorCommandResult(
                    0,
                    b"_NET_ACTIVE_WINDOW(WINDOW): window id # 0x02A\n",
                    b"",
                ),
                MonitorCommandResult(0, b"_NET_WM_PID(CARDINAL) = 4242\n", b""),
            ]
        )
        readlink = Mock(return_value="/opt/example/bin/game")
        result = self.monitor(runner, readlink=readlink).poll()

        self.assertEqual(result.status, ForegroundApplicationStatus.AVAILABLE)
        self.assertEqual(
            result.application,
            ApplicationIdentity(executable_path="/opt/example/bin/game"),
        )
        self.assertTrue(result.succeeded)
        self.assertEqual(result.applications, (result.application,))
        self.assertEqual(
            [call[0] for call in runner.calls],
            [
                ("/usr/bin/xprop", "-root", "_NET_ACTIVE_WINDOW"),
                ("/usr/bin/xprop", "-id", "0x02a", "_NET_WM_PID"),
            ],
        )
        self.assertTrue(
            all(
                call[1:] == (COMMAND_TIMEOUT_SECONDS, MAX_COMMAND_OUTPUT_BYTES)
                for call in runner.calls
            )
        )
        readlink.assert_called_once_with("/proc/4242/exe")

    def test_non_gnome_wayland_is_unavailable_and_never_uses_xwayland(self):
        runner = Mock(side_effect=AssertionError("must not run"))
        finder = Mock(side_effect=AssertionError("must not search"))
        result = self.monitor(
            runner,
            environment={
                "XDG_SESSION_TYPE": "wayland",
                "WAYLAND_DISPLAY": "wayland-0",
                "DISPLAY": ":0",
            },
            find_executable=finder,
        ).poll()
        self.assertEqual(result.status, ForegroundApplicationStatus.UNAVAILABLE)
        self.assertIn("Wayland", result.message)

    def test_sway_wayland_resolves_the_focused_pid_with_a_bounded_tree_query(self):
        payload = json.dumps(
            {
                "focused": False,
                "nodes": [
                    {
                        "focused": False,
                        "nodes": [
                            {
                                "focused": True,
                                "pid": 4242,
                                "nodes": [],
                                "floating_nodes": [],
                            }
                        ],
                        "floating_nodes": [],
                    }
                ],
                "floating_nodes": [],
            }
        ).encode()
        runner = CommandFixture([MonitorCommandResult(0, payload, b"")])
        readlink = Mock(return_value="/opt/example/bin/sway-game")
        result = self.monitor(
            runner,
            environment={
                "XDG_SESSION_TYPE": "wayland",
                "XDG_CURRENT_DESKTOP": "sway",
                "DISPLAY": ":0",
            },
            find_executable=lambda name: "/usr/bin/swaymsg"
            if name == "swaymsg"
            else None,
            readlink=readlink,
        ).poll()

        self.assertEqual(result.status, ForegroundApplicationStatus.AVAILABLE)
        self.assertEqual(
            result.application,
            ApplicationIdentity(executable_path="/opt/example/bin/sway-game"),
        )
        self.assertEqual(
            runner.calls,
            [
                (
                    ("/usr/bin/swaymsg", "--raw", "-t", "get_tree"),
                    COMMAND_TIMEOUT_SECONDS,
                    MAX_SWAY_TREE_OUTPUT_BYTES,
                )
            ],
        )
        readlink.assert_called_once_with("/proc/4242/exe")

    def test_sway_wayland_reports_no_focused_window_without_using_xwayland(self):
        payload = b'{"focused":false,"nodes":[],"floating_nodes":[]}'
        runner = CommandFixture([MonitorCommandResult(0, payload, b"")])
        result = self.monitor(
            runner,
            environment={
                "XDG_SESSION_TYPE": "wayland",
                "SWAYSOCK": "/run/user/1000/sway-ipc.sock",
                "DISPLAY": ":0",
            },
            find_executable=lambda name: "/usr/bin/swaymsg",
        ).poll()

        self.assertEqual(result.status, ForegroundApplicationStatus.NO_APPLICATION)
        self.assertTrue(result.succeeded)
        self.assertEqual(len(runner.calls), 1)

    def test_sway_wayland_rejects_malformed_tree_schema_and_pids(self):
        oversized_tree = {
            "focused": False,
            "nodes": [
                {"focused": False, "nodes": [], "floating_nodes": []}
                for _ in range(MAX_SWAY_TREE_NODES)
            ],
            "floating_nodes": [],
        }
        cases = (
            b"not-json",
            b'{"focused":false,"focused":true}',
            b'{"focused":0,"nodes":[],"floating_nodes":[]}',
            b'{"focused":true,"pid":true,"nodes":[],"floating_nodes":[]}',
            b'{"focused":true,"pid":2147483648,"nodes":[],"floating_nodes":[]}',
            b'{"focused":false,"nodes":{},"floating_nodes":[]}',
            json.dumps(
                {
                    "focused": True,
                    "pid": 1,
                    "nodes": [
                        {
                            "focused": True,
                            "pid": 2,
                            "nodes": [],
                            "floating_nodes": [],
                        }
                    ],
                    "floating_nodes": [],
                }
            ).encode(),
            json.dumps(oversized_tree).encode(),
        )
        for payload in cases:
            with self.subTest(payload_length=len(payload)):
                result = self.monitor(
                    CommandFixture([MonitorCommandResult(0, payload, b"")]),
                    environment={
                        "XDG_SESSION_TYPE": "wayland",
                        "XDG_CURRENT_DESKTOP": "sway",
                    },
                    find_executable=lambda name: "/usr/bin/swaymsg",
                ).poll()
                self.assertEqual(result.status, ForegroundApplicationStatus.ERROR)
                self.assertFalse(result.succeeded)

    def test_sway_wayland_handles_output_limit_missing_tool_and_ipc_failure(self):
        environment = {"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "sway"}
        oversized = self.monitor(
            CommandFixture(
                [
                    MonitorCommandResult(
                        0, b"x" * (MAX_SWAY_TREE_OUTPUT_BYTES + 1), b""
                    )
                ]
            ),
            environment=environment,
            find_executable=lambda name: "/usr/bin/swaymsg",
        ).poll()
        self.assertEqual(oversized.status, ForegroundApplicationStatus.ERROR)
        self.assertIn("size limit", oversized.message)

        missing = self.monitor(
            Mock(side_effect=AssertionError("must not run")),
            environment=environment,
            find_executable=lambda name: None,
        ).poll()
        self.assertEqual(missing.status, ForegroundApplicationStatus.UNAVAILABLE)
        self.assertIn("swaymsg", missing.message)

        failed = self.monitor(
            CommandFixture(
                [MonitorCommandResult(1, b"", b"Unable to connect to IPC socket")]
            ),
            environment=environment,
            find_executable=lambda name: "/usr/bin/swaymsg",
        ).poll()
        self.assertEqual(failed.status, ForegroundApplicationStatus.UNAVAILABLE)

    def test_hyprland_wayland_resolves_the_active_window_pid(self):
        runner = CommandFixture(
            [MonitorCommandResult(0, b'{"address":"0x123","pid":31337}', b"")]
        )
        readlink = Mock(return_value="/opt/example/bin/hypr-game")
        result = self.monitor(
            runner,
            environment={
                "XDG_SESSION_TYPE": "wayland",
                "HYPRLAND_INSTANCE_SIGNATURE": "instance-token",
                "DISPLAY": ":0",
            },
            find_executable=lambda name: "/usr/bin/hyprctl"
            if name == "hyprctl"
            else None,
            readlink=readlink,
        ).poll()

        self.assertEqual(result.status, ForegroundApplicationStatus.AVAILABLE)
        self.assertEqual(
            result.application,
            ApplicationIdentity(executable_path="/opt/example/bin/hypr-game"),
        )
        self.assertEqual(
            runner.calls,
            [
                (
                    ("/usr/bin/hyprctl", "-j", "activewindow"),
                    COMMAND_TIMEOUT_SECONDS,
                    MAX_COMMAND_OUTPUT_BYTES,
                )
            ],
        )
        readlink.assert_called_once_with("/proc/31337/exe")

    def test_hyprland_wayland_reports_no_active_window(self):
        result = self.monitor(
            CommandFixture([MonitorCommandResult(0, b"{}", b"")]),
            environment={
                "WAYLAND_DISPLAY": "wayland-1",
                "XDG_SESSION_DESKTOP": "Hyprland",
            },
            find_executable=lambda name: "/usr/bin/hyprctl",
        ).poll()
        self.assertEqual(result.status, ForegroundApplicationStatus.NO_APPLICATION)
        self.assertTrue(result.succeeded)

    def test_wayland_desktop_name_takes_precedence_over_stale_socket_variables(self):
        runner = CommandFixture([MonitorCommandResult(0, b"{}", b"")])

        def find_executable(name):
            if name != "hyprctl":
                raise AssertionError(f"unexpected provider: {name}")
            return "/usr/bin/hyprctl"

        result = self.monitor(
            runner,
            environment={
                "XDG_SESSION_TYPE": "wayland",
                "XDG_CURRENT_DESKTOP": "Hyprland",
                "SWAYSOCK": "/stale/sway.sock",
            },
            find_executable=find_executable,
        ).poll()

        self.assertEqual(result.status, ForegroundApplicationStatus.NO_APPLICATION)
        self.assertEqual(runner.calls[0][0][0], "/usr/bin/hyprctl")

    def test_hyprland_wayland_rejects_malformed_schema_and_pids(self):
        cases = (
            b"[]",
            b'{"pid":12,"pid":13}',
            b'{"address":"0x123"}',
            b'{"pid":null}',
            b'{"pid":true}',
            b'{"pid":0}',
            b'{"pid":2147483648}',
            b'{"pid":NaN}',
            b"\xff",
        )
        for payload in cases:
            with self.subTest(payload=payload):
                result = self.monitor(
                    CommandFixture([MonitorCommandResult(0, payload, b"")]),
                    environment={
                        "XDG_SESSION_TYPE": "wayland",
                        "XDG_CURRENT_DESKTOP": "Hyprland",
                    },
                    find_executable=lambda name: "/usr/bin/hyprctl",
                ).poll()
                self.assertEqual(result.status, ForegroundApplicationStatus.ERROR)

    def test_hyprland_wayland_handles_output_limit_missing_tool_and_failures(self):
        environment = {
            "XDG_SESSION_TYPE": "wayland",
            "XDG_CURRENT_DESKTOP": "Hyprland",
        }
        oversized = self.monitor(
            CommandFixture(
                [MonitorCommandResult(0, b"x" * (MAX_COMMAND_OUTPUT_BYTES + 1), b"")]
            ),
            environment=environment,
            find_executable=lambda name: "/usr/bin/hyprctl",
        ).poll()
        self.assertEqual(oversized.status, ForegroundApplicationStatus.ERROR)

        missing = self.monitor(
            Mock(side_effect=AssertionError("must not run")),
            environment=environment,
            find_executable=lambda name: None,
        ).poll()
        self.assertEqual(missing.status, ForegroundApplicationStatus.UNAVAILABLE)
        self.assertIn("hyprctl", missing.message)

        failed = self.monitor(
            CommandFixture([MonitorCommandResult(1, b"", b"query failed")]),
            environment=environment,
            find_executable=lambda name: "/usr/bin/hyprctl",
        ).poll()
        self.assertEqual(failed.status, ForegroundApplicationStatus.ERROR)
        self.assertIn("Hyprland", failed.message)

    def test_gnome_wayland_resolves_the_extension_pid_to_an_exact_path(self):
        runner = CommandFixture(
            [MonitorCommandResult(0, b"(true, uint32 4242)\n", b"")]
        )
        readlink = Mock(return_value="/opt/example/bin/game")
        result = self.monitor(
            runner,
            environment={
                "XDG_SESSION_TYPE": "wayland",
                "WAYLAND_DISPLAY": "wayland-0",
                "DISPLAY": ":0",
                "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
            },
            find_executable=lambda name: "/usr/bin/gdbus" if name == "gdbus" else None,
            readlink=readlink,
        ).poll()

        self.assertEqual(result.status, ForegroundApplicationStatus.AVAILABLE)
        self.assertEqual(
            result.application,
            ApplicationIdentity(executable_path="/opt/example/bin/game"),
        )
        self.assertEqual(
            runner.calls,
            [
                (
                    (
                        "/usr/bin/gdbus",
                        "call",
                        "--session",
                        "--dest",
                        GNOME_DBUS_NAME,
                        "--object-path",
                        GNOME_DBUS_OBJECT_PATH,
                        "--method",
                        f"{GNOME_DBUS_INTERFACE}.GetFocusedWindowPid",
                    ),
                    COMMAND_TIMEOUT_SECONDS,
                    MAX_COMMAND_OUTPUT_BYTES,
                )
            ],
        )
        readlink.assert_called_once_with("/proc/4242/exe")

    def test_missing_gnome_extension_is_actionable_and_never_selects_default(self):
        runner = CommandFixture(
            [
                MonitorCommandResult(
                    1,
                    b"",
                    b"GDBus.Error:org.freedesktop.DBus.Error.ServiceUnknown: "
                    b"The name was not provided by any .service files",
                )
            ]
        )
        result = self.monitor(
            runner,
            environment={
                "XDG_SESSION_TYPE": "wayland",
                "XDG_CURRENT_DESKTOP": "GNOME",
            },
            find_executable=lambda name: "/usr/bin/gdbus",
        ).poll()

        self.assertEqual(result.status, ForegroundApplicationStatus.UNAVAILABLE)
        self.assertFalse(result.succeeded)
        self.assertEqual(result.applications, ())
        self.assertIn(GNOME_EXTENSION_UUID, result.message)
        self.assertIn("swarm2-gnome-extension install", result.message)

    def test_gnome_wayland_distinguishes_no_window_from_a_window_without_pid(self):
        environment = {
            "WAYLAND_DISPLAY": "wayland-0",
            "XDG_CURRENT_DESKTOP": "GNOME-Classic:GNOME",
        }
        no_window = self.monitor(
            CommandFixture([MonitorCommandResult(0, b"(false, uint32 0)\n", b"")]),
            environment=environment,
            find_executable=lambda name: "/usr/bin/gdbus",
        ).poll()
        self.assertEqual(no_window.status, ForegroundApplicationStatus.NO_APPLICATION)
        self.assertTrue(no_window.succeeded)

        no_pid = self.monitor(
            CommandFixture([MonitorCommandResult(0, b"(true, uint32 0)\n", b"")]),
            environment=environment,
            find_executable=lambda name: "/usr/bin/gdbus",
        ).poll()
        self.assertEqual(no_pid.status, ForegroundApplicationStatus.ERROR)
        self.assertFalse(no_pid.succeeded)
        self.assertIn("no process identity", no_pid.message)

    def test_gnome_wayland_rejects_inconsistent_or_malformed_responses(self):
        environment = {
            "XDG_SESSION_TYPE": "wayland",
            "XDG_CURRENT_DESKTOP": "GNOME",
        }
        for response in (
            b"(false, uint32 12)\n",
            b"(true, uint32 2147483648)\n",
            b"(true, '12')\n",
            b"(true, uint32 12) trailing\n",
        ):
            with self.subTest(response=response):
                result = self.monitor(
                    CommandFixture([MonitorCommandResult(0, response, b"")]),
                    environment=environment,
                    find_executable=lambda name: "/usr/bin/gdbus",
                ).poll()
                self.assertEqual(result.status, ForegroundApplicationStatus.ERROR)
                self.assertFalse(result.succeeded)

    def test_missing_gdbus_and_incompatible_extension_are_unavailable(self):
        environment = {
            "XDG_SESSION_TYPE": "wayland",
            "XDG_CURRENT_DESKTOP": "GNOME",
        }
        missing = self.monitor(
            Mock(side_effect=AssertionError("must not run")),
            environment=environment,
            find_executable=lambda name: None,
        ).poll()
        self.assertEqual(missing.status, ForegroundApplicationStatus.UNAVAILABLE)
        self.assertIn("gdbus", missing.message)

        incompatible = self.monitor(
            CommandFixture(
                [
                    MonitorCommandResult(
                        1,
                        b"",
                        b"GDBus.Error:org.freedesktop.DBus.Error.UnknownMethod",
                    )
                ]
            ),
            environment=environment,
            find_executable=lambda name: "/usr/bin/gdbus",
        ).poll()
        self.assertEqual(incompatible.status, ForegroundApplicationStatus.UNAVAILABLE)
        self.assertIn("Reinstall", incompatible.message)

    def test_gnome_wayland_dbus_access_denial_is_an_explicit_permission_state(self):
        result = self.monitor(
            CommandFixture(
                [
                    MonitorCommandResult(
                        1,
                        b"",
                        b"GDBus.Error:org.freedesktop.DBus.Error.AccessDenied",
                    )
                ]
            ),
            environment={
                "XDG_SESSION_TYPE": "wayland",
                "XDG_CURRENT_DESKTOP": "GNOME",
            },
            find_executable=lambda name: "/usr/bin/gdbus",
        ).poll()
        self.assertEqual(result.status, ForegroundApplicationStatus.PERMISSION_DENIED)
        self.assertFalse(result.succeeded)

    def test_gnome_wayland_process_race_and_permission_denial_are_failures(self):
        environment = {
            "XDG_SESSION_TYPE": "wayland",
            "XDG_CURRENT_DESKTOP": "GNOME",
        }
        for error, expected in (
            (FileNotFoundError(), ForegroundApplicationStatus.ERROR),
            (PermissionError(), ForegroundApplicationStatus.PERMISSION_DENIED),
        ):
            with self.subTest(expected=expected):
                result = self.monitor(
                    CommandFixture(
                        [MonitorCommandResult(0, b"(true, uint32 77)\n", b"")]
                    ),
                    environment=environment,
                    find_executable=lambda name: "/usr/bin/gdbus",
                    readlink=Mock(side_effect=error),
                ).poll()
                self.assertEqual(result.status, expected)
                self.assertFalse(result.succeeded)

    def test_missing_display_or_xprop_is_explicitly_unavailable(self):
        cases = (
            ({"XDG_SESSION_TYPE": "x11"}, lambda name: "/usr/bin/xprop"),
            (
                {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
                lambda name: None,
            ),
        )
        for environment, finder in cases:
            with self.subTest(environment=environment):
                result = self.monitor(
                    Mock(side_effect=AssertionError("must not run")),
                    environment=environment,
                    find_executable=finder,
                ).poll()
                self.assertEqual(
                    result.status, ForegroundApplicationStatus.UNAVAILABLE
                )

    def test_no_window_is_empty_but_process_exit_is_not_a_safe_default(self):
        no_window = CommandFixture(
            [MonitorCommandResult(0, b"_NET_ACTIVE_WINDOW(WINDOW): window id # 0x0\n", b"")]
        )
        self.assertEqual(
            self.monitor(no_window).poll().status,
            ForegroundApplicationStatus.NO_APPLICATION,
        )

        exited = CommandFixture(
            [
                MonitorCommandResult(0, b"_NET_ACTIVE_WINDOW(WINDOW): window id # 0x2a\n", b""),
                MonitorCommandResult(0, b"_NET_WM_PID(CARDINAL) = 77\n", b""),
            ]
        )
        result = self.monitor(
            exited,
            readlink=Mock(side_effect=FileNotFoundError),
        ).poll()
        self.assertEqual(result.status, ForegroundApplicationStatus.ERROR)
        self.assertFalse(result.succeeded)

    def test_active_window_without_pid_is_not_a_safe_default(self):
        runner = CommandFixture(
            [
                MonitorCommandResult(0, b"_NET_ACTIVE_WINDOW(WINDOW): window id # 0x2a\n", b""),
                MonitorCommandResult(0, b"_NET_WM_PID:  no such atom on any window.\n", b""),
            ]
        )
        result = self.monitor(runner).poll()
        self.assertEqual(result.status, ForegroundApplicationStatus.UNAVAILABLE)
        self.assertFalse(result.succeeded)

    def test_permission_denial_is_distinct_from_unavailable_and_error(self):
        command_denied = CommandFixture(
            [MonitorCommandResult(1, b"", b"Authorization required")]
        )
        self.assertEqual(
            self.monitor(command_denied).poll().status,
            ForegroundApplicationStatus.PERMISSION_DENIED,
        )

        readlink_denied = CommandFixture(
            [
                MonitorCommandResult(0, b"_NET_ACTIVE_WINDOW(WINDOW): window id # 0x2a\n", b""),
                MonitorCommandResult(0, b"_NET_WM_PID(CARDINAL) = 77\n", b""),
            ]
        )
        self.assertEqual(
            self.monitor(
                readlink_denied,
                readlink=Mock(side_effect=PermissionError),
            ).poll().status,
            ForegroundApplicationStatus.PERMISSION_DENIED,
        )

    def test_malformed_or_failed_x11_responses_are_explicit_errors(self):
        cases = (
            MonitorCommandResult(0, b"not a window", b""),
            MonitorCommandResult(1, b"", b"xprop failed"),
            MonitorCommandResult(0, b"\xff", b""),
        )
        for response in cases:
            with self.subTest(response=response):
                result = self.monitor(CommandFixture([response])).poll()
                self.assertEqual(result.status, ForegroundApplicationStatus.ERROR)

    def test_missing_active_window_atom_marks_the_monitor_unavailable(self):
        runner = CommandFixture(
            [
                MonitorCommandResult(
                    0,
                    b"_NET_ACTIVE_WINDOW:  no such atom on any window.\n",
                    b"",
                )
            ]
        )
        self.assertEqual(
            self.monitor(runner).poll().status,
            ForegroundApplicationStatus.UNAVAILABLE,
        )

    def test_inaccessible_x11_display_is_unavailable(self):
        runner = CommandFixture(
            [MonitorCommandResult(1, b"", b"xprop: unable to open display ':0'")]
        )
        result = self.monitor(runner).poll()
        self.assertEqual(result.status, ForegroundApplicationStatus.UNAVAILABLE)
        self.assertFalse(result.succeeded)
        self.assertEqual(result.applications, ())


class MacOSForegroundApplicationMonitorTests(unittest.TestCase):
    def monitor(self, runner, **changes):
        values = {
            "system": "Darwin",
            "environment": {},
            "find_executable": lambda name: "/usr/bin/osascript",
            "run_command": runner,
        }
        values.update(changes)
        return ForegroundApplicationMonitor(**values)

    def test_frontmost_nsworkspace_application_returns_path_and_bundle_id(self):
        payload = json.dumps(
            {
                "bundle_id": "COM.Example.Game",
                "executable_path": "/Applications/Game.app/Contents/MacOS/Game",
            }
        ).encode()
        runner = CommandFixture([MonitorCommandResult(0, payload, b"")])
        result = self.monitor(runner).poll()

        self.assertEqual(result.status, ForegroundApplicationStatus.AVAILABLE)
        self.assertEqual(
            result.application,
            ApplicationIdentity(
                executable_path="/Applications/Game.app/Contents/MacOS/Game",
                bundle_id="com.example.game",
            ),
        )
        arguments = runner.calls[0][0]
        self.assertEqual(arguments[:4], ("/usr/bin/osascript", "-l", "JavaScript", "-e"))
        self.assertIn("frontmostApplication", arguments[4])
        self.assertNotIn("/Applications/Game.app", arguments[4])

    def test_no_frontmost_application_is_an_empty_observation(self):
        payload = b'{"bundle_id": null, "executable_path": null}'
        result = self.monitor(
            CommandFixture([MonitorCommandResult(0, payload, b"")])
        ).poll()
        self.assertEqual(result.status, ForegroundApplicationStatus.NO_APPLICATION)
        self.assertTrue(result.succeeded)
        self.assertEqual(result.applications, ())

    def test_privacy_denial_is_an_explicit_permission_state(self):
        runner = CommandFixture(
            [MonitorCommandResult(1, b"", b"Not authorized to send Apple events. (-1743)")]
        )
        self.assertEqual(
            self.monitor(runner).poll().status,
            ForegroundApplicationStatus.PERMISSION_DENIED,
        )

    def test_invalid_json_fields_and_identities_are_errors(self):
        cases = (
            b"not-json",
            b'{"bundle_id": "com.example.Game"}',
            b'{"bundle_id": 4, "executable_path": null}',
            b'{"bundle_id": "invalid", "executable_path": "relative"}',
        )
        for payload in cases:
            with self.subTest(payload=payload):
                result = self.monitor(
                    CommandFixture([MonitorCommandResult(0, payload, b"")])
                ).poll()
                self.assertEqual(result.status, ForegroundApplicationStatus.ERROR)

    def test_missing_osascript_is_unavailable_without_running_a_command(self):
        runner = Mock(side_effect=AssertionError("must not run"))
        result = self.monitor(runner, find_executable=lambda name: None).poll()
        self.assertEqual(result.status, ForegroundApplicationStatus.UNAVAILABLE)


class ForegroundMonitorBoundaryTests(unittest.TestCase):
    def test_windows_uses_foreground_executable_identity(self):
        runner = Mock(side_effect=AssertionError("must not run"))
        result = ForegroundApplicationMonitor(
            system="Windows", environment={}, run_command=runner,
            windows_foreground=lambda: "C:/Program Files/Editor/editor.exe",
        ).poll()
        self.assertEqual(result.status, ForegroundApplicationStatus.AVAILABLE)
        self.assertEqual(
            result.application.executable_path,
            "c:/program files/editor/editor.exe",
        )
        runner.assert_not_called()

    def test_windows_no_foreground_application_is_explicit(self):
        result = ForegroundApplicationMonitor(
            system="Windows", environment={}, windows_foreground=lambda: None,
        ).poll()
        self.assertEqual(result.status, ForegroundApplicationStatus.NO_APPLICATION)

    def test_timeout_output_limit_and_wrong_injected_result_are_errors(self):
        cases = (
            _CommandTimedOut(),
            _CommandOutputLimit(),
            object(),
            MonitorCommandResult(0, b"x" * (MAX_COMMAND_OUTPUT_BYTES + 1), b""),
        )
        for value in cases:
            with self.subTest(value=type(value).__name__):
                runner = CommandFixture([value])
                result = ForegroundApplicationMonitor(
                    system="Darwin",
                    environment={},
                    find_executable=lambda name: "/usr/bin/osascript",
                    run_command=runner,
                ).poll()
                self.assertEqual(result.status, ForegroundApplicationStatus.ERROR)

    def test_available_snapshot_enforces_a_normalized_identity(self):
        snapshot = ForegroundApplicationSnapshot(
            ForegroundApplicationStatus.AVAILABLE,
            ApplicationIdentity(bundle_id="COM.Example.Game"),
        )
        self.assertEqual(snapshot.application.bundle_id, "com.example.game")
        with self.assertRaises(TypeError):
            ForegroundApplicationSnapshot(ForegroundApplicationStatus.AVAILABLE)
        with self.assertRaises(TypeError):
            ForegroundApplicationSnapshot(
                ForegroundApplicationStatus.UNAVAILABLE,
                ApplicationIdentity(executable_path="/bin/game"),
            )

    def test_real_runner_enforces_output_and_time_limits_without_a_shell(self):
        with self.assertRaises(_CommandOutputLimit):
            _run_bounded_command(
                (sys.executable, "-c", "import sys;sys.stdout.write('x'*257)"),
                2,
                256,
            )
        with self.assertRaises(_CommandTimedOut):
            _run_bounded_command(
                (sys.executable, "-c", "import time;time.sleep(2)"),
                0.05,
                256,
            )


if __name__ == "__main__":
    unittest.main()
