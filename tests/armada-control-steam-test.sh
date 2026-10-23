#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

python3 - "$ROOT" "$WORK" <<'PYEOF'
import importlib.util
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
work = pathlib.Path(sys.argv[2])
spec = importlib.util.spec_from_file_location(
    "armada_control_steam",
    root / "decky/freedom-control/decky/armada-control/py_modules/armada_control/steam.py",
)
steam = importlib.util.module_from_spec(spec)
spec.loader.exec_module(steam)


def cstring(value):
    return value.encode() + b"\0"


def string_field(key, value):
    return b"\x01" + cstring(key) + cstring(value)


def int_field(key, value):
    return b"\x02" + cstring(key) + int(value).to_bytes(4, "little")


def object_field(key, contents):
    return b"\x00" + cstring(key) + contents + b"\x08"


steam.STEAM_ROOT = work / "Steam"
steam.STEAM_APPS_DIR = steam.STEAM_ROOT / "steamapps"
steam.STEAM_APPS_DIR.mkdir(parents=True)
(steam.STEAM_APPS_DIR / "appmanifest_620.acf").write_text(
    '"AppState"\n{\n\t"appid"\t\t"620"\n\t"name"\t\t"Portal 2"\n}\n'
)

shortcut_dir = steam.STEAM_ROOT / "userdata/100/config"
shortcut_dir.mkdir(parents=True)
entries = object_field(
    "0",
    string_field("AppName", "Dolphin")
    + int_field("appid", 3929000724)
    + string_field("Exe", "flatpak")
    + string_field("LaunchOptions", "run org.DolphinEmu.dolphin-emu")
    + object_field("tags", string_field("0", "Emulator")),
)
(shortcut_dir / "shortcuts.vdf").write_bytes(object_field("shortcuts", entries) + b"\x08")

bad_dir = steam.STEAM_ROOT / "userdata/200/config"
bad_dir.mkdir(parents=True)
(bad_dir / "shortcuts.vdf").write_bytes(b"\x00shortcuts\0\xff")

games = steam.installed_games()
assert games == [
    {"appid": "3929000724", "name": "Dolphin", "nonSteam": True},
    {"appid": "620", "name": "Portal 2"},
], games
PYEOF

echo "Armada Control Steam discovery tests passed"
