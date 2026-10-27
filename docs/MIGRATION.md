# Migrating from the old single-screen setup

The old setup has two parts:

| Part | Unit | Notes |
| --- | --- | --- |
| Upstream daemon | `gnome-virtual-monitor.service` | Creates one virtual screen and rewrites GNOME's whole layout |
| iPad Sunshine | `app-dev.lizardbyte.app.Sunshine.Android.service` | Config in `~/.config/sunshine-instances/android`, bound to the daemon |
| v1 CLI (optional) | `~/scripts/virtual-monitor`, `~/.bashrc.d/40-virtual-monitor.sh` | Shell function `virtual-monitor` |

`vm` **never changes any of these for you**. It only reads them, and `vm enable` refuses to run while
the old daemon is running, because two programs managing GNOME's virtual screens can fight
over the layout.

Your normal (Mac) Sunshine, `app-dev.lizardbyte.app.Sunshine.service`, is not part of this
migration, and `vm` never controls or edits it.

## 1. See what's there

```bash
vm doctor
```

Every finding lists the exact file and the commands to run **yourself**.

## 2. If `vm doctor` reports a drop-in on your normal Sunshine

Upstream's installer can add `~/.config/systemd/user/app-dev.lizardbyte.app.Sunshine.service.d/virtual-monitor.conf`,
which makes your normal Sunshine start or wait for the old daemon. If it exists and you retire
the old daemon, your normal Sunshine waits about 20 s at its next start and then fails.

Check what is actually loaded:

```bash
systemctl --user show app-dev.lizardbyte.app.Sunshine.service -p Requires -p Wants -p BindsTo -p After -p DropInPaths
```

If `DropInPaths` lists the file and the dependencies mention `gnome-virtual-monitor`, and you
still want to retire the old daemon, do this at a time when a later Sunshine restart is OK:

```bash
mkdir -p ~/virtual-monitor-legacy
mv ~/.config/systemd/user/app-dev.lizardbyte.app.Sunshine.service.d/virtual-monitor.conf ~/virtual-monitor-legacy/
systemctl --user daemon-reload      # reloads definitions; does not restart anything
```

The running Sunshine is unaffected until you restart it yourself. If nothing is listed, skip this step.

## 3. Retire the old daemon and iPad Sunshine

```bash
systemctl --user disable --now app-dev.lizardbyte.app.Sunshine.Android.service
systemctl --user disable --now gnome-virtual-monitor.service
```

This ends any stream that is still using the old virtual screen.

## 4. Add your iPad to vm

```bash
vm add ipad 2420x1668
vm enable ipad && vm pair ipad
```

In Moonlight, delete the old PC entry (port 48049).

## 5. Remove the v1 CLI (optional)

```bash
bash ~/scripts/virtual-monitor/uninstall.sh    # removes only its shell wrapper
```

Archive `~/scripts/virtual-monitor` once you've checked it.

## Going back

```bash
vm disable --all
systemctl --user enable --now gnome-virtual-monitor.service
systemctl --user start app-dev.lizardbyte.app.Sunshine.Android.service
# restore the drop-in if you moved it:
mv ~/virtual-monitor-legacy/virtual-monitor.conf ~/.config/systemd/user/app-dev.lizardbyte.app.Sunshine.service.d/
systemctl --user daemon-reload
```
