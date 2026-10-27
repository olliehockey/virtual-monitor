"""Run inside `dbus-run-session`: real D-Bus round trip of vm's layout code against fake_mutter."""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

here = Path(__file__).resolve().parent
tmp = Path(tempfile.mkdtemp())
os.environ.update(VM_CONFIG_DIR=str(tmp / "c"), VM_RUNTIME_DIR=str(tmp / "r"), VM_STATE_DIR=str(tmp / "s"),
                  VM_UNIT_DIR=str(tmp / "u"), FAKE_MUTTER_LOG=str(tmp / "applied.jsonl"))
sys.path.insert(0, str(here.parent))
fake = subprocess.Popen([sys.executable, str(here / "fake_mutter.py")], stdout=subprocess.PIPE, text=True)
assert fake.stdout.readline().strip() == "READY"

from virtual_monitor import config as cfg, mutter, runtime  # noqa: E402

(tmp / "c").mkdir()
config = cfg.Config()
config.devices["ipad"] = cfg.new_device(config, "ipad", 2388, 1668)
cfg.save(config)
state = mutter.get_state()
assert state.serial == 42 and "Meta-0" in state.virtual_connectors(), state
runtime.write_ready("ipad", "Meta-0")
summary = mutter.relayout()
print("relayout:", summary)
applied = [json.loads(line) for line in (tmp / "applied.jsonl").read_text().splitlines()]
assert [a["method"] for a in applied] == [0, 1], applied     # verify, then temporary (never persistent)
layout = applied[-1]["layout"]
assert layout[1] == [2560, 303, 2.0, 0, False, [["Meta-0", "2388x1668@60", {}]]], layout
assert applied[-1]["props"] == {"layout-mode": 1}
fake.terminate()
print("D-BUS CHECK OK")
