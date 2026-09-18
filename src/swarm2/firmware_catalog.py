"""Dated, verified public MC7 firmware releases; no vendor client credentials.

This is a release catalog, not an automatic latest-version discovery service.
Metadata and archive contents were checked against the official updater on
the date below. Downloading a package does not install it on a device.
"""

from dataclasses import dataclass


class FirmwareError(ValueError):
    pass


@dataclass(frozen=True)
class FirmwareRelease:
    role: str
    vendor_id: int
    product_id: int
    device_id: int
    package_version: str
    firmware_version: int
    auto_reset_version: int
    size: int
    md5: str
    sha256: str
    resolver_url: str
    cdn_url: str
    archive_stem: str
    catalog_date: str
    equivalent_archive_stems: tuple[str, ...] = ()
    installation_supported: bool = False

    @property
    def filename(self) -> str:
        return f"mc7-{self.role}-{self.package_version}.7z"

    @property
    def key(self) -> str:
        return f"{self.role}:{self.package_version}"


_KNOWN_RELEASES = (
    FirmwareRelease(
        role="mouse", vendor_id=0x10F5, product_id=0x502C, device_id=454,
        package_version="5.9.0.0", firmware_version=509, auto_reset_version=506,
        size=1864401, md5="0e636a3886bfd1d97f89fdc2e5a04184",
        sha256="0f89ba9a62fbcf0484adc939b7ff23d3ad6a224500f54e1205e53a37c2b1cf00",
        resolver_url="https://acpr.prod.turtlebeach.com/swarm2/download/2895/1258/firmware.7z",
        cdn_url="https://cdn.turtlebeach.com/device/driver-firmware/command-series-mc7/command-series-mc7_454-5.9.0.0-8260-v1.7z",
        archive_stem="RTL8772GWP_ImgPacketFile_App_UI", catalog_date="2026-09-16",
        installation_supported=True,
    ),
    FirmwareRelease(
        role="mouse", vendor_id=0x10F5, product_id=0x502C, device_id=454,
        package_version="5.8.0.0", firmware_version=508, auto_reset_version=506,
        size=1847660, md5="a45d41a0a311feac93cc10054bf4eb83",
        sha256="714ea09bc1b1860d8b8e3e12054e02c2f1e4a162b7396e5e41c905264a3984e3",
        resolver_url="https://acpr.prod.turtlebeach.com/swarm2/download/1/1250/firmware.7z",
        cdn_url="https://cdn.turtlebeach.com/device/driver-firmware/command-series-mc7/command-series-mc7_454-5.8.0.0-2572-v1.7z",
        archive_stem="RTL8772GWP_ImgPacketFile_App_UI", catalog_date="2026-09-16",
    ),
    FirmwareRelease(
        role="mouse", vendor_id=0x10F5, product_id=0x502C, device_id=454,
        package_version="5.5.0.0", firmware_version=505, auto_reset_version=505,
        size=1837979, md5="e4d5997040407b380c45612ba9414414",
        sha256="b1f58b3063bb1634f77e51a0b92086c544dd1efcbaa5e1109ee81e00e70f283c",
        resolver_url="https://acpr.prod.turtlebeach.com/swarm2/download/1/1215/firmware.7z",
        cdn_url="https://cdn.turtlebeach.com/device/driver-firmware/command-series-mc7/command-series-mc7_454-5.5.0.0-2645-v1.7z",
        archive_stem="RTL8772GWP_ImgPacketFile_APPUI", catalog_date="2026-09-16",
    ),
    FirmwareRelease(
        role="mouse", vendor_id=0x10F5, product_id=0x502C, device_id=454,
        package_version="5.4.0.0", firmware_version=504, auto_reset_version=504,
        size=1829502, md5="9767a1a69f5d4c46936348ea93c0626c",
        sha256="494a047a8414deef763e47f79ed0fb91a0359e91ecf690045d70be39d7749910",
        resolver_url="https://acpr.prod.turtlebeach.com/swarm2/download/1/1198/firmware.7z",
        cdn_url="https://cdn.turtlebeach.com/device/driver-firmware/command-series-mc7/command-series-mc7_454-5.4.0.0-0974-v1.7z",
        archive_stem="RTL8772GWP_ImgPacketFile_App_0702", catalog_date="2026-09-16",
        equivalent_archive_stems=("RTL8772GWP_ImgPacketFile_App_UI",),
    ),
    FirmwareRelease(
        role="transmitter", vendor_id=0x10F5, product_id=0x502E, device_id=454,
        package_version="5.4.0.0", firmware_version=504, auto_reset_version=504,
        size=88694, md5="5e7b4167b1b54dcb26bcdc39ecc0fe8d",
        sha256="3434bb800455bed281cd989132c02868d35f4f2ba36f296b3288037c22a2557f",
        resolver_url="https://acpr.prod.turtlebeach.com/swarm2/download/2896/1259/firmware.7z",
        cdn_url="https://cdn.turtlebeach.com/device/driver-firmware/command-series-mc7/command-series-mc7_454-5.4.0.0-4561-v1.7z",
        archive_stem="RTL8772GWP_ImgPacketFile_Dongle", catalog_date="2026-09-16",
    ),
    FirmwareRelease(
        role="transmitter", vendor_id=0x10F5, product_id=0x502E, device_id=454,
        package_version="5.3.0.0", firmware_version=503, auto_reset_version=503,
        size=88672, md5="a778cb7b08f9330e9c79125a3b9572c0",
        sha256="735859317c75f149beb1dfb17431b28bd5229e44a673eceaec8a46c3b71599ef",
        resolver_url="https://acpr.prod.turtlebeach.com/swarm2/download/1/1216/firmware.7z",
        cdn_url="https://cdn.turtlebeach.com/device/driver-firmware/command-series-mc7/command-series-mc7_454-5.3.0.0-7735-v1.7z",
        archive_stem="RTL8772GWP_ImgPacketFile_Dongle", catalog_date="2026-09-16",
    ),
    FirmwareRelease(
        role="transmitter", vendor_id=0x10F5, product_id=0x502E, device_id=454,
        package_version="5.2.0.0", firmware_version=502, auto_reset_version=502,
        size=88712, md5="cea2b55e1cfac409e084ead9b8b1a0da",
        sha256="fc6f79d9d98ba5ae13568a44a6359fd42bc0ddebde59bfb011bcdf39a8351ea8",
        resolver_url="https://acpr.prod.turtlebeach.com/swarm2/download/1/1199/firmware.7z",
        cdn_url="https://cdn.turtlebeach.com/device/driver-firmware/command-series-mc7/command-series-mc7_454-5.2.0.0-2290-v1.7z",
        archive_stem="RTL8772GWP_ImgPacketFile_Dongle_0702", catalog_date="2026-09-16",
        equivalent_archive_stems=("RTL8772GWP_ImgPacketFile_Dongle",),
    ),
)


def known_releases() -> tuple[FirmwareRelease, ...]:
    """Return packages verified on their catalog_date; newer releases may exist."""
    return _KNOWN_RELEASES


def release_by_key(key: str) -> FirmwareRelease | None:
    """Resolve one exact role/version identity without accepting caller URLs."""
    if not isinstance(key, str):
        return None
    return next((release for release in _KNOWN_RELEASES if release.key == key), None)


def current_release(role: str) -> FirmwareRelease | None:
    """Return the highest verified version for a role in this dated catalog."""
    matches = [release for release in _KNOWN_RELEASES if release.role == role]
    return max(matches, key=lambda release: release.firmware_version, default=None)


def validate_release(release: FirmwareRelease) -> FirmwareRelease:
    """Reject caller-supplied URLs, hashes, identities, and unverified releases."""
    if type(release) is not FirmwareRelease or release not in _KNOWN_RELEASES:
        raise FirmwareError("Choose a firmware package from the verified MC7 catalog")
    return release
