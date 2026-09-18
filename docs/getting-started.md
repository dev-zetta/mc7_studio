# Getting started

MC7 Studio configures the Turtle Beach Command Series MC7 on Linux, macOS and Windows. Connect the mouse directly by USB for setup. The wireless receiver can be detected, but wireless and Bluetooth configuration are not available yet.

For an overview of the application, see the [project README](../README.md).

## Requirements

- A Turtle Beach Command Series MC7 connected directly by USB.
- For the preferred AppImage: an x86_64 Linux system with glibc 2.35 or newer. The AppImage is built on Ubuntu 22.04 and can run on other glibc-based distributions, including Fedora and Arch-derived systems, when they meet that baseline.
- For the portable Windows application: 64-bit Windows 10 or newer.
- For the Apple Silicon application: an M1 or newer Mac running macOS 13 or newer.
- For a wheel or source installation: Python 3.10 or newer on Linux, macOS or Windows. Core USB configuration has been exercised on Linux; the macOS and Windows HIDAPI backends still need hardware validation.
- A graphical desktop session for the Qt interface.

The Linux x86_64 AppImage includes Python, Qt, HIDAPI, system-monitoring support, the local OBS WebSocket client, `7zz`, `libusb` and a certificate trust store for HTTPS downloads. A wheel or source installation obtains the Python dependencies from the Python package index and needs host `7zz` or `7z` for firmware inspection and host `libusb-1.0` on Linux for firmware installation. See [Firmware updates and backups](firmware.md) before using the updater.

The AppImage does not need Python or a virtual environment. Installing a wheel or source archive normally downloads the GUI dependencies from the Python package index. If `python3 -m venv` is unavailable, install the Python virtual-environment support provided by your operating system first.

## Install a release

### Linux x86_64 AppImage (recommended)

Download the AppImage and its `.sha256` file from the release. Verify the download, place the application where you intend to keep it, mark it executable and launch it:

```sh
sha256sum -c MC7-Studio-0.1.0-x86_64.AppImage.sha256
mkdir -p "$HOME/Applications"
mv MC7-Studio-0.1.0-x86_64.AppImage "$HOME/Applications/"
chmod +x "$HOME/Applications/MC7-Studio-0.1.0-x86_64.AppImage"
"$HOME/Applications/MC7-Studio-0.1.0-x86_64.AppImage"
```

Do not run the AppImage as root. It keeps presets and other user data in the normal per-user locations rather than inside the AppImage.

### Windows x86_64 portable application

Download the Windows ZIP and adjacent checksum from the release. Verify the SHA-256 value with `Get-FileHash`, extract the entire archive, and launch `MC7-Studio.exe`:

```powershell
Get-FileHash .\MC7-Studio-0.1.0-windows-x86_64.zip -Algorithm SHA256
Expand-Archive .\MC7-Studio-0.1.0-windows-x86_64.zip -DestinationPath "$env:LOCALAPPDATA\Programs"
& "$env:LOCALAPPDATA\Programs\MC7-Studio\MC7-Studio.exe"
```

Close Swarm II before using MC7 Studio. No udev rule or separate USB driver is required on Windows. The portable build is currently unsigned, so review the downloaded checksum before accepting any Windows unrecognized-publisher warning.

### macOS Apple Silicon application

Download `MC7-Studio-0.1.0-macos-arm64.zip` and its checksum from [GitHub Releases](https://github.com/dev-zetta/swarm2/releases/latest). Verify the archive before opening it:

```sh
shasum -a 256 -c MC7-Studio-0.1.0-macos-arm64.zip.sha256
```

Extract the archive in Finder and move **MC7 Studio.app** to **Applications**. Open it there, connect one MC7 directly by USB, and choose **Refresh devices**. The bundle includes its Python runtime, Qt, HIDAPI and 7-Zip; Homebrew is not needed.

The first release is ad-hoc signed but is not Apple notarized. If macOS blocks the application, review the source and checksum, then use **System Settings → Privacy & Security → Open Anyway** for MC7 Studio. Move the app to its permanent location before enabling start at login. macOS device configuration still needs physical hardware acceptance.

### Wheel on Linux, macOS or Windows

Download the wheel from the release and replace the example path below with its location:

```sh
python3 -m venv mc7-studio
mc7-studio/bin/python -m pip install '/path/to/swarm2_mc7-0.1.0-py3-none-any.whl[gui]'
mc7-studio/bin/swarm2-gui
```

The virtual environment keeps MC7 Studio and its Python dependencies separate from the system Python. Run `mc7-studio/bin/swarm2-gui` again whenever you want to open the application.

Apple Silicon Mac and Windows users should normally use their platform's portable ZIP. The wheel is also available for other supported Python environments. The Linux x86_64 AppImage does not run on macOS or Windows.

### Source archive

The source archive contains the same application and is useful when a wheel cannot be installed directly:

```sh
tar -xzf swarm2_mc7-0.1.0.tar.gz
cd swarm2_mc7-0.1.0
python3 -m venv .venv
.venv/bin/python -m pip install '.[gui]'
.venv/bin/swarm2-gui
```

Do not run MC7 Studio as root. Linux device access is handled with a udev rule, described below.

## Grant USB access on Linux

You can open the application before installing the rule. MC7 Studio can then check whether the bundled rule is missing or outdated:

1. Open **Device**.
2. Choose **Check and install host integration…**.
3. Find **MC7 USB access** and choose **Install or update udev rule…**.
4. Approve the administrator authentication request.
5. Disconnect and reconnect the mouse and receiver.
6. Choose **Refresh devices**.

The rule grants the signed-in desktop user access to the supported MC7 HID and USB interfaces. It is also required for firmware installation. The app does not open or change the mouse while checking or installing host integration.

If the in-app installer is unavailable, see the manual procedure in [Troubleshooting](troubleshooting.md#the-linux-udev-installer-is-unavailable).

## Check the connection

Run the read-only diagnostic with the same AppImage used to open the application:

```sh
"$HOME/Applications/MC7-Studio-0.1.0-x86_64.AppImage" doctor
```

For a wheel installation, run it from the same virtual environment:

```sh
mc7-studio/bin/swarm2 doctor
```

For a source installation, use `.venv/bin/swarm2 doctor` or `.venv\Scripts\swarm2.exe doctor` on Windows. A healthy Linux setup should find the MC7 interfaces and report writable device nodes. On macOS and Windows, it should report that HIDAPI is available. The diagnostic discovers interfaces and permissions without opening the mouse or changing settings.

## Update the AppImage and start at login

MC7 Studio does not download or install application updates silently. Download a replacement AppImage from a newer release, or use a compatible AppImage update tool with the `.zsync` file published beside it. The `.zsync` data supports external update tools; it is not an in-app updater and it never updates mouse firmware.

The start-at-login entry records the AppImage's absolute path. Move the file to a permanent location before enabling **Start at login** in **Device → Check and install host integration…**. After moving, renaming or replacing a version-named AppImage, launch the new file and choose **Install or update start-at-login entry** again.

## Read, edit and apply

MC7 Studio separates the local draft from settings stored on the mouse. Follow this sequence so the app can preserve settings it does not edit:

1. Open **Device**, select the directly connected MC7, and use **Refresh devices** if it is not listed.
2. Open **Profiles** and select the onboard slot you want to edit.
3. Choose **Read mouse**. Wait for the read to finish before editing.
4. Change settings on **Sensitivity**, **Buttons**, **Lighting**, **Display** or **Device**.
5. Choose that page's **Apply** button, such as **Apply sensitivity**. The app writes that section and reads it back before reporting success.
6. Read the mouse again before changing another profile or retrying an apply that could not be verified.

**Save preset** stores the local draft on this computer; it does not write the mouse. **Activate this profile** changes which onboard profile is active. JSON export is useful for moving a local preset between Linux, macOS and Windows.

Start with one small change and confirm it on the mouse. Keep the cable connected until every active operation has finished.

## Set up the mouse display

To replace the **Download Swarm II** prompt:

1. Read the active mouse profile.
2. Open **Display** and choose **Set up LCD**.
3. Review the three display pages and their tiles.
4. Choose **Apply display settings** and wait for readback to finish.

Some tiles run entirely on the mouse. Live statistics, countdown, General Media, application/file/website launch and OBS actions need MC7 Studio to stay open with the LCD action listener or live updates enabled. Configure those features only after applying and rereading the active profile. See [Desktop integrations](integrations.md) for their requirements.

## Continue configuring

- [User guide](user-guide.md): profiles, sensitivity, buttons, macros, lighting, display settings and presets.
- [Desktop integrations](integrations.md): automatic profiles, tray behavior, media controls, host launch tiles and OBS.
- [Firmware updates and backups](firmware.md): package requirements, backup limits and the update workflow.
- [Troubleshooting](troubleshooting.md): connection, permission and feature problems.
