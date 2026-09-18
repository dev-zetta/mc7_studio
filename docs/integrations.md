# Desktop integrations

MC7 Studio can combine settings stored on the mouse with services running on your computer. This page explains which features continue in the mouse and which require MC7 Studio to remain open.

## Onboard and host-assisted features

**Onboard** means that MC7 Studio writes the setting or assignment to the mouse. A key or media assignment still relies on the receiving operating system or focused application to interpret the input, but it does not need the MC7 Studio listener.

| Feature | Where it runs | MC7 Studio must remain open |
| --- | --- | --- |
| Profiles, sensitivity, buttons, lighting and power settings | Mouse | No |
| LCD layout, brightness, haptics and stored background | Mouse | No |
| Remap Key, Keyboard Shortcut and assigned Macro tiles | Mouse | No |
| Standard key, editing, browser, calculator and System Media tiles | Mouse sends normal input to the computer | No |
| Countdown timer | Mouse touch plus host timer | Yes |
| General Media panel | Mouse touch plus a host media provider | Yes |
| Open Application, Website, File or Folder | Mouse touch plus a local target | Yes |
| Launch OBS, OBS Screenshot and OBS Studio Mode | Mouse touch plus host integration | Yes |
| CPU, RAM, GPU and temperature widgets | Host measurements sent to the mouse | Yes |
| Automatic application profiles | Foreground-app monitor plus an onboard profile switch | Yes |

The LCD action listener, live system monitoring and automatic profiles are separate services. Only enable the one you need. Mouse operations and dialogs may pause them to keep device access ordered.

## Host integration setup

Open **Device → Check and install host integration…** to inspect this computer. The dialog never changes the mouse and performs an installation only after you choose its button.

### Linux USB access

The bundled udev rule allows the signed-in desktop user to access the supported MC7 HID interfaces and the USB interface needed by the firmware updater. Choose **Install or update udev rule…**, authorize the request, then reconnect the mouse and receiver. Refresh devices in MC7 Studio afterward.

### Start at login

Choose **Install or update start-at-login entry** to open MC7 Studio the next time this user signs in. The dialog can also remove an entry it manages.

Starting the application does not automatically enable battery polling, low battery notifications, the LCD action listener or live system values. Automatic-profile switching resumes when its saved rule settings are enabled.

### GNOME Wayland companion

GNOME Wayland needs the included companion to identify the application that owns the focused window. The companion supports GNOME Shell 45 through 50 and reports only the focused process ID to MC7 Studio.

1. From a GNOME Wayland session, choose **Install or update companion**.
2. If the status says GNOME has not loaded it, log out and back in.
3. Reopen the dialog and choose **Enable companion**.

The companion is installed for the current user. It is not needed on X11, Sway, Hyprland or macOS.

## Live LCD action listener

One listener handles countdown, General Media, launch-target and supported OBS tiles.

1. Select and read the mouse's active profile.
2. Add the desired tiles and complete their local settings.
3. Choose **Apply display settings**.
4. Read the same active profile again.
5. Enable **Listen for timer, media and launch taps on the mouse**.

The listener verifies that the active profile and applied layout still match before handling a tap. While it is enabled, settings writes, firmware work, live system values and automatic profile changes wait. Stop the listener before performing those operations.

If the profile or layout changes, read and apply as needed, then start the listener again. To keep it running with the window hidden, enable battery monitoring or automatic profiles to activate the tray, then choose **Keep running in the tray when the window is closed**. This requires a desktop system tray.

### Countdown timers

Create a timer from 1 to 600 seconds in the Display page, add a **Count down timer** tile and assign that timer to the cell. A tap starts the countdown; a second tap stops it and restores its initial value. The timer definition stays in the local preset. The mouse does not run the countdown by itself.

### Application, website, file and folder tiles

These tiles store a short trigger on the mouse while their full target remains in the local preset:

- **Open application** uses an absolute executable or application path. It can use the detected application icon or a PNG/JPEG chosen by you.
- **Open website** accepts an `http://` or `https://` URL and uses the default browser.
- **Open file** and **Open folder** use absolute paths and the desktop's normal opener.

Moving a preset to another computer does not guarantee that its filesystem paths exist there. Review imported targets before enabling the listener. Custom application-icon pixels cannot be read back from the mouse; retain the source image if you will need it again.

## General Media

The three-slot **General Media** panel controls Shuffle, Next, Play/Pause, Previous and Repeat through a host media service. It also reflects available playing, shuffle and repeat state on the mouse.

On Linux, it controls applications that expose the standard MPRIS interface. Choose **Refresh players** and select one when more than one player is present. **Automatic** works only when the choice is unambiguous and will not silently switch to another player if a saved player disappears.

On macOS, the current provider controls Apple Music when it is already running. macOS may ask for Automation permission. Other media applications and system-wide browser playback are not supported by this provider.

General Media playback-state control is not yet available on Windows. The separate **System Media** tile remains an onboard mouse action and works without a Windows host provider.

The separate **System Media** tile sends onboard media input and does not need this listener.

## OBS Studio

Three LCD tiles are available:

- **Launch OBS** starts an existing OBS installation. Linux supports a native `obs` command or the Flatpak application `com.obsproject.Studio`; macOS uses the installed OBS application; Windows uses `obs64.exe` or `obs32.exe` from `PATH` or the standard OBS installation folder.
- **OBS Screenshot** asks an already-running OBS instance to perform its Screenshot Output action. OBS controls the output folder and format.
- **OBS Studio Mode** toggles Studio Mode in an already-running OBS instance and updates the tile with the verified state.

Launch OBS does not install OBS and does not require WebSocket configuration. Screenshot and Studio Mode require OBS Studio 28 or newer with its WebSocket server enabled:

1. In OBS, open **Tools → WebSocket Server Settings**. Some versions call it **obs-websocket Settings**.
2. Enable the server, keep authentication enabled, set a password and apply the OBS settings.
3. Add the desired tile in MC7 Studio, apply the display settings and read the active profile again.
4. Start the MC7 Studio LCD action listener.

MC7 Studio reads OBS's current-user configuration and connects only to the local computer. It does not copy the OBS password into presets or a separate settings file. If native and Flatpak OBS configurations are both enabled on Linux, they must use the same port and authentication settings.

A Studio Mode change made directly inside OBS is not pushed immediately to the mouse. Restart the listener to refresh the icon. Pressing the tile also checks the current OBS state before toggling it.

## Live system monitoring

Add a CPU usage, CPU temperature, GPU usage, GPU temperature or RAM usage tile, apply the display settings and read the active profile again. Then enable **Send live system values to the mouse every 2 seconds**.

Only measurements exposed by the operating system are sent. Missing temperature or GPU values stay unavailable rather than being shown as zero. On a multi-GPU Linux system, choose **Refresh GPUs** and select a specific adapter when Automatic cannot identify one system-primary GPU. The selected GPU is a local computer preference.

Live values stop when the app closes and pause during other mouse operations or dialogs. A value can remain visible on the LCD after updates stop.

## Automatic application profiles

Open **Profiles → Manage automatic profiles…** to associate an exact executable path, or a macOS bundle identifier, with one of the five onboard profiles. Rules are ordered; the first enabled match wins. The configured default profile is selected only after a successful foreground check finds no matching rule.

The feature follows the application that owns the foreground window. A program merely running in the background does not trigger its rule. Profile switching is disabled by default and the rule list stays on this computer.

| Desktop | Requirement |
| --- | --- |
| Linux X11 | `xprop` available in `PATH` |
| GNOME Wayland | Install, load and enable the included GNOME companion |
| Sway | `swaymsg` available in `PATH` |
| Hyprland | `hyprctl` available in `PATH` |
| macOS | Built-in foreground-application provider |
| Windows | Built-in foreground-window process provider |
| KDE Plasma and other Wayland compositors | Not currently supported |

When an automatic switch occurs, MC7 Studio preserves the local draft but stops live LCD updates and invalidates the previous device read. Choose **Read mouse** before applying more settings.

## Privacy and local data

- Presets, launch targets, timer definitions and automatic-profile rules stay in the current user's application data.
- The GNOME companion reports a focused process ID and does not keep window titles or a window history.
- OBS credentials remain in the OBS configuration file and are hidden from MC7 Studio status messages.
- No integration starts during device discovery. Installation and live listeners require an explicit action in the interface.

Return to the [MC7 Studio user guide](user-guide.md) for the normal device workflow.

Presets preserve Linux, macOS and Windows application and file paths when moved between systems. After importing a preset from another platform, choose local targets for its launch tiles before using them; MC7 Studio does not reinterpret another platform's paths as local files.
