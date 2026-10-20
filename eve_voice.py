#!/usr/bin/env python3
"""
EVE Voice - offline voice commands for EVE Online (v1).

RULES (enforced in code, see README "Rules / EULA compliance"):
  1. 1:1 - each spoken word/phrase triggers exactly ONE input: one key (or one hotkey
     chord EVE treats as a single shortcut) OR one single mouse click. Never both, never chained.
  2. Pure input emulator - listens to the mic, sends OS-level input to the active window.
     No memory reading, no hooks into the game, no log scraping. The ONE exception is the
     opt-in, off-by-default "select next gate" (gate_finder.py): when you say it, it looks
     at your calibrated Overview box once to find the yellow row, then clicks it once.
  3. No timed delays between inputs (the only sleep in the send path is how long ONE key is held).

Usage:
    python eve_voice.py                 # run with commands.json
    python eve_voice.py --list-devices  # list microphones
    python eve_voice.py --dry-run       # recognise but don't press keys
    python eve_voice.py --text          # type phrases instead of speaking (test without a mic)
    python eve_voice.py --calibrate     # setup: drag a box over a click spot (e.g. next waypoint)
    python eve_voice.py --where         # show mouse coordinates (fallback for mouse_at)
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import queue
import re
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

APP_DIR = os.path.dirname(os.path.abspath(__file__))
VALID_MODES = ("always", "push_to_talk", "wake_word")
MODIFIERS = ("ctrl", "alt", "shift")

# Keys we know how to send. Names follow pydirectinput's naming.
_NAMED_KEYS = {
    "space", "enter", "esc", "tab", "backspace", "delete", "insert", "home", "end",
    "pageup", "pagedown", "up", "down", "left", "right", "capslock", "scrolllock",
    "numlock", "printscreen", "pause",
    "num0", "num1", "num2", "num3", "num4", "num5", "num6", "num7", "num8", "num9",
    "-", "=", "[", "]", ";", "'", ",", ".", "/", "\\", "`",
} | {f"f{i}" for i in range(1, 13)} | set(MODIFIERS)
_KEY_ALIASES = {
    "control": "ctrl", "escape": "esc", "return": "enter", "del": "delete",
    "pgup": "pageup", "pgdn": "pagedown", "caps lock": "capslock", "scroll lock": "scrolllock",
    "page up": "pageup", "page down": "pagedown",
}


# --------------------------------------------------------------------------- config
class ConfigError(Exception):
    pass


def normalize_phrase(text: str) -> str:
    """Lowercase, strip punctuation, collapse spaces - matches Vosk output format."""
    text = text.lower().replace("-", " ")
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    return " ".join(text.split())


def parse_key(spec: str) -> Tuple[List[str], str]:
    """'ctrl+f1' -> (['ctrl'], 'f1'); 'ctrl' -> ([], 'ctrl'); 'q' -> ([], 'q')."""
    if not isinstance(spec, str) or not spec.strip():
        raise ConfigError("empty key")
    parts = [p.strip().lower() for p in spec.split("+")]
    if any(p == "" for p in parts):
        raise ConfigError(f"bad key combo '{spec}'")
    parts = [_KEY_ALIASES.get(p, p) for p in parts]
    *mods, key = parts
    for m in mods:
        if m not in MODIFIERS:
            raise ConfigError(f"'{m}' in '{spec}' is not a modifier (use ctrl/alt/shift)")
    if len(set(mods)) != len(mods):
        raise ConfigError(f"duplicate modifier in '{spec}'")
    if not (len(key) == 1 and key.isalnum()) and key not in _NAMED_KEYS:
        raise ConfigError(f"unknown key '{key}' in '{spec}'")
    return mods, key


MOUSE_BUTTONS = ("left", "right", "middle")   # single clicks only (no double click: that's two clicks)
_TARGET_FIELDS = ("mouse_at", "mouse_box", "mouse_spot", "find_color")
# Per-command options. They tune how the ONE action is triggered/sent - they never add an action.
_OPTION_FIELDS = ("also", "hold", "cooldown", "hold_seconds", "hold_until", "hold_max")
HOLD_TIMED_MAX = 10.0       # "hold this key for N seconds" cap (taps stay capped at 0.5 s)
HOLD_SAFETY_MAX = 30.0      # hard cap for "hold until I click / say release"
HOLD_DEFAULT_MAX = {"click": 10.0, "release": 30.0}
RULE_1TO1 = "1:1 rule - one voice command = exactly ONE keyboard OR mouse action, no chaining"


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def box_center(box) -> Tuple[int, int]:
    """[left, top, right, bottom] -> (x, y) centre (works for drags in any direction)."""
    l, t, r, b = box
    l, r = min(l, r), max(l, r)
    t, b = min(t, b), max(t, b)
    return (l + r) // 2, (t + b) // 2


def normalize_box(x1, y1, x2, y2) -> List[int]:
    return [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]


def resolve_point(value, what: str) -> Tuple[int, int]:
    """Accepts [x, y] or [left, top, right, bottom]; returns the click point.
    Negative values are allowed (monitors left of / above the main one)."""
    if not isinstance(value, (list, tuple)) or len(value) not in (2, 4) or not all(_is_int(v) for v in value):
        raise ConfigError(f"{what} must be [x, y] or [left, top, right, bottom] whole numbers, got {value!r}")
    if len(value) == 2:
        return value[0], value[1]
    return box_center(value)


def color_target_problem(t) -> Optional[str]:
    """None if a screen colour target is fully set up, else what's missing."""
    if not isinstance(t, dict):
        return "isn't defined in \"color_targets\""
    box = t.get("box")
    if not (isinstance(box, (list, tuple)) and len(box) == 4 and all(_is_int(v) for v in box)
            and box[2] > box[0] and box[3] > box[1]):
        return "needs its Overview area set (app: Next gate tab > Set Overview area)"
    rgb = t.get("rgb")
    if not (isinstance(rgb, (list, tuple)) and len(rgb) == 3 and all(_is_int(v) and 0 <= v <= 255 for v in rgb)):
        return "needs a colour"
    if not t.get("sampled"):
        return "needs its colour picked from your screen (app: Next gate tab > Pick the yellow colour)"
    return None


class Command:
    """One spoken phrase -> exactly ONE input action (the 1:1 rule).

    Forms accepted in commands.json:
      "q" / "ctrl+f1"                         ONE key press, or ONE hotkey chord EVE treats as one shortcut
      {"key": "ctrl+f1"}                      same
      {"mouse": "left"|"right"|"middle"}      ONE single click at the current cursor
      {"mouse": "left", "mouse_spot": "name"} ONE single click at a saved spot (box centre)   (allow_mouse_at)
      {"mouse": "left", "mouse_box": [l,t,r,b]} / {"mouse": "left", "mouse_at": [x, y]}  same, own spot
      {"calibrate": "spot name"}              setup only: opens the box-select overlay, sends NO game input
      {"key": "q", "hold_seconds": 3}         ONE key held down 3 s (keyDown now, keyUp later; max 10 s)
      {"key": "ctrl", "hold_until": "click"}  ONE keyDown; keyUp right after YOUR next left click
                                              (hold-and-click: lock, approach, orbit...). Never clicks.
      {"key": "q", "hold_until": "release"}   ONE keyDown; keyUp when you say 'release' (safety max)
      {"release": true}                       the keyUp of the key that's being held (no new input)

    Rejected (1:1 rule): "steps", key+mouse together, double click, modifier+click.
    """

    def __init__(self, phrase: str, spec, group: str, allow_mouse_at: bool = False,
                 spots: Optional[dict] = None, color_targets: Optional[dict] = None,
                 allow_screen_read: bool = False):
        spots = spots or {}
        self.phrase, self.group = phrase, group
        self.kind = "key"
        self.key: Optional[str] = None
        self.modifiers: List[str] = []
        self.mouse: Optional[str] = None
        self.target: Optional[Tuple[int, int]] = None  # None = current cursor position
        self.spot: Optional[str] = None
        self.color_target: Optional[str] = None   # opt-in: click position found on screen when said
        self.alternates: List[str] = []           # other phrasings for the SAME single action
        self.hold: Optional[float] = None         # per-command key hold time (None = global)
        self.cooldown: Optional[float] = None     # per-command repeat cooldown (None = global)
        self.hold_mode = "tap"                    # tap | timed | click | release
        self.hold_seconds: Optional[float] = None  # timed: how long; click/release: safety max
        if isinstance(spec, dict):
            spec = self._options(spec)
        self._parse(spec, allow_mouse_at, spots, color_targets or {}, allow_screen_read)
        if self.hold is not None and self.kind != "key":
            raise ConfigError("'hold' only applies to key commands")
        if self.hold_mode != "tap" and self.kind != "key":
            raise ConfigError("holding only applies to key commands (a click can't be held)")
        self.key_spec = self.describe()

    def _options(self, spec: dict) -> dict:
        also = spec.get("also") or []
        if isinstance(also, str):
            also = [also]
        if not isinstance(also, list) or not all(isinstance(a, str) for a in also):
            raise ConfigError("'also' must be a list of phrases")
        seen = {self.phrase}
        for a in also:
            n = normalize_phrase(a)
            if n and n not in seen:
                seen.add(n)
                self.alternates.append(n)
        for name, lo, hi in (("hold", 0.0, 0.5), ("cooldown", 0.0, 30.0)):
            v = spec.get(name)
            if v is None or v == "":
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise ConfigError(f"'{name}' must be a number of seconds")
            if not lo <= v <= hi:
                raise ConfigError(f"'{name}' must be between {lo:g} and {hi:g} seconds")
            setattr(self, name, v)
        self._hold_options(spec)
        return {k: v for k, v in spec.items() if k not in _OPTION_FIELDS}

    def _hold_options(self, spec: dict) -> None:
        def num(name, lo, hi):
            try:
                v = float(spec[name])
            except (TypeError, ValueError):
                raise ConfigError(f"'{name}' must be a number of seconds")
            if not lo <= v <= hi:
                raise ConfigError(f"'{name}' must be between {lo:g} and {hi:g} seconds")
            return v
        has_t = spec.get("hold_seconds") not in (None, "")
        until = spec.get("hold_until")
        if until in (None, "", False, "tap"):
            until = None
        if has_t and until:
            raise ConfigError("use 'hold_seconds' OR 'hold_until', not both")
        if (has_t or until) and spec.get("hold") not in (None, ""):
            raise ConfigError("'hold' is the tap length - leave it out for a held key")
        if has_t:
            self.hold_mode, self.hold_seconds = "timed", num("hold_seconds", 0.1, HOLD_TIMED_MAX)
        elif until:
            until = str(until).strip().lower()
            if until not in ("click", "release"):
                raise ConfigError("'hold_until' must be 'click' or 'release'")
            self.hold_mode = until
            self.hold_seconds = (num("hold_max", 0.5, HOLD_SAFETY_MAX)
                                 if spec.get("hold_max") not in (None, "") else HOLD_DEFAULT_MAX[until])
        elif spec.get("hold_max") not in (None, ""):
            raise ConfigError("'hold_max' only goes with 'hold_until'")

    @property
    def mouse_at(self):  # backward-compatible name
        return self.target

    def _parse(self, spec, allow_mouse_at, spots, color_targets=None, allow_screen_read=False):
        if isinstance(spec, str):
            self.modifiers, self.key = parse_key(spec)
            return
        if not isinstance(spec, dict):
            raise ConfigError(f"unsupported action {spec!r}")
        if "steps" in spec:
            raise ConfigError(f"'steps' combos are not allowed ({RULE_1TO1})")
        present = [k for k in ("key", "mouse", "calibrate", "teach", "release") if k in spec]
        if "key" in spec and "mouse" in spec:
            raise ConfigError(f"has both 'key' and 'mouse' ({RULE_1TO1}) - split it into two phrases")
        if len(present) != 1:
            if not present:
                raise ConfigError("needs a 'key' or 'mouse' field")
            raise ConfigError(f"use only one of {present} ({RULE_1TO1})")
        if "key" in spec:
            self.modifiers, self.key = parse_key(spec["key"])
            return
        if "calibrate" in spec:
            self.kind, self.spot = "calibrate", str(spec["calibrate"])
            return
        if "teach" in spec:   # setup only: opens the app's Teach window, sends NO game input
            self.kind = "teach"
            return
        if "release" in spec:  # the keyUp of the key being held - never a new input
            self.kind = "release"
            return
        self.kind = "mouse"
        self.mouse = str(spec.get("mouse", "")).strip().lower()
        if self.mouse in ("double", "double click"):
            raise ConfigError(f"double click is two clicks ({RULE_1TO1}) - use a single 'left' click")
        if self.mouse not in MOUSE_BUTTONS:
            raise ConfigError(f"mouse must be one of {MOUSE_BUTTONS}, got {spec.get('mouse')!r}")
        if spec.get("modifiers"):
            raise ConfigError(f"modifier+click mixes keyboard and mouse ({RULE_1TO1}) - bind the action "
                              "to a key in EVE (Esc > Shortcuts) instead")
        fields = [f for f in _TARGET_FIELDS if f in spec]
        if len(fields) > 1:
            raise ConfigError(f"use only one of {fields}")
        if not fields:
            return
        f = fields[0]
        if f == "find_color":
            name = str(spec[f])
            if not allow_screen_read:
                raise ConfigError("finds its click on screen, which is off (opt-in: settings.allow_screen_read)")
            t = (color_targets or {}).get(name)
            problem = color_target_problem(t)
            if problem:
                raise ConfigError(f"screen target '{name}' {problem}")
            self.color_target = name
            return
        if f == "mouse_spot":
            name = str(spec[f])
            if name not in spots:
                raise ConfigError(f"spot '{name}' isn't defined in \"spots\"")
            value, self.spot = spots[name], name
            if value is None:
                raise ConfigError(f"spot '{name}' not set yet - set it in the app (Settings > Saved spots) or run 'run_cli.bat --calibrate'")
        else:
            value = spec[f]
            if value is None:
                raise ConfigError(f"{f} not set yet - run 'run_cli.bat --calibrate' (or use --where)")
        if not allow_mouse_at:
            raise ConfigError(f"uses a saved screen position ({f}) but settings.allow_mouse_at is false "
                              "(--calibrate turns it on for you)")
        self.target = resolve_point(value, f)

    def describe(self) -> str:
        if self.kind == "key":
            k = "+".join(self.modifiers + [self.key])
            if self.hold_mode == "timed":
                return f"hold {k} for {self.hold_seconds:g}s"
            if self.hold_mode == "click":
                return f"hold {k} until you click (max {self.hold_seconds:g}s)"
            if self.hold_mode == "release":
                return f"hold {k} until 'release' (max {self.hold_seconds:g}s)"
            return k
        if self.kind == "release":
            return "release the held key"
        if self.kind == "calibrate":
            return f"calibrate spot '{self.spot}' (setup, no game input)"
        if self.kind == "teach":
            return "open Teach (setup, no game input)"
        d = f"{self.mouse} click"
        if self.color_target:
            return d + f" on {self.color_target} (found on screen)"
        if self.target:
            d += f" @ {self.target[0]},{self.target[1]}"
        return d

    def __repr__(self):
        return f"Command({self.phrase!r} -> {self.key_spec!r})"


DEFAULT_SETTINGS = {
    # updates (read by launcher.py / eve_update.py; the voice engine ignores them)
    "auto_update": True,
    "update_source": {"type": "github", "repo": "WeBeAscended/eve-voice"},
    "update_url": None,
    "mode": "always",
    "mic_device": None,
    "push_to_talk_key": "scroll lock",
    "wake_word": "computer",
    "wake_window_seconds": 4.0,
    "require_eve_focus": True,
    "window_title_contains": "EVE",
    "tts_confirm": False,
    "print_confirm": True,
    "cooldown_seconds": 0.75,
    "key_hold_seconds": 0.05,
    "allow_mouse_at": False,
    "model_path": "model",
    # recognition
    "min_confidence": 0.55,          # how sure (0..1) the recogniser must be before acting
    "noise_gate": 0.04,              # ignore audio quieter than this peak level (0 = off)
    "endpoint_silence_seconds": 0.35,  # act this soon after you stop talking (0 = recogniser default)
    "log_unmatched": True,           # show 'heard X - not a command' lines
    "fast_response": True,           # act on partial results as soon as a whole command is heard
    # opt-in, off by default: let "select next gate" look at the Overview box when said
    "allow_screen_read": False,
}


TARGET_MODES = ("foreground", "background_window")


class Config:
    def __init__(self, settings: dict, commands: Dict[str, Command], warnings: List[str],
                 spots: Optional[dict] = None, target: Optional[dict] = None):
        self.spots = spots or {}
        self.aliases: Dict[str, str] = {}        # alternate phrase -> main phrase
        self.color_targets: Dict[str, dict] = {}
        self.target = target or {"mode": "foreground", "window_title": ""}
        self.settings = settings
        self.commands = commands
        self.warnings = warnings

    def lookup(self, text: str) -> Optional["Command"]:
        """Main phrase or alternate -> its Command (one action)."""
        cmd = self.commands.get(text)
        if cmd is None and text in self.aliases:
            cmd = self.commands.get(self.aliases[text])
        return cmd

    def segment(self, text: str) -> Optional[List[str]]:
        """'warp gate' -> ['warp', 'gate']: split one utterance into known phrases, in spoken
        order, longest phrase first (backtracking if needed). None if any word isn't part of a
        command. Each piece is its own command = its own ONE input; nothing is added between."""
        words = normalize_phrase(text).split()
        if not words:
            return None
        longest = max((len(p.split()) for p in self.all_phrases()), default=1)

        def go(i):
            if i == len(words):
                return []
            for n in range(min(longest, len(words) - i), 0, -1):
                ph = " ".join(words[i:i + n])
                if self.lookup(ph) is not None:
                    rest = go(i + n)
                    if rest is not None:
                        return [ph] + rest
            return None
        return go(0)

    def could_continue(self, text: str) -> bool:
        """True if some command phrase starts with `text` + more words ('lock' -> 'lock target')."""
        t = normalize_phrase(text) + " "
        return any(p.startswith(t) for p in self.all_phrases())

    def all_phrases(self) -> List[str]:
        return sorted(set(self.commands) | set(self.aliases))

    def closest(self, text: str) -> Optional[str]:
        import difflib
        m = difflib.get_close_matches(normalize_phrase(text), self.all_phrases(), n=1, cutoff=0.5)
        return m[0] if m else None

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        try:
            return self.settings[name]
        except KeyError:
            raise AttributeError(name)


def load_config_dict(data: dict) -> Config:
    warnings: List[str] = []
    raw = {k: v for k, v in (data.get("settings") or {}).items() if not k.startswith("_")}
    unknown = set(raw) - set(DEFAULT_SETTINGS)
    for k in sorted(unknown):
        if k in ("step_delay_seconds", "restore_cursor"):
            warnings.append(f"setting '{k}' was removed ({RULE_1TO1}) - ignored")
        else:
            warnings.append(f"unknown setting '{k}' ignored")
    settings = dict(DEFAULT_SETTINGS)
    settings.update({k: v for k, v in raw.items() if k in DEFAULT_SETTINGS})

    if settings["mode"] not in VALID_MODES:
        raise ConfigError(f"mode must be one of {VALID_MODES}, got {settings['mode']!r}")
    settings["wake_word"] = normalize_phrase(settings.get("wake_word") or "")
    if settings["mode"] == "wake_word" and not settings["wake_word"]:
        raise ConfigError("mode is 'wake_word' but wake_word is empty")
    if settings["mode"] == "push_to_talk" and not settings.get("push_to_talk_key"):
        raise ConfigError("mode is 'push_to_talk' but push_to_talk_key is empty")
    for num in ("wake_window_seconds", "cooldown_seconds", "key_hold_seconds", "min_confidence",
                "noise_gate", "endpoint_silence_seconds"):
        try:
            settings[num] = float(settings[num])
        except (TypeError, ValueError):
            raise ConfigError(f"{num} must be a number")
    if not 0 <= settings["key_hold_seconds"] <= 0.5:
        raise ConfigError("key_hold_seconds must be between 0 and 0.5 (it's how long ONE key is held)")
    for num, lo, hi in (("min_confidence", 0.0, 1.0), ("noise_gate", 0.0, 1.0), ("endpoint_silence_seconds", 0.0, 3.0)):
        if not lo <= settings[num] <= hi:
            raise ConfigError(f"{num} must be between {lo:g} and {hi:g}")
    color_targets = {k: v for k, v in (data.get("color_targets") or {}).items() if not k.startswith("_")}
    spots = {k: v for k, v in (data.get("spots") or {}).items() if not k.startswith("_")}

    traw = data.get("target") or {}
    if not isinstance(traw, dict):
        raise ConfigError("'target' must be an object")
    target = {"mode": str(traw.get("mode") or "foreground").strip().lower(),
              "window_title": str(traw.get("window_title") or "").strip()}
    if target["mode"] not in TARGET_MODES:
        raise ConfigError(f"target.mode must be one of {TARGET_MODES}, got {target['mode']!r}")
    background = target["mode"] == "background_window"
    if background and not target["window_title"]:
        raise ConfigError("target.mode is 'background_window' but target.window_title is empty "
                          "(app: Clients tab > Refresh list, or 'run_cli.bat --list-windows' and copy the exact title, e.g. \"EVE - Character Name\")")
    bg_skipped: List[str] = []

    commands: Dict[str, Command] = {}
    groups = data.get("groups") or {}
    if not isinstance(groups, dict):
        raise ConfigError("'groups' must be an object")
    for gname, group in groups.items():
        if gname.startswith("_") or not isinstance(group, dict):
            continue
        if group.get("enabled", True) is False:
            continue
        for phrase, key_spec in (group.get("commands") or {}).items():
            if phrase.startswith("_"):
                continue
            norm = normalize_phrase(phrase)
            if not norm:
                warnings.append(f"[{gname}] empty phrase skipped")
                continue
            if re.search(r"\d", norm):
                warnings.append(f"[{gname}] '{phrase}': spell numbers as words ('high one', not 'high 1') "
                                "- the recogniser outputs words")
            if not key_spec or (isinstance(key_spec, dict) and not any(
                    key_spec.get(k) for k in ("key", "mouse", "calibrate", "teach", "release")) and "steps" not in key_spec):
                continue  # placeholder, user hasn't set a key yet
            if settings["wake_word"] and settings["mode"] == "wake_word" and norm == settings["wake_word"]:
                warnings.append(f"[{gname}] phrase '{phrase}' equals the wake word - skipped")
                continue
            if isinstance(key_spec, dict) and "find_color" in key_spec and not settings["allow_screen_read"]:
                continue  # opt-in screen-finding command while screen reading is off: inactive
            try:
                cmd = Command(norm, key_spec, gname, allow_mouse_at=bool(settings["allow_mouse_at"]),
                              spots=spots, color_targets=color_targets,
                              allow_screen_read=bool(settings["allow_screen_read"]))
            except ConfigError as e:
                warnings.append(f"[{gname}] '{phrase}': {e} - skipped")
                continue
            if background and cmd.kind == "key" and cmd.modifiers:
                bg_skipped.append(f"{norm} ({cmd.key_spec}: chord)")
                continue
            if background and cmd.kind == "key" and cmd.hold_mode == "click":
                bg_skipped.append(f"{norm} (hold until click: your click can't reach a background window)")
                continue
            if background and cmd.kind == "key" and bg_vk(cmd.key) is None:
                bg_skipped.append(f"{norm} ({cmd.key}: key not supported in background mode)")
                continue
            if background and cmd.kind == "mouse" and cmd.target is None:
                bg_skipped.append(f"{norm} ({'screen-found click' if cmd.color_target else 'click at cursor'})")
                continue
            if norm in commands:
                prev = commands[norm]
                warnings.append(f"phrase '{norm}' defined twice ([{prev.group}] {prev.key_spec} "
                                f"and [{gname}] {cmd.key_spec}) - using the later one")
            commands[norm] = cmd
    if bg_skipped:
        warnings.append(f"background_window mode (experimental) supports single keys and saved-spot clicks "
                        f"only - skipped {len(bg_skipped)}: " + ", ".join(bg_skipped))
    if not commands:
        raise ConfigError("no usable commands in config")
    # alternate phrasings ("also"): each maps to its command's ONE action
    aliases: Dict[str, str] = {}
    for norm, cmd in commands.items():
        for alt in cmd.alternates:
            if alt in commands:
                warnings.append(f"'{norm}': alternate '{alt}' is already its own command - ignored")
            elif alt in aliases and aliases[alt] != norm:
                warnings.append(f"'{norm}': alternate '{alt}' is already an alternate of '{aliases[alt]}' - ignored")
            elif settings["mode"] == "wake_word" and alt == settings["wake_word"]:
                warnings.append(f"'{norm}': alternate '{alt}' equals the wake word - ignored")
            else:
                aliases[alt] = norm
    cfg = Config(settings, commands, warnings, spots, target)
    cfg.aliases = aliases
    cfg.color_targets = color_targets
    return cfg


def load_config(path: str) -> Config:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path} is not valid JSON (line {e.lineno}, col {e.colno}): {e.msg}")
    return load_config_dict(data)


def build_grammar(cfg: Config) -> List[str]:
    """Phrase list for Vosk. '[unk]' soaks up everything else so random speech
    doesn't get forced onto a command."""
    phrases = cfg.all_phrases()   # main phrases + alternates
    grammar = list(phrases)
    if cfg.mode == "wake_word":
        ww = cfg.wake_word
        grammar = [ww] + [f"{ww} {p}" for p in phrases] + phrases
    grammar.append("[unk]")
    return grammar


# --------------------------------------------------------------------------- trigger logic
class TriggerGate:
    """Decides whether recognised text should become a command, per mode.

    always       - every utterance is considered.
    push_to_talk - audio is only fed to the recogniser while the key is held
                   (see audio_enabled()); utterances are accepted only from that.
    wake_word    - 'computer warp' fires immediately; 'computer' alone opens a
                   window of wake_window_seconds for ONE command.
    """

    def __init__(self, cfg: Config, ptt_pressed: Callable[[], bool] = lambda: False,
                 clock: Callable[[], float] = time.monotonic):
        self.mode = cfg.mode
        self.wake_word = cfg.wake_word
        self.window = cfg.wake_window_seconds
        self.ptt_pressed = ptt_pressed
        self.clock = clock
        self.wake_until = 0.0

    def audio_enabled(self) -> bool:
        if self.mode == "push_to_talk":
            return bool(self.ptt_pressed())
        return True

    def wake_active(self) -> bool:
        return self.clock() < self.wake_until

    def filter(self, text: str) -> Optional[str]:
        """Return the command text to look up, or None to ignore.
        Returns the special value '' when the wake word opened a window."""
        text = normalize_phrase(text)
        if not text:
            return None
        if self.mode != "wake_word":
            return text
        ww = self.wake_word
        if text == ww:
            self.wake_until = self.clock() + self.window
            return ""
        if text.startswith(ww + " "):
            self.wake_until = 0.0
            return text[len(ww) + 1:]
        if self.wake_active():
            self.wake_until = 0.0  # one command per wake
            return text
        return None


# --------------------------------------------------------------------------- key senders
class KeySender:
    name = "base"

    def press(self, modifiers: List[str], key: str, hold: float) -> None:
        raise NotImplementedError

    # A held key is still ONE input: key_down now, the matching key_up later (HoldManager).
    def key_down(self, modifiers: List[str], key: str) -> None:
        raise NotImplementedError

    def key_up(self, modifiers: List[str], key: str) -> None:
        raise NotImplementedError


class PyDirectInputSender(KeySender):
    name = "pydirectinput"

    def __init__(self):
        import pydirectinput  # noqa
        pydirectinput.PAUSE = 0
        pydirectinput.FAILSAFE = False
        self.p = pydirectinput

    def press(self, modifiers, key, hold):
        for m in modifiers:
            self.p.keyDown(m)
        try:
            self.p.keyDown(key)
            time.sleep(hold)  # hold time of this ONE press (keyDown..keyUp), not a gap between actions
            self.p.keyUp(key)
        finally:
            for m in reversed(modifiers):
                self.p.keyUp(m)

    def key_down(self, modifiers, key):
        for m in modifiers:
            self.p.keyDown(m)
        self.p.keyDown(key)

    def key_up(self, modifiers, key):
        try:
            self.p.keyUp(key)
        finally:
            for m in reversed(modifiers):
                self.p.keyUp(m)


class KeyboardLibSender(KeySender):
    name = "keyboard"
    _MAP = {"pageup": "page up", "pagedown": "page down", "capslock": "caps lock",
            "scrolllock": "scroll lock", "numlock": "num lock", "printscreen": "print screen"}

    def __init__(self):
        import keyboard  # noqa
        self.k = keyboard

    def _n(self, key):
        if key.startswith("num") and key[3:].isdigit():
            return key[3:]  # best effort
        return self._MAP.get(key, key)

    def press(self, modifiers, key, hold):
        for m in modifiers:
            self.k.press(m)
        try:
            self.k.press(self._n(key))
            time.sleep(hold)  # hold time of this ONE press (keyDown..keyUp), not a gap between actions
            self.k.release(self._n(key))
        finally:
            for m in reversed(modifiers):
                self.k.release(m)

    def key_down(self, modifiers, key):
        for m in modifiers:
            self.k.press(m)
        self.k.press(self._n(key))

    def key_up(self, modifiers, key):
        try:
            self.k.release(self._n(key))
        finally:
            for m in reversed(modifiers):
                self.k.release(m)


class PynputSender(KeySender):
    name = "pynput"

    def __init__(self):
        from pynput.keyboard import Controller, Key, KeyCode  # noqa
        self.c, self.Key, self.KeyCode = Controller(), Key, KeyCode

    def _k(self, key):
        K = self.Key
        special = {"ctrl": K.ctrl, "alt": K.alt, "shift": K.shift, "space": K.space,
                   "enter": K.enter, "esc": K.esc, "tab": K.tab, "backspace": K.backspace,
                   "delete": K.delete, "home": K.home, "end": K.end, "pageup": K.page_up,
                   "pagedown": K.page_down, "up": K.up, "down": K.down, "left": K.left,
                   "right": K.right, "capslock": K.caps_lock}
        if key in special:
            return special[key]
        if re.fullmatch(r"f\d{1,2}", key):
            return getattr(K, key)
        return self.KeyCode.from_char(key)

    def press(self, modifiers, key, hold):
        mods = [self._k(m) for m in modifiers]
        for m in mods:
            self.c.press(m)
        try:
            self.c.press(self._k(key))
            time.sleep(hold)  # hold time of this ONE press (keyDown..keyUp), not a gap between actions
            self.c.release(self._k(key))
        finally:
            for m in reversed(mods):
                self.c.release(m)

    def key_down(self, modifiers, key):
        for m in modifiers:
            self.c.press(self._k(m))
        self.c.press(self._k(key))

    def key_up(self, modifiers, key):
        try:
            self.c.release(self._k(key))
        finally:
            for m in reversed(modifiers):
                self.c.release(self._k(m))


def make_sender() -> KeySender:
    errors = []
    for cls in (PyDirectInputSender, KeyboardLibSender, PynputSender):
        try:
            return cls()
        except Exception as e:  # ImportError or platform errors
            errors.append(f"{cls.name}: {e}")
    raise RuntimeError("No key-sending library worked:\n  " + "\n  ".join(errors))


# --------------------------------------------------------------------------- mouse senders
class MouseSender:
    name = "base"

    def click(self, button: str, at: Optional[Tuple[int, int]] = None) -> None:
        """Exactly ONE single click of `button`, at the current cursor, or - if `at`
        is given - ONE click-at-location (the OS moves the pointer there as part of
        that same click, like clicking a spot by hand). No modifiers, no double
        clicks, and the cursor is NOT moved back afterwards."""
        raise NotImplementedError


class PyDirectInputMouse(MouseSender):
    name = "pydirectinput"

    def __init__(self):
        import pydirectinput  # noqa
        pydirectinput.PAUSE = 0
        pydirectinput.FAILSAFE = False
        self.p = pydirectinput

    def click(self, button, at=None):
        if at is None:
            self.p.click(button=button)                      # one click at the cursor
        else:
            self.p.click(x=at[0], y=at[1], button=button)    # one click at a location


class PynputMouse(MouseSender):
    name = "pynput"

    def __init__(self):
        from pynput.mouse import Controller, Button  # noqa
        self.ms, self.Button = Controller(), Button

    def click(self, button, at=None):
        btn = {"left": self.Button.left, "right": self.Button.right, "middle": self.Button.middle}[button]
        if at is not None:
            self.ms.position = at   # pointer placement for this one click-at-location
        self.ms.click(btn, 1)       # count=1: a single click


def make_mouse_sender() -> MouseSender:
    errors = []
    for cls in (PyDirectInputMouse, PynputMouse):
        try:
            return cls()
        except Exception as e:
            errors.append(f"{cls.name}: {e}")
    raise RuntimeError("No mouse library worked:\n  " + "\n  ".join(errors))


class PrintSender(KeySender, MouseSender):
    name = "dry-run"

    def press(self, modifiers, key, hold):
        print(f"    (dry run) would press {'+'.join(modifiers + [key])}")

    def key_down(self, modifiers, key):
        print(f"    (dry run) would hold down {'+'.join(modifiers + [key])}")

    def key_up(self, modifiers, key):
        print(f"    (dry run) would let go of {'+'.join(modifiers + [key])}")

    def click(self, button, at=None):
        where = f"at {at[0]},{at[1]}" if at else "at cursor"
        print(f"    (dry run) would {button} click {where}")


# --------------------------------------------------------------------------- focus check
def foreground_window_title() -> Optional[str]:
    """Windows only. Returns None if it can't be determined."""
    if sys.platform != "win32":
        return None
    import ctypes
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def running_as_admin() -> Optional[bool]:
    """Is THIS program elevated? (Windows blocks input from a normal program to an elevated
    one.) None if unknown. We can't safely check EVE itself, so the app only advises."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return None


def make_dpi_aware() -> None:
    """So saved coordinates, --where and clicks all use real screen pixels
    even with Windows display scaling (125%, 150%...)."""
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def cursor_position() -> Optional[Tuple[int, int]]:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        pt = wintypes.POINT()
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            return pt.x, pt.y
    try:
        from pynput.mouse import Controller
        x, y = Controller().position
        return int(x), int(y)
    except Exception:
        return None


def run_where(pos_fn=cursor_position, title_fn=None, sleep=time.sleep, count=None,
              log=lambda m: print(m, flush=True)):
    """Print the cursor position every second (for filling in mouse_at)."""
    title_fn = title_fn or foreground_window_title
    log("Move the mouse over the spot you want; copy the [x, y] into mouse_at. Ctrl+C to stop.")
    n = 0
    while count is None or n < count:
        pos = pos_fn()
        title = title_fn() or ""
        log(f"  mouse_at: [{pos[0]}, {pos[1]}]   window: {title}" if pos else "  (can't read cursor position)")
        n += 1
        if count is None or n < count:
            sleep(1.0)  # display refresh only - sends no input


# --------------------------------------------------------------------------- background target (EXPERIMENTAL)
# Sends ONE posted key/click message pair to ONE specific window (by exact title),
# without focusing it or touching the user's cursor. Off by default.
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
_WM_BUTTON = {"left": (0x0201, 0x0202, 0x0001),     # WM_LBUTTONDOWN/UP, MK_LBUTTON
              "right": (0x0204, 0x0205, 0x0002),    # WM_RBUTTONDOWN/UP, MK_RBUTTON
              "middle": (0x0207, 0x0208, 0x0010)}   # WM_MBUTTONDOWN/UP, MK_MBUTTON
_BG_VK = {"space": 0x20, "enter": 0x0D, "esc": 0x1B, "tab": 0x09, "backspace": 0x08,
          "delete": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
          "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "capslock": 0x14, "scrolllock": 0x91,
          "numlock": 0x90, "pause": 0x13, "printscreen": 0x2C,
          "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE,
          "/": 0xBF, "\\": 0xDC, "`": 0xC0}
_BG_VK.update({f"f{i}": 0x6F + i for i in range(1, 13)})
_BG_VK.update({f"num{i}": 0x60 + i for i in range(10)})
# keys whose scan code has the "extended" flag (bit 24 of lParam). F-keys are NOT extended.
_EXTENDED = {"delete", "insert", "home", "end", "pageup", "pagedown", "up", "down", "left", "right",
             "printscreen"}


def bg_vk(key: str) -> Optional[int]:
    if key in _BG_VK:
        return _BG_VK[key]
    if len(key) == 1 and key.isalnum():
        return ord(key.upper())
    return None


def key_lparams(scan: int, extended: bool) -> Tuple[int, int]:
    """lParam for WM_KEYDOWN / WM_KEYUP: repeat count 1, scan code in bits 16-23,
    extended flag bit 24; key-up also sets previous-state (30) and transition (31)."""
    base = 1 | ((scan & 0xFF) << 16) | ((1 << 24) if extended else 0)
    return base, base | (1 << 30) | (1 << 31)


class Win32Api:
    """The ONLY Windows calls background mode makes: list windows, map a key to its scan
    code, convert a point to client coords, post a message. Deliberately nothing that
    sends global input, changes focus, moves the cursor, or reads another process."""

    def __init__(self):
        if sys.platform != "win32":
            raise RuntimeError("background_window mode only works on Windows")
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.wt = ctypes, wintypes
        u = ctypes.windll.user32
        self.u = u
        u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.PostMessageW.restype = wintypes.BOOL
        u.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        u.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
        u.MapVirtualKeyW.restype = wintypes.UINT

    def enum_windows(self) -> List[Tuple[int, str]]:
        ctypes, wt, u = self.ctypes, self.wt, self.u
        out: List[Tuple[int, str]] = []
        proto = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

        def cb(hwnd, _lp):
            if u.IsWindowVisible(hwnd):
                n = u.GetWindowTextLengthW(hwnd)
                if n:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    u.GetWindowTextW(hwnd, buf, n + 1)
                    out.append((int(hwnd), buf.value))
            return True

        u.EnumWindows(proto(cb), 0)
        return out

    def map_virtual_key(self, vk: int) -> int:
        return int(self.u.MapVirtualKeyW(vk, 0))  # MAPVK_VK_TO_VSC

    def screen_to_client(self, hwnd: int, x: int, y: int) -> Tuple[int, int]:
        pt = self.wt.POINT(x, y)
        self.u.ScreenToClient(hwnd, self.ctypes.byref(pt))
        return pt.x, pt.y

    def post_message(self, hwnd: int, msg: int, wparam: int, lparam: int) -> bool:
        return bool(self.u.PostMessageW(hwnd, msg, wparam, lparam))


class DryRunApi:
    """Background-mode dry run: real window list (if on Windows), posts only printed."""

    def __init__(self, real=None):
        self.real = real

    def enum_windows(self):
        return self.real.enum_windows() if self.real else []

    def map_virtual_key(self, vk):
        return self.real.map_virtual_key(vk) if self.real else 0

    def screen_to_client(self, hwnd, x, y):
        return self.real.screen_to_client(hwnd, x, y) if self.real else (x, y)

    def post_message(self, hwnd, msg, wparam, lparam):
        print(f"    (dry run) would post msg 0x{msg:04X} wParam=0x{wparam:X} lParam=0x{lparam:08X} to hwnd {hwnd}")
        return True


class BlockedSender(KeySender, MouseSender):
    """Used in background mode so global/foreground input can never happen."""
    name = "blocked (background mode)"

    def press(self, modifiers, key, hold):
        raise RuntimeError("foreground input is disabled in background_window mode")

    key_down = key_up = lambda self, modifiers, key: BlockedSender.press(self, modifiers, key, 0)

    def click(self, button, at=None):
        raise RuntimeError("foreground input is disabled in background_window mode")


def run_list_windows(api, log=print) -> int:
    wins = list_eve_windows(api)
    if not wins:
        log("No visible windows with 'EVE' in the title. Start the EVE client(s) and log in a character.")
        return 0
    log("Visible windows with 'EVE' in the title (copy the exact title into target.window_title):")
    for h, t in wins:
        log(f"  {t!r}   (hwnd {h})")
    return len(wins)


def run_test_background(cfg: "Config", api, log=print, sleep=time.sleep, countdown: int = 3) -> bool:
    """Send ONE key (the 'test background' phrase's key, default F10 = map) to the target."""
    title = cfg.target.get("window_title") or ""
    if not title:
        log("Set target.window_title first (app: Clients tab, or run_cli.bat --list-windows).")
        return False
    cmd = cfg.commands.get("test background")
    key = cmd.key if (cmd and cmd.kind == "key" and not cmd.modifiers) else "f10"
    bg = BackgroundTarget(title, api, log)
    log(f"Will send ONE '{key}' to '{title}' in {countdown}s. Put your OTHER client in front now.")
    for i in range(countdown, 0, -1):
        log(f"  {i}...")
        sleep(1)  # countdown only - sends no input
    hwnd = bg.resolve()
    if hwnd is None:
        return False
    bg.press(hwnd, [], key, cfg.key_hold_seconds)
    log("Sent. If the map opened/closed on that client, background mode works for you.")
    return True


def list_eve_windows(api) -> List[Tuple[int, str]]:
    return [(h, t) for h, t in api.enum_windows() if "eve" in t.lower()]


class BackgroundTarget:
    """Resolves ONE window by exact title and posts ONE key or click to it."""

    def __init__(self, title: str, api, log: Callable[[str], None] = print):
        self.title, self.api, self.log = title, api, log

    def matches(self) -> List[int]:
        want = self.title.strip().lower()
        return [h for h, t in self.api.enum_windows() if t.strip().lower() == want]

    def resolve(self) -> Optional[int]:
        """Exactly one match or nothing. Never returns more than one window."""
        m = self.matches()
        if len(m) == 1:
            return m[0]
        if not m:
            self.log(f"  background target '{self.title}' not found - nothing sent")
        else:
            self.log(f"  {len(m)} windows are titled '{self.title}' - refusing to guess, nothing sent")
        return None

    def _key(self, modifiers: List[str], key: str):
        if modifiers:  # GetKeyState in the target would see the REAL keyboard - chords can't work
            raise ConfigError("background mode supports single keys only")
        vk = bg_vk(key)
        if vk is None:
            raise ConfigError(f"key '{key}' isn't supported in background mode")
        scan = self.api.map_virtual_key(vk)
        down, up = key_lparams(scan, key in _EXTENDED)
        return vk, down, up

    def press(self, hwnd: int, modifiers: List[str], key: str, hold: float) -> None:
        vk, down, up = self._key(modifiers, key)
        self.api.post_message(hwnd, WM_KEYDOWN, vk, down)
        time.sleep(hold)  # hold time of this ONE press (keydown..keyup), not a gap between actions
        self.api.post_message(hwnd, WM_KEYUP, vk, up)

    def key_down(self, hwnd: int, modifiers: List[str], key: str) -> None:
        vk, down, _ = self._key(modifiers, key)
        self.api.post_message(hwnd, WM_KEYDOWN, vk, down)

    def key_up(self, hwnd: int, modifiers: List[str], key: str) -> None:
        vk, _, up = self._key(modifiers, key)
        self.api.post_message(hwnd, WM_KEYUP, vk, up)

    def click(self, hwnd: int, button: str, at: Optional[Tuple[int, int]]) -> None:
        if at is None:
            raise ConfigError("background mode can only click a saved spot (the cursor isn't over that window)")
        cx, cy = self.api.screen_to_client(hwnd, at[0], at[1])
        lp = ((cy & 0xFFFF) << 16) | (cx & 0xFFFF)
        down, up, mk = _WM_BUTTON[button]
        self.api.post_message(hwnd, down, mk, lp)
        self.api.post_message(hwnd, up, 0, lp)


# --------------------------------------------------------------------------- speech-model vocabulary
_VOCAB_CACHE: Dict[str, Optional[set]] = {}


def load_vocab(model_dir: str) -> Optional[set]:
    """Words the Vosk model knows, read from the word symbol table embedded in
    graph/Gr.fst (no Vosk import needed). None if it can't be read."""
    if model_dir in _VOCAB_CACHE:
        return _VOCAB_CACHE[model_dir]
    words: Optional[set] = None
    try:
        import struct
        with open(os.path.join(model_dir, "graph", "Gr.fst"), "rb") as f:
            data = f.read(8 * 1024 * 1024)
        i = data.find(b"words.txt")
        if i >= 0:
            p = i + len(b"words.txt")
            _avail, n = struct.unpack_from("<qq", data, p)
            p += 16
            found = set()
            for _ in range(n):
                (ln,) = struct.unpack_from("<i", data, p); p += 4
                found.add(data[p:p + ln].decode("utf-8", "replace")); p += ln + 8
            words = found if len(found) > 1000 else None
    except Exception:
        words = None
    _VOCAB_CACHE[model_dir] = words
    return words


def unknown_words(phrase: str, vocab: Optional[set]) -> List[str]:
    if not vocab:
        return []
    return [w for w in normalize_phrase(phrase).split() if w not in vocab]


# --------------------------------------------------------------------------- calibration (box select)
def virtual_screen() -> Tuple[int, int, int, int]:
    """(x, y, width, height) covering ALL monitors (Windows), in physical pixels."""
    if sys.platform == "win32":
        import ctypes
        gsm = ctypes.windll.user32.GetSystemMetrics
        w, h = gsm(78), gsm(79)  # SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN
        if w and h:
            return gsm(76), gsm(77), w, h  # SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN
    import tkinter as tk
    r = tk.Tk(); r.withdraw()
    w, h = r.winfo_screenwidth(), r.winfo_screenheight()
    r.destroy()
    return 0, 0, w, h


def select_box_overlay(title: str, master=None, heading: Optional[str] = None,
                       hint: Optional[str] = None) -> Optional[List[int]]:
    """Semi-transparent always-on-top overlay over every monitor. Drag a box, release
    to accept; Esc or right-click cancels. Returns [left, top, right, bottom] in screen
    pixels (same coordinates pydirectinput clicks use, because we're DPI aware)."""
    import tkinter as tk
    if master is not None and sys.platform != "win32":
        vx, vy, vw, vh = 0, 0, master.winfo_screenwidth(), master.winfo_screenheight()
    else:
        vx, vy, vw, vh = virtual_screen()
    result: Dict[str, Optional[List[int]]] = {"box": None}
    root = tk.Toplevel(master) if master is not None else tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    try:
        root.attributes("-alpha", 0.35)
    except tk.TclError:
        pass
    root.geometry(f"{vw}x{vh}{vx:+d}{vy:+d}")
    cv = tk.Canvas(root, bg="black", highlightthickness=0, cursor="crosshair")
    cv.pack(fill="both", expand=True)
    # instructions near the top-left of the PRIMARY monitor (screen 0,0)
    tx, ty = 40 - vx, 40 - vy
    cv.create_text(tx, ty, anchor="nw", fill="white", font=("Segoe UI", 20, "bold"),
                   text=heading or f"EVE Voice - set spot: {title}")
    cv.create_text(tx, ty + 44, anchor="nw", fill="white", font=("Segoe UI", 14),
                   text=hint or ("Drag a box over the thing to click (e.g. the next-waypoint row).\n"
                                 "Release the mouse to save.   Esc or right-click = cancel."))
    st = {"x": 0, "y": 0, "rect": None}

    def down(e):
        st["x"], st["y"] = e.x_root, e.y_root
        if st["rect"]:
            cv.delete(st["rect"])
        st["rect"] = cv.create_rectangle(e.x_root - vx, e.y_root - vy, e.x_root - vx, e.y_root - vy,
                                         outline="#00ff66", width=3)

    def drag(e):
        if st["rect"]:
            cv.coords(st["rect"], st["x"] - vx, st["y"] - vy, e.x_root - vx, e.y_root - vy)

    def up(e):
        result["box"] = normalize_box(st["x"], st["y"], e.x_root, e.y_root)
        root.destroy()

    def cancel(_e=None):
        result["box"] = None
        root.destroy()

    cv.bind("<ButtonPress-1>", down)
    cv.bind("<B1-Motion>", drag)
    cv.bind("<ButtonRelease-1>", up)
    cv.bind("<ButtonPress-3>", cancel)
    root.bind("<Escape>", cancel)
    root.after(50, lambda: (root.lift(), root.focus_force()))
    if master is not None:
        root.grab_set()
        master.wait_window(root)   # modal inside the GUI's own event loop
    else:
        root.mainloop()
    return result["box"]


def _iter_entries(data: dict, enabled_only: bool = False):
    """Yield (group, phrase, spec) for every command entry in the raw JSON."""
    for gname, group in (data.get("groups") or {}).items():
        if gname.startswith("_") or not isinstance(group, dict):
            continue
        if enabled_only and group.get("enabled", True) is False:
            continue
        for phrase, spec in (group.get("commands") or {}).items():
            if not phrase.startswith("_"):
                yield gname, phrase, spec


def _click_parts(spec):
    """Yield (None, action_dict) if the entry is a mouse click (combos don't exist)."""
    if isinstance(spec, dict) and "mouse" in spec and "steps" not in spec:
        yield None, spec


def find_calibration_targets(data: dict, enabled_only: bool = True) -> List[dict]:
    """Things that can be calibrated: shared spots, plus commands with their own
    mouse_at / mouse_box. Each target: {label, type, ...}."""
    targets = []
    spots = {k: v for k, v in (data.get("spots") or {}).items() if not k.startswith("_")}
    users: Dict[str, List[str]] = {k: [] for k in spots}
    own = []
    for g, phrase, spec in _iter_entries(data, enabled_only=enabled_only):
        for idx, act in _click_parts(spec):
            if "mouse_spot" in act:
                users.setdefault(str(act["mouse_spot"]), []).append(phrase)
            elif "mouse_at" in act or "mouse_box" in act:
                own.append({"type": "command", "group": g, "phrase": phrase, "step": idx,
                            "label": f"'{phrase}'",
                            "current": act.get("mouse_box", act.get("mouse_at"))})
    for name in spots:
        used = users.get(name) or []
        targets.append({"type": "spot", "spot": name, "users": used, "current": spots[name],
                        "label": f"spot '{name}'" + (f" - used by {', '.join(repr(u) for u in used)}" if used else "")})
    return targets + own


def apply_box(data: dict, target: dict, box: List[int]) -> None:
    """Write the box into the raw JSON (in place). Also switches allow_mouse_at on."""
    if target["type"] == "spot":
        data.setdefault("spots", {})[target["spot"]] = box
    else:
        spec = data["groups"][target["group"]]["commands"][target["phrase"]]
        spec.pop("mouse_at", None)
        spec["mouse_box"] = box
    data.setdefault("settings", {})["allow_mouse_at"] = True


def save_config_data(path: str, data: dict) -> None:
    """Pretty-print back to disk, keeping every key (incl. _notes) and order.
    Keeps one backup as commands.json.bak; writes atomically."""
    if os.path.exists(path):
        try:
            import shutil
            shutil.copyfile(path, path + ".bak")
        except OSError:
            pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def _read_raw(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _targets_for_name(data: dict, name: str) -> List[dict]:
    want = normalize_phrase(name)
    targets = find_calibration_targets(data, enabled_only=False)
    hits = [t for t in targets if t["type"] == "spot" and normalize_phrase(t["spot"]) == want]
    if hits:
        return hits
    for g, phrase, spec in _iter_entries(data):
        if normalize_phrase(phrase) != want:
            continue
        out = []
        for idx, act in _click_parts(spec):
            if "mouse_spot" in act:
                out += [t for t in targets if t["type"] == "spot" and t["spot"] == act["mouse_spot"]]
            else:
                out += [t for t in targets if t["type"] == "command" and t["phrase"] == phrase
                        and t["step"] == idx]
        return out
    return []


def calibrate_targets(path: str, targets: List[dict], overlay=None, log=print) -> int:
    overlay = overlay or select_box_overlay
    saved = 0
    for t in targets:
        box = overlay(t["label"])
        if box is None:
            log(f"  cancelled: {t['label']}")
            continue
        data = _read_raw(path)  # re-read so we never clobber edits
        apply_box(data, t, box)
        save_config_data(path, data)
        cx, cy = box_center(box)
        log(f"  saved {t['label']}: box {box} -> clicks at {cx},{cy}")
        saved += 1
    return saved


def calibrate_cli(path: str, name: Optional[str] = None, overlay=None, input_fn=input,
                  log=print, sleep=time.sleep, countdown: int = 3) -> int:
    data = _read_raw(path)
    if name:
        targets = _targets_for_name(data, name)
        if not targets:
            log(f"Nothing to calibrate for '{name}'. It must be a spot name or a command that "
                "clicks a mouse_spot / mouse_box / mouse_at.")
            return 0
    else:
        targets = find_calibration_targets(data)
        if not targets:
            log("No commands use a saved click spot. (Plain 'click' commands use the cursor and need no setup.)")
            return 0
        log("Click spots you can set:")
        for i, t in enumerate(targets, 1):
            state = "not set" if t["current"] is None else f"set {t['current']}"
            log(f"  {i}. {t['label']}  [{state}]")
        ans = input_fn("Numbers to set (e.g. 1 or 1,3 or 'all'; Enter = cancel): ").strip().lower()
        if not ans:
            return 0
        if ans == "all":
            picked = targets
        else:
            try:
                idx = [int(x) for x in re.split(r"[ ,]+", ans) if x]
                picked = [targets[i - 1] for i in idx if 1 <= i <= len(targets)]
            except ValueError:
                picked = []
            if not picked:
                log("No valid numbers given.")
                return 0
        targets = picked
    log("Get EVE on screen so the spot is visible (e.g. the next waypoint in your Overview/route).")
    input_fn("Press Enter, then click into EVE - the overlay opens after a short countdown...")
    for i in range(countdown, 0, -1):
        log(f"  {i}...")
        sleep(1)  # countdown before the setup overlay - sends no input
    return calibrate_targets(path, targets, overlay=overlay, log=log)


# --------------------------------------------------------------------------- push-to-talk key state
# Windows virtual-key codes for push-to-talk. Read with GetAsyncKeyState: a plain
# "is this key down right now?" poll - no keyboard hook is installed.
_VK = {"scroll lock": 0x91, "scrolllock": 0x91, "caps lock": 0x14, "capslock": 0x14,
       "pause": 0x13, "insert": 0x2D, "home": 0x24, "end": 0x23, "page up": 0x21, "page down": 0x22,
       "right alt": 0xA5, "left alt": 0xA4, "alt": 0x12, "right ctrl": 0xA3, "left ctrl": 0xA2,
       "ctrl": 0x11, "right shift": 0xA1, "left shift": 0xA0, "shift": 0x10, "space": 0x20,
       "tab": 0x09, "num lock": 0x90, "`": 0xC0, "menu": 0x5D, "apps": 0x5D}
_VK.update({f"f{i}": 0x6F + i for i in range(1, 25)})
_VK.update({f"num{i}": 0x60 + i for i in range(10)})
_VK.update({f"num {i}": 0x60 + i for i in range(10)})


def vk_code(key_name: str) -> Optional[int]:
    k = key_name.strip().lower()
    if k in _VK:
        return _VK[k]
    if len(k) == 1 and (k.isalpha() or k.isdigit()):
        return ord(k.upper())
    return None


def make_ptt_checker(key_name: str) -> Callable[[], bool]:
    if sys.platform == "win32":
        import ctypes
        vk = vk_code(key_name)
        if vk is None:
            raise ConfigError(f"push_to_talk_key '{key_name}' not recognised - try 'scroll lock', 'f13', "
                              "'right alt', a letter or a digit")
        gaks = ctypes.windll.user32.GetAsyncKeyState
        return lambda: bool(gaks(vk) & 0x8000)
    # Non-Windows (development only): pynput key listener.
    from pynput import keyboard as pk
    target = key_name.lower().replace(" ", "_")
    held = set()

    def name_of(k):
        if hasattr(k, "char") and k.char:
            return k.char.lower()
        return str(k).replace("Key.", "").lower()

    pk.Listener(on_press=lambda k: held.add(name_of(k)),
                on_release=lambda k: held.discard(name_of(k)), daemon=True).start()
    return lambda: target in held


# --------------------------------------------------------------------------- TTS
class Speaker:
    def __init__(self, enabled: bool):
        self.q: "queue.Queue[str]" = queue.Queue()
        self.enabled = False
        if not enabled:
            return
        try:
            import pyttsx3  # noqa
        except ImportError:
            print("[warn] tts_confirm is on but pyttsx3 isn't installed - TTS disabled")
            return
        self.enabled = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        import pyttsx3
        engine = pyttsx3.init()
        engine.setProperty("rate", 210)
        while True:
            text = self.q.get()
            engine.say(text)
            engine.runAndWait()

    def say(self, text: str):
        if self.enabled and self.q.qsize() < 2:  # never build up a backlog
            self.q.put(text)


# --------------------------------------------------------------------------- capture (Practice / Teach)
class Capture:
    """While set on VoiceApp.capture, final results go to `cb(text, conf)` instead of
    being acted on. free=True -> full-vocabulary recognition (Teach: 'what did I say?').
    once=True -> one utterance, then normal listening resumes. Sends no input."""

    def __init__(self, cb: Callable[[Optional[str], Optional[float]], None], free: bool = False,
                 once: bool = True, timeout: Optional[float] = None, hint: str = "",
                 clock: Callable[[], float] = time.monotonic):
        self.cb, self.free, self.once, self.hint, self.clock = cb, free, once, hint, clock
        self.deadline = clock() + timeout if timeout else None

    def expired(self) -> bool:
        return self.deadline is not None and self.clock() > self.deadline

    def deliver(self, text: Optional[str], conf: Optional[float]) -> None:
        try:
            self.cb(text, conf)
        except Exception:
            pass


def parse_result(js: str) -> Tuple[str, Optional[float]]:
    """Vosk Result()/FinalResult() JSON -> (text, confidence). Confidence is the LOWEST
    word confidence (SetWords(True) gives per-word 'conf'); None if not reported."""
    try:
        r = json.loads(js)
    except (TypeError, ValueError):
        return "", None
    text = r.get("text", "") or ""
    words = r.get("result") or []
    confs = [float(w["conf"]) for w in words if isinstance(w, dict) and "conf" in w]
    return text, (min(confs) if confs else None)


# --------------------------------------------------------------------------- core
# --------------------------------------------------------------------------- held keys
def make_left_button_checker() -> Callable[[], Optional[bool]]:
    """'Is the left mouse button down right now?' - a plain GetAsyncKeyState poll
    (VK_LBUTTON), the same kind of read push-to-talk uses. No mouse hook. None = can't tell."""
    if sys.platform == "win32":
        try:
            import ctypes
            gaks = ctypes.windll.user32.GetAsyncKeyState
            return lambda: bool(gaks(0x01) & 0x8000)
        except Exception:
            pass
    return lambda: None


_LIVE_HOLDS: "List[HoldManager]" = []


def release_all_holds(why: str = "app closing") -> None:
    for h in list(_LIVE_HOLDS):
        try:
            h.release(why)
        except Exception:
            pass


atexit.register(release_all_holds)


def _spawn_thread(fn, *args) -> None:
    threading.Thread(target=fn, args=args, name="eve-voice-hold", daemon=True).start()


class HoldManager:
    """A held key is still ONE input: one keyDown now, the matching keyUp later.
    Only one key is held at a time. The keyUp comes from: the timer (timed), your own
    left click being released (until click - the tool never clicks), the 'release'
    phrase, the safety max, EVE losing focus, Stop, a config/mode change, or exit.
    Runs a small timer thread so the listener keeps working while the key is down."""

    POLL = 0.01

    def __init__(self, app: "VoiceApp", clock: Callable[[], float] = time.monotonic,
                 left_button: Optional[Callable[[], Optional[bool]]] = None,
                 spawn: Optional[Callable] = _spawn_thread, sleep: Callable[[float], None] = time.sleep):
        self.app, self.clock, self.spawn, self.sleep = app, clock, spawn, sleep
        self.left_button = left_button or make_left_button_checker()
        self.lock = threading.RLock()
        self.active: Optional[dict] = None
        self.token = 0
        _LIVE_HOLDS.append(self)

    @staticmethod
    def label(cmd: "Command") -> str:
        return "+".join(m.title() for m in cmd.modifiers + [cmd.key]) if len(cmd.key) > 1 or cmd.modifiers \
            else cmd.key.upper()

    def holding(self) -> Optional[str]:
        a = self.active
        return a["label"] if a else None

    def remaining(self) -> Optional[float]:
        a = self.active
        return max(0.0, a["deadline"] - self.clock()) if a else None

    def start(self, cmd: "Command", hwnd: Optional[int] = None) -> None:
        with self.lock:
            if self.active is not None:
                # releasing YOUR earlier held key (its keyUp) - not a second action
                self.release(f"you started holding {self.label(cmd)}")
            if hwnd is not None:
                self.app.background.key_down(hwnd, cmd.modifiers, cmd.key)
            else:
                self.app.sender.key_down(cmd.modifiers, cmd.key)
            now = self.clock()
            lb = self.left_button()
            self.token += 1
            self.active = {"cmd": cmd, "label": self.label(cmd), "hwnd": hwnd, "mode": cmd.hold_mode,
                           "start": now, "deadline": now + float(cmd.hold_seconds), "token": self.token,
                           "armed": not lb, "seen_down": False}
            lbl, secs = self.active["label"], float(cmd.hold_seconds)
            msg = {"timed": f"holding {lbl} for {secs:.1f}s",
                   "click": f"holding {lbl} - click your target (max {secs:g}s)",
                   "release": f"holding {lbl} until you say 'release' (max {secs:g}s)"}[cmd.hold_mode]
            if cmd.hold_mode == "click" and lb is None:
                msg += " [can't see clicks here - lets go at the time limit]"
            self.app.log(f"  {msg}")
            self.app._emit("holding", cmd.phrase, f"{lbl}|{cmd.hold_mode}|{secs:g}")
            token = self.token
        if self.spawn is not None:
            self.spawn(self._loop, token)

    def _loop(self, token: int) -> None:
        while True:
            with self.lock:
                if self.active is None or self.active["token"] != token:
                    return
                self.tick()
            self.sleep(self.POLL)

    def tick(self) -> None:
        """One check (the thread runs this every 10 ms; tests call it directly)."""
        with self.lock:
            a = self.active
            if a is None:
                return
            app = self.app
            if a["hwnd"] is None and app.cfg.target.get("mode") != "background_window" and not app.eve_focused():
                self.release("EVE lost focus")
                return
            if a["mode"] == "click":
                lb = self.left_button()
                if lb is not None:
                    if not a["armed"]:
                        a["armed"] = not lb          # button was already down at the start: wait for it up
                    elif lb:
                        a["seen_down"] = True
                    elif a["seen_down"]:
                        self.release("you clicked")
                        return
            if self.clock() >= a["deadline"]:
                self.release("time's up" if a["mode"] == "timed" else "safety limit reached")

    def release(self, why: str = "released") -> bool:
        with self.lock:
            a, self.active = self.active, None
            if a is None:
                return False
            cmd = a["cmd"]
            try:
                if a["hwnd"] is not None:
                    self.app.background.key_up(a["hwnd"], cmd.modifiers, cmd.key)
                else:
                    self.app.sender.key_up(cmd.modifiers, cmd.key)
            except Exception as e:
                self.app.log(f"  [error] couldn't let go of {a['label']}: {e}")
            self.app.log(f"  released {a['label']} ({why})")
            self.app._emit("released", cmd.phrase, f"{a['label']}|{why}")
            return True


class VoiceApp:
    """Turns recognised phrases into input. Enforces the rules:
    exactly ONE send per command (one key/chord OR one click), only when EVE is the
    foreground window, never a second action, never a delay between actions."""

    def __init__(self, cfg: Config, sender: KeySender, gate: TriggerGate,
                 title_fn: Callable[[], Optional[str]] = foreground_window_title,
                 speaker: Optional[Speaker] = None, clock: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = print, mouse: Optional[MouseSender] = None,
                 calibrate_fn: Optional[Callable[[str], bool]] = None,
                 background: Optional["BackgroundTarget"] = None,
                 on_event: Optional[Callable[[str, str, str], None]] = None,
                 locator: Optional[Callable[[dict], Tuple[Optional[Tuple[int, int]], str]]] = None,
                 teach_fn: Optional[Callable[[], None]] = None):
        self.cfg, self.sender, self.gate, self.mouse = cfg, sender, gate, mouse
        self.locator = locator        # opt-in screen finder for color_target clicks (gate_finder.locate)
        self.teach_fn = teach_fn      # 'teach command' -> app opens its Teach window
        self.capture: Optional[Capture] = None   # set by the app: practice / teach listening (never fires)
        self.on_event = on_event      # optional UI hook: (kind, phrase, detail); kind = sent|skipped|wake|setup
        self.background = background  # set only in target.mode == background_window
        self.calibrate_fn = calibrate_fn   # called with a spot name; returns True if saved
        self.config_changed = False        # set after a voice calibration -> caller reloads
        self.title_fn, self.clock, self.log = title_fn, clock, log
        self.speaker = speaker or Speaker(False)
        self.last_fired: Dict[str, float] = {}
        self.hold = HoldManager(self, clock=clock)

    def eve_focused(self) -> bool:
        if not self.cfg.require_eve_focus:
            return True
        title = self.title_fn()
        if title is None:
            return False
        return self.cfg.window_title_contains.lower() in title.lower()

    def hear(self, text: str, conf: Optional[float] = None,
             already: Optional[List[str]] = None) -> Optional[Command]:
        """Entry point for every final recognition result. While the app is capturing
        (Practice / Teach) the result goes to the app and NOTHING is sent."""
        cap = self.capture
        if cap is not None:
            if cap.once:
                self.capture = None
            cap.deliver(text, conf)
            return None
        return self.handle_text(text, conf, already=already)

    def _heard(self, kind: str, text: str, conf: Optional[float], why: str) -> None:
        pct = f" ({conf:.0%})" if conf is not None else ""
        near = self.cfg.closest(text) if text and "[unk]" != text else None
        tail = f"; closest command: '{near}'" if near and near != text else ""
        detail = f"{why}{tail}"
        if self.cfg.log_unmatched:
            self.log(f"  heard '{text}'{pct} - {detail}")
        self._emit(kind, text, f"{pct.strip()} {detail}".strip())

    def handle_text(self, text: str, conf: Optional[float] = None,
                    already: Optional[List[str]] = None) -> Optional[Command]:
        """Process one recognised utterance. Returns the Command fired, or None.
        conf = the recogniser's confidence (0..1, lowest word); None = not known (typed text)."""
        if not text:
            return None
        if "[unk]" in text:
            if normalize_phrase(text.replace("[unk]", " ")):
                self._heard("unmatched", text, conf, "only partly a command, skipped")
            else:
                self._heard("unmatched", "[unk]", conf, "not a command")
            return None
        if conf is not None and conf < self.cfg.min_confidence:
            self._heard("unsure", normalize_phrase(text), conf, "not sure, skipped")
            return None
        cmd_text = self.gate.filter(text)
        if cmd_text is None:
            return None
        if cmd_text == "":
            self.log(f"  [wake] listening for {self.cfg.wake_window_seconds:g}s...")
            self._emit("wake", self.cfg.wake_word, f"listening for {self.cfg.wake_window_seconds:g}s")
            return None
        segs = [cmd_text] if self.cfg.lookup(cmd_text) is not None else self.cfg.segment(cmd_text)
        if not segs:
            self._heard("unmatched", cmd_text, conf, "not a command")  # never guess
            return None
        # commands already fired from the partial result of this same utterance aren't fired again
        done = list(already or [])
        while done and segs and done[0] == segs[0]:
            done.pop(0); segs.pop(0)
        last = None
        for ph in segs:   # several commands said in one breath: each fires its own ONE input, in order
            fired = self.fire(self.cfg.lookup(ph))
            last = fired or last
        return last

    def _emit(self, kind: str, phrase: str, detail: str) -> None:
        if self.on_event:
            try:
                self.on_event(kind, phrase, detail)
            except Exception:
                pass

    def _skip(self, cmd: Command, why: str) -> None:
        self.log(f"  '{cmd.phrase}' {why}")
        self._emit("skipped", cmd.phrase, why)

    def fire(self, cmd: Command) -> Optional[Command]:
        """Run ONE already-recognised command through every check, then send its ONE input.
        Used by handle_text and by the GUI's per-command Test button."""
        now = self.clock()
        cooldown = self.cfg.cooldown_seconds if cmd.cooldown is None else cmd.cooldown
        if now - self.last_fired.get(cmd.phrase, -1e9) < cooldown:
            self._emit("skipped", cmd.phrase, "cooldown (repeat ignored)")
            return None  # cooldown only ever DROPS a repeat; it never adds input
        if cmd.kind == "calibrate":   # setup tool: opens the overlay, sends no game input
            self.last_fired[cmd.phrase] = now
            if self.calibrate_fn is None:
                self.log("  calibration isn't available here")
                return None
            self.log(f"  > {cmd.phrase}: opening the box-select overlay...")
            self._emit("setup", cmd.phrase, "opening the box-select overlay")
            if self.calibrate_fn(cmd.spot):
                self.config_changed = True
            return cmd
        if cmd.kind == "release":   # the keyUp of the held key - completes that ONE input
            self.last_fired[cmd.phrase] = now
            if not self.hold.release(f"you said '{cmd.phrase}'"):
                self.log("  nothing is being held")
                self._emit("skipped", cmd.phrase, "nothing is being held")
                return None
            return cmd
        if cmd.kind == "teach":     # setup tool: the app opens its Teach window, no game input
            self.last_fired[cmd.phrase] = now
            if self.teach_fn is None:
                self.log("  Teach is only available in the app window")
                return None
            self._emit("setup", cmd.phrase, "opening Teach")
            self.teach_fn()
            return cmd
        hwnd = None
        if self.cfg.target["mode"] == "background_window":
            if self.background is None:
                self._skip(cmd, "heard, but the background target isn't available - nothing sent")
                return None
            hwnd = self.background.resolve()   # exactly ONE window, or None
            if hwnd is None:
                self._emit("skipped", cmd.phrase, "background client not found (or 2+ match) - nothing sent")
                return None
        else:
            if cmd.kind == "mouse" and self.mouse is None:
                self._skip(cmd, "heard, but no mouse library is available - not sent")
                return None
            if not self.eve_focused():
                self._skip(cmd, "heard, but EVE window isn't focused - not sent")
                return None
        at = cmd.target
        if cmd.color_target:   # opt-in: look at the Overview box ONCE, now; zero or one click
            if self.locator is None or hwnd is not None:
                self._skip(cmd, "- screen finding isn't available here - nothing sent")
                return None
            try:
                at, msg = self.locator(self.cfg.color_targets[cmd.color_target])
            except Exception as e:
                at, msg = None, f"screen check failed ({e})"
            if at is None:
                self._skip(cmd, f"- {msg} - nothing sent")
                return None
            self.log(f"  {cmd.phrase}: {msg}")
            self._emit("found", cmd.phrase, msg)
        try:
            self._send_one(cmd, hwnd, at)
        except ConfigError as e:
            self._skip(cmd, f"- {e} - nothing sent")
            return None
        self.last_fired[cmd.phrase] = now
        if self.cfg.print_confirm:
            self.log(f"  > {cmd.phrase}  [{cmd.key_spec}]")
        self._emit("sent", cmd.phrase, cmd.key_spec)
        self.speaker.say(cmd.phrase)
        return cmd

    def _send_one(self, cmd: Command, hwnd: Optional[int] = None,
                  at: Optional[Tuple[int, int]] = None) -> None:
        """The ONLY place input is sent. One call, one action - keyboard OR mouse -
        to the active window, or (experimental) to the ONE background window `hwnd`.
        `at` = where the ONE click goes (None = at the cursor)."""
        if cmd.kind == "key" and cmd.hold_mode != "tap":
            self.hold.start(cmd, hwnd)   # ONE keyDown now; its keyUp comes later (HoldManager)
            return
        held = self.hold.active
        if held is not None and cmd.kind != "mouse":
            # a key tap must not turn into a chord with the held key: let go of the held key first
            self.hold.release(f"before '{cmd.phrase}'")
        # a CLICK command keeps the held key down: that's EVE's hold-key-then-click ('warp' ... 'gate').
        # Two spoken commands, two inputs; the tool adds nothing between them.
        hold = self.cfg.key_hold_seconds if cmd.hold is None else cmd.hold
        if hwnd is not None:
            if cmd.kind == "mouse":
                self.background.click(hwnd, cmd.mouse, at)
            else:
                self.background.press(hwnd, cmd.modifiers, cmd.key, hold)
        elif cmd.kind == "mouse":
            self.mouse.click(cmd.mouse, at)
        else:
            self.sender.press(cmd.modifiers, cmd.key, hold)
        if cmd.kind == "mouse" and held is not None and self.hold.active is held and held["mode"] == "click":
            self.hold.release(f"clicked with '{cmd.phrase}'")   # keyUp of that same held key

    def apply_config(self, cfg: Config) -> None:
        """Swap in a freshly loaded config (after calibration)."""
        self.hold.release("settings changed")
        self.cfg = cfg
        self.gate = TriggerGate(cfg, ptt_pressed=self.gate.ptt_pressed, clock=self.gate.clock)
        self.config_changed = False


# --------------------------------------------------------------------------- audio loop
def pick_device(sd, wanted):
    if wanted is None or wanted == "":
        return None
    if isinstance(wanted, int) or (isinstance(wanted, str) and wanted.isdigit()):
        return int(wanted)
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and str(wanted).lower() in d["name"].lower():
            return i
    raise ConfigError(f"no input device matching '{wanted}' (try --list-devices)")


SPEECH_LEVEL = 0.03   # peak level that counts as "talking" for our own end-of-speech timer


class AudioFeeder:
    """Feeds audio chunks to the recogniser, honouring push-to-talk.
    Audio is dropped while PTT is up; on release the utterance is finalised.
    - noise gate: chunks quieter than settings.noise_gate are fed as silence
    - fast finalising: after endpoint_silence_seconds of quiet following speech we ask
      the recogniser for its final result instead of waiting for its own (slower) rule
    - fast_response: a command is acted on from the PARTIAL result as soon as it's complete
      (no longer phrase could follow, and the partial is stable); the final result then
      doesn't fire it again. Several commands in one breath ('warp gate') fire in order.
    - while app.capture is set (Practice/Teach) results go to the app, nothing is sent"""

    def __init__(self, rec, app: "VoiceApp", free_rec_fn: Optional[Callable[[], object]] = None,
                 chunk_seconds: float = 0.05, on_partial: Optional[Callable[[str], None]] = None):
        self.rec, self.app, self.was_on = rec, app, False
        self.free_rec_fn, self.free_rec = free_rec_fn, None
        self.chunk_seconds, self.on_partial = chunk_seconds, on_partial
        self.partial, self.speaking, self.quiet = "", False, 0.0
        self.fired: List[str] = []      # commands already fired from this utterance's partials
        self.last_raw = None            # previous partial reading (stability check)

    def _rec(self):
        cap = self.app.capture
        if cap is not None and cap.free and self.free_rec_fn is not None:
            if self.free_rec is None:
                self.free_rec = self.free_rec_fn()
            return self.free_rec
        return self.rec

    def _final(self, rec, js: str) -> None:
        self.speaking, self.quiet = False, 0.0
        if self.partial and self.on_partial:
            self.on_partial("")
        self.partial = ""
        already, self.fired, self.last_raw = self.fired, [], None
        text, conf = parse_result(js)
        was_free = rec is self.free_rec
        if text:
            self.app.hear(text, conf, already=already)
        if was_free and self.app.capture is None:   # Teach capture done -> back to commands
            self.free_rec = None
            self.rec.Reset()

    def _partial_fire(self, p: str, pj: dict) -> None:
        """Fire commands from a partial result once they're complete (see class doc)."""
        stable, self.last_raw = (p == self.last_raw), p
        app = self.app
        if not p or "[unk]" in p or app.capture is not None or app.cfg.mode == "wake_word":
            return
        confs = [float(w["conf"]) for w in (pj.get("partial_result") or [])
                 if isinstance(w, dict) and "conf" in w]
        if confs and min(confs) < app.cfg.min_confidence:
            return
        segs = app.cfg.segment(p)
        if not segs or segs[:len(self.fired)] != self.fired:
            return
        words = normalize_phrase(p).split()
        i = 0
        for k, ph in enumerate(segs):
            n = len(ph.split())
            if app.cfg.could_continue(" ".join(words[i:])):
                return                     # a longer command might still be coming
            last = k == len(segs) - 1
            if last and not stable:
                return                     # wait one more reading before acting on the last word
            if k >= len(self.fired):
                self.fired.append(ph)
                app.fire(app.cfg.lookup(ph))
            i += n

    def feed(self, data: bytes, level: Optional[float] = None):
        cap = self.app.capture
        if cap is not None and cap.expired():
            self.app.capture = None
            self.free_rec = None
            cap.deliver(None, None)
        if level is None:
            level = chunk_level(data) if isinstance(data, (bytes, bytearray)) else 1.0
        on = self.app.gate.audio_enabled() or self.app.capture is not None
        rec = self._rec()
        gate = self.app.cfg.noise_gate
        if on:
            if gate > 0 and level < gate:
                data = bytes(len(data))   # below the noise gate: feed silence
            if rec.AcceptWaveform(data):
                self._final(rec, rec.Result())
            else:
                if level >= max(gate, SPEECH_LEVEL):
                    self.speaking, self.quiet = True, 0.0
                elif self.speaking:
                    self.quiet += self.chunk_seconds
                    end = self.app.cfg.endpoint_silence_seconds
                    if end > 0 and self.quiet >= end:
                        self._final(rec, rec.FinalResult())
                        return
                fast = bool(self.app.cfg.settings.get("fast_response"))
                if self.on_partial or fast:
                    try:
                        pj = json.loads(rec.PartialResult())
                    except (TypeError, ValueError, AttributeError):
                        pj = {}
                    p = pj.get("partial", "") or ""
                    if p != self.partial:
                        self.partial = p
                        if self.on_partial:
                            self.on_partial(p)
                    if fast:
                        self._partial_fire(p, pj)
        elif self.was_on:
            self._final(rec, rec.FinalResult())
            rec.Reset()
        self.was_on = on


def chunk_level(data: bytes) -> float:
    """Peak level 0..1 of a chunk of 16-bit mono audio (for the GUI's mic meter)."""
    from array import array
    a = array("h")
    a.frombytes(data[: len(data) - (len(data) % 2)])
    if not a:
        return 0.0
    return min(1.0, max(max(a), -min(a)) / 32768.0)


def run_mic(cfg: Config, app: VoiceApp, model_dir: str, verbose: bool = False,
            reload_fn: Optional[Callable[[], Optional[Config]]] = None,
            stop_event: Optional[threading.Event] = None,
            tick_cb: Optional[Callable[["VoiceApp", float], None]] = None):
    import sounddevice as sd
    import vosk
    # verbose shows Vosk warnings like "Ignoring word missing in vocabulary: 'xyz'"
    vosk.SetLogLevel(0 if verbose else -1)
    if not os.path.isdir(model_dir):
        raise ConfigError(f"Vosk model not found at '{model_dir}'. Run setup.bat first.")
    model = vosk.Model(model_dir)
    device = pick_device(sd, cfg.mic_device)
    info = sd.query_devices(device, "input")
    rate = int(info["default_samplerate"])

    def make_rec(c: Optional[Config] = None, free: bool = False):
        r = vosk.KaldiRecognizer(model, rate) if free else \
            vosk.KaldiRecognizer(model, rate, json.dumps(build_grammar(c or cfg)))
        r.SetWords(True)   # per-word confidence -> min_confidence check
        if hasattr(r, "SetPartialWords"):
            r.SetPartialWords(True)   # confidence on partial results too (fast_response)
        if hasattr(r, "SetEndpointerDelays") and (c or cfg).endpoint_silence_seconds > 0:
            r.SetEndpointerDelays(5.0, float((c or cfg).endpoint_silence_seconds), 6.0)
        return r
    rec = make_rec()
    audio: "queue.Queue[bytes]" = queue.Queue()

    def cb(indata, frames, t, status):
        audio.put(bytes(indata))

    print(f"Mic: {info['name']} @ {rate} Hz")
    hint = {"always": "Listening - just speak a command.",
            "push_to_talk": f"Hold [{cfg.push_to_talk_key}] while speaking.",
            "wake_word": f"Say '{cfg.wake_word} <command>' (or '{cfg.wake_word}', then the command)."}
    print(hint[cfg.mode] + "  Ctrl+C to quit.\n")

    def partial(p: str) -> None:
        app._emit("partial", p, "")
    feeder = AudioFeeder(rec, app, free_rec_fn=lambda: make_rec(app.cfg, free=True),
                         chunk_seconds=0.05, on_partial=partial)
    with sd.RawInputStream(samplerate=rate, blocksize=rate // 20, device=device,
                           dtype="int16", channels=1, callback=cb):
        while not (stop_event and stop_event.is_set()):
            try:
                chunk = audio.get(timeout=0.25)
            except queue.Empty:
                continue
            level = chunk_level(chunk)
            feeder.feed(chunk, level)
            if tick_cb:
                tick_cb(app, level)
            if app.config_changed and reload_fn:
                new_cfg = reload_fn()
                if new_cfg is not None:
                    app.apply_config(new_cfg)
                    feeder.rec = make_rec(new_cfg)
                    print(f"Reloaded config: {len(new_cfg.commands)} commands.")
                app.config_changed = False
                while not audio.empty():  # drop audio captured while the overlay was open
                    audio.get_nowait()


def run_text(app: VoiceApp, reload_fn: Optional[Callable[[], Optional[Config]]] = None):
    print("Text mode: type what you'd say (e.g. 'warp'). Ctrl+C / empty line to quit.")
    while True:
        try:
            line = input("say> ")
        except EOFError:
            break
        if not line.strip():
            break
        app.hear(line)
        if app.config_changed and reload_fn:
            new_cfg = reload_fn()
            if new_cfg is not None:
                app.apply_config(new_cfg)
                print(f"Reloaded config: {len(new_cfg.commands)} commands.")
            app.config_changed = False


def build_app(cfg: Config, dry_run: bool = False, log: Callable[[str], None] = print,
              use_ptt: bool = True, calibrate_fn: Optional[Callable[[str], bool]] = None,
              on_event: Optional[Callable[[str, str, str], None]] = None,
              teach_fn: Optional[Callable[[], None]] = None, locator=None) -> VoiceApp:
    """Wire up senders / background target / push-to-talk for a loaded config.
    Shared by the CLI (main) and the GUI so there is exactly one implementation."""
    if dry_run:
        cfg.settings["require_eve_focus"] = False  # nothing is really pressed, so skip the focus check
    is_bg = cfg.target["mode"] == "background_window"
    sender = BlockedSender() if is_bg else (PrintSender() if dry_run else make_sender())
    log(f"Key sender: {sender.name}")
    mouse: Optional[MouseSender] = None
    background = None
    if is_bg:
        log(f"[EXPERIMENTAL] background_window mode: ONE input per phrase to the window titled "
            f"{cfg.target['window_title']!r} only. Foreground input is disabled.")
        try:
            real = Win32Api()
        except RuntimeError:
            if not dry_run:
                raise
            real = None
        background = BackgroundTarget(cfg.target["window_title"], DryRunApi(real) if dry_run else real, log)
    elif dry_run:
        mouse = sender
    else:
        try:
            mouse = make_mouse_sender()
            log(f"Mouse sender: {mouse.name}")
        except RuntimeError as e:
            if any(c.kind == "mouse" for c in cfg.commands.values()):
                log(f"[warn] mouse commands disabled: {e}")
    if cfg.require_eve_focus and sys.platform != "win32" and background is None:
        log("[warn] focus check only works on Windows; keys will be blocked. "
            "Set require_eve_focus=false to test elsewhere.")
    ptt = (lambda: False)
    if cfg.mode == "push_to_talk" and use_ptt:
        ptt = make_ptt_checker(cfg.push_to_talk_key)
    gate = TriggerGate(cfg, ptt_pressed=ptt)
    if locator is None and cfg.allow_screen_read and any(c.color_target for c in cfg.commands.values()):
        import gate_finder   # opt-in only: looks at the Overview box when the command is said
        locator = gate_finder.locate
        log("[opt-in] screen reading is ON for: " + ", ".join(
            p for p, c in cfg.commands.items() if c.color_target) + " (Overview box only, only when said)")
    return VoiceApp(cfg, sender, gate, speaker=Speaker(cfg.tts_confirm), mouse=mouse, log=log,
                    calibrate_fn=calibrate_fn, background=background, on_event=on_event,
                    locator=locator, teach_fn=teach_fn)


def main(argv=None):
    ap = argparse.ArgumentParser(description="EVE Online offline voice commands")
    ap.add_argument("--config", default=os.path.join(APP_DIR, "commands.json"))
    ap.add_argument("--list-devices", action="store_true", help="list microphones and exit")
    ap.add_argument("--dry-run", action="store_true", help="don't press keys, just print")
    ap.add_argument("--text", action="store_true", help="type phrases instead of using the mic")
    ap.add_argument("--calibrate", nargs="?", const="", default=None, metavar="NAME",
                    help="drag a box over a click spot. Optional NAME = a command (e.g. \"warp to next\") "
                         "or spot name; without it you pick from a list")
    ap.add_argument("--where", action="store_true",
                    help="print the mouse position every second (fallback for mouse_at) - Ctrl+C to stop")
    ap.add_argument("--list-windows", action="store_true",
                    help="list visible windows with 'EVE' in the title (for target.window_title)")
    ap.add_argument("--test-background", action="store_true",
                    help="EXPERIMENTAL: send ONE F10 to target.window_title after a 3s countdown")
    ap.add_argument("--verbose", action="store_true",
                    help="show Vosk messages (reveals phrase words the model doesn't know)")
    args = ap.parse_args(argv)
    if not os.path.exists(args.config) and os.path.basename(args.config) == "commands.json":
        default = os.path.join(os.path.dirname(os.path.abspath(args.config)), "commands.default.json")
        if os.path.exists(default):   # first run: your own copy of the defaults
            import shutil
            shutil.copy2(default, args.config)

    make_dpi_aware()
    if args.where:
        try:
            run_where()
        except KeyboardInterrupt:
            pass
        return 0

    if args.list_windows or args.test_background:
        try:
            api = Win32Api()
        except RuntimeError as e:
            print(e)
            return 2
        if args.list_windows:
            run_list_windows(api)
            return 0
        try:
            cfg = load_config(args.config)
        except (ConfigError, FileNotFoundError) as e:
            print(f"Config problem: {e}")
            return 2
        return 0 if run_test_background(cfg, api) else 1

    if args.calibrate is not None:
        try:
            n = calibrate_cli(args.config, args.calibrate or None)
        except KeyboardInterrupt:
            n = 0
        except (OSError, json.JSONDecodeError) as e:
            print(f"Problem: {e}")
            return 2
        print(f"Done - {n} spot(s) saved to {args.config}." if n else "Nothing saved.")
        return 0

    if args.list_devices:
        import sounddevice as sd
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                print(f"{i:3d}  {d['name']}")
        return 0

    def load(show=True) -> Optional[Config]:
        try:
            c = load_config(args.config)
        except (ConfigError, FileNotFoundError) as e:
            print(f"Config problem: {e}")
            return None
        for w in c.warnings:
            print(f"[warn] {w}")
        if args.dry_run:
            c.settings["require_eve_focus"] = False  # nothing is really pressed, so skip the focus check
        return c

    cfg = load()
    if cfg is None:
        return 2
    print(f"Loaded {len(cfg.commands)} commands, mode = {cfg.mode}")
    try:
        app = build_app(cfg, dry_run=args.dry_run, use_ptt=not args.text)
    except (ConfigError, RuntimeError) as e:
        print(f"Problem: {e}")
        return 2

    def voice_calibrate(spot: str) -> bool:
        try:
            targets = _targets_for_name(_read_raw(args.config), spot)
            if not targets:
                print(f"  no spot named '{spot}' in \"spots\"")
                return False
            return calibrate_targets(args.config, targets) > 0
        except Exception as e:  # never kill the voice loop over this
            print(f"  calibration failed: {e}")
            return False

    app.calibrate_fn = voice_calibrate

    try:
        if args.text:
            run_text(app, reload_fn=load)
        else:
            model_dir = cfg.model_path
            if not os.path.isabs(model_dir):
                model_dir = os.path.join(APP_DIR, model_dir)
            run_mic(cfg, app, model_dir, verbose=args.verbose, reload_fn=load)
    except KeyboardInterrupt:
        print("\nBye.")
    except ConfigError as e:
        print(f"Problem: {e}")
        return 2
    finally:
        app.hold.release("stopped")    # never leave a key held down
    return 0


if __name__ == "__main__":
    sys.exit(main())
