"""EVE Voice glue for autoupdater.py: which files are the user's, where updates come
from, first-run config copy and merging new default commands/settings."""
from __future__ import annotations

import json
import os
import re
import shutil
from typing import Callable, List, Optional

import autoupdater as au

APP_DIR = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "eve-voice"
USER_CONFIG = "commands.json"
DEFAULT_CONFIG = "commands.default.json"
SEEN_DEFAULTS = ".defaults_merged.json"
# never overwritten or deleted by an update
USER_FILES = ("commands.json", "commands.json.*", "gui_state.json", "eve_voice.log",
              "eve_voice.log.*", "model", "model-*", "vosk-model*", SEEN_DEFAULTS, "what_it_sees.png",
              "screenshots", "release", "*.zip.part")
DEFAULT_SOURCE = {"type": "github", "repo": "WeBeAscended/eve-voice"}


def read_version(app_dir: str = APP_DIR) -> str:
    """Version from version.py on disk (not the imported module - it may just have changed)."""
    try:
        with open(os.path.join(app_dir, "version.py"), encoding="utf-8") as f:
            m = re.search(r'^VERSION\s*=\s*"([^"]+)"', f.read(), re.M)
        return m.group(1) if m else "0.0.0"
    except OSError:
        return "0.0.0"


def _version_module_value(app_dir: str, name: str) -> Optional[str]:
    try:
        with open(os.path.join(app_dir, "version.py"), encoding="utf-8") as f:
            m = re.search(rf'^{name}\s*=\s*"([^"]*)"', f.read(), re.M)
        return m.group(1) if m else None
    except OSError:
        return None


def user_settings(app_dir: str = APP_DIR) -> dict:
    try:
        with open(os.path.join(app_dir, USER_CONFIG), encoding="utf-8") as f:
            s = json.load(f).get("settings") or {}
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def ensure_user_config(app_dir: str = APP_DIR) -> bool:
    """First run: copy commands.default.json -> commands.json. True if it was created."""
    user, default = os.path.join(app_dir, USER_CONFIG), os.path.join(app_dir, DEFAULT_CONFIG)
    if os.path.exists(user) or not os.path.exists(default):
        return False
    shutil.copy2(default, user)
    # everything in today's defaults is now 'seen', so later merges only add NEW things
    shutil.copy2(default, os.path.join(app_dir, SEEN_DEFAULTS))
    return True


def migrate_user_config(app_dir: str = APP_DIR) -> List[str]:
    """1.0.1: the yellow-gate command 'select next gate' is now just 'gate' (old phrase kept as an
    alternate). Renames it in the user's file (same click, same settings) so it isn't duplicated."""
    import json
    from collections import OrderedDict
    path = os.path.join(app_dir, USER_CONFIG)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f, object_pairs_hook=OrderedDict)
    except (OSError, ValueError):
        return []
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, dict):
        return []
    if any(isinstance(g, dict) and isinstance(g.get("commands"), dict) and "gate" in g["commands"]
           for g in groups.values()):
        return []
    done = []
    for gname, g in groups.items():
        cmds = g.get("commands") if isinstance(g, dict) else None
        if not isinstance(cmds, dict) or not isinstance(cmds.get("select next gate"), dict) \
                or "find_color" not in cmds["select next gate"]:
            continue
        new = OrderedDict()
        for k, v in cmds.items():
            if k == "select next gate":
                v = OrderedDict(v)
                also = [a for a in (v.get("also") or []) if a not in ("gate", "select next gate")]
                v["also"] = ["select next gate"] + also
                new["gate"] = v
            else:
                new[k] = v
        g["commands"] = new
        done.append(f"groups/{gname}: 'select next gate' -> 'gate'")
    if done:
        shutil.copy2(path, path + ".before-update")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    return done


CORE_COMMANDS = {"warp", "gate", "align", "approach", "orbit", "lock", "dock", "jump", "stop ship", "release"}
# every phrase the app shipped before 1.0.3 - anything else in commands.json is the user's own (left alone)
SHIPPED_PHRASES = set(['afterburner', 'align', 'approach', 'click', 'click undock', 'd scan window', 'dock', 'drones attack', 'drones return', 'fire', 'gate', 'high eight', 'high five', 'high four', 'high one', 'high seven', 'high six', 'high three', 'high two', 'jump', 'keep at range', 'lasers', 'launch drones', 'lock', 'lock target', 'low four', 'low one', 'low three', 'low two', 'map', 'mid eight', 'mid five', 'mid four', 'mid one', 'mid seven', 'mid six', 'mid three', 'mid two', 'middle click', 'next overview tab', 'open cargo', 'orbit', 'point', 'previous overview tab', 'probe scanner', 'release', 'repair', 'right click', 'scan', 'second laser', 'select', 'select next', 'select next gate', 'set way point spot', 'stop ship', 'survey scan', 'teach command', 'test background', 'toggle autopilot', 'unlock target', 'warp', 'web'])
TRIM_ALTERNATES = {"gate": {"next gate", "select gate", "select next gate"}, "release": {"let go"}}
MIGRATION_CORE = "1.0.3-core-commands"


def migrate_core_commands(app_dir: str = APP_DIR) -> List[str]:
    """1.0.3, runs ONCE: only the core travel commands stay on; other shipped commands are
    switched off (kept in the list, keys/holds untouched). The user's own commands stay on.
    Too many active phrases made the recogniser hear 'web', 'next gate' ... by mistake."""
    import json
    from collections import OrderedDict
    path = os.path.join(app_dir, USER_CONFIG)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f, object_pairs_hook=OrderedDict)
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    done_list = data.get("_migrations") if isinstance(data.get("_migrations"), list) else []
    if MIGRATION_CORE in done_list:
        return []
    off, trimmed = [], []
    for g in (data.get("groups") or {}).values():
        cmds = g.get("commands") if isinstance(g, dict) else None
        if not isinstance(cmds, dict):
            continue
        for ph in list(cmds):
            spec = cmds[ph]
            if ph.startswith("_") or ph not in SHIPPED_PHRASES or spec == "":
                continue
            if ph in CORE_COMMANDS:
                if isinstance(spec, dict) and isinstance(spec.get("also"), list) and ph in TRIM_ALTERNATES:
                    keep = [a for a in spec["also"] if a not in TRIM_ALTERNATES[ph]]
                    if keep != spec["also"]:
                        trimmed.append(ph)
                        if keep:
                            spec["also"] = keep
                        else:
                            spec.pop("also")
                continue
            spec = OrderedDict([("key", spec)]) if isinstance(spec, str) else spec
            if isinstance(spec, dict) and spec.get("enabled") is not False:
                spec["enabled"] = False
                off.append(ph)
            cmds[ph] = spec
    s = data.get("settings")
    bumped = isinstance(s, dict) and s.get("min_confidence") in (0.55, None)
    if bumped:
        s["min_confidence"] = 0.7
    data["_migrations"] = done_list + [MIGRATION_CORE]
    shutil.copy2(path, path + ".before-update")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    out = []
    if off:
        out.append(f"switched OFF {len(off)} non-core commands (turn them back on in Commands): " + ", ".join(off))
    if trimmed:
        out.append("removed extra phrasings from: " + ", ".join(trimmed))
    if bumped:
        out.append("'how sure before acting' raised 55% -> 70%")
    return out


def merge_defaults(app_dir: str = APP_DIR, log: Callable[[str], None] = lambda m: None) -> List[str]:
    for m in migrate_user_config(app_dir):
        log("updated commands.json: " + m)
    for m in migrate_core_commands(app_dir):
        log("updated commands.json: " + m)
    added = au.merge_new_defaults(os.path.join(app_dir, USER_CONFIG), os.path.join(app_dir, DEFAULT_CONFIG),
                                  os.path.join(app_dir, SEEN_DEFAULTS))
    if added:
        log("added new defaults to commands.json (your values unchanged): " + ", ".join(added))
    return added


def make_config(app_dir: str = APP_DIR, log: Optional[Callable[[str], None]] = None, **kw) -> au.UpdaterConfig:
    s = user_settings(app_dir)
    source = s.get("update_source") if isinstance(s.get("update_source"), dict) else DEFAULT_SOURCE
    url = s.get("update_url") or _version_module_value(app_dir, "UPDATE_URL")

    def on_applied(cfg, new_version):
        merge_defaults(app_dir, lambda m: au._log(cfg, m))
    return au.UpdaterConfig(
        app_name=APP_NAME, app_dir=app_dir, current_version=read_version(app_dir),
        public_key=_version_module_value(app_dir, "UPDATE_PUBLIC_KEY") or "",
        source=source, fallback_url=url or None, preserve=USER_FILES, on_applied=on_applied,
        log=log, **kw)


def auto_update_enabled(app_dir: str = APP_DIR) -> bool:
    return user_settings(app_dir).get("auto_update", True) is not False
