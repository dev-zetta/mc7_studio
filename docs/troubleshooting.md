# Troubleshooting

Start with the read-only diagnostic. It reports device discovery and access without opening the mouse or changing its settings:

```sh
~/Applications/MC7-Studio-0.1.0-x86_64.AppImage --version
~/Applications/MC7-Studio-0.1.0-x86_64.AppImage doctor
~/Applications/MC7-Studio-0.1.0-x86_64.AppImage devices
```

For a wheel or source installation, use the installed commands:

```sh
swarm2 --version
swarm2 doctor
swarm2 devices
```

If the commands are not on your shell path, use their full path in the virtual environment, for example `mc7-studio/bin/swarm2` or `.venv/bin/swarm2`. Machine-readable output is available by adding `--json` to the AppImage or installed `doctor` and `devices` commands.

## The AppImage does not start

The release AppImage supports x86_64 Linux systems with glibc 2.35 or newer. It does not run on ARM64, musl-based systems or macOS. Run `ldd --version` if you need to check the glibc version.

Make sure the download is executable and stored on a filesystem that permits programs to run:

```sh
chmod +x MC7-Studio-0.1.0-x86_64.AppImage
./MC7-Studio-0.1.0-x86_64.AppImage
```

If it is on a filesystem mounted with `noexec`, move it to a local directory such as `~/Applications` and try again. If the error mentions FUSE or `libfuse.so.2`, use the AppImage runtime's extraction fallback:

```sh
./MC7-Studio-0.1.0-x86_64.AppImage --appimage-extract-and-run
```

The AppImage includes Python, the GUI dependencies, `7zz`, `libusb` and its HTTPS certificate trust store. A missing one of those components in a current AppImage is a packaging problem; record the terminal output and report it with the AppImage checksum.

## Wheel or source installation fails

Check `python3 --version`; MC7 Studio requires Python 3.10 or newer. If Python cannot create a virtual environment, install the virtual-environment support provided by your operating system. The release wheel contains MC7 Studio, but pip normally downloads its GUI dependencies during installation, so it also needs access to the Python package index.

Make sure the wheel command ends in `[gui]`. Installing the wheel without that extra provides the command-line tools but omits the desktop dependencies.

## The mouse is not listed

1. Connect the mouse itself directly by USB. Connecting only the wireless receiver is not sufficient for configuration.
2. Disconnect and reconnect the cable, then choose **Device → Refresh devices**.
3. Try another data-capable USB cable or port.
4. Close other MC7 Studio instances and any vendor software running through Wine, then refresh again.
5. Run the AppImage `doctor` and `devices` commands, or the installed `swarm2 doctor` and `swarm2 devices` commands, from the same release that launches the GUI.

On macOS, connect exactly one MC7 mouse directly. If the diagnostic reports that HIDAPI is unavailable, reinstall the GUI extra from the release wheel:

```sh
mc7-studio/bin/python -m pip install --force-reinstall '/path/to/swarm2_mc7-0.1.0-py3-none-any.whl[gui]'
```

macOS USB support is experimental and has not yet completed physical hardware acceptance.

On Windows, close Swarm II and any other mouse utility, connect exactly one MC7 directly, and refresh devices. The portable build needs no custom driver. If the wheel diagnostic reports that HIDAPI is unavailable, reinstall the `[gui]` extra in the same virtual environment. Windows USB support is experimental and has not yet completed physical hardware acceptance.

## The mouse is listed but cannot be read or changed on Linux

Open **Device → Check and install host integration…** and inspect **MC7 USB access**. Install or update the rule when offered, approve the administrator request, and reconnect the mouse and receiver. Then choose **Refresh devices** and run the AppImage `doctor` command or the installed `swarm2 doctor` command again.

Do not run the GUI as root. If the diagnostic still reports device nodes that are not writable, confirm that you reconnected the mouse after the rule was installed and that you are running MC7 Studio in the signed-in graphical session.

## The Linux udev installer is unavailable

The in-app installer needs `pkexec`, `install` and `udevadm`. Install the corresponding PolicyKit and udev tools supplied by your Linux distribution, then reopen the host-integration dialog.

From an extracted source archive, you can install the bundled rule manually:

```sh
sudo install -D -o root -g root -m 0644 packaging/udev/70-swarm2-mc7.rules /etc/udev/rules.d/70-swarm2-mc7.rules
sudo udevadm control --reload-rules
```

Disconnect and reconnect the mouse and receiver afterward. The rule must grant access to both hidraw and USB interfaces for all features, including firmware installation.

## Apply is disabled or asks for another read

An apply needs a current baseline for the selected mouse and profile. Choose **Read mouse**, wait for it to finish, then make the change and use the Apply button on that settings page.

The baseline is intentionally cleared after a profile switch, reconnect, firmware operation, automatic-profile switch or an ambiguous device error. Read the mouse again in those cases. If an apply says it could not be verified, inspect the connection and reread before retrying.

Remember that **Save preset** saves only the local draft. It does not apply the draft to the mouse.

## Settings changed on the wrong profile

The profile selected for editing and the profile currently active on the mouse are separate choices. Select the intended slot on **Profiles**, read it, and apply the required page. Use **Activate this profile** only when you also want that slot to become active.

Automatic profiles may switch the active slot while monitoring is enabled. Pause monitoring while making manual changes, or read the selected profile again after an automatic switch.

## The Download Swarm II message remains on the LCD

Read the active profile, open **Display**, choose **Set up LCD**, and then choose **Apply display settings**. Wait for the verification read to finish. Setting up the draft or saving a preset alone does not update the display.

## A host-assisted LCD tile does nothing

Countdown, General Media, application/website/file/folder launch and OBS tiles need the following sequence:

1. Make the intended profile active.
2. Apply its display layout.
3. Read that profile again.
4. Keep MC7 Studio open.
5. Enable **Listen for timer, media and launch taps on the mouse**.

Live CPU, GPU, temperature and RAM tiles use their separate live-update option. If General Media cannot find the intended application, start playback and use **Refresh players** before selecting it.

For application, file and folder tiles, confirm that the saved target still exists. Website targets must use an HTTP or HTTPS URL. See [Desktop integrations](integrations.md) for platform-specific behavior.

## OBS tiles do not work

**Launch OBS** can start a native OBS installation or the official Linux Flatpak. **OBS screenshot** and **OBS Studio mode** require OBS to be running already with **Tools → WebSocket Server Settings → Enable WebSocket server** enabled.

After changing the OBS WebSocket setting, restart the LCD action listener. MC7 Studio reads the current user's OBS configuration and connects only to the local computer. If Studio Mode was changed directly inside OBS and the mouse icon is stale, restart the listener. The next tile press also reads the current OBS state before toggling it.

See [Desktop integrations](integrations.md#obs-studio) for setup details.

## Automatic profiles do not switch

Confirm that the rule is enabled, points to the exact application and selects the intended onboard slot. Automatic switching requires two matching foreground observations before it changes the mouse profile.

GNOME Wayland needs the bundled per-user companion. Open **Device → Check and install host integration…**, install it, log out and back in if requested, then return to the dialog and enable it. X11, Sway and Hyprland use their desktop command-line tools. KDE Plasma and other Wayland compositors are not currently supported for foreground monitoring.

See [Desktop integrations](integrations.md#automatic-application-profiles) for the complete platform list.

## Background or custom-icon upload stopped

Keep the mouse on a stable direct USB connection during an image transfer. A background upload can take up to two minutes and affects every mouse profile. Choosing or previewing an image does not upload it.

The mouse cannot return stored image pixels for verification. If a transfer stops, read the profile again, inspect the physical display, and begin a fresh upload only after the connection is stable.

## Firmware package or update problems

- The AppImage includes `7zz`, `libusb` and the HTTPS certificate trust store. If it reports one of them missing, download and verify the current AppImage again, then report the error if it persists.
- For a wheel or source installation, install `7zz` or `7z` if package inspection cannot start. On Linux, also install the system `libusb-1.0` library.
- On Linux, check and install the current udev rule from **Device → Check and install host integration…**, then reconnect the mouse.
- On Windows, firmware package download and inspection are available, but firmware installation is intentionally disabled.
- Connect the mouse directly and keep the computer powered throughout an update.
- If the package URL or checksum differs from the built-in catalog, stop. Do not bypass package validation.
- If an update cannot be verified, refresh devices and read the installed firmware version before taking another action.

Only the supported current mouse upgrade path is enabled. Historical reinstallation, downgrade, recovery and transmitter installation are not available. A settings backup is not a complete firmware image and cannot restore custom image pixels. Read [Firmware updates and backups](firmware.md) before starting an update.

## The GUI does not start

Launch the AppImage from a terminal so the startup error remains visible:

```sh
~/Applications/MC7-Studio-0.1.0-x86_64.AppImage
```

For a wheel installation, launch its GUI command from a terminal:

```sh
mc7-studio/bin/swarm2-gui
```

For a source installation, use `.venv/bin/swarm2-gui`. If Python reports a missing Qt, HID or system-monitoring module, reinstall the release with the `gui` extra. Also confirm that the terminal belongs to an active graphical desktop session.

For the Windows portable build, run `MC7-Studio-CLI.exe` from PowerShell if the window closes immediately. Keep `MC7-Studio-CLI.exe` and the extracted `_internal` folder beside the executable; moving only the `.exe` makes the application incomplete.

## AppImage update or start at login uses an old file

The AppImage does not update itself silently. Download the new release manually or use a compatible update tool with the published `.zsync` metadata, then run the resulting AppImage.

Start at login records the absolute AppImage path. If you moved, renamed or replaced a version-named file, launch the current AppImage, open **Device → Check and install host integration…**, and choose **Install or update start-at-login entry**. You can keep a stable filename and replace its contents during future updates to keep the path unchanged.

## Collect useful diagnostics

When reporting a reproducible problem, include:

- MC7 Studio version from the AppImage `--version` command or `swarm2 --version`.
- Operating system, desktop environment and whether the session uses X11 or Wayland.
- Mouse firmware version shown on **Overview** or **Device**.
- Output from the AppImage `doctor --json` command or the installed `swarm2 doctor --json` command.
- The exact operation and complete error shown by the app.
- Whether the mouse was connected directly, and whether reconnecting changed the result.

Do not publish firmware backups, exported presets or OBS configuration files. They can contain macros, local file paths or application details.

Return to [Getting started](getting-started.md), read the full [User guide](user-guide.md), or open the [project README](../README.md).

## macOS blocks the downloaded application

The first Apple Silicon release requires macOS 13 or newer and uses ad-hoc signing, without Apple notarization. Check the ZIP against its published `.sha256` file, extract it in Finder, and move the complete **MC7 Studio.app** to **Applications**. If macOS blocks the first launch, use **System Settings → Privacy & Security → Open Anyway** for this application after reviewing the download.

For a startup error, run `/Applications/MC7\ Studio.app/Contents/MacOS/MC7-Studio` in Terminal and include its output in the issue. Do not move the executable out of the app bundle; it depends on the libraries and resources beside it.

The Windows and Apple Silicon portable bundles include 7-Zip for firmware package inspection. Windows firmware installation remains unavailable; macOS hardware acceptance is still pending.
