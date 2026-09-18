"""PyInstaller entry point for MC7 Studio AppImages."""

import sys

from swarm2.runtime import appimage_main


if __name__ == "__main__":
    if sys.argv[1:] == ["--swarm2-appimage-self-test"]:
        from appimage_self_test import main as self_test_main

        raise SystemExit(self_test_main())
    raise SystemExit(appimage_main())
