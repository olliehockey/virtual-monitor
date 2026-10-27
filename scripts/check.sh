#!/usr/bin/env bash
# Offline checks. Needs no GNOME session and changes nothing on this machine.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
PY="${PYTHON:-python3}"

echo "› compile"
"$PY" -m compileall -q virtual_monitor tests
echo "› unit + safety tests"
"$PY" -m unittest discover -s tests
echo "› shell scripts"
bash -n install.sh uninstall.sh scripts/check.sh
"$PY" -m virtual_monitor completion bash | bash -n
if command -v shellcheck >/dev/null; then shellcheck -S warning install.sh uninstall.sh scripts/check.sh; fi
echo "› D-Bus round trip against a fake Mutter"
GI_PY=""
for candidate in "$PY" python3 /usr/bin/python3; do
  if "$candidate" -c 'import gi; from gi.repository import Gio' 2>/dev/null; then GI_PY=$candidate; break; fi
done
if [[ -n $GI_PY ]] && command -v dbus-run-session >/dev/null; then
  timeout 60 dbus-run-session -- "$GI_PY" tests/dbus_check.py 2>&1 | grep -v 'fd limit'
else
  echo "  (skipped: needs python3-gobject and dbus-run-session)"
fi
echo "✓ all checks passed"
