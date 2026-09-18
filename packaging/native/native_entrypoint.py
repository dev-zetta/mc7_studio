"""Native bundle GUI, CLI and isolated hardware-helper dispatcher."""

import os
import sys

import certifi

from swarm2.runtime import appimage_main


def main():
    # Finder and portable installations do not provide Python's CA locations.
    # Preserve the host setting for programs started by launch tiles.
    if "SSL_CERT_FILE" not in os.environ:
        os.environ["MC7_STUDIO_HOST_SSL_CERT_FILE_SET"] = "0"
        os.environ["SSL_CERT_FILE"] = certifi.where()
    if sys.argv[1:] == ["--swarm2-native-self-test"]:
        from native_self_test import main as self_test
        return self_test()
    return appimage_main()


if __name__ == "__main__":
    raise SystemExit(main())
