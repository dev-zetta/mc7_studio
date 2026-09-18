"""Offline checks for explicit host integration installation."""

from pathlib import Path
import os
import shutil
import sys
import tempfile
import time
import unittest

from swarm2.autostart import AutostartManager, AutostartState
from swarm2.gnome_extension import GNOME_EXTENSION_UUID
from swarm2.host_integrations import (
    CommandResult,
    HostIntegrationError,
    HostIntegrationManager,
    IntegrationState,
    UDEV_RULE_NAME,
    _MAX_COMMAND_OUTPUT,
    _default_runner,
    bundled_udev_rule,
)


GNOME_ENV = {
    "XDG_SESSION_TYPE": "wayland",
    "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
}


class HostIntegrationRunnerTests(unittest.TestCase):
    def test_real_runner_collects_both_output_streams_and_exit_status(self):
        result = _default_runner(
            (
                sys.executable,
                "-c",
                "import sys; print('normal output'); "
                "print('normal error', file=sys.stderr); sys.exit(7)",
            ),
            2.0,
        )

        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, f"normal output{os.linesep}")
        self.assertEqual(result.stderr, f"normal error{os.linesep}")

    def test_real_runner_accepts_exactly_the_output_limit(self):
        script = (
            "import sys; "
            f"sys.stdout.buffer.write(b'x' * {_MAX_COMMAND_OUTPUT}); "
            f"sys.stderr.buffer.write(b'y' * {_MAX_COMMAND_OUTPUT})"
        )
        result = _default_runner((sys.executable, "-c", script), 2.0)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "x" * _MAX_COMMAND_OUTPUT)
        self.assertEqual(result.stderr, "y" * _MAX_COMMAND_OUTPUT)

    def test_real_runner_enforces_timeout(self):
        started = time.monotonic()
        with self.assertRaisesRegex(HostIntegrationError, "did not finish within"):
            _default_runner(
                (sys.executable, "-c", "import time; time.sleep(10)"),
                0.05,
            )
        self.assertLess(time.monotonic() - started, 2.0)

    def test_real_runner_stops_child_immediately_on_output_overflow(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "child-survived"
            script = (
                "from pathlib import Path; import sys, time; "
                f"sys.stdout.buffer.write(b'x' * ({_MAX_COMMAND_OUTPUT} + 1)); "
                "sys.stdout.buffer.flush(); time.sleep(0.4); "
                "Path(sys.argv[1]).write_text('survived')"
            )
            with self.assertRaisesRegex(HostIntegrationError, "one output stream"):
                _default_runner((sys.executable, "-c", script, str(marker)), 2.0)
            time.sleep(0.6)
            self.assertFalse(marker.exists())


class HostIntegrationAssetTests(unittest.TestCase):
    def test_wheel_asset_and_packaging_rule_are_identical(self):
        packaged = (
            Path(__file__).resolve().parents[1]
            / "packaging"
            / "udev"
            / UDEV_RULE_NAME
        )
        self.assertEqual(bundled_udev_rule(), packaged.read_bytes())

    @unittest.skipIf(sys.platform == "win32", "Start-at-login uses POSIX desktop APIs")
    def test_macos_only_start_at_login_is_applicable(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = HostIntegrationManager(
                platform="darwin",
                environ={},
                autostart_manager=AutostartManager(
                    platform="darwin",
                    environ={},
                    home=directory,
                    executable="/test/python",
                ),
            )
            snapshot = manager.check_all()
        self.assertEqual(snapshot.autostart.state, AutostartState.MISSING)
        self.assertEqual(snapshot.gnome.state, IntegrationState.NOT_APPLICABLE)
        self.assertEqual(snapshot.udev.state, IntegrationState.NOT_APPLICABLE)
        self.assertFalse(snapshot.gnome.can_install)
        self.assertFalse(snapshot.udev.can_install)

    @unittest.skipIf(sys.platform == "win32", "Start-at-login uses POSIX desktop APIs")
    def test_start_at_login_operations_refresh_the_combined_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            autostart = AutostartManager(
                platform="linux",
                environ={},
                home=directory,
                executable="/test/python",
            )
            manager = HostIntegrationManager(
                platform="unsupported",
                environ={},
                autostart_manager=autostart,
            )
            self.assertEqual(
                manager.check_all().autostart.state, AutostartState.MISSING
            )
            installed = manager.install_autostart()
            self.assertTrue(installed.autostart.current)
            self.assertEqual(
                installed.gnome.state, IntegrationState.NOT_APPLICABLE
            )
            removed = manager.remove_autostart()
            self.assertEqual(removed.autostart.state, AutostartState.MISSING)


@unittest.skipIf(sys.platform == "win32", "Exercises POSIX desktop or USB APIs")
class GnomeHostIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.enabled = False
        self.known = False
        self.calls = []

    def tearDown(self):
        self.directory.cleanup()

    def runner(self, argv, timeout):
        self.calls.append((tuple(argv), timeout))
        if argv[1:] == ("list",):
            return CommandResult(0, f"{GNOME_EXTENSION_UUID}\n" if self.known else "")
        if argv[1:] == ("list", "--enabled"):
            output = f"{GNOME_EXTENSION_UUID}\n" if self.enabled else ""
            return CommandResult(0, output)
        if argv[1:] == ("enable", GNOME_EXTENSION_UUID):
            self.enabled = True
            return CommandResult(0)
        raise AssertionError(f"Unexpected command: {argv!r}")

    def manager(self):
        return HostIntegrationManager(
            platform="linux",
            environ=GNOME_ENV,
            data_home=self.directory.name,
            udev_paths=(Path(self.directory.name) / "rules" / UDEV_RULE_NAME,),
            commands={"gnome-extensions": "/test/gnome-extensions"},
            runner=self.runner,
            autostart_manager=AutostartManager(platform="unsupported"),
        )

    def test_install_is_per_user_and_never_enables(self):
        manager = self.manager()
        missing = manager.check_gnome_extension()
        self.assertEqual(missing.state, IntegrationState.MISSING)
        self.assertTrue(missing.can_install)

        result = manager.install_gnome_extension()
        self.assertTrue(result.gnome.current)
        self.assertFalse(result.gnome.known_to_shell)
        self.assertFalse(result.gnome.can_enable)
        self.assertIn("Log out and back in", result.gnome.detail)
        self.assertFalse(
            any(call[0][1:2] == ("enable",) for call in self.calls),
            "installation must never enable the extension",
        )
        self.assertTrue(
            Path(result.gnome.destination, "extension.js").is_file()
        )

    def test_enable_is_separate_fixed_argv_and_verified(self):
        manager = self.manager()
        manager.install_gnome_extension()
        self.known = True
        ready = manager.check_gnome_extension()
        self.assertTrue(ready.can_enable)

        result = manager.enable_gnome_extension()
        self.assertTrue(result.gnome.enabled)
        self.assertIn(
            (("/test/gnome-extensions", "enable", GNOME_EXTENSION_UUID), 10.0),
            self.calls,
        )

    def test_enable_requires_shell_to_reload_installed_extension(self):
        manager = self.manager()
        manager.install_gnome_extension()
        with self.assertRaisesRegex(HostIntegrationError, "Log out and back in"):
            manager.enable_gnome_extension()

    def test_symlink_extension_directory_is_reported_and_not_installable(self):
        manager = self.manager()
        destination = Path(self.directory.name) / "gnome-shell" / "extensions" / GNOME_EXTENSION_UUID
        destination.parent.mkdir(parents=True)
        outside = Path(self.directory.name) / "outside"
        outside.mkdir()
        try:
            destination.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"symlinks unavailable: {error}")
        status = manager.check_gnome_extension()
        self.assertEqual(status.state, IntegrationState.ERROR)
        self.assertFalse(status.can_install)

    def test_companion_is_not_offered_outside_gnome_wayland(self):
        manager = HostIntegrationManager(
            platform="linux",
            environ={"XDG_SESSION_TYPE": "x11", "XDG_CURRENT_DESKTOP": "GNOME"},
            data_home=self.directory.name,
        )
        self.assertEqual(
            manager.check_gnome_extension().state,
            IntegrationState.NOT_APPLICABLE,
        )

    def test_wayland_display_and_session_desktop_are_recognized(self):
        manager = HostIntegrationManager(
            platform="linux",
            environ={
                "WAYLAND_DISPLAY": "wayland-0",
                "XDG_SESSION_DESKTOP": "gnome-classic",
            },
            data_home=self.directory.name,
            commands={"gnome-extensions": None},
        )
        self.assertEqual(
            manager.check_gnome_extension().state,
            IntegrationState.MISSING,
        )


@unittest.skipIf(sys.platform == "win32", "Exercises POSIX desktop or USB APIs")
class UdevHostIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.target = self.root / "etc" / "udev" / "rules.d" / UDEV_RULE_NAME
        self.calls = []
        self.temporary_sources = []

    def tearDown(self):
        self.directory.cleanup()

    def runner(self, argv, timeout):
        self.calls.append((tuple(argv), timeout))
        if argv[1] == "/test/install":
            source = Path(argv[-2])
            self.temporary_sources.append(source)
            self.assertEqual(source.read_bytes(), bundled_udev_rule())
            self.assertEqual(source.stat().st_mode & 0o777, 0o400)
            self.target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, self.target)
            return CommandResult(0)
        if argv[1:] == ("/test/udevadm", "control", "--reload-rules"):
            return CommandResult(0)
        raise AssertionError(f"Unexpected command: {argv!r}")

    def manager(self, **overrides):
        values = {
            "platform": "linux",
            "environ": {},
            "udev_paths": (self.target,),
            "commands": {
                "pkexec": "/test/pkexec",
                "install": "/test/install",
                "udevadm": "/test/udevadm",
            },
            "runner": self.runner,
            "temp_directory": self.root,
            "autostart_manager": AutostartManager(platform="unsupported"),
        }
        values.update(overrides)
        return HostIntegrationManager(**values)

    def test_install_uses_fixed_argv_reloads_and_checks_exact_bytes(self):
        manager = self.manager()
        self.assertEqual(manager.check_udev_rule().state, IntegrationState.MISSING)

        result = manager.install_udev_rule()
        self.assertTrue(result.udev.current)
        self.assertEqual(self.target.read_bytes(), bundled_udev_rule())
        install_call, reload_call = self.calls
        self.assertEqual(
            install_call[0][0:10],
            (
                "/test/pkexec",
                "/test/install",
                "-D",
                "-o",
                "root",
                "-g",
                "root",
                "-m",
                "0644",
                "--",
            ),
        )
        self.assertEqual(install_call[0][-1], str(self.target))
        self.assertEqual(
            reload_call,
            (("/test/pkexec", "/test/udevadm", "control", "--reload-rules"), 120.0),
        )
        self.assertTrue(all(not path.exists() for path in self.temporary_sources))

    def test_different_rule_is_reported_as_needing_update(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text("different\n", encoding="ascii")
        status = self.manager().check_udev_rule()
        self.assertEqual(status.state, IntegrationState.NEEDS_UPDATE)
        self.assertFalse(status.current)
        self.assertTrue(status.can_install)

    def test_missing_command_preflight_does_not_write_or_run_anything(self):
        manager = self.manager(
            commands={
                "pkexec": "/test/pkexec",
                "install": "/test/install",
                "udevadm": None,
            }
        )
        with self.assertRaisesRegex(HostIntegrationError, "udevadm"):
            manager.install_udev_rule()
        self.assertFalse(self.target.exists())
        self.assertEqual(self.calls, [])

    def test_authorization_failure_is_reported_and_temp_file_removed(self):
        def denied(argv, timeout):
            self.calls.append((tuple(argv), timeout))
            self.temporary_sources.append(Path(argv[-2]))
            return CommandResult(126, stderr="Not authorized")

        manager = self.manager(runner=denied)
        with self.assertRaisesRegex(HostIntegrationError, "exit 126"):
            manager.install_udev_rule()
        self.assertFalse(self.target.exists())
        self.assertTrue(all(not path.exists() for path in self.temporary_sources))

    def test_symbolic_link_rule_is_never_replaced(self):
        self.target.parent.mkdir(parents=True)
        outside = self.root / "outside"
        outside.write_text("keep\n", encoding="ascii")
        try:
            self.target.symlink_to(outside)
        except OSError as error:
            self.skipTest(f"symlinks unavailable: {error}")
        status = self.manager().check_udev_rule()
        self.assertEqual(status.state, IntegrationState.ERROR)
        with self.assertRaisesRegex(HostIntegrationError, "symbolic-link"):
            self.manager().install_udev_rule()
        self.assertEqual(outside.read_text(encoding="ascii"), "keep\n")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
