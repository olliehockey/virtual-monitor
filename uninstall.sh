#!/usr/bin/env bash
# Uninstall vm (virtual-monitor).
#
# Disables vm devices (Sunshine first, then the screen), removes vm's own systemd
# units, the `vm` command, completions and program files.
# Keeps ~/.config/virtual-monitor (devices, pairing, credentials) unless --purge.
# Never touches your normal (Mac) Sunshine or any legacy setup.
#
# Usage: uninstall.sh [--purge] [--yes]
set -euo pipefail

PURGE=0
YES=0
for arg in "$@"; do
  case $arg in
    --purge) PURGE=1 ;;
    -y|--yes) YES=1 ;;
    -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

DEST="${XDG_DATA_HOME:-$HOME/.local/share}/virtual-monitor"
LAUNCHER="$HOME/.local/bin/vm"
MARK="# vm launcher (virtual-monitor)"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/virtual-monitor"

if [[ -x $LAUNCHER ]] && grep -qF "$MARK" "$LAUNCHER"; then
  "$LAUNCHER" _uninstall-units || echo "  ! Could not remove units cleanly; check: systemctl --user list-units 'vm-display@*'"
  rm -f "$LAUNCHER"
  echo "✓ Removed $LAUNCHER"
else
  echo "  (vm launcher not found or not ours; skipping unit cleanup)"
fi
rm -f "${XDG_DATA_HOME:-$HOME/.local/share}/bash-completion/completions/vm" \
      "${XDG_DATA_HOME:-$HOME/.local/share}/zsh/site-functions/_vm" \
      "${XDG_CONFIG_HOME:-$HOME/.config}/fish/completions/vm.fish"
rm -rf "$DEST"
echo "✓ Removed program files and completions"

if (( PURGE )); then
  if (( ! YES )); then
    read -r -p "Delete $CONFIG_DIR (devices, Moonlight pairings, credentials)? [y/N] " answer
    [[ $answer == [yY]* ]] || { echo "Kept $CONFIG_DIR"; exit 0; }
  fi
  rm -rf "$CONFIG_DIR"
  echo "✓ Deleted $CONFIG_DIR"
else
  echo "  Kept your devices and pairings in $CONFIG_DIR"
  echo "  (reinstalling picks them up again; to delete them: rm -rf $CONFIG_DIR)"
fi
