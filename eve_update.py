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


def merge_defaults(app_dir: str = APP_DIR, log: Callable[[str], None] = lambda m: None) -> List[str]:
    for m in migrate_user_config(app_dir):
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
