# MC7 Studio user guide

MC7 Studio configures a Turtle Beach Command Series MC7 from Linux, macOS or Windows. It manages the mouse's five profile slots, sensor settings, buttons, lighting, touch display, macros and power preferences. It also provides optional desktop features that run while MC7 Studio is open.

Use a direct USB connection when changing mouse settings. Configuration through the wireless transmitter is not supported.

## Mouse settings and local data

MC7 Studio keeps two kinds of state:

| Location | What is stored there |
| --- | --- |
| **On the mouse** | The five profile slots, sensitivity and sensor settings, button assignments, assigned macros, lighting, power preferences, LCD layout and controls, display settings, and uploaded background or application-icon images. |
| **On this computer** | Named presets and their artwork, the full macro and timer libraries, launch targets, preferred media player and GPU, automatic-profile rules, and desktop-integration choices. |

A local preset is a reusable editing copy. **Save preset** does not write to the mouse, and an **Apply** button does not save the complete preset. Host-assisted LCD controls also keep part of their configuration on the computer, so storing their tile on the mouse does not make them independent of MC7 Studio.

## First start

1. Connect the mouse directly to the computer with a USB cable.
2. Start the AppImage, run `swarm2-gui`, or run `swarm2 gui`.
3. Open **Device** and choose **Refresh devices** if the mouse is not selected.
4. On Linux, open **Check and install host integration…**. Check the **MC7 USB access** section and install or update the udev rule if requested. Administrator authentication is required for this one action. Reconnect the mouse and receiver afterward, then refresh devices again.
5. Choose **Read firmware and battery** to refresh device status.
6. Open **Profiles**, choose a mouse profile slot, then choose **Read mouse**.

Run MC7 Studio as your normal desktop user. The Linux udev rule grants the required device access; running the whole application as root is unnecessary.

## The read, edit and apply workflow

Use this sequence whenever you configure the mouse:

1. Under **Profiles**, select the profile slot you want to edit.
2. Choose **Read mouse**. The initial values shown before a read are editor defaults and may not match the connected mouse.
3. Edit one settings page.
4. Use that page's **Apply** button. MC7 Studio checks the current mouse state, writes the selected section and reads it back.
5. Use **Save preset** if you also want a reusable copy on this computer.

The profile selected for editing is separate from the mouse's active profile. Choose **Activate this profile** when you want the mouse to switch slots. Some features, including calibration and live LCD actions, require you to read the currently active slot.

Read the mouse again after reconnecting it, switching profiles, restoring a backup, updating firmware or receiving a write error. A previous read is not reused after MC7 Studio detects that the mouse may have changed.

## Pages

### Overview

Overview shows the selected connection, firmware and battery information when available, the current local preset, and the sensitivity from the last successful mouse read.

### Sensitivity

You can configure:

- five DPI stages from 50 to 30,000 DPI in steps of 50;
- the enabled stages, active stage and stage colors;
- polling rate, DPI indicator and debounce time;
- Motion Sync, angle snapping and angle adjustment; and
- Very Low, Low or Custom lift-off distance.

At least one DPI stage must remain enabled. **Find a comfortable DPI…** and **Align my angle…** use pointer exercises to suggest values; accepting a suggestion changes the local draft until you choose **Apply sensitivity**.

**Calibrate my surface…** starts the mouse's custom lift-off calibration. This is a separate device operation that affects all profiles. Keep the mouse on its usual surface and follow the Save or Cancel status shown by the dialog.

### Buttons

The button table has a standard layer and an Easy-Shift layer. Available assignments include mouse and wheel actions, profile and DPI changes, Easy-Aim, media controls, browser and calculator actions, Easy-Wheel modes, keyboard shortcuts and macros.

Easy-Aim can use a fixed stage or a custom value. A keyboard assignment accepts a shortcut such as `Ctrl+S` or `F5`. Actions that MC7 Studio can read but cannot edit are shown as preserved on-device actions. They remain unchanged unless you replace them.

Choose **Apply buttons** after editing. MC7 Studio prevents a mapping that would leave the mouse without reachable primary left and right clicks.

### Lighting

Choose Off, Static, Blink, Breathing, Heartbeat, AIMO or Color Wave, then set the accent color, brightness and effect speed. Choose **Apply lighting** to write only the lighting section of the selected profile.

### Display

The display editor exposes three pages with four logical slots per page. Some panels occupy three consecutive slots. You can move complete pages left or right and configure:

- DPI and LED brightness controls;
- remapped keys, keyboard shortcuts and Macro tiles;
- media, editing, browser and calculator controls;
- countdown, application, website, file and folder tiles;
- Launch OBS, OBS Screenshot and OBS Studio Mode; and
- live CPU, RAM, GPU and temperature values.

Key, shortcut, macro and standard media/editing tiles send input from the mouse; the focused application and desktop decide how that input is handled. Countdown, General Media, OBS and launch-target tiles require the host listener described in [Desktop integrations](integrations.md).

If the LCD still shows **Download Swarm II**, choose **Set up LCD**. This writes and reads back a useful starter layout immediately while preserving the other pages and any remaining tile on that page. Other pending Display edits still need **Apply display settings**.

To change the background, choose a PNG or JPEG under **Background image**, review the fitted preview, then explicitly upload it. Selecting **Built-in background** or **Stored custom background** and applying display settings only changes which existing background is active. Image pixels are not stored in a local preset and cannot be downloaded from the mouse, so retain the source image yourself.

Choose **Apply display settings** after changing the layout, brightness, timeout, haptic strength or active background. Read the active profile again before starting a live LCD service.

### Macros

Create, duplicate, rename and delete macros in the local library. You can add key, mouse and delay events manually or use the focused recording area. The recorder captures input only while its area has focus; it is not a global input recorder.

Button macros support Once, Repeat, While held and Toggle playback. LCD Macro tiles support Once, Repeat and Toggle. Assign a macro from **Buttons** or from a Macro cell on **Display**, then apply that section. Assigned macro data is sent to the mouse before its button or tile is enabled.

### Profiles

Local presets can have a name, color and image. You can create, duplicate, save, load, delete, import and export them as JSON files. Their color and image identify the local library entry and are not written as an onboard mouse profile appearance.

**Manage automatic profiles…** associates foreground applications with the five onboard slots. These rules stay on this computer. See [Desktop integrations](integrations.md#automatic-application-profiles).

### Device

The Device page contains:

- connection, firmware, battery and charging status;
- the host-integration checker and installers;
- optional once-per-minute battery monitoring, tray status and a low-battery notification;
- standby, lighting timeout, ECO and energy-saving preferences;
- the firmware manager; and
- settings-backup restoration.

Battery and tray options apply to the current app session. **Keep running in the tray when the window is closed** works only when the desktop provides a system tray and monitoring has activated it. Start-at-login is managed separately in the host-integration dialog.

Firmware preparation creates a special five-profile settings backup. It is different from a local preset and is not a complete device or firmware image. Read [Firmware updates](firmware.md) before using it.

## Keyboard shortcuts

Standard desktop shortcuts work in the editor: `Ctrl+S` saves the preset, `Ctrl+O` imports a preset and `Ctrl+N` starts a new draft on Linux. On macOS, they use the corresponding Command shortcuts.

## Troubleshooting

### The mouse is not listed or cannot be opened

- Connect it directly by USB and choose **Device → Refresh devices**.
- On Linux, check the udev rule under **Device → Check and install host integration…**, install or update it, and reconnect the mouse.
- Close another MC7 Studio or command-line process that may own the device.
- Run the AppImage `doctor` command or the installed `swarm2 doctor` command in a terminal for a device-access summary.

### An Apply button is unavailable

Read the same device and profile slot first. Also close any calibration, firmware, restore or integration dialog and stop active LCD services. The Device page reports which features the selected connection exposes.

### A live LCD tile does nothing

Apply the tile to the active profile, read that profile again, and enable **Listen for timer, media and launch taps on the mouse**. Keep MC7 Studio open. Then check the feature-specific setup in [Desktop integrations](integrations.md).

### An operation failed

Follow the message in the window and read the mouse again before retrying. If a firmware or restore operation may have started writing, do not assume it was rolled back; use the guidance in [Firmware updates](firmware.md).

## Current limitations

- Mouse configuration through the wireless transmitter is unavailable.
- Transmitter firmware installation, pairing, standalone factory reset and interrupted-update recovery are unavailable.
- KDE Plasma and Wayland compositors other than GNOME, Sway and Hyprland do not provide automatic foreground-profile switching.
- macOS and Windows support are implemented, but physical MC7 validation is less complete than Linux validation.
- Windows firmware installation, per-application audio volume and desktop media playback-state control are unavailable.
- Start-at-login setup is currently available on Linux and macOS only.
- LCD Macro playback and custom lift-off calibration remain experimental.
- Some host-specific values, especially GPU and temperature sensors, may not be available on every computer.
