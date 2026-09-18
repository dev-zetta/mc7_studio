"""Offline tests for explicit per-user start-at-login integration."""

from pathlib import Path
import errno
import os
import plistlib
import sys
import tempfile
import unittest
from unittest.mock import patch

from swarm2.autostart import (
    AutostartError,
    AutostartManager,
    AutostartState,
    LINUX_AUTOSTART_NAME,
    LINUX_MANAGED_KEY,
    MACOS_LAUNCH_AGENT_LABEL,
    MACOS_LAUNCH_AGENT_NAME,
    _read_target,
)


@unittest.skipIf(sys.platform == "win32", "Exercises POSIX desktop or USB APIs")
class LinuxAutostartTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.home = self.root / "home"
        self.executable = "/opt/MC7 Studio/bin/python3"

    def tearDown(self):
        self.directory.cleanup()

    def manager(self, **overrides):
        values = {
            "platform": "linux",
            "environ": {},
            "home": self.home,
            "executable": self.executable,
        }
        values.update(overrides)
        return AutostartManager(**values)

    def test_check_has_no_side_effect_and_install_is_complete_and_repeatable(self):
        manager = self.manager()
        expected_path = self.home / ".config" / "autostart" / LINUX_AUTOSTART_NAME

        missing = manager.check()
        self.assertEqual(missing.state, AutostartState.MISSING)
        self.assertEqual(missing.path, str(expected_path))
        self.assertFalse(expected_path.parent.exists())

        current = manager.install()
        self.assertEqual(current.state, AutostartState.CURRENT)
        self.assertEqual(expected_path.read_bytes(), manager.expected_bytes())
        self.assertEqual(expected_path.stat().st_mode & 0o777, 0o644)
        self.assertEqual(manager.install(), current)
        self.assertEqual(list(expected_path.parent.glob(".*.tmp")), [])

        removed = manager.remove()
        self.assertEqual(removed.state, AutostartState.MISSING)
        self.assertFalse(expected_path.exists())
        self.assertEqual(manager.remove().state, AutostartState.MISSING)

    def test_desktop_exec_is_tokenized_and_escapes_reserved_values(self):
        manager = self.manager(
            executable="/opt/MC7 Studio/python",
            arguments=('quote"x', "tick`", "$HOME", r"c:\mc7", "100%"),
        )
        line = next(
            value
            for value in manager.expected_bytes().decode("utf-8").splitlines()
            if value.startswith("Exec=")
        )
        self.assertEqual(
            line,
            r'Exec="/opt/MC7 Studio/python" "quote\\"x" "tick\\`" "\\$HOME" "c:\\\\mc7" "100%%"',
        )
        self.assertIn(LINUX_MANAGED_KEY.encode("ascii"), manager.expected_bytes())

    def test_appimage_autostart_uses_outer_image_path(self):
        manager = AutostartManager(
            platform="linux",
            environ={"APPIMAGE": "/home/test/Applications/MC7 Studio.AppImage"},
            home=self.home,
        )
        self.assertEqual(
            manager.program_arguments(),
            ("/home/test/Applications/MC7 Studio.AppImage",),
        )
        self.assertIn(
            b'Exec="/home/test/Applications/MC7 Studio.AppImage"\n',
            manager.expected_bytes(),
        )

    def test_relative_appimage_and_explicit_command_do_not_override_defaults(self):
        relative = AutostartManager(
            platform="linux", environ={"APPIMAGE": "MC7.AppImage"}, home=self.home
        )
        self.assertEqual(
            relative.program_arguments(),
            (sys.executable, "-m", "swarm2.gui"),
        )
        explicit = AutostartManager(
            platform="linux",
            environ={"APPIMAGE": "/unused/MC7.AppImage"},
            home=self.home,
            executable="/opt/python",
            arguments=("-m", "custom.gui"),
        )
        self.assertEqual(
            explicit.program_arguments(), ("/opt/python", "-m", "custom.gui")
        )

    def test_absolute_xdg_config_home_is_used_and_relative_value_is_ignored(self):
        xdg = self.root / "xdg"
        manager = self.manager(environ={"XDG_CONFIG_HOME": str(xdg)})
        self.assertEqual(
            manager.path(), xdg / "autostart" / LINUX_AUTOSTART_NAME
        )

        relative = self.manager(environ={"XDG_CONFIG_HOME": "relative/config"})
        self.assertEqual(
            relative.path(),
            self.home / ".config" / "autostart" / LINUX_AUTOSTART_NAME,
        )

    def test_stale_managed_entry_can_be_updated_or_removed(self):
        manager = self.manager()
        path = manager.path()
        path.parent.mkdir(parents=True)
        stale = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Old MC7 Studio\n"
            "Exec=\"/old/python\"\n"
            f"{LINUX_MANAGED_KEY}\n"
        ).encode("utf-8")
        path.write_bytes(stale)

        status = manager.check()
        self.assertEqual(status.state, AutostartState.NEEDS_UPDATE)
        self.assertTrue(status.can_install)
        self.assertTrue(status.can_remove)
        self.assertTrue(manager.install().current)

        path.write_bytes(stale)
        self.assertEqual(manager.remove().state, AutostartState.MISSING)

    def test_unrecognized_regular_file_is_preserved(self):
        manager = self.manager()
        path = manager.path()
        path.parent.mkdir(parents=True)
        original = b"[Desktop Entry]\nName=Someone else's entry\n"
        path.write_bytes(original)

        status = manager.check()
        self.assertEqual(status.state, AutostartState.ERROR)
        self.assertFalse(status.can_install)
        self.assertFalse(status.can_remove)
        with self.assertRaisesRegex(AutostartError, "unrecognized"):
            manager.install()
        with self.assertRaisesRegex(AutostartError, "unrecognized"):
            manager.remove()
        self.assertEqual(path.read_bytes(), original)

    def test_symbolic_link_is_never_followed_replaced_or_removed(self):
        manager = self.manager()
        path = manager.path()
        path.parent.mkdir(parents=True)
        outside = self.root / "outside.desktop"
        outside.write_text("keep\n", encoding="utf-8")
        try:
            path.symlink_to(outside)
        except OSError as error:
            self.skipTest(f"symlinks unavailable: {error}")

        self.assertEqual(manager.check().state, AutostartState.ERROR)
        with self.assertRaisesRegex(AutostartError, "link or non-regular"):
            manager.install()
        with self.assertRaisesRegex(AutostartError, "link or non-regular"):
            manager.remove()
        self.assertTrue(path.is_symlink())
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep\n")

    def test_invalid_invocation_is_rejected_before_writing(self):
        relative = self.manager(executable="bin/python3")
        self.assertEqual(relative.check().state, AutostartState.ERROR)
        with self.assertRaisesRegex(AutostartError, "absolute"):
            relative.install()

        control = self.manager(arguments=("bad\nargument",))
        self.assertEqual(control.check().state, AutostartState.ERROR)
        with self.assertRaisesRegex(AutostartError, "control characters"):
            control.install()

        surrogate = self.manager(executable="/tmp/\udcff")
        self.assertEqual(surrogate.check().state, AutostartState.ERROR)
        with self.assertRaisesRegex(AutostartError, "valid UTF-8"):
            surrogate.install()

    def test_update_restores_a_concurrently_substituted_regular_file(self):
        manager = self.manager()
        path = manager.path()
        path.parent.mkdir(parents=True)
        path.write_bytes(
            (
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Exec=\"/old/python\"\n"
                f"{LINUX_MANAGED_KEY}\n"
            ).encode("utf-8")
        )
        racer = self.root / "concurrent.desktop"
        unrelated = b"[Desktop Entry]\nName=Concurrent user entry\n"
        racer.write_bytes(unrelated)
        real_rename = os.rename

        def substitute_then_rename(source, destination):
            os.replace(racer, path)
            return real_rename(source, destination)

        with patch(
            "swarm2.autostart.os.rename", side_effect=substitute_then_rename
        ):
            with self.assertRaisesRegex(AutostartError, "changed before"):
                manager.install()
        self.assertEqual(path.read_bytes(), unrelated)
        self.assertEqual(list(path.parent.glob(".*.recovery-*")), [])

    def test_update_restores_the_previous_entry_when_publication_fails(self):
        manager = self.manager()
        path = manager.path()
        path.parent.mkdir(parents=True)
        stale = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Exec=\"/old/python\"\n"
            f"{LINUX_MANAGED_KEY}\n"
        ).encode("utf-8")
        path.write_bytes(stale)
        real_link = os.link
        failed = False

        def fail_first_link(source, destination, *args, **kwargs):
            nonlocal failed
            if not failed and Path(destination) == path:
                failed = True
                raise OSError(errno.ENOSPC, "injected publication failure")
            return real_link(source, destination, *args, **kwargs)

        with patch("swarm2.autostart.os.link", side_effect=fail_first_link):
            with self.assertRaisesRegex(AutostartError, "entry was restored"):
                manager.install()
        self.assertEqual(path.read_bytes(), stale)
        self.assertEqual(list(path.parent.glob(".*.recovery-*")), [])

    def test_temp_cleanup_failure_does_not_hide_the_recovery_path(self):
        manager = self.manager()
        path = manager.path()
        path.parent.mkdir(parents=True)
        path.write_bytes(
            (
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Exec=\"/old/python\"\n"
                f"{LINUX_MANAGED_KEY}\n"
            ).encode("utf-8")
        )
        real_unlink = Path.unlink

        def reject_links(*args, **kwargs):
            raise OSError(errno.ENOSPC, "injected publication failure")

        def reject_temp_unlink(target, *args, **kwargs):
            if target.name.endswith(".tmp"):
                raise OSError(errno.EACCES, "injected cleanup failure")
            return real_unlink(target, *args, **kwargs)

        with patch("swarm2.autostart.os.link", side_effect=reject_links), patch(
            "swarm2.autostart.Path.unlink", new=reject_temp_unlink
        ):
            with self.assertRaisesRegex(AutostartError, r"preserved at .*recovery-"):
                manager.install()
        self.assertFalse(path.exists())
        self.assertEqual(len(list(path.parent.glob(".*.recovery-*"))), 1)

    def test_remove_restores_a_concurrently_substituted_regular_file(self):
        manager = self.manager()
        manager.install()
        path = manager.path()
        racer = self.root / "concurrent.desktop"
        unrelated = b"[Desktop Entry]\nName=Concurrent user entry\n"
        racer.write_bytes(unrelated)
        real_rename = os.rename

        def substitute_then_rename(source, destination):
            os.replace(racer, path)
            return real_rename(source, destination)

        with patch(
            "swarm2.autostart.os.rename", side_effect=substitute_then_rename
        ):
            with self.assertRaisesRegex(AutostartError, "changed before"):
                manager.remove()
        self.assertEqual(path.read_bytes(), unrelated)
        self.assertEqual(list(path.parent.glob(".*.recovery-*")), [])

    def test_remove_restores_entry_when_quarantined_read_fails(self):
        manager = self.manager()
        manager.install()
        path = manager.path()
        original = path.read_bytes()

        def fail_quarantined_read(target):
            if ".recovery-" in Path(target).parent.name:
                raise OSError(errno.EIO, "injected read failure")
            return _read_target(target)

        with patch(
            "swarm2.autostart._read_target", side_effect=fail_quarantined_read
        ):
            with self.assertRaisesRegex(AutostartError, "entry was restored"):
                manager.remove()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob(".*.recovery-*")), [])

    def test_remove_restores_entry_when_quarantined_unlink_fails(self):
        manager = self.manager()
        manager.install()
        path = manager.path()
        original = path.read_bytes()
        real_unlink = Path.unlink
        failed = False

        def fail_first_quarantined_unlink(target, *args, **kwargs):
            nonlocal failed
            if not failed and ".recovery-" in target.parent.name:
                failed = True
                raise OSError(errno.EACCES, "injected unlink failure")
            return real_unlink(target, *args, **kwargs)

        with patch(
            "swarm2.autostart.Path.unlink",
            new=fail_first_quarantined_unlink,
        ):
            with self.assertRaisesRegex(AutostartError, "entry was restored"):
                manager.remove()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob(".*.recovery-*")), [])


@unittest.skipIf(sys.platform == "win32", "Exercises POSIX desktop or USB APIs")
class MacOSAutostartTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name) / "home"

    def tearDown(self):
        self.directory.cleanup()

    def manager(self, **overrides):
        values = {
            "platform": "darwin",
            "environ": {},
            "home": self.home,
            "executable": "/Applications/MC7 Studio/python3",
            "arguments": ("-m", "swarm2.gui", "value with spaces", "100%"),
        }
        values.update(overrides)
        return AutostartManager(**values)

    def test_launch_agent_uses_tokenized_arguments_and_run_at_load(self):
        manager = self.manager()
        expected_path = (
            self.home / "Library" / "LaunchAgents" / MACOS_LAUNCH_AGENT_NAME
        )
        self.assertEqual(manager.path(), expected_path)
        self.assertEqual(manager.check().state, AutostartState.MISSING)

        status = manager.install()
        self.assertEqual(status.state, AutostartState.CURRENT)
        document = plistlib.loads(expected_path.read_bytes())
        self.assertEqual(document["Label"], MACOS_LAUNCH_AGENT_LABEL)
        self.assertEqual(
            document["ProgramArguments"],
            [
                "/Applications/MC7 Studio/python3",
                "-m",
                "swarm2.gui",
                "value with spaces",
                "100%",
            ],
        )
        self.assertIs(document["RunAtLoad"], True)
        self.assertEqual(document["ProcessType"], "Interactive")
        self.assertEqual(expected_path.read_bytes(), manager.expected_bytes())

    def test_only_matching_parsed_label_marks_a_stale_agent_as_managed(self):
        manager = self.manager()
        path = manager.path()
        path.parent.mkdir(parents=True)
        stale = plistlib.dumps(
            {
                "Label": MACOS_LAUNCH_AGENT_LABEL,
                "ProgramArguments": ["/old/mc7"],
                "RunAtLoad": True,
            }
        )
        path.write_bytes(stale)
        self.assertEqual(manager.check().state, AutostartState.NEEDS_UPDATE)
        self.assertTrue(manager.install().current)

        unrelated = plistlib.dumps(
            {"Label": "org.example.other", "ProgramArguments": ["/other"]}
        )
        path.write_bytes(unrelated)
        self.assertEqual(manager.check().state, AutostartState.ERROR)
        with self.assertRaisesRegex(AutostartError, "unrecognized"):
            manager.install()
        with self.assertRaisesRegex(AutostartError, "unrecognized"):
            manager.remove()
        self.assertEqual(path.read_bytes(), unrelated)

    def test_stale_matching_agent_can_be_removed(self):
        manager = self.manager()
        path = manager.path()
        path.parent.mkdir(parents=True)
        path.write_bytes(
            plistlib.dumps(
                {"Label": MACOS_LAUNCH_AGENT_LABEL, "ProgramArguments": ["/old"]}
            )
        )
        self.assertEqual(manager.remove().state, AutostartState.MISSING)
        self.assertFalse(path.exists())


class UnsupportedAutostartTests(unittest.TestCase):
    def test_unsupported_platform_is_not_applicable(self):
        manager = AutostartManager(
            platform="win32", home="/home/test", executable="/python"
        )
        status = manager.check()
        self.assertEqual(status.state, AutostartState.NOT_APPLICABLE)
        self.assertFalse(status.can_install)
        self.assertFalse(status.can_remove)
        with self.assertRaisesRegex(AutostartError, "only on Linux and macOS"):
            manager.install()


if __name__ == "__main__":
    unittest.main()
