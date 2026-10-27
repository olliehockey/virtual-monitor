# vm: use your iPad, iPhone or Mac as an extra screen for Linux

`vm` turns another device into a real extra monitor for your Linux desktop. Windows drag across
to it just like a second screen plugged in with a cable, over Wi-Fi or over the internet.

```console
$ vm add ipad ipad-pro-11
✓ added ipad  2420x1668 @ 60 Hz, scale 2, right, port 48100
$ vm enable ipad
✓ ipad on  2420x1668, right  moonlight 192.168.1.20:48100 (same Wi-Fi) or 100.90.1.5:48100 (Tailscale)
$ vm disable
✓ off: ipad
```

## How it fits together

You install software in two places:

| | Install | What it does |
|---|---|---|
| **Your Linux PC** (the host) | **Sunshine**, **Tailscale**, **vm** | Sunshine streams the screen; vm creates the extra screens and runs a private Sunshine for each device |
| **Each device** you want as a screen | **Moonlight**, **Tailscale** | Moonlight shows the screen; Tailscale lets it reach your PC from anywhere |

Each device gets its own virtual screen and its own Sunshine instance, so devices turn on and off
independently. If you already use Sunshine for something else, vm leaves it alone.

**Requirements for the PC:** Linux with **GNOME on Wayland**, which is the default on Fedora 40+ and
Ubuntu 24.04+. Check with:

```bash
echo $XDG_SESSION_TYPE $XDG_CURRENT_DESKTOP     # should print: wayland GNOME
```

---

## Part 1: Set up the Linux PC

Do this once. It takes about 15 minutes.

### 1.1 Install the basics

**Fedora**
```bash
sudo dnf install git python3-gobject gstreamer1 pipewire-gstreamer
```

**Ubuntu**
```bash
sudo apt install git python3-gi gir1.2-gstreamer-1.0 gstreamer1.0-pipewire
```

### 1.2 Install Sunshine

Sunshine is the streaming server. **You only need to install it**: no setup, no web page, no
service to start. `vm` creates and configures a separate Sunshine for each device.

**Fedora**
```bash
sudo dnf copr enable lizardbyte/stable
sudo dnf install Sunshine
```

**Ubuntu**
```bash
curl -1sLf 'https://dl.cloudsmith.io/public/lizardbyte/stable/cfg/setup/bash.deb.sh' | sudo -E bash
sudo apt update
sudo apt install sunshine
```

Then **reboot**. Sunshine installs a permission rule that lets it control the mouse and keyboard,
and it only takes effect after a reboot.

Check it worked:
```bash
sunshine --version
```

> If Sunshine's own docs or a pop-up tell you to open `https://localhost:47990` and create a login,
> you can ignore that for `vm`. That page belongs to Sunshine's default instance, which `vm` doesn't use.

### 1.3 Install Tailscale

Tailscale gives your PC and your devices private addresses that work from anywhere: home,
university or work Wi-Fi (which often block devices from seeing each other), or mobile data. It's
free for personal use. If all your devices are always on the same home Wi-Fi you can skip this,
but it saves a lot of "why can't it connect" trouble.

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

`tailscale up` prints a link. Open it and log in (Google, Microsoft, GitHub or Apple account).
Your PC's Tailscale address is shown by:

```bash
tailscale ip -4          # e.g. 100.90.1.5
```

### 1.4 Install vm

```bash
git clone https://github.com/olliehockey/virtual-monitor.git
cd virtual-monitor
./install.sh
```

The installer needs no sudo, puts everything in your home folder, and **doesn't start anything or
turn anything on at login**. Open a **new terminal**, then check the PC is ready:

```bash
vm doctor
```

Everything should be ✓. Anything marked ✕ comes with the command that fixes it. Things marked ! are only warnings.

---

## Part 2: Set up each device

On every iPad, iPhone, Mac or other device you want to use as a screen:

### 2.1 Install Moonlight

| Device | Get Moonlight |
|---|---|
| iPad / iPhone | App Store: **Moonlight Game Streaming** |
| Mac | [moonlight-stream.org](https://moonlight-stream.org), or `brew install --cask moonlight` |
| Android | Play Store: **Moonlight Game Streaming** |
| Windows / Linux | [moonlight-stream.org](https://moonlight-stream.org) |

### 2.2 Install Tailscale (if you set it up on the PC)

Get **Tailscale** from the App Store, Play Store or [tailscale.com/download](https://tailscale.com/download),
then log in with the **same account** you used on the PC. Leave it switched on.

### 2.3 Moonlight settings (one time per device)

Open Moonlight's **Settings** (the gear icon):

| Setting | Set it to | Why |
|---|---|---|
| **Resolution** | **Full** on iPad/iPhone; on a Mac, **Native** or the size `vm` shows | So the picture fills the screen. Use the row of buttons, not the grey "Set PC/Game resolution" box (that's just a help link) |
| **Frame rate** | 60 FPS (120 on ProMotion iPads/iPhones if your Wi-Fi is good) | Smoothness |
| **Touch Mode** (iPad/iPhone) | **Touchscreen** to tap things directly, **Touchpad** for precise mouse control | Personal taste |
| **Display mode** (Mac) | **Fullscreen** | Fills the screen |
| **On-Screen Controls** | Off | They're for games |

Mac tip: press **Ctrl + Alt + Shift + Q** to leave the stream.

---

## Part 3: Add a device and connect

### 3.1 Add it

Pick your model (`vm presets` lists them with sizes):

| | Presets |
|---|---|
| iPhone | `iphone-13-mini` `iphone-13` `iphone-13-pro-max` `iphone-15` `iphone-15-pro-max` `iphone-16-pro` `iphone-16-pro-max` |
| iPad | `ipad-mini` `ipad` `ipad-air-11` `ipad-air-13` `ipad-pro-11` `ipad-pro-11-2018` `ipad-pro-13` `ipad-pro-12-9` |
| MacBook | `macbook-air` (13") `macbook-air-15` `macbook-pro-14` `macbook-pro-16` |

```bash
vm add ipad ipad-pro-11
vm add phone iphone-13 --position below
vm add mac macbook-air --position left
```

Device names are yours to choose (short and without spaces is easiest). Options:
`--position left|right|above|below` (which side of your monitor; default right), `--scale 1.5`
(smaller text; default 2), `--refresh 120`, `--audio` (send sound to the device too).

Not on the list? Use its screen resolution in landscape (it's on the maker's spec page), and
optionally save it as your own preset:

```bash
vm add tablet 2560x1600
vm presets add galaxy-tab 2560x1600      # then: vm add tablet galaxy-tab
```

### 3.2 Turn it on and pair (first time)

```bash
vm enable ipad
```

1. A new screen appears next to your monitor, and GNOME asks **which screen to share**. Pick the one
   with your device's resolution (not your real monitor) and click **Share**.
2. `vm` shows the address(es) to use and waits for a PIN.
3. On the device, open Moonlight, tap **+** and type the address: the `100.x` (Tailscale) one works
   everywhere, the `192.168.x` one only on the same Wi-Fi. Tap the new PC that appears.
4. Moonlight shows a 4-digit **PIN**. Type it into the terminal on the PC.
5. Tap the PC in Moonlight, then **Desktop**. Done.

Pairing happens **once per device**. After that it's just enable, then Moonlight.

---

## Everyday use

```bash
vm enable ipad      # screen on, then on the iPad: Moonlight → your PC → Desktop
vm disable          # all screens off
vm                  # see devices and what's on
```

GNOME asks which screen to share each time a screen is turned on. Pick the device's one.

## Commands

| Command | |
|---|---|
| `vm` | list devices |
| `vm enable [NAME]` | screen on, pairing the first time (`vm on`) |
| `vm disable [NAME]` | screen off; no name turns off everything (`vm off`) |
| `vm add NAME PRESET\|WxH` | add a device |
| `vm edit NAME [PRESET\|WxH] [--scale …] [--restart]` | change a device |
| `vm remove NAME` | remove a device (`vm undo` brings it back) |
| `vm presets` | list presets; `vm presets add NAME WxH` / `vm presets rm NAME` for your own |
| `vm pair NAME` / `vm unpair NAME` | pair again (e.g. after reinstalling Moonlight) / forget paired devices |
| `vm status [NAME]` | detailed health check |
| `vm web NAME` | that device's Sunshine page and login |
| `vm logs NAME [-f]` | logs |
| `vm doctor` | check the PC |
| `vm autostart NAME on\|off` | turn on automatically at login (off by default) |
| `vm uninstall` | remove vm |

Tab completion works for commands, device names and presets.

## Troubleshooting

| Problem | Fix |
|---|---|
| Moonlight can't find or add the PC | Use the `100.x` Tailscale address and check Tailscale is on on both sides. On the same Wi-Fi, use `192.168.x`. Run `vm status NAME`: "sunshine yes" means the PC side is ready. |
| Connects, but black screen or "no video" | GNOME's "share screen" dialog is waiting on the PC. Pick the device's screen. |
| Picture doesn't fill the device / black bars | Moonlight **Settings → Resolution → Full** (Mac: the size vm shows), and use the right preset for your model. |
| Text too big or too small | `vm edit ipad --scale 1.5 --restart` (smaller) or `--scale 2` (bigger) |
| "Certificate verification failed" / "client not authorized" | `vm enable ipad` again (it repairs pairings itself). Still failing: `vm unpair ipad`, delete the PC in Moonlight, then `vm pair ipad`. |
| Laggy or blurry | Wired or 5 GHz Wi-Fi for the PC; lower Moonlight's bitrate/frame rate; or a smaller size: `vm edit ipad 1600x1200 --scale 1 --restart` |
| Anything else | `vm doctor`, then `vm logs NAME` |

Firewall: Fedora Workstation and Ubuntu desktop allow these connections out of the box. On other setups,
allow ports 48095–48500 (TCP and UDP).

## Uninstall

```bash
vm uninstall            # removes vm; keeps your devices and pairings in case you reinstall
vm uninstall --purge    # removes everything vm created
```

This never touches Sunshine, Tailscale or Moonlight themselves. To remove those too:

```bash
sudo dnf remove Sunshine tailscale        # Fedora
sudo apt remove sunshine tailscale        # Ubuntu
```

On each device, delete the Moonlight and Tailscale apps.

## Safety

- One `vm` change at a time, and every step has a timeout, so nothing hangs forever.
- `vm enable` refuses (and changes nothing) if something's wrong, such as a port being in use, GNOME
  not responding or too many screens on, and rolls back if starting fails.
- Screens are added and removed one at a time, and layouts are checked first and never saved permanently.
- `vm` only ever controls the services it created, and never your own Sunshine.
- `vm undo` reverts the last device change.

Exit codes: `0` ok · `1` error · `2` bad usage · `3` refused by a safety check · `4` another `vm` command is running.

## How it works

For each device, `vm` creates two user services: `vm-display@NAME` asks GNOME (Mutter) for a
virtual monitor and places it next to your screen, and `app-dev.lizardbyte.app.Sunshine.vm_NAME`
runs a private Sunshine for that device. Each has its own port (48100, 48200, …), pairing and
settings, in `~/.config/virtual-monitor/`.

Upgrading from the older single-screen setup? See [docs/MIGRATION.md](docs/MIGRATION.md).
Developing? `scripts/check.sh` runs the tests (no GNOME needed).

Based on the technique from
[gnome-wayland-virtual-monitors-sunshine](https://github.com/lirenzzzin/gnome-wayland-virtual-monitors-sunshine).
MIT licensed.
