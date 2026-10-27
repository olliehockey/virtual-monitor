# Changelog

## 2.4.0
- `vm uninstall [--purge]`.
- Addresses labelled "same Wi-Fi" / "Tailscale".
- README rewritten as a host + device setup guide (Sunshine, Tailscale, Moonlight settings).

## 2.3.1
- MacBook presets now use the area below the notch (13" Air: 2560x1600), matching full-screen Moonlight.

## 2.3.0
- Presets for current iPhones, iPads and MacBooks, grouped; shown in `vm --help`.
- Your own presets: `vm presets add NAME WxH [--scale] [--label]`, `vm presets rm NAME`.

## 2.2.2
- `vm presets` lists presets.
- Friendlier "unknown command" message with a suggestion; internal commands no longer shown.

## 2.2.1
- Show "starting…/stopping…" while slow steps run, so nothing looks frozen.

## 2.2.0
- Presets: `iphone-13`, `ipad-pro-11` (M4), `macbook-air`, `macbook-air-15`: `vm add ipad ipad-pro-11`.
- `vm enable` pairs the device the first time; `vm disable` with no name turns everything off.
- Duplicate pairings ("certificate identity is not enabled") are cleaned up automatically.
- `vm enable` on a running device repairs its screen placement.
- `vm web` is back. README is a complete from-scratch setup guide.

## 2.1.0
- Simpler CLI: `vm add NAME WxH` (no presets). Scale defaults to 2 for high-density screens.
- Shorter help and output. Removed `presets`, `import-old`, `login`, `web`.
- `enable`/`disable` also available as `on`/`off`.

## 2.0.2
- `vm unpair DEVICE`: forget paired Moonlight clients (fixes "client certificate identity is not enabled").
- `vm pair` refuses to pair an already-paired device again (duplicates make Sunshine reject it) unless `--again`.
- `vm pair` refuses to guess when several pairing requests are waiting.
- `vm doctor` no longer flags the old iPad Sunshine unit as a stray dependency.

## 2.0.0
Rewritten from scratch as `vm`, a per-device CLI.

- One virtual screen and one dedicated Sunshine instance per device. Enable, disable and pair each one independently.
- `vm pair` sends Moonlight's PIN straight to Sunshine (supports both the new and old pairing APIs).
- Safety: operation lock, preflight refusals (exit 3), timeouts, rollback on a failed enable, Sunshine-then-screen
  stop order, teardown cool-down, transactional config changes with `vm undo`.
- Never controls, edits or depends on the normal Sunshine service.
- Detects the old setup (read-only), refuses to run alongside the old daemon, and documents a manual migration.
- Bash/zsh/fish completion, `--json` output, documented exit codes.

## 1.1.0
Single-monitor profile switcher for the upstream `gnome-virtual-monitor` daemon (superseded).
