# Firmware updates

MC7 Studio can download and inspect known official MC7 firmware packages. Its native updater has been validated for the current mouse upgrade on Linux. Treat firmware installation as a maintenance operation: use a direct USB connection, keep the computer powered, and read this page before starting.

## Supported update scope

- **Installation:** the current official MC7 mouse upgrade only.
- **Validated path:** a directly connected Linux MC7 upgraded from firmware 5.04 to 5.09.
- **Download and inspection:** known current and historical mouse and transmitter packages.
- **Disabled:** same-version reinstall, firmware downgrade, transmitter flashing, wireless updating and standalone recovery.
- **macOS:** package download and inspection are available, but firmware installation has not been validated on MC7 hardware.
- **Windows:** package download and inspection are available; firmware installation is disabled until a native Windows update path is implemented and validated.

The catalog bundled with an MC7 Studio release is a fixed list of verified packages. It is not a live check for firmware published later. The firmware dialog identifies which package it considers current.

## Before updating

1. Use a reliable cable connected directly to the computer. Avoid a hub and do not change USB ports during the operation.
2. Charge the mouse to at least 30 percent.
3. On Linux, open **Device → Check and install host integration…** and make sure the bundled udev rule is current. Reconnect the mouse after installing it.
4. If you use the Linux x86_64 AppImage, its bundled `7zz` and `libusb` provide the archive and USB libraries used by the updater. For a wheel or source installation, install `7zz` or `7z`; on Linux, also install the system `libusb-1.0` library.
5. Save or export any local presets you want to keep.
6. Keep the original PNG/JPEG files used for the LCD background and custom application icons.
7. Close other software that may access the MC7 and stop live LCD services in MC7 Studio.

Use stable mains power for the computer. Once firmware transfer begins, MC7 Studio does not offer cancellation. Do not disconnect the mouse, suspend, restart or power off the computer until the dialog reports a verified result.

## Download and inspect a package

Open **Device → Firmware…**. The dialog reads the connected mouse version and lists the known packages.

Official package downloads use HTTPS. The AppImage uses its bundled certificate trust store; wheel and source installations use the trust configuration available to their Python environment. Do not bypass a certificate error or substitute an unverified package.

- **Download official package…** downloads the selected vendor archive and verifies its expected identity, size and hashes. It does not install it.
- **Open downloaded package…** verifies an archive you already saved and checks its contained firmware image.

Historical mouse packages and transmitter packages can be preserved and inspected, but their installation controls remain disabled. An old official package is not a backup copied from your individual mouse and does not provide a validated downgrade or recovery path.

Replacing the MC7 Studio AppImage updates the application and its bundled tools only. It never updates the mouse firmware; firmware transfer starts only after the explicit preparation and confirmation steps below.

## Prepare and install the current upgrade

1. Select the current mouse package and download it or open an existing copy.
2. Choose **Prepare mouse update…**.
3. Select a durable folder for the settings backup.
4. Wait while MC7 Studio verifies the device and package and reads all five profiles. Preparation does not start firmware transfer.
5. Review the source and target versions, backup location, settings-reset notice and any warning shown by the dialog.
6. Choose **Update mouse firmware** only when you are ready to keep the mouse and computer connected for the complete operation.

Immediately before transfer, MC7 Studio checks the prepared archive, backup, mouse identity, firmware version and settings again. If any of them changed, prepare a new update. During installation it transfers the package, waits for the same mouse to restart, verifies the reported firmware and performs the settings reset required by that upgrade.

The updater reports success only after reconnecting and verifying the target version. After completion, refresh devices and read the mouse again.

## What the settings backup contains

Preparation saves a JSON backup containing:

- all five readable settings profiles;
- supported button assignments; and
- readable macro data that is currently assigned to buttons or supported LCD Macro tiles.

This file helps restore supported settings after an update. **It is not a full firmware backup, a flash image or a guaranteed recovery image.** MC7 Studio has no software-only method to read the installed firmware image from the mouse.

The backup cannot reconstruct:

- custom LCD background pixels;
- custom Open Application icon pixels;
- custom lift-off calibration data;
- host-only timer definitions, launch targets, media/GPU choices or automatic profile rules; or
- unknown and unsupported device data as writable settings.

Keep local presets and original image files separately. They cover information that the firmware-preparation backup cannot recover.

The backup is bound to the mouse and connection used during preparation. Use the same mouse and, on Linux, the same USB port when restoring it. Use exported local presets when you need to move supported settings between computers.

## Settings reset and restoration

Some upgrades reset the mouse's settings. The Firmware dialog tells you during preparation whether the selected upgrade crosses such a reset point. The 5.04-to-5.09 upgrade resets settings.

Settings are never restored automatically. After the update:

1. Open **Firmware… → Restore settings backup…** or **Device → Restore backup…**.
2. Select the JSON backup created during preparation.
3. Choose **Prepare restore…** and select a folder for a new backup of the mouse's current state.
4. Review the supported changes and skipped fields.
5. Choose **Restore supported settings**.
6. Read the mouse again after the operation finishes.

Restoration applies only supported settings and assigned macros. It preserves the currently active profile, current lift-off calibration, current background and unsupported or opaque fields. Restoration consists of several checked writes; it is not atomic. If one step fails, completed earlier steps are not automatically rolled back or retried.

## If an update fails or is interrupted

Do not immediately retry. Leave the mouse connected and record the exact message and last progress phase shown by MC7 Studio. Then:

1. Wait briefly for the mouse to restart and choose **Refresh devices**.
2. Use **Read firmware and battery** to check what version the mouse reports.
3. If normal configuration access is available, choose **Read mouse** before changing any setting.
4. Keep the package and every backup created by preparation or restoration.

MC7 Studio does not provide an interrupted-update recovery procedure and does not guarantee that an older official package can recover the mouse. Do not use historical packages as an experimental downgrade. If the mouse no longer appears as a normal MC7, stop and seek device-specific recovery assistance.

Return to the [MC7 Studio user guide](user-guide.md) for ordinary configuration.

The Windows and Apple Silicon portable bundles include 7-Zip for firmware package inspection. Windows firmware installation remains unavailable; macOS hardware acceptance is still pending.
