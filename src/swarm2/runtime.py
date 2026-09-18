"""Runtime entry points shared by source installs and frozen applications."""

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
import sys
from typing import Sequence


HELPER_FLAG = "--swarm2-helper"
HELPER_MODULES = (
    "swarm2.hardware",
    "swarm2.firmware_hardware",
    "swarm2.restore_hardware",
    "swarm2.dcu",
    "swarm2.countdown",
)
_APPIMAGE_HOST_VARIABLES = (
    "PATH",
    "XDG_DATA_DIRS",
    "SSL_CERT_FILE",
    "QT_PLUGIN_PATH",
    "QML2_IMPORT_PATH",
)


def _restore_appimage_host_variables(environment: dict[str, str]) -> None:
    for name in _APPIMAGE_HOST_VARIABLES:
        marker = f"MC7_STUDIO_HOST_{name}_SET"
        saved = f"MC7_STUDIO_HOST_{name}_VALUE"
        state = environment.pop(marker, None)
        value = environment.pop(saved, None)
        if state == "1":
            environment[name] = "" if value is None else value
        elif state == "0":
            environment.pop(name, None)


def host_command_environment(
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an environment suitable for starting host-owned programs.

    PyInstaller prepends its private library directory to ``LD_LIBRARY_PATH``
    and keeps the host value in ``LD_LIBRARY_PATH_ORIG``.  External programs
    must see the host value so that they load their matching system libraries.
    """

    environment = os.environ.copy()
    if getattr(sys, "frozen", False):
        _restore_appimage_host_variables(environment)
    if "LD_LIBRARY_PATH_ORIG" in environment:
        environment["LD_LIBRARY_PATH"] = environment["LD_LIBRARY_PATH_ORIG"]
    elif getattr(sys, "frozen", False):
        environment.pop("LD_LIBRARY_PATH", None)
    if overrides is not None:
        environment.update(overrides)
    return environment


def helper_command(module: str) -> list[str]:
    """Return an isolated helper command for this runtime."""

    if module not in HELPER_MODULES:
        raise ValueError(f"Unsupported MC7 helper: {module}")
    if getattr(sys, "frozen", False):
        executable = sys.executable
        if sys.platform == "win32":
            executable = str(Path(executable).with_name("MC7-Studio-CLI.exe"))
        return [executable, HELPER_FLAG, module]
    return [sys.executable, "-m", module]


def _run_helper(module: str) -> int:
    if module == "swarm2.hardware":
        from .hardware import main
    elif module == "swarm2.firmware_hardware":
        from .firmware_hardware import main
    elif module == "swarm2.restore_hardware":
        from .restore_hardware import main
    elif module == "swarm2.dcu":
        from .dcu import main
    elif module == "swarm2.countdown":
        from .countdown import main
    else:
        print(f"MC7 Studio: unsupported helper {module!r}", file=sys.stderr)
        return 2
    return int(main() or 0)


def appimage_main(argv: Sequence[str] | None = None) -> int:
    """Launch the GUI, CLI, or one exact internal helper from an AppImage."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == [HELPER_FLAG]:
        if len(arguments) != 2:
            print("MC7 Studio: an exact internal helper name is required", file=sys.stderr)
            return 2
        return _run_helper(arguments[1])
    if not arguments:
        from .gui import main

        return main()
    from .cli import main

    return main(arguments)
