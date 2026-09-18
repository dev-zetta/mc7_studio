# Turtle Beach Command Series MC7 firmware archive

This directory preserves a local archival copy of all seven MC7 firmware packages recovered from the public Turtle Beach Swarm II resolver and CDN during the bounded catalog research on 2026-09-16. The archives retain their official CDN filenames and their exact downloaded bytes.

The `.7z` payloads are intentionally ignored by Git and excluded from public release artifacts. The tracked files in this directory preserve their official URLs, sizes and hashes. Turtle Beach's [Swarm II EULA](https://acpr.prod.turtlebeach.com/swarm2/eula.json) permits an archival copy but restricts distribution of the software; obtain written permission before publishing the payloads.

## Contents

| Target | Package versions | USB identity |
| --- | --- | --- |
| Mouse | 5.4.0.0, 5.5.0.0, 5.8.0.0, 5.9.0.0 | `10F5:502C` |
| Transmitter | 5.2.0.0, 5.3.0.0, 5.4.0.0 | `10F5:502E` |

[`manifest.json`](manifest.json) records the package role, version, size, MD5, SHA-256, resolver URL, and CDN URL for every archive.

## Offline verification

Run either check from this directory:

```sh
sha256sum --check SHA256SUMS
python3 verify.py
```

`verify.py` validates the manifest, exact archive set, sizes, MD5 hashes, SHA-256 hashes, and `SHA256SUMS`. It performs no network or USB operations.

## Recovery limits

These files are official update packages, not full-flash backups of a specific mouse or transmitter. They do not preserve resident boot/system regions, OTP/eFuse state, factory calibration, or other device-unique data. Keeping the 5.4.0.0 mouse package provides an original-version reinstall source, but neither same-version reinstall nor downgrade has been validated on hardware.

Archive presence does not authorize an installation. The application enforces its separately documented package and hardware safety gates. Firmware and associated copyrights remain with Turtle Beach and their respective owners.
