import io
import json
from pathlib import Path
import tempfile
import sys
import unittest

from swarm2.gnome_extension import (
    GNOME_DBUS_INTERFACE,
    GNOME_DBUS_NAME,
    GNOME_DBUS_OBJECT_PATH,
    GNOME_EXTENSION_UUID,
    GNOME_SHELL_VERSIONS,
    GnomeExtensionInstallError,
    bundled_extension_assets,
    extension_destination,
    install_extension,
    main,
)


class GnomeExtensionAssetTests(unittest.TestCase):
    def test_assets_expose_only_the_focused_window_pid(self):
        assets = bundled_extension_assets()
        self.assertEqual(set(assets), {"extension.js", "metadata.json"})
        metadata = json.loads(assets["metadata.json"])
        self.assertEqual(metadata["uuid"], GNOME_EXTENSION_UUID)
        self.assertEqual(tuple(metadata["shell-version"]), GNOME_SHELL_VERSIONS)

        javascript = assets["extension.js"].decode("utf-8")
        self.assertIn("GetFocusedWindowPid", javascript)
        self.assertIn("global.display.focus_window", javascript)
        self.assertIn("window.get_pid()", javascript)
        for identity in (GNOME_DBUS_NAME, GNOME_DBUS_OBJECT_PATH, GNOME_DBUS_INTERFACE):
            self.assertIn(identity, javascript)
        for forbidden in (
            "get_title",
            "get_window_actors",
            "get_running",
            "GetWindows",
            "GetRunningApplications",
        ):
            self.assertNotIn(forbidden, javascript)

    @unittest.skipIf(sys.platform == "win32", "Exercises GNOME POSIX paths")
    def test_destination_is_per_user_and_requires_an_absolute_data_home(self):
        target = extension_destination("/tmp/example-data")
        self.assertEqual(
            target,
            Path("/tmp/example-data/gnome-shell/extensions") / GNOME_EXTENSION_UUID,
        )
        with self.assertRaises(GnomeExtensionInstallError):
            extension_destination("relative")


@unittest.skipIf(sys.platform == "win32", "Exercises POSIX desktop or USB APIs")
class GnomeExtensionInstallerTests(unittest.TestCase):
    def test_install_is_atomic_repeatable_and_does_not_enable_the_extension(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = install_extension(directory)
            expected = (
                Path(directory)
                / "gnome-shell"
                / "extensions"
                / GNOME_EXTENSION_UUID
            )
            self.assertEqual(destination, expected)
            self.assertEqual(
                {path.name for path in destination.iterdir()},
                {"extension.js", "metadata.json"},
            )
            self.assertTrue(
                all(
                    path.stat().st_mode & 0o777 == 0o644
                    for path in destination.iterdir()
                )
            )

            unrelated = destination / "keep.txt"
            unrelated.write_text("keep", encoding="ascii")
            (destination / "extension.js").write_text("damaged", encoding="ascii")
            self.assertEqual(install_extension(directory), destination)
            self.assertEqual(unrelated.read_text(encoding="ascii"), "keep")
            self.assertEqual(
                (destination / "extension.js").read_bytes(),
                bundled_extension_assets()["extension.js"],
            )

    def test_install_rejects_a_symbolic_link_extension_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            data_home = Path(directory) / "data"
            target = extension_destination(data_home)
            target.parent.mkdir(parents=True)
            outside = Path(directory) / "outside"
            outside.mkdir()
            try:
                target.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symbolic links unavailable: {exc}")
            with self.assertRaisesRegex(GnomeExtensionInstallError, "symbolic-link"):
                install_extension(data_home)
            self.assertEqual(list(outside.iterdir()), [])

    def test_cli_prints_manual_enable_step_without_running_it(self):
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            errors = io.StringIO()
            result = main(
                ("install", "--data-home", directory),
                stdout=output,
                stderr=errors,
            )
            self.assertEqual(result, 0)
            self.assertEqual(errors.getvalue(), "")
            self.assertIn(f"gnome-extensions enable {GNOME_EXTENSION_UUID}", output.getvalue())
            self.assertTrue(extension_destination(directory).is_dir())


if __name__ == "__main__":
    unittest.main()
