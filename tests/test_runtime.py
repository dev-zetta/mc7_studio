"""Source and frozen runtime behavior tests without hardware access."""

import os
import sys
import unittest
from unittest.mock import patch

from swarm2.runtime import (
    HELPER_FLAG,
    appimage_main,
    helper_command,
    host_command_environment,
)


class RuntimeTests(unittest.TestCase):
    def test_host_environment_restores_bootloader_library_path_and_overrides(self):
        source = {
            "HOME": "/home/example",
            "LD_LIBRARY_PATH": "/appimage/private",
            "LD_LIBRARY_PATH_ORIG": "/host/libraries",
            "LANG": "de_DE.UTF-8",
        }
        with patch.dict(os.environ, source, clear=True):
            environment = host_command_environment({"LANG": "C"})

        self.assertEqual(environment["LD_LIBRARY_PATH"], "/host/libraries")
        self.assertEqual(environment["LD_LIBRARY_PATH_ORIG"], "/host/libraries")
        self.assertEqual(environment["LANG"], "C")
        self.assertEqual(source["LD_LIBRARY_PATH"], "/appimage/private")

    def test_frozen_host_environment_removes_injected_library_path_without_original(self):
        with patch.dict(
            os.environ,
            {"HOME": "/home/example", "LD_LIBRARY_PATH": "/appimage/private"},
            clear=True,
        ), patch.object(sys, "frozen", True, create=True):
            environment = host_command_environment()

        self.assertNotIn("LD_LIBRARY_PATH", environment)
        self.assertEqual(environment["HOME"], "/home/example")

    def test_frozen_host_environment_restores_values_replaced_inside_appimage(self):
        injected = {
            "PATH": "/appdir/usr/bin:/usr/bin",
            "XDG_DATA_DIRS": "/appdir/usr/share:/usr/share",
            "SSL_CERT_FILE": "/appdir/cacert.pem",
            "QT_PLUGIN_PATH": "/appdir/qt/plugins",
            "QML2_IMPORT_PATH": "/appdir/qt/qml",
            "MC7_STUDIO_HOST_PATH_SET": "1",
            "MC7_STUDIO_HOST_PATH_VALUE": "/usr/local/bin:/usr/bin",
            "MC7_STUDIO_HOST_XDG_DATA_DIRS_SET": "1",
            "MC7_STUDIO_HOST_XDG_DATA_DIRS_VALUE": "/usr/local/share:/usr/share",
            "MC7_STUDIO_HOST_SSL_CERT_FILE_SET": "1",
            "MC7_STUDIO_HOST_SSL_CERT_FILE_VALUE": "/host/cacert.pem",
            "MC7_STUDIO_HOST_QT_PLUGIN_PATH_SET": "0",
            "MC7_STUDIO_HOST_QML2_IMPORT_PATH_SET": "1",
            "MC7_STUDIO_HOST_QML2_IMPORT_PATH_VALUE": "/host/qml",
        }
        with patch.dict(os.environ, injected, clear=True), patch.object(
            sys, "frozen", True, create=True
        ):
            environment = host_command_environment()

        self.assertEqual(environment["PATH"], "/usr/local/bin:/usr/bin")
        self.assertEqual(
            environment["XDG_DATA_DIRS"], "/usr/local/share:/usr/share"
        )
        self.assertEqual(environment["SSL_CERT_FILE"], "/host/cacert.pem")
        self.assertNotIn("QT_PLUGIN_PATH", environment)
        self.assertEqual(environment["QML2_IMPORT_PATH"], "/host/qml")
        self.assertFalse(
            any(name.startswith("MC7_STUDIO_HOST_") for name in environment)
        )

    def test_source_host_environment_preserves_regular_library_path(self):
        with patch.dict(
            os.environ,
            {"LD_LIBRARY_PATH": "/developer/libraries"},
            clear=True,
        ), patch.object(sys, "frozen", False, create=True):
            environment = host_command_environment()

        self.assertEqual(environment["LD_LIBRARY_PATH"], "/developer/libraries")

    def test_source_helpers_use_python_module_invocation(self):
        with patch.object(sys, "frozen", False, create=True):
            self.assertEqual(
                helper_command("swarm2.hardware"),
                [sys.executable, "-m", "swarm2.hardware"],
            )

    def test_frozen_helpers_reenter_the_exact_dispatcher(self):
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "platform", "linux"):
            self.assertEqual(
                helper_command("swarm2.countdown"),
                [sys.executable, HELPER_FLAG, "swarm2.countdown"],
            )

    def test_windows_frozen_helpers_use_the_console_executable(self):
        from pathlib import Path
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "platform", "win32"):
            self.assertEqual(helper_command("swarm2.hardware"), [str(Path(sys.executable).with_name("MC7-Studio-CLI.exe")), HELPER_FLAG, "swarm2.hardware"])

    def test_unknown_helper_is_rejected_before_process_creation(self):
        with self.assertRaisesRegex(ValueError, "Unsupported MC7 helper"):
            helper_command("swarm2.not_a_helper")
        self.assertEqual(appimage_main([HELPER_FLAG, "swarm2.not_a_helper"]), 2)
        self.assertEqual(appimage_main([HELPER_FLAG]), 2)

    def test_arguments_select_cli_and_no_arguments_select_gui(self):
        with patch("swarm2.cli.main", return_value=7) as cli:
            self.assertEqual(appimage_main(["doctor", "--json"]), 7)
        cli.assert_called_once_with(["doctor", "--json"])
        with patch("swarm2.gui.main", return_value=8) as gui:
            self.assertEqual(appimage_main([]), 8)
        gui.assert_called_once_with()

    def test_hardware_helper_uses_explicit_in_process_dispatch(self):
        with patch("swarm2.hardware.main", return_value=9) as helper:
            self.assertEqual(
                appimage_main([HELPER_FLAG, "swarm2.hardware"]), 9
            )
        helper.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
