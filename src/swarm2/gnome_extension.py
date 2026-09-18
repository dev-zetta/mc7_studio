"""Install the optional GNOME Wayland foreground-application bridge."""

from __future__ import annotations

import argparse
from importlib import resources
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Sequence, TextIO


GNOME_EXTENSION_UUID = "automatic-profiles@swarm2-mc7.local"
GNOME_DBUS_NAME = "io.github.swarm2mc7.ForegroundApplication"
GNOME_DBUS_OBJECT_PATH = "/io/github/swarm2mc7/ForegroundApplication"
GNOME_DBUS_INTERFACE = GNOME_DBUS_NAME
GNOME_SHELL_VERSIONS = ("45", "46", "47", "48", "49", "50")

_ASSET_NAMES = ("extension.js", "metadata.json")
_MAX_ASSET_BYTES = 64 * 1024


class GnomeExtensionInstallError(RuntimeError):
    """The bundled extension or its user installation path is invalid."""


def extension_destination(data_home: str | Path | None = None) -> Path:
    """Return the deterministic per-user GNOME extension directory."""

    if data_home is None:
        configured = os.environ.get("XDG_DATA_HOME", "").strip()
        base = Path(configured) if configured else Path.home() / ".local" / "share"
    else:
        base = Path(data_home)
    base = base.expanduser()
    if not base.is_absolute():
        raise GnomeExtensionInstallError("The GNOME data directory must be absolute.")
    return base / "gnome-shell" / "extensions" / GNOME_EXTENSION_UUID


def bundled_extension_assets() -> dict[str, bytes]:
    """Read and validate the two wheel-contained extension assets."""

    root = (
        resources.files("swarm2")
        .joinpath("data")
        .joinpath("gnome-shell")
        .joinpath(GNOME_EXTENSION_UUID)
    )
    assets: dict[str, bytes] = {}
    try:
        for name in _ASSET_NAMES:
            value = root.joinpath(name).read_bytes()
            if not value or len(value) > _MAX_ASSET_BYTES or b"\0" in value:
                raise GnomeExtensionInstallError(
                    f"The bundled GNOME extension asset {name} is invalid."
                )
            assets[name] = value
    except (OSError, FileNotFoundError) as exc:
        raise GnomeExtensionInstallError(
            "The GNOME extension assets are missing from this installation."
        ) from exc

    try:
        metadata = json.loads(assets["metadata.json"].decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise GnomeExtensionInstallError(
            "The bundled GNOME extension metadata is invalid."
        ) from exc
    versions = metadata.get("shell-version") if isinstance(metadata, dict) else None
    if (
        not isinstance(metadata, dict)
        or metadata.get("uuid") != GNOME_EXTENSION_UUID
        or not isinstance(versions, list)
        or any(not isinstance(version, str) for version in versions)
        or tuple(versions) != GNOME_SHELL_VERSIONS
    ):
        raise GnomeExtensionInstallError(
            "The bundled GNOME extension metadata does not match this application."
        )
    try:
        javascript = assets["extension.js"].decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise GnomeExtensionInstallError(
            "The bundled GNOME extension JavaScript is invalid."
        ) from exc
    for identity in (GNOME_DBUS_NAME, GNOME_DBUS_OBJECT_PATH, GNOME_DBUS_INTERFACE):
        if identity not in javascript:
            raise GnomeExtensionInstallError(
                "The bundled GNOME extension D-Bus identity is inconsistent."
            )
    return assets


def install_extension(data_home: str | Path | None = None) -> Path:
    """Atomically install assets for the current user without enabling them."""

    assets = bundled_extension_assets()
    destination = extension_destination(data_home)
    try:
        if destination.is_symlink():
            raise GnomeExtensionInstallError(
                "Refusing to install through a symbolic-link extension directory."
            )
        destination.mkdir(parents=True, mode=0o755, exist_ok=True)
        if not destination.is_dir():
            raise GnomeExtensionInstallError(
                "The GNOME extension destination is not a directory."
            )
        for name, value in assets.items():
            _replace_file(destination / name, value)
        directory_fd = os.open(destination, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except GnomeExtensionInstallError:
        raise
    except OSError as exc:
        raise GnomeExtensionInstallError(
            f"Could not install the GNOME extension in {destination}."
        ) from exc
    return destination


def _replace_file(destination: Path, value: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="swarm2-gnome-extension",
        description="Install the optional MC7 Studio GNOME Wayland companion.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser(
        "install", help="install or update the extension for the current user"
    )
    install.add_argument(
        "--data-home",
        metavar="PATH",
        help="override XDG_DATA_HOME (primarily for packaging and tests)",
    )
    commands.add_parser("path", help="print the per-user extension directory")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run the installed companion-extension command."""

    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "path":
            print(extension_destination(), file=output)
            return 0
        destination = install_extension(arguments.data_home)
    except GnomeExtensionInstallError as exc:
        print(f"error: {exc}", file=errors)
        return 1
    print(f"Installed {GNOME_EXTENSION_UUID} in {destination}", file=output)
    print("Log out and back in, then enable it with:", file=output)
    print(f"  gnome-extensions enable {GNOME_EXTENSION_UUID}", file=output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
