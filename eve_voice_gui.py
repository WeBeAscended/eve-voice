#!/usr/bin/env python3
"""
EVE Voice - desktop app (tkinter/ttk, no extra dependencies).

This is only a front end. All voice/command/input logic lives in eve_voice.py
(config loader, VoiceApp, senders, calibration overlay) and is reused here, so
the same rules apply everywhere:
  * one input per word/phrase (one key/chord OR one single click), never chained
  * input only to the active EVE window (or, experimental, ONE named client)
  * no delays between inputs, no memory reading, no hooks, no log scraping

Key capture in dialogs uses ordinary tkinter key events inside that dialog only.
"""
from __future__ import annotations

import colorsys
import copy
import json
import logging
import os
import queue
import sys
import threading
import time
import traceback
import webbrowser
from collections import OrderedDict
from typing import Callable, Dict, List, Optional, Tuple

import eve_voice as ev

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(APP_DIR, "commands.json")
try:
    from version import VERSION
except Exception:  # pragma: no cover
    VERSION = "dev"
RESTART_CODE = 75   # launcher.py: 'restart me and apply the staged update'
STATE_PATH = os.path.join(APP_DIR, "gui_state.json")
LOG_PATH = os.path.join(APP_DIR, "eve_voice.log")
MODEL_DIR = os.path.join(APP_DIR, "model")
README_PATH = os.path.join(APP_DIR, "README.md")

log = logging.getLogger("eve_voice_gui")


def setup_logging(path: str = LOG_PATH) -> None:
    try:
        logging.basicConfig(filename=path, level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s")
    except OSError:
        logging.basicConfig(level=logging.INFO)


# =============================================================================
#  Model layer (no tkinter) - unit tested
# =============================================================================
class ValidationError(Exception):
    """Friendly message for the user."""


_KEY_LABEL = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "space": "Space", "enter": "Enter",
              "esc": "Esc", "tab": "Tab", "backspace": "Backspace", "delete": "Delete",
              "insert": "Insert", "home": "Home", "end": "End", "pageup": "Page Up",
              "pagedown": "Page Down", "up": "Up", "down": "Down", "left": "Left", "right": "Right",
              "capslock": "Caps Lock", "scrolllock": "Scroll Lock", "numlock": "Num Lock",
              "printscreen": "Print Screen", "pause": "Pause"}


def key_label(spec: str) -> str:
    """'alt+f1' -> 'Alt+F1', 'd' -> 'D', 'num3' -> 'Num 3'."""
    out = []
    for part in spec.split("+"):
        p = part.strip().lower()
        if p in _KEY_LABEL:
            out.append(_KEY_LABEL[p])
        elif p.startswith("num") and p[3:].isdigit():
            out.append(f"Num {p[3:]}")
        else:
            out.append(p.upper())
    return "+".join(out)


def describe_spec(spec) -> str:
    """Human-friendly action text for the command table."""
    if spec == "" or spec is None:
        return "Not set - pick a key"
    if isinstance(spec, str):
        return ("Keys: " if "+" in spec else "Key: ") + key_label(spec)
    if isinstance(spec, dict):
        if "steps" in spec:
            return "INVALID: combo (not allowed)"
        if "key" in spec:
            base = describe_spec(spec.get("key") or "")
            k = base.split(": ", 1)[-1]
            mode, secs = hold_mode_of(spec)
            if mode == "timed":
                return f"Hold {k} for {secs:g} s"
            if mode == "click":
                return f"Hold {k} until you click (max {secs:g} s)"
            if mode == "release":
                return f"Hold {k} until you say 'release' (max {secs:g} s)"
            return base
        if "release" in spec:
            return "Let go of the held key"
        if "calibrate" in spec:
            return f"Setup: set spot '{spec['calibrate']}' (no game input)"
        if "teach" in spec:
            return "Setup: open Teach (no game input)"
        if "mouse" in spec:
            btn = str(spec.get("mouse") or "").capitalize() or "?"
            if "find_color" in spec:
                return f"{btn} click on the yellow {spec['find_color'].replace('_', ' ')} (screen, opt-in)"
            if "mouse_spot" in spec:
                return f"{btn} click at saved spot: {spec['mouse_spot']}"
            if "mouse_box" in spec:
                return f"{btn} click at its own saved box"
            if "mouse_at" in spec:
                return f"{btn} click at {spec['mouse_at']}"
            return f"{btn} click at cursor"
    return "?"


def spec_options(spec) -> dict:
    """The per-command options of a spec: also / hold / cooldown."""
    if isinstance(spec, dict):
        return {k: spec[k] for k in ev._OPTION_FIELDS if k in spec}
    return {}


def spec_alternates(spec) -> List[str]:
    also = spec_options(spec).get("also") or []
    return [also] if isinstance(also, str) else list(also)


def with_options(spec, also=None, hold=None, cooldown=None):
    """Attach options to ONE action. A plain key with no options stays a plain string."""
    also = [ev.normalize_phrase(a) for a in (also or []) if ev.normalize_phrase(a)]
    if isinstance(spec, str):
        if not (also or hold is not None or cooldown is not None):
            return spec
        spec = OrderedDict([("key", spec)])
    else:
        spec = OrderedDict((k, v) for k, v in spec.items() if k not in ("also", "hold", "cooldown"))
    if also:
        spec["also"] = also
    if hold is not None:
        spec["hold"] = hold
    if cooldown is not None:
        spec["cooldown"] = cooldown
    return spec


def describe_action_short(key_spec: str) -> str:
    """Engine key_spec ('d', 'alt+f1', 'left click', 'left click @ 1,2') -> friendly text."""
    if key_spec.startswith("hold ") or key_spec.startswith("release"):
        parts = key_spec.split(" ", 2)
        return f"holding {key_label(parts[1])} {parts[2]}" if key_spec.startswith("hold ") else "let go"
    if "click" in key_spec:
        return key_spec.replace("@", "at") if "@" in key_spec else f"{key_spec} at cursor"
    return f"pressed {key_label(key_spec)}"


def hold_mode_of(spec) -> Tuple[str, Optional[float]]:
    """('tap'|'timed'|'click'|'release', seconds) for a key spec."""
    if not isinstance(spec, dict):
        return "tap", None
    if spec.get("hold_seconds") not in (None, ""):
        return "timed", float(spec["hold_seconds"])
    until = spec.get("hold_until")
    if until in ("click", "release"):
        m = spec.get("hold_max")
        return until, float(m) if m not in (None, "") else ev.HOLD_DEFAULT_MAX[until]
    return "tap", None


def with_hold(spec, mode: str, seconds: Optional[float] = None):
    """Set how a key command is held. Still ONE key: tap, or one keyDown + one keyUp."""
    if isinstance(spec, str):
        spec = OrderedDict([("key", spec)])
    spec = OrderedDict((k, v) for k, v in spec.items() if k not in ("hold_seconds", "hold_until", "hold_max"))
    if mode == "timed":
        spec["hold_seconds"] = seconds if seconds is not None else 1.0
        spec.pop("hold", None)
    elif mode in ("click", "release"):
        spec["hold_until"] = mode
        if seconds is not None and seconds != ev.HOLD_DEFAULT_MAX[mode]:
            spec["hold_max"] = seconds
        spec.pop("hold", None)
    if list(spec) == ["key"]:
        return spec["key"]
    return spec


def build_spec(kind: str, key: str = "", button: str = "left", where: str = "cursor",
               spot: str = "", calibrate_spot: str = ""):
    """Dialog fields -> one JSON action. Only ever ONE action."""
    if kind == "key":
        return key
    if kind == "mouse":
        spec = OrderedDict([("mouse", button)])
        if where == "spot":
            spec["mouse_spot"] = spot
        return spec
    if kind == "calibrate":
        return OrderedDict([("calibrate", calibrate_spot)])
    if kind == "find":
        return OrderedDict([("mouse", button), ("find_color", spot or "next_gate")])
    raise ValidationError(f"Unknown action type '{kind}'")


def validate_command(phrase: str, spec, spots: dict, wake_word: str = "") -> Tuple[str, List[str]]:
    """Return (normalised phrase, warnings) or raise ValidationError. Enforces the 1:1
    rule by running the engine's own Command parser on the result."""
    norm = ev.normalize_phrase(phrase)
    if not norm:
        raise ValidationError("Type the word or phrase you'll say.")
    warnings = []
    if any(ch.isdigit() for ch in norm):
        warnings.append("Spell numbers as words (\"high one\", not \"high 1\").")
    if wake_word and norm == wake_word:
        raise ValidationError("That phrase is the same as your wake word.")
    if spec == "" or spec is None:
        raise ValidationError("Choose the key (click the box and press it).")
    if isinstance(spec, dict) and "mouse_spot" in spec and not spec["mouse_spot"]:
        raise ValidationError("Pick which saved spot to click.")
    if isinstance(spec, dict) and spec.get("calibrate") == "":
        raise ValidationError("Pick which saved spot to set.")
    fake_spots = {k: (v if v is not None else [0, 0, 1, 1]) for k, v in spots.items()}
    if isinstance(spec, dict) and spec.get("mouse_spot") and spec["mouse_spot"] not in fake_spots:
        fake_spots[spec["mouse_spot"]] = [0, 0, 1, 1]   # new spot, will be created
    fake_targets = {}
    if isinstance(spec, dict) and spec.get("find_color"):
        fake_targets[spec["find_color"]] = {"box": [0, 0, 2, 2], "rgb": [0, 0, 0], "sampled": True}
    try:
        cmd = ev.Command(norm, spec, "dialog", allow_mouse_at=True, spots=fake_spots,
                         color_targets=fake_targets, allow_screen_read=True)
    except ev.ConfigError as e:
        raise ValidationError(str(e))
    if wake_word and wake_word in cmd.alternates:
        raise ValidationError("An alternate is the same as your wake word.")
    return norm, warnings


def near_duplicates(phrase: str, existing: List[str], cutoff: float = 0.75) -> List[str]:
    """Phrases that sound/look too alike (the recogniser may mix them up)."""
    import difflib
    norm = ev.normalize_phrase(phrase)
    return [p for p in difflib.get_close_matches(norm, existing, n=3, cutoff=cutoff) if p != norm]


class ConfigModel:
    """In-memory editable commands.json that keeps every _note key and key order."""

    def __init__(self, path: str):
        self.path = path
        self.load()

    # ---- file
    def load(self) -> None:
        with open(self.path, "r", encoding="utf-8") as f:
            self.data = json.load(f, object_pairs_hook=OrderedDict)
        self.data.setdefault("settings", OrderedDict())
        self.data.setdefault("spots", OrderedDict())
        self.data.setdefault("target", OrderedDict([("mode", "foreground"), ("window_title", "")]))
        self.data.setdefault("groups", OrderedDict())
        self._saved = copy.deepcopy(self.data)

    revert = load

    @property
    def dirty(self) -> bool:
        return self.data != self._saved

    def check(self) -> ev.Config:
        """Run the engine's loader over the current data (raises ValidationError)."""
        try:
            return ev.load_config_dict(copy.deepcopy(self.data))
        except ev.ConfigError as e:
            raise ValidationError(str(e))

    def save(self) -> List[str]:
        cfg = self.check()
        ev.save_config_data(self.path, self.data)   # writes commands.json.bak first
        self._saved = copy.deepcopy(self.data)
        return cfg.warnings

    def snapshot(self) -> dict:
        return copy.deepcopy(self.data)

    # ---- groups
    def groups(self) -> List[str]:
        return [g for g, v in self.data["groups"].items() if not g.startswith("_") and isinstance(v, dict)]

    def group_enabled(self, group: str) -> bool:
        return self.data["groups"][group].get("enabled", True) is not False

    def set_group_enabled(self, group: str, on: bool) -> None:
        g = self.data["groups"][group]
        if on:
            if "enabled" in g:
                g["enabled"] = True
        else:
            g["enabled"] = False

    def _ensure_group(self, group: str) -> dict:
        group = group.strip()
        if not group or group.startswith("_"):
            raise ValidationError("Pick or type a group name.")
        if group not in self.data["groups"]:
            self.data["groups"][group] = OrderedDict([("commands", OrderedDict())])
        g = self.data["groups"][group]
        g.setdefault("commands", OrderedDict())
        return g

    # ---- commands
    def commands(self) -> List[Tuple[str, str, object]]:
        out = []
        for g in self.groups():
            for phrase, spec in (self.data["groups"][g].get("commands") or {}).items():
                if not phrase.startswith("_"):
                    out.append((g, phrase, spec))
        return out

    def find(self, phrase: str, alternates: bool = True) -> Optional[Tuple[str, str]]:
        """Command that already uses this phrase (as its phrase, or as an alternate)."""
        norm = ev.normalize_phrase(phrase)
        for g, p, spec in self.commands():
            if ev.normalize_phrase(p) == norm or (alternates and norm in spec_alternates(spec)):
                return g, p
        return None

    def all_phrases(self, exclude: Optional[Tuple[str, str]] = None) -> List[str]:
        out = []
        for g, p, spec in self.commands():
            if (g, p) != exclude:
                out.append(ev.normalize_phrase(p))
                out += spec_alternates(spec)
        return out

    def _check_alternates(self, spec, me: Optional[Tuple[str, str]], norm: str) -> None:
        for alt in spec_alternates(spec):
            hit = self.find(alt)
            if alt == norm:
                raise ValidationError(f"'{alt}' is the command's own phrase - no need to add it.")
            if hit and hit != me:
                raise ValidationError(f"'{alt}' is already used by '{hit[1]}'.")

    def add_alternate(self, group: str, phrase: str, alt: str) -> None:
        spec = self.get(group, phrase)
        alts = spec_alternates(spec) + [alt]
        o = spec_options(spec)
        new = with_options(spec, alts, o.get("hold"), o.get("cooldown"))
        self.edit_command(group, phrase, group, phrase, new)

    def color_target(self, name: str = "next_gate") -> dict:
        import gate_finder
        ct = self.data.setdefault("color_targets", OrderedDict())
        if name not in ct:
            ct[name] = OrderedDict(gate_finder.DEFAULT_TARGET)
        return ct[name]

    def set_color_target(self, name: str = "next_gate", **fields) -> None:
        t = self.color_target(name)
        for k, v in fields.items():
            t[k] = v

    def get(self, group: str, phrase: str):
        return self.data["groups"][group]["commands"][phrase]

    def _wake(self) -> str:
        s = self.data["settings"]
        return ev.normalize_phrase(s.get("wake_word") or "") if s.get("mode") == "wake_word" else ""

    def add_command(self, group: str, phrase: str, spec) -> List[str]:
        norm, warns = validate_command(phrase, spec, self.spots(), self._wake())
        hit = self.find(norm)
        if hit:
            raise ValidationError(f"'{norm}' already exists ('{hit[1]}', group {hit[0]}).")
        self._check_alternates(spec, None, norm)
        near = near_duplicates(norm, self.all_phrases())
        if near:
            warns.append(f"sounds a lot like {', '.join(repr(n) for n in near)} - the recogniser may mix them up")
        self._ensure_group(group)["commands"][norm] = spec
        self._ensure_spot_for(spec)
        return warns

    def edit_command(self, group: str, phrase: str, new_group: str, new_phrase: str, spec) -> List[str]:
        norm, warns = validate_command(new_phrase, spec, self.spots(), self._wake())
        hit = self.find(norm)
        if hit and hit != (group, phrase):
            raise ValidationError(f"'{norm}' already exists ('{hit[1]}', group {hit[0]}).")
        self._check_alternates(spec, (group, phrase), norm)
        near = near_duplicates(norm, self.all_phrases(exclude=(group, phrase)))
        if near:
            warns.append(f"sounds a lot like {', '.join(repr(n) for n in near)} - the recogniser may mix them up")
        cmds = self.data["groups"][group]["commands"]
        if new_group.strip() == group:   # rename in place: keep order and neighbouring _notes
            self.data["groups"][group]["commands"] = OrderedDict(
                ((norm, spec) if k == phrase else (k, v)) for k, v in cmds.items())
        else:
            target = self._ensure_group(new_group)
            del cmds[phrase]
            target["commands"][norm] = spec
        self._ensure_spot_for(spec)
        return warns

    def delete_command(self, group: str, phrase: str) -> None:
        del self.data["groups"][group]["commands"][phrase]

    def duplicate_command(self, group: str, phrase: str) -> str:
        base = f"{ev.normalize_phrase(phrase)} copy"
        extra = ["two", "three", "four", "five", "six", "seven", "eight", "nine"]
        new, n = base, 0
        while self.find(new):
            if n >= len(extra):
                raise ValidationError("Too many copies - rename one first.")
            new = f"{base} {extra[n]}"
            n += 1
        cmds = self.data["groups"][group]["commands"]
        items = list(cmds.items())
        idx = [k for k, _ in items].index(phrase)
        items.insert(idx + 1, (new, copy.deepcopy(cmds[phrase])))
        self.data["groups"][group]["commands"] = OrderedDict(items)
        return new

    # ---- settings / target
    def setting(self, key: str):
        return self.data["settings"].get(key, ev.DEFAULT_SETTINGS.get(key))

    def set_setting(self, key: str, value) -> None:
        if key not in ev.DEFAULT_SETTINGS:
            raise ValidationError(f"Unknown setting {key}")
        self.data["settings"][key] = value

    def target(self) -> dict:
        return self.data["target"]

    def set_target(self, mode: Optional[str] = None, window_title: Optional[str] = None) -> None:
        if mode is not None:
            if mode not in ev.TARGET_MODES:
                raise ValidationError(f"Unknown target mode {mode}")
            self.data["target"]["mode"] = mode
        if window_title is not None:
            self.data["target"]["window_title"] = window_title

    # ---- spots
    def spots(self) -> Dict[str, object]:
        return {k: v for k, v in self.data["spots"].items() if not k.startswith("_")}

    def set_spot(self, name: str, box) -> None:
        name = name.strip()
        if not name or name.startswith("_"):
            raise ValidationError("Give the spot a name.")
        self.data["spots"][name] = list(box) if box is not None else None
        if box is not None:
            self.data["settings"]["allow_mouse_at"] = True

    def clear_spot(self, name: str) -> None:
        self.data["spots"][name] = None

    def _ensure_spot_for(self, spec) -> None:
        if isinstance(spec, dict):
            name = spec.get("mouse_spot") or spec.get("calibrate")
            if name and name not in self.data["spots"]:
                self.data["spots"][name] = None

    def spot_users(self, name: str) -> List[str]:
        return [p for _, p, s in self.commands() if isinstance(s, dict) and s.get("mouse_spot") == name]


def command_status(model: ConfigModel, cfg: Optional[ev.Config], group: str, phrase: str, spec) -> str:
    """'On' / 'Off (group off)' / 'Not set' / 'Needs spot' / 'Skipped (see log)'."""
    if not model.group_enabled(group):
        return "Off (group off)"
    if isinstance(spec, dict) and spec.get("find_color"):
        if not model.setting("allow_screen_read"):
            return "Off (opt-in)"
        import gate_finder  # noqa - only for the setup check text
        if ev.color_target_problem(model.color_target(spec["find_color"])):
            return "Needs setup"
    if spec == "" or (isinstance(spec, dict) and "key" in spec and not spec["key"]):
        return "Not set"
    if isinstance(spec, dict) and spec.get("mouse_spot") and model.spots().get(spec["mouse_spot"]) is None:
        return "Needs spot"
    if cfg is not None and ev.normalize_phrase(phrase) not in cfg.commands:
        return "Skipped (see log)"
    return "On"


# ---- key capture (pure logic, fed by tk key events in the dialog only) ----
_MOD_KEYSYMS = {"Control_L": "ctrl", "Control_R": "ctrl", "Alt_L": "alt", "Alt_R": "alt",
                "Shift_L": "shift", "Shift_R": "shift", "Meta_L": "alt", "Meta_R": "alt"}
_KEYSYM_MAP = {"space": "space", "Return": "enter", "KP_Enter": "enter", "Escape": "esc",
               "Tab": "tab", "ISO_Left_Tab": "tab", "BackSpace": "backspace", "Delete": "delete",
               "Insert": "insert", "Home": "home", "End": "end", "Prior": "pageup", "Next": "pagedown",
               "Up": "up", "Down": "down", "Left": "left", "Right": "right", "Caps_Lock": "capslock",
               "Scroll_Lock": "scrolllock", "Num_Lock": "numlock", "Pause": "pause", "Print": "printscreen",
               "minus": "-", "equal": "=", "bracketleft": "[", "bracketright": "]", "semicolon": ";",
               "apostrophe": "'", "comma": ",", "period": ".", "slash": "/", "backslash": "\\",
               "grave": "`"}
_MOD_ORDER = ("ctrl", "alt", "shift")


def keysym_to_key(keysym: str) -> Optional[str]:
    if keysym in _KEYSYM_MAP:
        return _KEYSYM_MAP[keysym]
    if keysym.startswith("F") and keysym[1:].isdigit() and 1 <= int(keysym[1:]) <= 12:
        return keysym.lower()
    if keysym.startswith("KP_") and keysym[3:].isdigit():
        return f"num{keysym[3:]}"
    if len(keysym) == 1 and keysym.isalnum() and keysym.isascii():
        return keysym.lower()
    return None


class ChordCapture:
    """Turns press/release keysyms into ONE key or ONE chord (e.g. 'alt+f1').
    A modifier pressed and released alone becomes that single key (e.g. 'ctrl')."""

    def __init__(self):
        self.held: List[str] = []
        self.lone: Optional[str] = None

    def press(self, keysym: str) -> Optional[str]:
        mod = _MOD_KEYSYMS.get(keysym)
        if mod:
            if mod not in self.held:
                self.held.append(mod)
            self.lone = mod if len(self.held) == 1 else None
            return None
        key = keysym_to_key(keysym)
        self.lone = None
        if key is None:
            return None
        mods = [m for m in _MOD_ORDER if m in self.held]
        return "+".join(mods + [key])

    def release(self, keysym: str) -> Optional[str]:
        mod = _MOD_KEYSYMS.get(keysym)
        if not mod:
            return None
        result = mod if self.lone == mod else None
        if mod in self.held:
            self.held.remove(mod)
        self.lone = None
        return result


_PTT_KEYSYMS = {"Scroll_Lock": "scroll lock", "Caps_Lock": "caps lock", "Pause": "pause",
                "Insert": "insert", "Home": "home", "End": "end", "Prior": "page up", "Next": "page down",
                "Alt_R": "right alt", "Alt_L": "left alt", "Control_R": "right ctrl", "Control_L": "left ctrl",
                "Shift_R": "right shift", "Shift_L": "left shift", "space": "space", "Tab": "tab",
                "Num_Lock": "num lock", "grave": "`", "Menu": "menu", "App": "apps"}


def ptt_name_from_keysym(keysym: str) -> Optional[str]:
    """tk keysym -> push_to_talk_key name the engine understands (checked via ev.vk_code)."""
    if keysym in _PTT_KEYSYMS:
        name = _PTT_KEYSYMS[keysym]
    elif keysym.startswith("F") and keysym[1:].isdigit():
        name = keysym.lower()
    elif keysym.startswith("KP_") and keysym[3:].isdigit():
        name = f"num {keysym[3:]}"
    elif len(keysym) == 1 and keysym.isalnum() and keysym.isascii():
        name = keysym.lower()
    else:
        return None
    return name if ev.vk_code(name) is not None else None


# =============================================================================
#  Engine runner (background thread) - reuses eve_voice.build_app / run_mic
# =============================================================================
def status_of(app: "ev.VoiceApp") -> str:
    mode = app.cfg.mode
    if mode == "push_to_talk":
        return "listening" if app.gate.audio_enabled() else "ptt_wait"
    if mode == "wake_word":
        return "wake_active" if app.gate.wake_active() else "wake_wait"
    return "listening"


def default_runner(cfg, app, stop_event, tick, reload_fn, model_dir):
    ev.run_mic(cfg, app, model_dir, reload_fn=reload_fn, stop_event=stop_event, tick_cb=tick)


DEMO_SCRIPT = [("warp", 0.96), ("[unk]", 0.41), ("align", 0.62), ("select", 0.93), ("warp align", 0.9),
               ("approach", 0.94), ("orbit", 0.88), ("keep at range", 0.91), ("select next gate", 0.9),
               ("lock", 0.85)]
DEMO_FREE = ["warp to the gate", "open cargo", "dock please"]


def demo_hear(app, i: int, rnd) -> Tuple[str, Optional[float]]:
    """What the fake microphone 'hears' next (also answers Practice / Teach listening)."""
    cap = app.capture
    if cap is not None and cap.free:
        return DEMO_FREE[i % len(DEMO_FREE)], 0.83
    if cap is not None and cap.hint:
        r = rnd.random()
        if r < 0.65:
            return cap.hint, 0.8 + rnd.random() * 0.19
        if r < 0.8:
            return cap.hint, 0.45 + rnd.random() * 0.2
        near = [p for p in app.cfg.all_phrases() if p != cap.hint and p.split()[0] == cap.hint.split()[0]]
        return (rnd.choice(near) if near else "[unk]"), 0.7 + rnd.random() * 0.2
    text, conf = DEMO_SCRIPT[i % len(DEMO_SCRIPT)]
    if app.cfg.mode == "wake_word" and text != "[unk]":
        text = f"{app.cfg.wake_word} {text}"
    return text, conf


def demo_runner(cfg, app, stop_event, tick, reload_fn, model_dir):
    """Fake microphone for demos/tests: 'hears' a few phrases. Only used with --demo."""
    import random
    rnd = random.Random(7)
    i = n = 0
    while not stop_event.wait(0.1):
        tick(app, rnd.random() * 0.6)
        if i % 12 == 3:
            text, conf = demo_hear(app, n, rnd)
            app._emit("partial", text.split()[0][:3], "")
            app._emit("partial", "", "")
            app.hear(text, conf)
            n += 1
        i += 1
        if app.config_changed and reload_fn:
            new = reload_fn()
            if new is not None:
                app.apply_config(new)
            app.config_changed = False


def friendly_error(e: BaseException) -> str:
    text = str(e) or e.__class__.__name__
    low = text.lower()
    if "portaudio" in low or "sounddevice" in low or "input device" in low or "invalid device" in low:
        return ("Couldn't open the microphone.\n\nCheck it's plugged in, pick it under Settings > "
                "Microphone, and allow desktop apps to use the mic in Windows privacy settings.")
    if "vosk" in low or "model" in low:
        return f"The speech model couldn't be loaded. Run setup.bat again.\n\n({text})"
    if "pydirectinput" in low or "no key-sending" in low:
        return "Couldn't load the keyboard library. Run setup.bat again."
    return f"Something went wrong:\n\n{text}\n\nDetails were written to eve_voice.log."


class Engine:
    """Runs the voice loop in a thread. Everything it reports goes through `out` (a Queue),
    so only the GUI thread touches tkinter."""

    def __init__(self, data_fn: Callable[[], dict], out: "queue.Queue", test_mode: bool,
                 runner: Callable = default_runner, model_dir: str = MODEL_DIR):
        self.data_fn, self.out, self.test_mode = data_fn, out, test_mode
        self.runner, self.model_dir = runner, model_dir
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.app: Optional[ev.VoiceApp] = None
        self.pending_capture: Optional[ev.Capture] = None

    def capture(self, cap: Optional[ev.Capture]) -> None:
        """Practice / Teach listening (sends nothing). Applied as soon as the listener is up."""
        if self.app is not None:
            self.app.capture = cap
        else:
            self.pending_capture = cap

    def reload(self) -> None:
        """Commands changed: rebuild the grammar without restarting the microphone."""
        if self.app is not None:
            self.app.config_changed = True

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="eve-voice-engine", daemon=True)
        self.thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout)
        self.thread = None

    def _post(self, *msg) -> None:
        self.out.put(msg)

    def _load(self) -> Optional[ev.Config]:
        try:
            cfg = ev.load_config_dict(self.data_fn())
        except ev.ConfigError as e:
            self._post("error", f"Your commands/settings have a problem:\n\n{e}")
            return None
        if self.test_mode:
            cfg.settings["require_eve_focus"] = False
        return cfg

    def _calibrate(self, spot: str) -> bool:
        """Voice 'set way point spot': ask the GUI thread to show the overlay, wait for it."""
        done = threading.Event()
        result = {"ok": False}
        self._post("calibrate", spot, done, result)
        done.wait(600)
        return bool(result["ok"])

    def _tick(self, app, level: float) -> None:
        self._post("tick", status_of(app), level)

    def _run(self) -> None:
        try:
            cfg = self._load()
            if cfg is None:
                return
            for w in cfg.warnings:
                self._post("log", f"[warn] {w}")
            app = ev.build_app(cfg, dry_run=self.test_mode, log=lambda m: self._post("log", m),
                               calibrate_fn=self._calibrate, teach_fn=lambda: self._post("teach"),
                               on_event=lambda k, p, d: self._post("event", k, p, d))
            app.capture, self.pending_capture = self.pending_capture, None
            self.app = app
            self._post("started", cfg.mode, cfg.push_to_talk_key, cfg.wake_word, len(cfg.commands))
            self.runner(cfg, app, self.stop_event, self._tick, self._load, self.model_dir)
        except ev.ConfigError as e:
            self._post("error", str(e))
        except Exception as e:  # never show a traceback to the user
            log.exception("engine crashed")
            self._post("error", friendly_error(e))
        finally:
            if self.app is not None:
                self.app.hold.release("listener stopped")   # never leave a key held down
            self.app = None
            self._post("stopped")


def fire_test_command(data: dict, phrase: str, test_mode: bool, out: "queue.Queue",
                      countdown: int = 3, sleep=time.sleep) -> None:
    """Per-command Test button: after a countdown, run ONE command once through the
    normal checks (focus / 1:1). Runs in its own thread."""
    def post(*m):
        out.put(m)
    try:
        cfg = ev.load_config_dict(data)
        norm = ev.normalize_phrase(phrase)
        cmd = cfg.commands.get(norm)
        if cmd is None:
            post("log", f"[test] '{norm}' can't run: it isn't active (not set, group off, or needs a spot).")
            return
        if cmd.kind == "calibrate":
            post("log", "[test] that's a setup command - use Settings > Saved spots instead.")
            return
        app = ev.build_app(cfg, dry_run=test_mode, log=lambda m: post("log", m), use_ptt=False,
                           on_event=lambda k, p, d: post("event", k, p, d))
        for i in range(countdown, 0, -1):
            post("log", f"[test] '{norm}' in {i}...")
            sleep(1)   # countdown before the single test input - not a gap between inputs
        app.fire(cmd)
    except Exception as e:
        log.exception("test command failed")
        post("error", friendly_error(e))


# =============================================================================
#  Practice (pure logic) - results live in memory only
# =============================================================================
class PracticeStats:
    """Say a command, see what was heard. Flags unreliable phrases and mixed-up pairs."""
    MIN_TRIES = 3

    def __init__(self, threshold: float = 0.7, aliases: Optional[Dict[str, str]] = None):
        self.threshold = threshold
        self.aliases = aliases or {}
        self.tries: Dict[str, List[Tuple[Optional[str], Optional[float], str]]] = OrderedDict()

    def verdict(self, target: str, heard: Optional[str], conf: Optional[float]) -> str:
        if not heard or "[unk]" in heard:
            return "missed"
        main = self.aliases.get(heard, heard)
        if main == target:
            return "ok" if conf is None or conf >= self.threshold else "unsure"
        return "wrong"

    def record(self, target: str, heard: Optional[str], conf: Optional[float]) -> str:
        v = self.verdict(target, heard, conf)
        self.tries.setdefault(target, []).append((heard, conf, v))
        return v

    def summary(self, target: str) -> dict:
        t = self.tries.get(target, [])
        confs = [c for h, c, v in t if c is not None and v in ("ok", "unsure")]
        mixed: Dict[str, int] = {}
        for h, c, v in t:
            if v == "wrong":
                mixed[self.aliases.get(h, h)] = mixed.get(self.aliases.get(h, h), 0) + 1
        ok = sum(1 for _, _, v in t if v == "ok")
        return {"tries": len(t), "ok": ok, "rate": ok / len(t) if t else 0.0,
                "avg_conf": sum(confs) / len(confs) if confs else None, "mixed": mixed}

    def unreliable(self) -> List[Tuple[str, float]]:
        out = []
        for target in self.tries:
            s = self.summary(target)
            if s["tries"] >= self.MIN_TRIES and s["rate"] < 2 / 3:
                out.append((target, s["rate"]))
        return out

    def confusions(self) -> List[Tuple[str, str, int, int]]:
        """(said, heard instead, times, tries) - flagged after a few tries."""
        out = []
        for target in self.tries:
            s = self.summary(target)
            for other, n in s["mixed"].items():
                if n >= 2 or (s["tries"] >= self.MIN_TRIES and n / s["tries"] >= 1 / 3):
                    out.append((target, other, n, s["tries"]))
        return out


_SYNONYMS = {"high": ["gun", "weapon"], "mid": ["middle", "module"], "low": ["bottom", "lower"],
             "warp": ["engage", "warp out"], "align": ["line up"], "approach": ["go close"],
             "orbit": ["circle"], "select": ["pick"], "lock": ["target"], "stop": ["halt"],
             "drones": ["drone"], "scan": ["scanner"], "map": ["star map"], "fire": ["shoot"],
             "repair": ["rep"], "next": ["following"], "dock": ["dock up"], "jump": ["jump through"]}


def suggest_alternates(phrase: str, vocab: Optional[set], taken: List[str],
                       avoid: Optional[List[str]] = None) -> List[str]:
    """Other ways to say the SAME command that sound different from `avoid`."""
    import difflib
    words = ev.normalize_phrase(phrase).split()
    cands: List[str] = []
    for i, w in enumerate(words):
        for syn in _SYNONYMS.get(w, []):
            cands.append(" ".join(words[:i] + [syn] + words[i + 1:]))
    if len(words) == 2 and words[0] in ("high", "mid", "low"):
        cands.append(f"{words[0]} slot {words[1]}")
    out = []
    for c in cands:
        if c in taken or c in out or c == phrase or ev.unknown_words(c, vocab):
            continue
        if any(difflib.SequenceMatcher(None, c, a).ratio() >= 0.8 for a in (avoid or [])):
            continue
        out.append(c)
    return out[:4]


# =============================================================================
#  Speech models (download on demand)
# =============================================================================
MODELS = OrderedDict([
    ("fast", {"label": "Fast (small model, ~40 MB)", "path": "model",
              "folder": "vosk-model-small-en-us-0.15",
              "url": "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"}),
    ("accurate", {"label": "Accurate (larger English model, ~128 MB)", "path": "model-accurate",
                  "folder": "vosk-model-en-us-0.22-lgraph",
                  "url": "https://alphacephei.com/vosk/models/vosk-model-en-us-0.22-lgraph.zip"}),
])


def model_key_for_path(path: str) -> str:
    for k, m in MODELS.items():
        if os.path.normpath(path or "model") == os.path.normpath(m["path"]):
            return k
    return "fast"


def resolve_model_dir(path: str) -> str:
    return path if os.path.isabs(path or "") else os.path.join(APP_DIR, path or "model")


def model_installed(model_dir: str) -> bool:
    return os.path.isdir(os.path.join(model_dir, "am")) and os.path.isdir(os.path.join(model_dir, "graph"))


def download_model(key: str, dest_root: str, progress: Callable[[float, str], None],
                   cancel: Optional[threading.Event] = None, urlopen=None) -> str:
    """Download + unzip a model into dest_root/<path>. Returns the folder. Raises on failure."""
    import shutil
    import urllib.request
    import zipfile
    m = MODELS[key]
    urlopen = urlopen or urllib.request.urlopen
    dest = os.path.join(dest_root, m["path"])
    zpath = os.path.join(dest_root, m["folder"] + ".zip.part")
    with urlopen(m["url"]) as r, open(zpath, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            if cancel is not None and cancel.is_set():
                raise RuntimeError("download cancelled")
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            progress(got / total if total else 0.0, f"{got / 1e6:.0f} of {total / 1e6:.0f} MB")
    progress(1.0, "Unzipping...")
    tmp = os.path.join(dest_root, m["path"] + ".unzip")
    shutil.rmtree(tmp, ignore_errors=True)
    with zipfile.ZipFile(zpath) as z:
        z.extractall(tmp)
    inner = os.path.join(tmp, m["folder"])
    if not model_installed(inner):
        raise RuntimeError("the download doesn't look like a speech model")
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(inner, dest)
    shutil.rmtree(tmp, ignore_errors=True)
    os.remove(zpath)
    return dest


def load_state(path: str = STATE_PATH) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state: dict, path: str = STATE_PATH) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except OSError:
        log.warning("couldn't save GUI state")


def list_microphones() -> Tuple[List[Tuple[Optional[int], str]], Optional[str]]:
    """[(device index or None, label)], error text."""
    default = [(None, "Windows default microphone")]
    try:
        import sounddevice as sd
        devs = [(i, f"{d['name']}") for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]
        return default + devs, None
    except Exception as e:
        return default, f"Microphone list not available here ({e.__class__.__name__})."


class MicMonitor:
    """Live level meter while NOT listening (while listening, the engine reports the level)."""

    def __init__(self):
        self.level = 0.0
        self.stream = None

    def start(self, device) -> Optional[str]:
        self.stop()
        try:
            import sounddevice as sd
            info = sd.query_devices(device, "input")
            rate = int(info["default_samplerate"])

            def cb(indata, frames, t, status):
                self.level = ev.chunk_level(bytes(indata))
            self.stream = sd.RawInputStream(samplerate=rate, blocksize=rate // 20, device=device,
                                            dtype="int16", channels=1, callback=cb)
            self.stream.start()
            return None
        except Exception as e:
            self.stream = None
            return friendly_error(e)

    def stop(self) -> None:
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
        self.stream = None
        self.level = 0.0


# =============================================================================
#  GUI widgets
# =============================================================================
try:                                                   # the model layer above works without tk
    import tkinter as tk                               # noqa: E402
    from tkinter import ttk, messagebox, simpledialog  # noqa: E402
    _Frame, _Toplevel, _Tk = ttk.Frame, tk.Toplevel, tk.Tk
except ImportError:                                    # pragma: no cover - only on Pythons without tk
    tk = ttk = messagebox = simpledialog = None
    _Frame = _Toplevel = _Tk = object

COL = {"bg": "#101318", "panel": "#181c23", "panel2": "#1f242d", "line": "#2b323d",
       "fg": "#d6dde6", "dim": "#8a96a6", "accent": "#d9a441", "accent2": "#3fb6c9",
       "ok": "#4cc38a", "warn": "#e2a03f", "bad": "#e05d5d", "entry": "#0c0f13"}
FONT = "Segoe UI" if sys.platform == "win32" else "DejaVu Sans"
MONO = "Consolas" if sys.platform == "win32" else "DejaVu Sans Mono"


def apply_theme(root) -> None:
    st = ttk.Style(root)
    st.theme_use("clam")
    f = (FONT, 10)
    root.configure(bg=COL["bg"])
    root.option_add("*TCombobox*Listbox.background", COL["panel2"])
    root.option_add("*TCombobox*Listbox.foreground", COL["fg"])
    root.option_add("*TCombobox*Listbox.selectBackground", COL["accent2"])
    st.configure(".", background=COL["bg"], foreground=COL["fg"], fieldbackground=COL["entry"],
                 bordercolor=COL["line"], lightcolor=COL["panel2"], darkcolor=COL["bg"],
                 troughcolor=COL["entry"], focuscolor=COL["accent2"], font=f,
                 selectbackground=COL["accent2"], selectforeground="#000000", insertcolor=COL["fg"])
    st.configure("TLabel", background=COL["bg"], foreground=COL["fg"])
    for name, color in (("Dim", "dim"), ("Warn", "warn"), ("Bad", "bad"), ("Ok", "ok")):
        st.configure(f"{name}.TLabel", foreground=COL[color])
    st.configure("Title.TLabel", font=(FONT, 16, "bold"), foreground=COL["accent"])
    st.configure("Big.TLabel", font=(FONT, 13))
    st.configure("Accent.TLabel", foreground=COL["accent"])
    st.configure("TLabelframe", background=COL["bg"], bordercolor=COL["line"])
    st.configure("TLabelframe.Label", background=COL["bg"], foreground=COL["accent"])
    st.configure("TButton", background=COL["panel2"], foreground=COL["fg"], padding=(10, 5),
                 bordercolor=COL["line"])
    st.map("TButton", background=[("active", COL["line"]), ("disabled", COL["panel"])],
           foreground=[("disabled", COL["dim"])])
    st.configure("Accent.TButton", background=COL["accent"], foreground="#111111",
                 font=(FONT, 14, "bold"), padding=(24, 12))
    st.map("Accent.TButton", background=[("active", "#f0bb57")])
    st.configure("Primary.TButton", background=COL["accent"], foreground="#111111", font=(FONT, 10, "bold"))
    st.map("Primary.TButton", background=[("active", "#f0bb57")])
    st.configure("Stop.TButton", background=COL["bad"], foreground="#111111",
                 font=(FONT, 14, "bold"), padding=(24, 12))
    st.map("Stop.TButton", background=[("active", "#f07a7a")])
    for w in ("TCheckbutton", "TRadiobutton"):
        st.configure(w, background=COL["bg"], foreground=COL["fg"], indicatorbackground=COL["entry"],
                     indicatorforeground=COL["entry"], upperbordercolor=COL["dim"], lowerbordercolor=COL["dim"],
                     indicatormargin=(0, 0, 6, 0))
        st.map(w, background=[("active", COL["bg"])],
               indicatorbackground=[("selected", COL["accent2"]), ("active", COL["panel2"])],
               indicatorforeground=[("selected", "#000000")],
               upperbordercolor=[("selected", COL["accent2"])], lowerbordercolor=[("selected", COL["accent2"])])
    for w in ("TEntry", "TSpinbox", "TCombobox"):
        st.configure(w, fieldbackground=COL["entry"], foreground=COL["fg"], arrowcolor=COL["fg"])
    st.map("TCombobox", fieldbackground=[("readonly", COL["entry"])], foreground=[("readonly", COL["fg"])])
    st.configure("TNotebook", background=COL["bg"], bordercolor=COL["line"], tabmargins=(6, 6, 6, 0))
    st.configure("TNotebook.Tab", background=COL["panel"], foreground=COL["dim"], padding=(16, 7))
    st.map("TNotebook.Tab", background=[("selected", COL["panel2"])], foreground=[("selected", COL["accent"])])
    st.configure("Treeview", background=COL["panel"], fieldbackground=COL["panel"], foreground=COL["fg"],
                 rowheight=26, bordercolor=COL["line"])
    st.map("Treeview", background=[("selected", COL["accent2"])], foreground=[("selected", "#000000")])
    st.configure("Treeview.Heading", background=COL["panel2"], foreground=COL["accent"], relief="flat")
    st.map("Treeview.Heading", background=[("active", COL["line"])])
    st.configure("Horizontal.TProgressbar", background=COL["ok"], troughcolor=COL["entry"], bordercolor=COL["line"])
    st.configure("Horizontal.TScale", background=COL["accent2"], troughcolor=COL["entry"])


class KeyCaptureBox(_Frame):
    """'Click here and press the key' box. Listens to tk key events on THIS widget only
    (no global keyboard hook)."""

    def __init__(self, master, initial: str = "", mode: str = "chord", on_change=None):
        super().__init__(master)
        self.mode, self.on_change = mode, on_change
        self.value = initial or ""
        self.cap = ChordCapture()
        self.lbl = tk.Label(self, text="", width=26, anchor="w", bg=COL["entry"], fg=COL["fg"],
                            relief="flat", padx=8, pady=6, highlightthickness=2,
                            highlightbackground=COL["line"], highlightcolor=COL["accent"], takefocus=1)
        self.lbl.pack(side="left", fill="x", expand=True)
        ttk.Button(self, text="Clear", command=self.clear).pack(side="left", padx=(6, 0))
        self.lbl.bind("<Button-1>", lambda e: self.lbl.focus_set())
        self.lbl.bind("<FocusIn>", lambda e: self._show(listening=True))
        self.lbl.bind("<FocusOut>", lambda e: self._show())
        self.lbl.bind("<KeyPress>", self._press)
        self.lbl.bind("<KeyRelease>", self._release)
        self._show()

    def _text(self, v: str) -> str:
        return key_label(v) if self.mode == "chord" else v.title()

    def _show(self, listening: bool = False) -> None:
        if listening:
            self.lbl.configure(text=(f"{self._text(self.value)}   - press another key to change"
                                     if self.value else "Press the key now..."), fg=COL["accent"])
        else:
            self.lbl.configure(text=self._text(self.value) if self.value else "Click here and press the key",
                               fg=COL["fg"] if self.value else COL["dim"])

    def set(self, v: str) -> None:
        self.value = v
        try:
            focused = self.focus_get() == self.lbl
        except (KeyError, tk.TclError):
            focused = False
        self._show(listening=focused)
        if self.on_change:
            self.on_change(v)

    def clear(self) -> None:
        self.cap = ChordCapture()
        self.set("")

    def feed(self, keysym: str, release: bool = False) -> None:
        """Same path as a real key event (also used by the GUI smoke test)."""
        if self.mode == "chord":
            r = self.cap.release(keysym) if release else self.cap.press(keysym)
        else:
            r = None if release else ptt_name_from_keysym(keysym)
        if r:
            self.set(r)

    def _press(self, e):
        self.feed(e.keysym)
        return "break"

    def _release(self, e):
        self.feed(e.keysym, release=True)
        return "break"


class LevelMeter(_Frame):
    """Live mic level with the noise-gate threshold drawn as a line."""

    def __init__(self, master, width=200, height=14):
        super().__init__(master)
        self.w, self.h = width, height
        self.cv = tk.Canvas(self, width=width, height=height, bg=COL["entry"], highlightthickness=1,
                            highlightbackground=COL["line"])
        self.cv.pack()
        self.bar = self.cv.create_rectangle(0, 0, 0, height, fill=COL["ok"], outline="")
        self.line = self.cv.create_line(0, 0, 0, height, fill=COL["bad"], width=2)
        self.value, self.gate = 0.0, 0.0

    def set(self, level: float, gate: Optional[float] = None) -> None:
        self.value = max(0.0, min(1.0, float(level)))
        if gate is not None:
            self.gate = max(0.0, min(1.0, float(gate)))
        x = int(self.value * self.w)
        self.cv.coords(self.bar, 0, 0, x, self.h)
        self.cv.itemconfigure(self.bar, fill=COL["ok"] if self.value >= self.gate else COL["dim"])
        gx = int(self.gate * self.w)
        self.cv.coords(self.line, gx, 0, gx, self.h)
        self.cv.itemconfigure(self.line, state="normal" if self.gate > 0 else "hidden")


class CommandDialog(_Toplevel):
    """Add / edit ONE command. It can only ever produce ONE action."""

    def __init__(self, app: "MainWindow", title: str, group: str = "", phrase: str = "", spec=""):
        super().__init__(app)
        self.app, self.result = app, None
        self.orig = (group, phrase)
        self.title(title)
        self.configure(bg=COL["bg"])
        self.transient(app)
        self.resizable(False, False)
        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="What you say").grid(row=0, column=0, sticky="w")
        self.phrase = tk.StringVar(value=phrase)
        self.phrase_entry = ttk.Entry(body, textvariable=self.phrase, width=36)
        self.phrase_entry.grid(row=0, column=1, sticky="we", pady=3)
        self.vocab_lbl = ttk.Label(body, text="", style="Dim.TLabel", wraplength=400)
        self.vocab_lbl.grid(row=1, column=1, sticky="w")
        self.phrase.trace_add("write", lambda *a: self._check_phrase())

        ttk.Label(body, text="Group").grid(row=2, column=0, sticky="w", pady=(8, 0))
        groups = app.model.groups()
        self.group = tk.StringVar(value=group or (groups[0] if groups else "custom"))
        ttk.Combobox(body, textvariable=self.group, values=groups, width=34).grid(
            row=2, column=1, sticky="we", pady=(8, 0))

        kind, key, button, where, spot, cal = "key", "", "left", "cursor", "", ""
        self.find_name = "next_gate"
        if isinstance(spec, str):
            key = spec
        elif isinstance(spec, dict):
            if "key" in spec:
                key = spec.get("key") or ""
            elif "mouse" in spec:
                kind, button = "mouse", spec.get("mouse") or "left"
                if spec.get("mouse_spot"):
                    where, spot = "spot", spec["mouse_spot"]
                elif spec.get("find_color"):
                    where, self.find_name = "find", spec["find_color"]
            elif "calibrate" in spec:
                kind, cal = "calibrate", spec["calibrate"]
            elif "teach" in spec:
                kind = "teach"
            elif "release" in spec:
                kind = "release"
        opts = spec_options(spec)
        hmode, hsecs = hold_mode_of(spec)
        self.kind = tk.StringVar(value=kind)

        af = ttk.LabelFrame(body, text="  It does ONE thing  ", padding=10)
        af.grid(row=3, column=0, columnspan=2, sticky="we", pady=(14, 0))
        for col, (val, txt) in enumerate((("key", "Press a key"), ("mouse", "Mouse click"),
                                          ("calibrate", "Setup: set a spot"))):
            ttk.Radiobutton(af, text=txt, value=val, variable=self.kind,
                            command=self._kind_changed).grid(row=0, column=col, sticky="w", padx=(0, 14))
        self.holder = ttk.Frame(af)
        self.holder.grid(row=1, column=0, columnspan=3, sticky="we")

        self.key_frame = ttk.Frame(self.holder)
        self.keybox = KeyCaptureBox(self.key_frame, key)
        self.keybox.pack(fill="x", pady=(10, 2))
        ttk.Label(self.key_frame, style="Dim.TLabel", wraplength=420,
                  text="One key, or one shortcut chord EVE treats as a single key (e.g. Alt+F1).").pack(anchor="w")
        hf = ttk.Frame(self.key_frame); hf.pack(fill="x", pady=(8, 0))
        ttk.Label(hf, text="When said:").grid(row=0, column=0, sticky="nw", padx=(0, 8))
        self.hold_mode = tk.StringVar(value=hmode)
        self.hold_secs = {m: tk.StringVar(value=f"{(hsecs if m == hmode and hsecs else d):g}")
                          for m, d in (("timed", 3.0), ("click", 10.0), ("release", 30.0))}
        rows = (("tap", "Tap the key", None, None),
                ("timed", "Hold this key for", (0.1, 10, 0.1), "seconds (max 10)"),
                ("click", "Hold until I click (I click the target myself)", (0.5, 30, 0.5), "s safety limit"),
                ("release", "Hold until I say 'release'", (0.5, 30, 0.5), "s safety limit"))
        for i, (m, txt, rng, unit) in enumerate(rows):
            ttk.Radiobutton(hf, text=txt, value=m, variable=self.hold_mode).grid(row=i, column=1, sticky="w")
            if rng:
                ttk.Spinbox(hf, from_=rng[0], to=rng[1], increment=rng[2], textvariable=self.hold_secs[m],
                            width=5).grid(row=i, column=2, sticky="w", padx=6)
                ttk.Label(hf, text=unit, style="Dim.TLabel").grid(row=i, column=3, sticky="w")
        ttk.Label(self.key_frame, style="Dim.TLabel", wraplength=440, text=(
            "Still ONE input: one key down, then one key up. EVE's hold-and-click actions (lock = Ctrl, "
            "approach Q, orbit W, keep at range E, align A, warp to S) need 'Hold until I click'. Use 'Tap' "
            "if something is already selected and EVE acts on a plain key press.")).pack(anchor="w", pady=(4, 0))

        self.mouse_frame = ttk.Frame(self.holder)
        self.button = tk.StringVar(value=button)
        self.where = tk.StringVar(value=where)
        self.spot = tk.StringVar(value=spot)
        r = ttk.Frame(self.mouse_frame); r.pack(fill="x", pady=(10, 4))
        ttk.Label(r, text="Button").pack(side="left")
        ttk.Combobox(r, textvariable=self.button, values=list(ev.MOUSE_BUTTONS), state="readonly",
                     width=8).pack(side="left", padx=8)
        ttk.Label(r, text="single click", style="Dim.TLabel").pack(side="left")
        ttk.Radiobutton(self.mouse_frame, text="At the cursor", value="cursor", variable=self.where).pack(anchor="w")
        r2 = ttk.Frame(self.mouse_frame); r2.pack(fill="x", pady=(2, 0))
        ttk.Radiobutton(r2, text="At a saved spot", value="spot", variable=self.where).pack(side="left")
        self.spot_box = ttk.Combobox(r2, textvariable=self.spot, values=list(app.model.spots()), width=16)
        self.spot_box.pack(side="left", padx=8)
        ttk.Button(r2, text="Set spot...", command=self._set_spot).pack(side="left")
        ttk.Radiobutton(self.mouse_frame, text="On the yellow next gate in my Overview (found on screen, opt-in)",
                        value="find", variable=self.where).pack(anchor="w", pady=(2, 0))
        ttk.Label(self.mouse_frame, style="Dim.TLabel", wraplength=420,
                  text="No double clicks or Ctrl+click - that would be two inputs.").pack(anchor="w", pady=(6, 0))

        self.cal_frame = ttk.Frame(self.holder)
        self.cal_spot = tk.StringVar(value=cal)
        rr = ttk.Frame(self.cal_frame); rr.pack(fill="x", pady=(10, 2))
        ttk.Label(rr, text="Spot").pack(side="left")
        ttk.Combobox(rr, textvariable=self.cal_spot, values=list(app.model.spots()), width=16).pack(side="left", padx=8)
        ttk.Label(self.cal_frame, style="Dim.TLabel", wraplength=420,
                  text="Saying this opens the box-select overlay. It sends nothing to EVE.").pack(anchor="w")

        self.release_frame = ttk.Frame(self.holder)
        ttk.Label(self.release_frame, style="Dim.TLabel", wraplength=420,
                  text="Lets go of the key that's being held (its key-up). Sends nothing new.").pack(
            anchor="w", pady=(10, 0))
        self.teach_frame = ttk.Frame(self.holder)
        ttk.Label(self.teach_frame, style="Dim.TLabel", wraplength=420,
                  text="Saying this opens the Teach window. It sends nothing to EVE.").pack(anchor="w", pady=(10, 0))

        of = ttk.LabelFrame(body, text="  Options (all optional)  ", padding=10)
        of.grid(row=4, column=0, columnspan=2, sticky="we", pady=(12, 0))
        ttk.Label(of, text="Also accept").grid(row=0, column=0, sticky="w")
        self.also = tk.StringVar(value=", ".join(spec_alternates(spec)))
        ttk.Entry(of, textvariable=self.also, width=34).grid(row=0, column=1, columnspan=3, sticky="we", padx=6)
        ttk.Label(of, style="Dim.TLabel", wraplength=420,
                  text="Other ways you might say it (comma-separated). They all do the SAME one action.").grid(
            row=1, column=1, columnspan=3, sticky="w", padx=6)
        self.also.trace_add("write", lambda *a: self._check_phrase())
        ttk.Label(of, text="Tap length (s)").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.hold = tk.StringVar(value="" if opts.get("hold") is None else f"{opts['hold']:g}")
        ttk.Entry(of, textvariable=self.hold, width=6).grid(row=2, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(of, text="Repeat cooldown (s)").grid(row=2, column=2, sticky="e", pady=(6, 0))
        self.cool = tk.StringVar(value="" if opts.get("cooldown") is None else f"{opts['cooldown']:g}")
        ttk.Entry(of, textvariable=self.cool, width=6).grid(row=2, column=3, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(of, style="Dim.TLabel", text="Blank = use the Settings value.").grid(
            row=3, column=1, columnspan=3, sticky="w", padx=6)

        self.err = ttk.Label(body, text="", style="Bad.TLabel", wraplength=440)
        self.err.grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 0))
        bf = ttk.Frame(body); bf.grid(row=6, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(bf, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bf, text="OK", command=self.ok).pack(side="right", padx=8)
        self.bind("<Escape>", lambda e: self.destroy())
        self._kind_changed()
        self._check_phrase()
        self.phrase_entry.focus_set()
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def _kind_changed(self):
        for f in (self.key_frame, self.mouse_frame, self.cal_frame, self.teach_frame, self.release_frame):
            f.pack_forget()
        {"key": self.key_frame, "mouse": self.mouse_frame, "calibrate": self.cal_frame,
         "teach": self.teach_frame, "release": self.release_frame}[self.kind.get()].pack(fill="x")

    def _check_phrase(self):
        p = self.phrase.get()
        if hasattr(self, "also"):
            p = " ".join([p] + self.also.get().split(","))
        msgs = []
        bad = [w for w in ev.unknown_words(p, self.app.vocab) if not w.isdigit()]
        if bad:
            msgs.append("The speech model doesn't know " + ", ".join(f"'{w}'" for w in bad) +
                        " - try other words (e.g. 'way point').")
        if any(c.isdigit() for c in p):
            msgs.append("Spell numbers as words: 'high one', not 'high 1'.")
        if msgs:
            self.vocab_lbl.configure(text="  ".join(msgs), style="Warn.TLabel")
        elif p.strip() and self.app.vocab:
            self.vocab_lbl.configure(text="All words recognised.", style="Ok.TLabel")
        else:
            self.vocab_lbl.configure(text="", style="Dim.TLabel")

    def _set_spot(self):
        name = self.spot.get().strip().replace(" ", "_") or "my_spot"
        self.spot.set(name)
        self.where.set("spot")
        try:
            self.grab_release()
        except tk.TclError:
            pass

        def done(box):
            if self.winfo_exists():
                self.spot_box.configure(values=list(self.app.model.spots()))
                try:
                    self.grab_set()
                except tk.TclError:
                    pass
        self.app.calibrate_spot(name, done)

    @staticmethod
    def _num(text: str, what: str) -> Optional[float]:
        text = text.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            raise ValidationError(f"{what} must be a number of seconds (or blank).")

    def spec(self):
        if self.kind.get() == "teach":
            base = OrderedDict([("teach", True)])
        elif self.kind.get() == "release":
            base = OrderedDict([("release", True)])
        elif self.kind.get() == "mouse" and self.where.get() == "find":
            base = build_spec("find", button=self.button.get(), spot=self.find_name)
        else:
            base = build_spec(self.kind.get(), key=self.keybox.value, button=self.button.get(),
                              where=self.where.get(), spot=self.spot.get().strip(),
                              calibrate_spot=self.cal_spot.get().strip())
        also = [a for a in self.also.get().split(",") if a.strip()]
        spec = with_options(base, also, self._num(self.hold.get(), "Hold"),
                            self._num(self.cool.get(), "Cooldown"))
        mode = self.hold_mode.get()
        if self.kind.get() == "key" and mode != "tap":
            secs = self._num(self.hold_secs[mode].get(), "Hold time")
            spec = with_hold(spec, mode, secs)
        return spec

    def ok(self):
        try:
            spec = self.spec()
            m = self.app.model
            if self.orig[1]:
                warns = m.edit_command(self.orig[0], self.orig[1], self.group.get().strip(), self.phrase.get(), spec)
            else:
                warns = m.add_command(self.group.get().strip(), self.phrase.get(), spec)
        except ValidationError as e:
            self.err.configure(text=str(e))
            return
        self.result = (self.group.get().strip(), ev.normalize_phrase(self.phrase.get()), warns)
        self.destroy()


# =============================================================================
#  Teach ("watch and learn" - ONE input per phrase, never a recorded sequence)
# =============================================================================
TEACH_HINT = "Press ONE key, or click ONE spot (Esc to cancel)"
TEACH_SPOT_HALF = 6     # a saved spot is a small box around where you clicked


def check_teach_phrase(phrase: str, model: "ConfigModel", vocab: Optional[set]) -> Tuple[List[str], List[str]]:
    """(errors, warnings) for a taught phrase: model vocabulary, duplicates, near-duplicates."""
    norm = ev.normalize_phrase(phrase)
    errors, warns = [], []
    if not norm or "[unk]" in phrase or norm == "unk":
        return ["Say or type a phrase."], []
    if any(c.isdigit() for c in norm):
        errors.append("Spell numbers as words ('high one').")
    bad = [w for w in ev.unknown_words(norm, vocab) if not w.isdigit()]
    if bad:
        errors.append("The speech model doesn't know " + ", ".join(f"'{w}'" for w in bad) + ".")
    hit = model.find(norm)
    if hit:
        errors.append(f"'{norm}' is already used by '{hit[1]}' (group {hit[0]}).")
    if norm == model._wake():
        errors.append("That's your wake word.")
    near = near_duplicates(norm, model.all_phrases())
    if near and not hit:
        warns.append(f"Sounds a lot like {', '.join(repr(n) for n in near)} - it may mix them up.")
    return errors, warns


def teach_spec(captured: dict, click_mode: str, phrase: str, spots: dict):
    """Captured input -> (spec, spot_name, spot_box). ONE key/chord or ONE click, nothing else."""
    if "key" in captured:
        return build_spec("key", key=captured["key"]), None, None
    btn = captured["mouse"]
    if click_mode == "spot":
        base = "taught_" + ev.normalize_phrase(phrase).replace(" ", "_")
        name, i = base, 2
        while name in spots:
            name, i = f"{base}_{i}", i + 1
        x, y = int(captured["x"]), int(captured["y"])
        box = [x - TEACH_SPOT_HALF, y - TEACH_SPOT_HALF, x + TEACH_SPOT_HALF, y + TEACH_SPOT_HALF]
        return build_spec("mouse", button=btn, where="spot", spot=name), name, box
    return build_spec("mouse", button=btn, where="cursor"), None, None


def teach_card(phrase: str, captured: dict, click_mode: str) -> str:
    norm = ev.normalize_phrase(phrase)
    if "key" in captured:
        what = f"press {key_label(captured['key'])}"
    elif click_mode == "spot":
        what = f"{captured['mouse']} click at the saved spot ({captured['x']}, {captured['y']})"
    else:
        what = f"{captured['mouse']} click wherever your mouse cursor is"
    return f'When you say "{norm}"  ->  {what}'


class InputOverlay:
    """Semi-transparent always-on-top window that takes ONE key/chord or ONE click.
    Only this window's own tk events are used - no global keyboard/mouse hook."""
    BUTTONS = {1: "left", 2: "middle", 3: "right"}

    def __init__(self, master, on_done: Callable[[Optional[dict]], None], hint: str = TEACH_HINT,
                 show: bool = True):
        self.on_done, self.cap, self.done = on_done, ChordCapture(), False
        self.win = None
        if not show:
            return
        w = tk.Toplevel(master)
        self.win = w
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        try:
            w.attributes("-alpha", 0.35)
        except tk.TclError:
            pass
        if sys.platform == "win32":
            vx, vy, vw, vh = ev.virtual_screen()
        else:
            vx, vy, vw, vh = 0, 0, master.winfo_screenwidth(), master.winfo_screenheight()
        w.geometry(f"{vw}x{vh}{vx:+d}{vy:+d}")
        cv = tk.Canvas(w, bg="black", highlightthickness=0, cursor="crosshair")
        cv.pack(fill="both", expand=True)
        tx, ty = 40 - vx, 40 - vy
        cv.create_text(tx, ty, anchor="nw", fill="white", font=("Segoe UI", 20, "bold"),
                       text="EVE Voice - Teach")
        cv.create_text(tx, ty + 44, anchor="nw", fill="white", font=("Segoe UI", 14), text=hint)
        cv.bind("<ButtonPress>", lambda e: self.pointer(e.num, e.x_root, e.y_root))
        w.bind("<KeyPress>", lambda e: self.key(e.keysym))
        w.bind("<KeyRelease>", lambda e: self.key(e.keysym, release=True))
        w.after(50, lambda: (w.lift(), w.focus_force()))
        try:
            w.grab_set()
        except tk.TclError:
            pass

    def _finish(self, result: Optional[dict]) -> None:
        if self.done:
            return
        self.done = True
        if self.win is not None:
            try:
                self.win.destroy()
            except tk.TclError:
                pass
        self.on_done(result)

    def key(self, keysym: str, release: bool = False):
        if keysym == "Escape" and not release:
            return self._finish(None)
        r = self.cap.release(keysym) if release else self.cap.press(keysym)
        if r:
            self._finish({"key": r})
        return "break"

    def pointer(self, num: int, x: int, y: int):
        btn = self.BUTTONS.get(num)
        if btn:      # wheel / extra buttons ignored
            self._finish({"mouse": btn, "x": int(x), "y": int(y)})


class TeachDialog(_Toplevel):
    """A: say it -> B: show ONE input on our overlay -> C: confirm. Sends nothing until you
    Test (respects test mode) or use the saved command."""

    def __init__(self, app: "MainWindow"):
        super().__init__(app)
        self.app = app
        self.title("Teach a command")
        self.configure(bg=COL["bg"])
        self.resizable(False, False)
        self.phrase = tk.StringVar()
        self.captured: Optional[dict] = None
        self.click_mode = tk.StringVar(value="spot")
        self.group = tk.StringVar(value="taught")
        self.stage = ""
        self.overlay: Optional[InputOverlay] = None
        self._started_engine = False
        self._listen_job = None
        self.body = ttk.Frame(self, padding=18); self.body.pack(fill="both", expand=True)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda e: self.cancel())
        self.stage_a()

    # ---- helpers
    def _clear(self, stage):
        self.stage = stage
        for w in self.body.winfo_children():
            w.destroy()

    def _head(self, step, text):
        ttk.Label(self.body, text=f"Step {step} of 3", style="Dim.TLabel").pack(anchor="w")
        ttk.Label(self.body, text=text, style="Big.TLabel").pack(anchor="w", pady=(0, 8))

    # ---- A: say the phrase (full vocabulary, matching paused)
    def stage_a(self):
        self._clear("A")
        self._head(1, "Say the phrase you want to use")
        self.status = ttk.Label(self.body, text="", style="Dim.TLabel", wraplength=460)
        self.status.pack(anchor="w")
        e = ttk.Entry(self.body, textvariable=self.phrase, width=40, font=("Segoe UI", 12))
        e.pack(anchor="w", fill="x", pady=(8, 2))
        self.check = ttk.Label(self.body, text="", style="Dim.TLabel", wraplength=460)
        self.check.pack(anchor="w")
        self.phrase.trace_add("write", lambda *a: self._validate())
        bf = ttk.Frame(self.body); bf.pack(fill="x", pady=(14, 0))
        ttk.Button(bf, text="Cancel", command=self.cancel).pack(side="right")
        self.ok_btn = ttk.Button(bf, text="OK", style="Primary.TButton", command=self.to_b)
        self.ok_btn.pack(side="right", padx=8)
        ttk.Button(bf, text="Retry", command=self.listen).pack(side="left")
        self._validate()
        self.listen()

    def listen(self):
        """Pause command matching and listen ONCE with the full vocabulary."""
        eng = self.app.engine
        if not (eng and eng.running):
            self.app.start_engine()
            self._started_engine = True
            eng = self.app.engine
        eng.capture(ev.Capture(lambda t, c: self.app.events.put(("capture", "teach", t, c)),
                               free=True, once=True, timeout=8.0, hint="teach"))
        self.status.configure(text="Listening... say it now (commands are paused).", style="Accent.TLabel")
        if self._listen_job:
            self.after_cancel(self._listen_job)
        self._listen_job = self.after(9000, lambda: self.heard(None, None))

    def heard(self, text, conf):
        if self.stage != "A":
            return
        if self._listen_job:
            self.after_cancel(self._listen_job); self._listen_job = None
        if text and text.strip() and text != "[unk]":
            self.phrase.set(ev.normalize_phrase(text))
            pct = "" if conf is None else f" ({conf:.0%} sure)"
            self.status.configure(text=f"Heard \"{text}\"{pct}. Fix the text if needed, then OK.",
                                  style="Ok.TLabel")
        else:
            self.status.configure(text="Didn't catch that. Press Retry, or type the phrase.", style="Warn.TLabel")

    def _validate(self):
        if self.stage != "A":
            return [], []
        errs, warns = check_teach_phrase(self.phrase.get(), self.app.model, self.app.vocab)
        if not self.phrase.get().strip():
            self.check.configure(text="", style="Dim.TLabel")
        elif errs:
            self.check.configure(text="  ".join(errs), style="Bad.TLabel")
        elif warns:
            self.check.configure(text="  ".join(warns), style="Warn.TLabel")
        else:
            self.check.configure(text="All words recognised, not used yet.", style="Ok.TLabel")
        return errs, warns

    # ---- B: show ONE input on our own overlay
    def to_b(self):
        errs, _ = self._validate()
        if errs:
            return
        if self.app.engine is not None:
            self.app.engine.capture(None)      # back to normal (nothing is matched while teaching anyway)
        self._clear("B")
        self._head(2, "Show me the ONE input")
        ttk.Label(self.body, wraplength=460, text=(
            f"Next, a see-through overlay covers the screen: {TEACH_HINT}.\n"
            "Only this one key/chord or click is stored - never a sequence.")).pack(anchor="w")
        ttk.Label(self.body, style="Warn.TLabel", wraplength=460, text=(
            "EVE in exclusive fullscreen hides the overlay - use Windowed or Fixed window mode.")).pack(
            anchor="w", pady=(8, 0))
        bf = ttk.Frame(self.body); bf.pack(fill="x", pady=(14, 0))
        ttk.Button(bf, text="Cancel", command=self.cancel).pack(side="right")
        ttk.Button(bf, text="Show overlay", style="Primary.TButton", command=self.show_overlay).pack(
            side="right", padx=8)
        ttk.Button(bf, text="Back", command=self.stage_a).pack(side="left")

    def show_overlay(self):
        self.withdraw(); self.app.iconify()
        self.after(400, lambda: setattr(self, "overlay", self.app.input_overlay_fn(self.app, self.on_input)))

    def on_input(self, result: Optional[dict]):
        self.overlay = None
        try:
            self.app.deiconify(); self.deiconify(); self.lift()
        except tk.TclError:
            return
        if result is None:
            self.app.add_log("Teach: overlay cancelled.", "info")
            return self.to_b()
        self.captured = result
        if "mouse" in result:
            return self.stage_click()
        self.stage_c()

    def stage_click(self):
        self._clear("B2")
        self._head(2, f"{self.captured['mouse'].title()} click at ({self.captured['x']}, {self.captured['y']})")
        ttk.Radiobutton(self.body, text="Click here every time (saved spot)", value="spot",
                        variable=self.click_mode).pack(anchor="w")
        ttk.Radiobutton(self.body, text="Click wherever my cursor is", value="cursor",
                        variable=self.click_mode).pack(anchor="w", pady=(4, 0))
        bf = ttk.Frame(self.body); bf.pack(fill="x", pady=(14, 0))
        ttk.Button(bf, text="Cancel", command=self.cancel).pack(side="right")
        ttk.Button(bf, text="Next", style="Primary.TButton", command=self.stage_c).pack(side="right", padx=8)
        ttk.Button(bf, text="Back", command=self.to_b).pack(side="left")

    # ---- C: confirm
    def stage_c(self):
        self._clear("C")
        self._head(3, "Check and save")
        self.card = ttk.Label(self.body, text=teach_card(self.phrase.get(), self.captured, self.click_mode.get()),
                              style="Big.TLabel", wraplength=480)
        self.card.pack(anchor="w", pady=(4, 8))
        r = ttk.Frame(self.body); r.pack(anchor="w")
        ttk.Label(r, text="Group").pack(side="left")
        ttk.Combobox(r, textvariable=self.group, values=self.app.model.groups(), width=20).pack(side="left", padx=6)
        self.c_err = ttk.Label(self.body, text="", style="Bad.TLabel", wraplength=460); self.c_err.pack(anchor="w")
        ttk.Label(self.body, style="Dim.TLabel", wraplength=460, text=(
            "Save writes commands.json (old one kept as .bak) and the listener picks it up right away. "
            "Test sends it once after 3 s (or only logs it in test mode).")).pack(anchor="w", pady=(6, 0))
        bf = ttk.Frame(self.body); bf.pack(fill="x", pady=(14, 0))
        ttk.Button(bf, text="Cancel", command=self.cancel).pack(side="right")
        ttk.Button(bf, text="Save", style="Primary.TButton", command=self.save).pack(side="right", padx=8)
        ttk.Button(bf, text="Test", command=self.test).pack(side="right")
        ttk.Button(bf, text="Back", command=self.to_b).pack(side="left")

    def _spec(self):
        return teach_spec(self.captured, self.click_mode.get(), self.phrase.get(), self.app.model.spots())

    def test(self):
        spec, spot, box = self._spec()
        data = copy.deepcopy(self.app.model.data)
        if spot:
            data.setdefault("spots", OrderedDict())[spot] = box
        g = self.group.get().strip() or "taught"
        data.setdefault("groups", OrderedDict()).setdefault(g, OrderedDict([("enabled", True),
                                                                           ("commands", OrderedDict())]))
        data["groups"][g].setdefault("commands", OrderedDict())[ev.normalize_phrase(self.phrase.get())] = spec
        data["groups"][g]["enabled"] = True
        threading.Thread(target=self.app.test_fn, daemon=True,
                         args=(data, self.phrase.get(), self.app.test_mode.get(), self.app.events)).start()

    def save(self) -> bool:
        spec, spot, box = self._spec()
        m = self.app.model
        g = self.group.get().strip() or "taught"
        try:
            if spot:
                m.set_spot(spot, box)
            warns = m.add_command(g, self.phrase.get(), spec)
        except ValidationError as e:
            if spot:
                m.data["spots"].pop(spot, None)
            self.c_err.configure(text=str(e))
            return False
        if not self.app.save():
            return False
        self.app.add_log(f"Taught: {teach_card(self.phrase.get(), self.captured, self.click_mode.get())}", "setup")
        for w in warns:
            self.app.add_log(f"[warn] {w}", "skipped")
        self.app.refresh_all(); self.app.select_phrase(ev.normalize_phrase(self.phrase.get()))
        self.app.schedule_restart()
        self.close()
        return True

    def cancel(self):
        self.app.add_log("Teach cancelled - nothing changed.", "info")
        self.close()

    def close(self):
        if self._listen_job:
            try:
                self.after_cancel(self._listen_job)
            except tk.TclError:
                pass
        if self.overlay is not None:
            self.overlay._finish(None)
        eng = self.app.engine
        if eng is not None:
            eng.capture(None)
            if self._started_engine and eng.running:
                self.app.stop_engine()
        self.app._teach = None
        try:
            self.destroy()
        except tk.TclError:
            pass


HELP_TEXT = """GETTING STARTED
 1. Settings tab: pick your microphone, press "Test mic" and talk - the bar should move.
 2. Commands tab: make sure the keys match YOUR EVE keybinds (in EVE: Esc > Shortcuts).
    Double-click a row to change it. "Click here and press the key" records a key.
 3. Listen tab: tick "Test mode", press Start and say "warp". The log shows what WOULD
    be pressed. Untick Test mode when you're happy, click into EVE and play.
 4. "select next": Settings > Saved spots > next_waypoint > Set..., then drag a box over
    the next waypoint row. Say "select next", then "warp".
 5. Press Save (bottom right) to keep your changes.

THE RULES (built in - they can't be switched off)
 * One input per word or phrase: ONE key / one shortcut chord, OR ONE single click.
   Never both, never a sequence. Combos, double clicks and Ctrl+click are refused.
 * Pure input: listens to your mic and sends normal key/mouse input to the active EVE
   window. No memory reading, no hooks into the game, no log scraping.
 * No timed delays between inputs. (Key hold time = how long ONE key is held down.)
 * Only sends while EVE is the active window (Settings > Behaviour).

HOW IT LISTENS
 Always listening - any command you say fires.
 Push-to-talk     - only while you hold your PTT key (pick one EVE doesn't use).
 Wake word        - say "computer warp", or "computer", pause, then "warp".

TROUBLESHOOTING
 Keys don't register in EVE: close this, right-click run.bat > Run as administrator.
 Nothing heard: pick the right mic in Settings; check Windows microphone privacy.
 Problems are written to eve_voice.log in this folder.
"""


class MainWindow(_Tk):
    def __init__(self, config_path: str = DEFAULT_CONFIG, demo: bool = False,
                 state_path: str = STATE_PATH, model_dir: str = MODEL_DIR, show_welcome: bool = True,
                 check_updates: bool = False):
        super().__init__()
        self.config_path, self.demo, self.state_path = config_path, demo, state_path
        self._model_dir_override = model_dir if model_dir != MODEL_DIR else None
        self._downloading = False
        self._measure: Optional[dict] = None
        self.practice: Optional[PracticeStats] = None
        self._teach: Optional["TeachDialog"] = None
        self.title(f"EVE Voice {VERSION}")
        self.exit_code = 0
        apply_theme(self)
        self.report_callback_exception = self._tk_error
        self.ui_state = load_state(state_path)
        self.geometry(self.ui_state.get("geometry") or "1080x780")
        self.minsize(960, 700)
        self.events: "queue.Queue" = queue.Queue()
        self.model = ConfigModel(config_path)
        self.vocab = ev.load_vocab(self.model_dir)
        self.engine: Optional[Engine] = None
        self.mic = MicMonitor()
        self.status = "stopped"
        self.level = 0.0
        self._restart_job = None
        self._loading = False
        self._iid_map: Dict[str, Tuple[str, str]] = {}
        self.test_mode = tk.BooleanVar(value=bool(self.ui_state.get("test_mode", demo)))
        self._build()
        self.refresh_all()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(50, self._poll)
        if show_welcome and not self.ui_state.get("welcomed"):
            self.after(400, self.welcome)
        self._check_updates = check_updates
        self.after(1500, self._started_ok)

    @property
    def model_dir(self) -> str:
        return self._model_dir_override or resolve_model_dir(self.model.setting("model_path"))

    # ------------------------------------------------------------------ layout
    def _build(self):
        top = ttk.Frame(self, padding=(14, 10, 14, 0)); top.pack(fill="x")
        ttk.Label(top, text="EVE Voice", style="Title.TLabel").pack(side="left")
        ttk.Label(top, text="   one phrase = one input", style="Dim.TLabel").pack(side="left", pady=(6, 0))
        ttk.Button(top, text="Help", command=lambda: self.nb.select(self.tab_help)).pack(side="right")
        self.nb = ttk.Notebook(self); self.nb.pack(fill="both", expand=True, padx=10, pady=8)
        (self.tab_listen, self.tab_cmds, self.tab_settings, self.tab_gate, self.tab_clients,
         self.tab_help) = (ttk.Frame(self.nb, padding=14) for _ in range(6))
        for t, name in ((self.tab_listen, "Listen"), (self.tab_cmds, "Commands"), (self.tab_settings, "Settings"),
                        (self.tab_gate, "Next gate"), (self.tab_clients, "Clients"), (self.tab_help, "Help")):
            self.nb.add(t, text=f"  {name}  ")
        self._build_listen(); self._build_commands(); self._build_settings(); self._build_gate()
        self._build_clients(); self._build_help()
        bar = ttk.Frame(self, padding=(14, 0, 14, 10)); bar.pack(fill="x")
        self.dirty_lbl = ttk.Label(bar, text="", style="Warn.TLabel"); self.dirty_lbl.pack(side="left")
        ttk.Label(bar, text=f"   {os.path.basename(self.config_path)}", style="Dim.TLabel").pack(side="left")
        ttk.Button(bar, text="Save", command=self.save).pack(side="right")
        ttk.Button(bar, text="Revert", command=self.revert).pack(side="right", padx=8)

    def _build_listen(self):
        t = self.tab_listen
        row = ttk.Frame(t); row.pack(fill="x")
        self.start_btn = ttk.Button(row, text="Start listening", style="Accent.TButton", command=self.toggle)
        self.start_btn.pack(side="left")
        ttk.Button(row, text="Teach a command", command=self.open_teach).pack(side="left", padx=(10, 0))
        sf = ttk.Frame(row); sf.pack(side="left", padx=20)
        self.light = tk.Canvas(sf, width=22, height=22, bg=COL["bg"], highlightthickness=0)
        self.light.grid(row=0, column=0, rowspan=2)
        self.light_dot = self.light.create_oval(3, 3, 19, 19, fill=COL["dim"], outline="")
        self.status_lbl = ttk.Label(sf, text="Stopped", style="Big.TLabel")
        self.status_lbl.grid(row=0, column=1, sticky="w", padx=8)
        self.last_lbl = ttk.Label(sf, text="Last heard: -", style="Dim.TLabel")
        self.last_lbl.grid(row=1, column=1, sticky="w", padx=8)
        self.hearing_lbl = ttk.Label(sf, text="", style="Dim.TLabel")
        self.hearing_lbl.grid(row=2, column=1, sticky="w", padx=8)
        self.held_bar = ttk.Frame(t)          # shown only while a key is held down
        self.held_lbl = ttk.Label(self.held_bar, text="", style="Warn.TLabel", font=(FONT, 13, "bold"))
        self.held_lbl.pack(side="left")
        ttk.Button(self.held_bar, text="Release", style="Primary.TButton",
                   command=self.release_held).pack(side="left", padx=12)
        self._held: Optional[dict] = None
        right = ttk.Frame(row); right.pack(side="right")
        ttk.Checkbutton(right, text="Test mode (don't press anything)", variable=self.test_mode,
                        command=self._test_mode_changed).pack(anchor="e")
        lv = ttk.Frame(right); lv.pack(anchor="e", pady=(6, 0))
        ttk.Label(lv, text="Mic  ", style="Dim.TLabel").pack(side="left")
        self.level_bar = LevelMeter(lv, width=180)
        self.level_bar.pack(side="left")
        lf = ttk.LabelFrame(t, text="  Activity  ", padding=6)
        lf.pack(fill="both", expand=True, pady=(14, 0))
        self.log_txt = tk.Text(lf, bg=COL["panel"], fg=COL["fg"], relief="flat", height=10, wrap="word",
                               insertbackground=COL["fg"], font=(MONO, 10), padx=8, pady=6,
                               highlightthickness=0, borderwidth=0)
        sb = ttk.Scrollbar(lf, command=self.log_txt.yview)
        self.log_txt.configure(yscrollcommand=sb.set, state="disabled")
        self.log_txt.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")
        for tag, c in (("sent", COL["ok"]), ("skipped", COL["warn"]), ("error", COL["bad"]), ("unsure", "#c9b26a"),
                       ("info", COL["dim"]), ("wake", COL["accent2"]), ("setup", COL["accent"])):
            self.log_txt.tag_configure(tag, foreground=c)
        bb = ttk.Frame(t); bb.pack(fill="x", pady=(6, 0))
        ttk.Button(bb, text="Clear log", command=self.clear_log).pack(side="right")
        self.mode_hint = ttk.Label(bb, text="", style="Dim.TLabel"); self.mode_hint.pack(side="left")

    def _build_commands(self):
        t = self.tab_cmds
        left = ttk.Frame(t); left.pack(side="left", fill="y", padx=(0, 12))
        ttk.Label(left, text="Show").pack(anchor="w")
        self.filter = tk.StringVar(value="All groups")
        self.filter_box = ttk.Combobox(left, textvariable=self.filter, state="readonly", width=22)
        self.filter_box.pack(anchor="w", pady=(2, 12))
        self.filter_box.bind("<<ComboboxSelected>>", lambda e: self.refresh_commands())
        ttk.Label(left, text="Groups on / off").pack(anchor="w")
        self.groups_frame = ttk.Frame(left); self.groups_frame.pack(anchor="w", fill="y", pady=(2, 0))
        right = ttk.Frame(t); right.pack(side="left", fill="both", expand=True)
        cols = ("phrase", "action", "group", "status")
        tf = ttk.Frame(right); tf.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tf, columns=cols, show="headings", selectmode="browse")
        for c, w, txt in (("phrase", 160, "Say"), ("action", 290, "Action"), ("group", 150, "Group"),
                          ("status", 120, "On/Off")):
            self.tree.heading(c, text=txt); self.tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(tf, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")
        self.tree.tag_configure("off", foreground=COL["dim"])
        self.tree.tag_configure("warn", foreground=COL["warn"])
        self.tree.bind("<Double-1>", lambda e: self.edit_command())
        bb = ttk.Frame(right); bb.pack(fill="x", pady=(8, 0))
        for txt, cmd in (("Add", self.add_command), ("Edit", self.edit_command),
                         ("Duplicate", self.duplicate_command), ("Delete", self.delete_command)):
            ttk.Button(bb, text=txt, command=cmd).pack(side="left", padx=(0, 6))
        ttk.Button(bb, text="Test selected (3 s)", command=self.test_command).pack(side="right")
        self._build_practice(right)

    # ------------------------------------------------------------------ practice (sends nothing)
    ALL = "All commands, one after another"

    def _build_practice(self, parent):
        pf = ttk.LabelFrame(parent, text="  Practice - say it, see what it hears (nothing is pressed)  ", padding=8)
        pf.pack(fill="x", pady=(10, 0))
        r = ttk.Frame(pf); r.pack(fill="x")
        ttk.Label(r, text="Practise").pack(side="left")
        self.pr_target = tk.StringVar(value=self.ALL)
        self.pr_box = ttk.Combobox(r, textvariable=self.pr_target, state="readonly", width=30)
        self.pr_box.pack(side="left", padx=6)
        self.pr_btn = ttk.Button(r, text="Start practice", command=self.toggle_practice)
        self.pr_btn.pack(side="left")
        self.pr_say = ttk.Label(r, text="", style="Big.TLabel"); self.pr_say.pack(side="left", padx=14)
        self.pr_last = ttk.Label(pf, text="Pick a command (or all) and press Start, then say it a few times.",
                                 style="Dim.TLabel")
        self.pr_last.pack(anchor="w", pady=(4, 0))
        self.pr_report = tk.Text(pf, height=4, bg=COL["panel"], fg=COL["fg"], relief="flat", wrap="word",
                                 font=(MONO, 9), highlightthickness=0, borderwidth=0, padx=6, pady=4)
        self.pr_report.pack(fill="x", pady=(4, 0))
        self.pr_report.configure(state="disabled")
        r2 = ttk.Frame(pf); r2.pack(fill="x", pady=(4, 0))
        ttk.Label(r2, text="Suggested other way to say").pack(side="left")
        self.pr_for = ttk.Label(r2, text="-", style="Dim.TLabel"); self.pr_for.pack(side="left", padx=4)
        self.pr_sugg = tk.StringVar()
        self.pr_sugg_box = ttk.Combobox(r2, textvariable=self.pr_sugg, width=22)
        self.pr_sugg_box.pack(side="left", padx=6)
        ttk.Button(r2, text="Add as alternate", command=self.practice_add_alternate).pack(side="left")
        self._pr_queue: List[str] = []
        self._pr_cur: Optional[str] = None
        self._pr_cap: Optional[ev.Capture] = None
        self._pr_started_engine = False
        self._pr_sugg_for: Optional[str] = None

    def _practice_phrases(self) -> List[str]:
        cfg = self._cfg_or_none()
        if cfg is None:
            return []
        return [p for p, c in cfg.commands.items() if c.kind not in ("calibrate", "teach")]

    def toggle_practice(self):
        if self._pr_cap is not None:
            return self.stop_practice()
        phrases = self._practice_phrases()
        if not phrases:
            return messagebox.showinfo("EVE Voice", "No active commands to practise.", parent=self)
        want = self.pr_target.get()
        self._pr_queue = list(phrases) if want == self.ALL or want not in phrases else [want]
        cfg = self._cfg_or_none()
        self.practice = PracticeStats(float(self.model.setting("min_confidence")), dict(cfg.aliases) if cfg else {})
        self._pr_cur = self._pr_queue[0]
        self._pr_cap = ev.Capture(lambda t, c: self.events.put(("capture", "practice", t, c)),
                                  once=False, hint=self._pr_cur)
        self._pr_started_engine = not (self.engine and self.engine.running)
        if self._pr_started_engine:
            self.start_engine()
        self.engine.capture(self._pr_cap)
        self.pr_btn.configure(text="Stop practice")
        self.pr_say.configure(text=f"Say: \"{self._pr_cur}\"")
        self.pr_last.configure(text="Listening... (practice: nothing is sent to EVE)", style="Dim.TLabel")
        self._practice_report()
        self.add_log("Practice started - nothing is pressed until you stop.", "setup")

    def stop_practice(self):
        if self.engine is not None:
            self.engine.capture(None)
        self._pr_cap = None
        if self._pr_started_engine and self.engine and self.engine.running:
            self.stop_engine()
        self._pr_started_engine = False
        self.pr_btn.configure(text="Start practice")
        self.pr_say.configure(text="")
        self.add_log("Practice stopped.", "info")

    def _practice_heard(self, text, conf):
        if self._pr_cap is None or self.practice is None or self._pr_cur is None:
            return
        target = self._pr_cur
        v = self.practice.record(target, text, conf)
        pct = "" if conf is None else f" ({conf:.0%})"
        msg = {"ok": ("Ok.TLabel", "OK"), "unsure": ("Warn.TLabel", "right, but not sure enough - would be skipped"),
               "wrong": ("Bad.TLabel", "WRONG command"), "missed": ("Warn.TLabel", "not understood")}[v]
        heard = "nothing clear" if not text or text == "[unk]" else f"'{text}'"
        self.pr_last.configure(text=f"You said '{target}' -> heard {heard}{pct}: {msg[1]}", style=msg[0])
        if len(self._pr_queue) > 1:
            i = self._pr_queue.index(target)
            self._pr_cur = self._pr_queue[(i + 1) % len(self._pr_queue)]
            self._pr_cap.hint = self._pr_cur
        self.pr_say.configure(text=f"Say: \"{self._pr_cur}\"")
        self._practice_report()

    def _practice_report(self):
        ps = self.practice
        lines = []
        if ps is not None:
            for target in ps.tries:
                s_ = ps.summary(target)
                avg = "-" if s_["avg_conf"] is None else f"{s_['avg_conf']:.0%}"
                lines.append(f"{target:<22} {s_['ok']}/{s_['tries']} ok, avg {avg}")
            for target, rate in ps.unreliable():
                lines.append(f"!! '{target}' is unreliable ({rate:.0%} ok) - add another way to say it")
            for said, other, n, tries in ps.confusions():
                lines.append(f"!! '{said}' was heard as '{other}' {n} of {tries} times - they sound alike")
            self._practice_suggest()
        self.pr_report.configure(state="normal"); self.pr_report.delete("1.0", "end")
        self.pr_report.insert("end", "\n".join(lines) or "No tries yet.")
        self.pr_report.see("end"); self.pr_report.configure(state="disabled")

    def _practice_suggest(self):
        ps = self.practice
        bad = [t for t, _ in ps.unreliable()] + [c[0] for c in ps.confusions()]
        target = bad[-1] if bad else None
        if target is None:
            return
        avoid = [o for s_, o, _, _ in ps.confusions() if s_ == target]
        sugg = suggest_alternates(target, self.vocab, self.model.all_phrases(), avoid)
        self._pr_sugg_for = target
        self.pr_for.configure(text=f"'{target}':", style="Warn.TLabel")
        self.pr_sugg_box.configure(values=sugg)
        if sugg and self.pr_sugg.get() not in sugg:
            self.pr_sugg.set(sugg[0])

    def practice_add_alternate(self):
        target, alt = self._pr_sugg_for, ev.normalize_phrase(self.pr_sugg.get())
        if not target or not alt:
            return messagebox.showinfo("EVE Voice", "Practise first - suggestions appear for phrases that "
                                       "get misheard. You can also type your own.", parent=self)
        hit = self.model.find(target)
        if not hit:
            return
        try:
            self.model.add_alternate(hit[0], hit[1], alt)
        except ValidationError as e:
            return messagebox.showerror("EVE Voice", str(e), parent=self)
        if self.practice is not None:
            self.practice.aliases[alt] = hit[1]
        self.add_log(f"'{alt}' now also does '{hit[1]}' (same single action). Press Save to keep it.", "setup")
        self.refresh_commands(); self.schedule_restart()

    def _build_settings(self):
        t = self.tab_settings
        t.columnconfigure(0, weight=1); t.columnconfigure(1, weight=1)
        mic = ttk.LabelFrame(t, text="  Microphone  ", padding=10)
        mic.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=(0, 8))
        self.mic_var = tk.StringVar()
        self.mic_box = ttk.Combobox(mic, textvariable=self.mic_var, state="readonly", width=38)
        self.mic_box.pack(fill="x")
        self.mic_box.bind("<<ComboboxSelected>>", lambda e: self._mic_changed())
        r = ttk.Frame(mic); r.pack(fill="x", pady=(8, 0))
        self.mic_test_btn = ttk.Button(r, text="Test mic", command=self.toggle_mic_test)
        self.mic_test_btn.pack(side="left")
        self.mic_bar = LevelMeter(r, width=200); self.mic_bar.pack(side="left", padx=8)
        ttk.Button(r, text="Measure noise", command=self.measure_noise).pack(side="left")
        ttk.Label(mic, style="Dim.TLabel", wraplength=380,
                  text="Red line = noise gate: anything quieter is ignored. 'Measure noise' listens for 2 s "
                       "(game on, you quiet) and sets the gate just above it.").pack(anchor="w", pady=(4, 0))
        self.mic_err = ttk.Label(mic, text="", style="Warn.TLabel", wraplength=380); self.mic_err.pack(anchor="w")

        trig = ttk.LabelFrame(t, text="  How it listens  ", padding=10)
        trig.grid(row=1, column=0, sticky="nsew", padx=(0, 8), pady=(0, 8))
        self.mode_var = tk.StringVar()
        ttk.Radiobutton(trig, text="Always listening", value="always", variable=self.mode_var,
                        command=self._mode_changed).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(trig, text="Push-to-talk, hold:", value="push_to_talk", variable=self.mode_var,
                        command=self._mode_changed).grid(row=1, column=0, sticky="w", pady=4)
        self.ptt_box = KeyCaptureBox(trig, "", mode="ptt", on_change=self._ptt_changed)
        self.ptt_box.grid(row=1, column=1, sticky="we")
        ttk.Radiobutton(trig, text="Wake word:", value="wake_word", variable=self.mode_var,
                        command=self._mode_changed).grid(row=2, column=0, sticky="w")
        wf = ttk.Frame(trig); wf.grid(row=2, column=1, sticky="w")
        self.wake_var = tk.StringVar()
        ttk.Entry(wf, textvariable=self.wake_var, width=12).pack(side="left")
        ttk.Label(wf, text="  then listen").pack(side="left")
        self.wake_win = tk.StringVar()
        ttk.Spinbox(wf, from_=1, to=15, increment=1, textvariable=self.wake_win, width=4).pack(side="left", padx=4)
        ttk.Label(wf, text="s").pack(side="left")
        self.wake_var.trace_add("write", lambda *a: self._wake_changed())
        self.wake_win.trace_add("write", lambda *a: self._wake_changed())
        self.wake_warn = ttk.Label(trig, text="", style="Warn.TLabel"); self.wake_warn.grid(row=3, column=1, sticky="w")

        beh = ttk.LabelFrame(t, text="  Behaviour  ", padding=10)
        beh.grid(row=0, column=1, sticky="nsew", pady=(0, 8))
        self.bools: Dict[str, tk.BooleanVar] = {}
        for i, (key, txt) in enumerate((("require_eve_focus", "Only send when EVE is the active window"),
                                        ("tts_confirm", "Speak the command back"),
                                        ("print_confirm", "Show sent commands in the activity log"),
                                        ("auto_update", "Update automatically on start"))):
            v = tk.BooleanVar(); self.bools[key] = v
            ttk.Checkbutton(beh, text=txt, variable=v, command=lambda k=key: self._bool_changed(k)).grid(
                row=i, column=0, columnspan=3, sticky="w", pady=2)
        ttk.Label(beh, text="EVE window title contains").grid(row=4, column=0, sticky="w", pady=(8, 2))
        self.title_var = tk.StringVar()
        ttk.Entry(beh, textvariable=self.title_var, width=10).grid(row=4, column=1, sticky="w", pady=(8, 2))
        self.title_var.trace_add("write", lambda *a: self._set("window_title_contains", self.title_var.get()))
        ttk.Label(beh, text="Repeat cooldown (s)").grid(row=5, column=0, sticky="w", pady=2)
        self.cool_var = tk.StringVar()
        ttk.Spinbox(beh, from_=0, to=5, increment=0.25, textvariable=self.cool_var, width=6).grid(
            row=5, column=1, sticky="w")
        self.cool_var.trace_add("write", lambda *a: self._num_changed("cooldown_seconds", self.cool_var))
        ttk.Label(beh, text="Key hold time").grid(row=6, column=0, sticky="w", pady=2)
        self.hold_var = tk.DoubleVar()
        ttk.Scale(beh, from_=0.02, to=0.2, variable=self.hold_var, length=140,
                  command=lambda v: self._hold_changed()).grid(row=6, column=1, sticky="w")
        self.hold_lbl = ttk.Label(beh, text="", style="Dim.TLabel"); self.hold_lbl.grid(row=6, column=2, sticky="w", padx=6)
        ttk.Label(beh, style="Dim.TLabel", wraplength=360, justify="left",
                  text="Cooldown ignores the same phrase said again too quickly. Key hold time is how long "
                       "ONE key is held down. Both can be set per command too (Edit > Options).").grid(
            row=7, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Button(beh, text="Keys sometimes don't register?", command=self.keys_helper).grid(
            row=8, column=0, columnspan=3, sticky="w", pady=(8, 0))

        rc = ttk.LabelFrame(t, text="  Recognition  ", padding=10)
        rc.grid(row=1, column=1, sticky="nsew", pady=(0, 8))
        ttk.Label(rc, text="How sure before acting").grid(row=0, column=0, sticky="w")
        self.conf_var = tk.DoubleVar()
        ttk.Scale(rc, from_=0.3, to=0.95, variable=self.conf_var, length=150,
                  command=lambda v: self._conf_changed()).grid(row=0, column=1, sticky="w", padx=6)
        self.conf_lbl = ttk.Label(rc, text="", style="Dim.TLabel"); self.conf_lbl.grid(row=0, column=2, sticky="w")
        ttk.Label(rc, text="Ignore sounds quieter than").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.gate_var = tk.DoubleVar()
        ttk.Scale(rc, from_=0.0, to=0.3, variable=self.gate_var, length=150,
                  command=lambda v: self._gate_changed()).grid(row=1, column=1, sticky="w", padx=6, pady=(4, 0))
        self.gate_lbl = ttk.Label(rc, text="", style="Dim.TLabel"); self.gate_lbl.grid(row=1, column=2, sticky="w")
        ttk.Label(rc, text="Act after you stop talking (s)").grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.endp_var = tk.StringVar()
        ttk.Spinbox(rc, from_=0, to=1.5, increment=0.05, textvariable=self.endp_var, width=6).grid(
            row=2, column=1, sticky="w", padx=6, pady=(4, 0))
        self.endp_var.trace_add("write", lambda *a: self._num_changed("endpoint_silence_seconds", self.endp_var))
        cbf = ttk.Frame(rc); cbf.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))
        v = tk.BooleanVar(); self.bools["log_unmatched"] = v
        ttk.Checkbutton(cbf, text="Show things it heard that weren't commands", variable=v,
                        command=lambda: self._bool_changed("log_unmatched")).pack(anchor="w")
        v = tk.BooleanVar(); self.bools["fast_response"] = v
        ttk.Checkbutton(cbf, text="Fast response (act on partial results) - 'warp gate' fires warp, then gate",
                        variable=v, command=lambda: self._bool_changed("fast_response")).pack(anchor="w")
        ttk.Label(rc, text="Speech accuracy").grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.acc_var = tk.StringVar()
        self.acc_box = ttk.Combobox(rc, textvariable=self.acc_var, state="readonly", width=34,
                                    values=[m["label"] for m in MODELS.values()])
        self.acc_box.grid(row=4, column=1, columnspan=2, sticky="w", padx=6, pady=(8, 0))
        self.acc_box.bind("<<ComboboxSelected>>", lambda e: self._accuracy_changed())
        self.dl_bar = ttk.Progressbar(rc, length=150, maximum=1.0)
        self.dl_bar.grid(row=5, column=1, sticky="w", padx=6, pady=(4, 0))
        self.dl_lbl = ttk.Label(rc, text="", style="Dim.TLabel", wraplength=320)
        self.dl_lbl.grid(row=6, column=0, columnspan=3, sticky="w")

        sp = ttk.LabelFrame(t, text="  Saved spots (for 'select next' etc.)  ", padding=10)
        sp.grid(row=2, column=0, columnspan=2, sticky="nsew")
        self.spot_tree_height = 3
        t.rowconfigure(2, weight=1)
        self.spot_tree = ttk.Treeview(sp, columns=("name", "box", "used"), show="headings", height=3)
        for c, w, txt in (("name", 150, "Spot"), ("box", 230, "Box (left, top, right, bottom)"),
                          ("used", 260, "Used by")):
            self.spot_tree.heading(c, text=txt); self.spot_tree.column(c, width=w, anchor="w")
        self.spot_tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Frame(sp); sb.pack(side="left", fill="y", padx=(8, 0))
        ttk.Button(sb, text="Set...", command=self.set_selected_spot).pack(fill="x")
        ttk.Button(sb, text="Clear", command=self.clear_selected_spot).pack(fill="x", pady=4)
        ttk.Button(sb, text="New spot...", command=self.new_spot).pack(fill="x")

    def _build_clients(self):
        t = self.tab_clients
        self.target_var = tk.StringVar()
        ttk.Label(t, text="Which EVE client gets the input?", style="Big.TLabel").pack(anchor="w")
        ttk.Radiobutton(t, text="The active window (recommended)", value="foreground", variable=self.target_var,
                        command=self._target_changed).pack(anchor="w", pady=(12, 2))
        ttk.Label(t, style="Dim.TLabel", wraplength=780, justify="left",
                  text="Input goes to the window in front - and only if it's EVE. This follows the rules exactly."
                  ).pack(anchor="w", padx=26)
        ttk.Radiobutton(t, text="A specific EVE client in the background (experimental)",
                        value="background_window", variable=self.target_var,
                        command=self._target_changed).pack(anchor="w", pady=(16, 2))
        ttk.Label(t, style="Warn.TLabel", wraplength=780, justify="left",
                  text="Experimental: sends ONE key or ONE saved-spot click to ONE client you pick, without "
                       "focusing it. That's not broadcasting, but it is NOT 'active window only', and EVE may "
                       "simply ignore it. Ask CCP support before relying on it. Chords (Alt+F1...) and clicks at "
                       "the cursor are skipped in this mode. The fully supported way: run the second client and "
                       "this app on a second PC.").pack(anchor="w", padx=26)
        r = ttk.Frame(t); r.pack(anchor="w", padx=26, pady=(14, 0))
        ttk.Label(r, text="Client window").pack(side="left")
        self.win_var = tk.StringVar()
        self.win_box = ttk.Combobox(r, textvariable=self.win_var, width=34)
        self.win_box.pack(side="left", padx=8)
        self.win_var.trace_add("write", lambda *a: self._win_changed())
        ttk.Button(r, text="Refresh list", command=self.refresh_windows).pack(side="left")
        ttk.Button(r, text="Send test (opens map)", command=self.send_bg_test).pack(side="left", padx=8)
        self.win_msg = ttk.Label(t, text="", style="Dim.TLabel", wraplength=780)
        self.win_msg.pack(anchor="w", padx=26, pady=(6, 0))
        ttk.Label(t, style="Dim.TLabel", wraplength=780, justify="left",
                  text="EVE windows are titled 'EVE - Character Name'. The test sends ONE F10 (map) to the chosen "
                       "client after 3 seconds - click into your other client first. If the map toggles on the "
                       "background client, background mode works for you.").pack(anchor="w", padx=26, pady=(14, 0))

    def _build_help(self):
        t = self.tab_help
        txt = tk.Text(t, bg=COL["panel"], fg=COL["fg"], relief="flat", wrap="word", padx=12, pady=10,
                      font=(MONO, 10), highlightthickness=0, borderwidth=0)
        txt.insert("1.0", HELP_TEXT); txt.configure(state="disabled")
        txt.pack(fill="both", expand=True)
        b = ttk.Frame(t); b.pack(fill="x", pady=(8, 0))
        ttk.Button(b, text="Open README", command=lambda: self._open(README_PATH)).pack(side="left")
        ttk.Button(b, text="Open log file", command=lambda: self._open(LOG_PATH)).pack(side="left", padx=8)
        ttk.Button(b, text="Show welcome again", command=self.welcome).pack(side="left")
        u = ttk.Frame(t); u.pack(fill="x", pady=(8, 0))
        ttk.Label(u, text=f"Version {VERSION}", style="Dim.TLabel").pack(side="left")
        self.upd_btn = ttk.Button(u, text="Check for updates", command=lambda: self.check_updates(manual=True))
        self.upd_btn.pack(side="left", padx=10)
        self.upd_lbl = ttk.Label(u, text="", style="Dim.TLabel"); self.upd_lbl.pack(side="left")
        self.restart_btn = ttk.Button(u, text="Restart to update", style="Primary.TButton", command=self.restart_to_update)

    # ------------------------------------------------------------------ updates (signed, see autoupdater.py)
    def _started_ok(self):
        """We're up: keep the update that was just applied (else the launcher rolls back)."""
        try:
            import autoupdater
            autoupdater.mark_started(APP_DIR)
        except Exception:
            log.exception("mark_started failed")
        if self._check_updates and self.model.setting("auto_update") is not False:
            self.check_updates()

    def check_updates(self, manual: bool = False):
        self.upd_lbl.configure(text="Checking...", style="Dim.TLabel")
        self.upd_btn.configure(state="disabled")

        def work():
            try:
                import autoupdater
                import eve_update
                ver, msg = autoupdater.check_and_stage_safe(eve_update.make_config(APP_DIR))
            except Exception as e:
                ver, msg = None, f"No update: {e}"
            self.events.put(("update", ver, msg, manual))
        threading.Thread(target=work, daemon=True, name="update-check").start()

    def restart_to_update(self):
        if os.environ.get("EVE_VOICE_LAUNCHER"):
            self.exit_code = RESTART_CODE          # launcher applies it and starts us again
        else:
            import subprocess
            subprocess.Popen([sys.executable, os.path.join(APP_DIR, "launcher.py")], cwd=APP_DIR)
        self.on_close()

    # ------------------------------------------------------------------ refresh
    def refresh_all(self):
        self._loading = True
        try:
            self.refresh_commands(); self.refresh_settings(); self.refresh_spots(); self.refresh_target()
            self.refresh_gate()
        finally:
            self._loading = False
        self.update_dirty()

    def update_dirty(self):
        self.dirty_lbl.configure(text="* Unsaved changes" if self.model.dirty else "")

    def _cfg_or_none(self):
        try:
            return self.model.check()
        except ValidationError:
            return None

    def refresh_commands(self):
        groups = self.model.groups()
        self.filter_box.configure(values=["All groups"] + groups)
        if self.filter.get() not in ["All groups"] + groups:
            self.filter.set("All groups")
        for w in self.groups_frame.winfo_children():
            w.destroy()
        self.group_vars = {}
        for g in groups:
            v = tk.BooleanVar(value=self.model.group_enabled(g)); self.group_vars[g] = v
            ttk.Checkbutton(self.groups_frame, text=g, variable=v,
                            command=lambda g=g: self._group_toggled(g)).pack(anchor="w")
        if hasattr(self, "pr_box"):
            self.pr_box.configure(values=[self.ALL] + self._practice_phrases())
        sel = self.selected()
        self.tree.delete(*self.tree.get_children())
        self._iid_map = {}
        cfg = self._cfg_or_none()
        want = self.filter.get()
        for i, (g, p, spec) in enumerate(self.model.commands()):
            if want != "All groups" and g != want:
                continue
            st = command_status(self.model, cfg, g, p, spec)
            tag = "" if st == "On" else ("off" if st.startswith("Off") else "warn")
            iid = f"c{i}"
            self._iid_map[iid] = (g, p)
            alts = spec_alternates(spec)
            say = p + (f"  (also: {', '.join(alts)})" if alts else "")
            self.tree.insert("", "end", iid=iid, values=(say, describe_spec(spec), g, st), tags=(tag,))
            if sel == (g, p):
                self.tree.selection_set(iid); self.tree.see(iid)

    def selected(self) -> Optional[Tuple[str, str]]:
        s = self.tree.selection() if hasattr(self, "tree") else ()
        return self._iid_map.get(s[0]) if s else None

    def select_phrase(self, phrase: str):
        for iid, (g, p) in self._iid_map.items():
            if p == phrase:
                self.tree.selection_set(iid); self.tree.see(iid)

    def refresh_settings(self):
        m = self.model
        mics, err = list_microphones()
        self._mics = mics
        self.mic_box.configure(values=[lbl for _, lbl in mics])
        cur = m.setting("mic_device")
        label = next((lbl for i, lbl in mics if (cur is None and i is None) or
                      (cur is not None and (str(cur) == str(i) or str(cur).lower() in lbl.lower()))), None)
        self.mic_var.set(label or (f"{cur} (not found)" if cur is not None else mics[0][1]))
        self.mic_err.configure(text=err or "")
        self.mode_var.set(m.setting("mode"))
        self.ptt_box.value = m.setting("push_to_talk_key") or ""
        self.ptt_box._show()
        self.wake_var.set(m.setting("wake_word") or "")
        self.wake_win.set(f"{m.setting('wake_window_seconds'):g}")
        for k, v in self.bools.items():
            v.set(bool(m.setting(k)))
        self.title_var.set(m.setting("window_title_contains") or "")
        self.cool_var.set(f"{m.setting('cooldown_seconds'):g}")
        self.hold_var.set(float(m.setting("key_hold_seconds")))
        self.hold_lbl.configure(text=f"{self.hold_var.get():.2f} s")
        self.conf_var.set(float(m.setting("min_confidence")))
        self.conf_lbl.configure(text=f"{self.conf_var.get():.0%}")
        self.gate_var.set(float(m.setting("noise_gate")))
        self.gate_lbl.configure(text=self._gate_text())
        self.endp_var.set(f"{m.setting('endpoint_silence_seconds'):g}")
        key = model_key_for_path(m.setting("model_path"))
        self.acc_var.set(MODELS[key]["label"])
        md = resolve_model_dir(m.setting("model_path"))
        if not self._downloading:
            self.dl_lbl.configure(text=("Installed." if model_installed(md) else
                                        "Not downloaded yet - pick it again to download.")
                                  if key == "accurate" else "The small model from setup.bat.")
        self._update_mode_hint()

    def refresh_spots(self):
        self.spot_tree.delete(*self.spot_tree.get_children())
        for name, box in self.model.spots().items():
            b = ", ".join(str(int(v)) for v in box) if isinstance(box, (list, tuple)) else "not set"
            self.spot_tree.insert("", "end", iid=name, values=(name, b, ", ".join(self.model.spot_users(name)) or "-"))

    def refresh_target(self):
        t = self.model.target()
        self.target_var.set(t.get("mode") or "foreground")
        self.win_var.set(t.get("window_title") or "")
        self.win_box.configure(state="normal" if self.target_var.get() == "background_window" else "disabled")

    # ------------------------------------------------------------------ engine
    def toggle(self):
        if self.engine and self.engine.running:
            self.stop_engine()
        else:
            self.start_engine()

    def start_engine(self):
        self.mic_stop()
        runner = demo_runner if self.demo else default_runner
        self.engine = Engine(self.model.snapshot, self.events, self.test_mode.get(), runner, self.model_dir)
        self._engine_key = self._restart_key()
        self.engine.start()
        self.start_btn.configure(text="Stop listening", style="Stop.TButton")
        self.set_status("starting")

    def stop_engine(self):
        if self.engine:
            self.engine.stop()
            self.engine = None
            self._drain_stale()
        self.start_btn.configure(text="Start listening", style="Accent.TButton")
        self.set_status("stopped")
        self.level = 0.0

    def _drain_stale(self):
        """After a stop: keep the old engine's log lines, drop its status updates."""
        try:
            while True:
                msg = self.events.get_nowait()
                if msg[0] in ("log", "event", "error"):
                    self._handle(msg)
                elif msg[0] == "calibrate":
                    msg[2].set()
        except queue.Empty:
            pass

    def _restart_key(self) -> str:
        """Things that need a full listener restart (mic, mode, model, screen-read wiring).
        Anything else (commands, alternates, spots) is hot-reloaded."""
        d = self.model.data
        has_find = any(isinstance(sp, dict) and sp.get("find_color")
                       for g in d.get("groups", {}).values() if isinstance(g, dict)
                       for sp in (g.get("commands") or {}).values())
        return json.dumps([d.get("settings"), d.get("target"), has_find], sort_keys=True, default=str)

    def schedule_restart(self, full: bool = False):
        """Something changed: hot-reload commands, or restart the listener (debounced)."""
        self.update_dirty()
        if self._loading or not (self.engine and self.engine.running):
            return
        if self._restart_job:
            self.after_cancel(self._restart_job)
        self._restart_job = self.after(700, lambda: self._restart(full))

    def _restart(self, full: bool = False):
        self._restart_job = None
        if not (self.engine and self.engine.running):
            return
        if not full and self._restart_key() == getattr(self, "_engine_key", None) and self.engine.app is not None:
            self.add_log("Commands changed - reloaded (no restart).", "info")
            self.engine.reload()
            return
        self.add_log("Settings changed - restarting the listener.", "info")
        cap = self.engine.app.capture if self.engine.app is not None else None
        self.stop_engine(); self.start_engine()
        if cap is not None:
            self.engine.capture(cap)

    _STATUS = {"stopped": ("Stopped", "dim"), "starting": ("Starting...", "accent2"),
               "listening": ("Listening", "ok"), "wake_active": ("Listening for a command", "ok"),
               "error": ("Error - see log", "bad")}

    def set_status(self, st: str):
        self.status = st
        if st == "ptt_wait":
            text, c = f"Hold {str(self.model.setting('push_to_talk_key')).title()} to talk", "warn"
        elif st == "wake_wait":
            text, c = f"Waiting for wake word \"{self.model.setting('wake_word')}\"", "warn"
        else:
            text, c = self._STATUS.get(st, (st, "dim"))
        if self.test_mode.get() and st not in ("stopped", "error"):
            text += "  (test mode)"
        self.status_lbl.configure(text=text)
        self.light.itemconfigure(self.light_dot, fill=COL[c])

    def _poll(self):
        try:
            for _ in range(200):
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        lvl = self.level if (self.engine and self.engine.running) else self.mic.level
        gate = float(self.model.setting("noise_gate") or 0)
        self.level_bar.set(lvl, gate)
        self.mic_bar.set(self.mic.level if self.mic.stream else lvl, gate)
        if self._held is not None:
            self._show_held()
        if self._measure is not None:
            self._measure["peak"] = max(self._measure["peak"], lvl)
            if time.monotonic() >= self._measure["until"]:
                self._finish_measure()
        self.after(50, self._poll)

    def _handle(self, msg):
        kind = msg[0]
        if kind == "log":
            text = msg[1]
            if text.strip().startswith((">", "holding ", "released ")):   # the GUI shows its own line
                return
            tag = "error" if "[error]" in text else ("skipped" if "[warn]" in text else "info")
            self.add_log(text.strip(), tag)
        elif kind == "event":
            _, k, phrase, detail = msg
            if k == "sent" and detail.startswith("hold "):
                pass                       # the 'holding' event right before it shows this
            elif k == "sent":
                act = describe_action_short(detail)
                if self.test_mode.get():
                    act = f"(test) would have {act}" if act.startswith("pressed") else f"(test) would {act}"
                self.last_lbl.configure(text=f"Last heard: \"{phrase}\"  ->  {act}", style="Ok.TLabel")
                if self.model.setting("print_confirm"):
                    self.add_log(f"\"{phrase}\" -> {act}", "sent")
            elif k == "skipped":
                self.last_lbl.configure(text=f"Last heard: \"{phrase}\"  ->  not sent ({detail})", style="Warn.TLabel")
                self.add_log(f"\"{phrase}\" not sent: {detail}", "skipped")
            elif k == "partial":
                self.hearing_lbl.configure(text=f"hearing: \"{phrase}...\"" if phrase else "")
            elif k in ("unsure", "unmatched"):
                self.hearing_lbl.configure(text="")
                if self.model.setting("log_unmatched"):
                    what = "something" if phrase == "[unk]" else f"\"{phrase}\""
                    self.add_log(f"heard {what} {detail}", "unsure" if k == "unsure" else "info")
                if k == "unsure":
                    self.last_lbl.configure(text=f"Last heard: \"{phrase}\"  ->  not sure, skipped",
                                            style="Warn.TLabel")
            elif k == "found":
                self.add_log(f"\"{phrase}\": {detail}", "setup")
            elif k == "holding":
                label, mode, secs = (detail.split("|") + ["", "", "0"])[:3]
                self._held = {"label": label, "mode": mode, "until": time.monotonic() + float(secs or 0)}
                text = {"timed": f"holding {label} for {float(secs):.1f}s",
                        "click": f"holding {label} - click your target (max {secs}s)",
                        "release": f"holding {label} until you say 'release' (max {secs}s)"}.get(mode, label)
                self.add_log(text, "sent")
                self.last_lbl.configure(text=f"Last heard: \"{phrase}\"  ->  {text}", style="Ok.TLabel")
                self._show_held()
            elif k == "released":
                label, why = (detail.split("|", 1) + [""])[:2]
                self.add_log(f"released {label}" + (f" ({why})" if why else ""), "info")
                self._held = None
                self._show_held()
            else:
                self.add_log(f"\"{phrase}\" {detail}", k if k in ("wake", "setup") else "info")
        elif kind == "tick":
            _, st, lvl = msg
            self.level = lvl
            if st != self.status:
                self.set_status(st)
        elif kind == "started":
            _, mode, ptt, wake, n = msg
            self.set_status({"push_to_talk": "ptt_wait", "wake_word": "wake_wait"}.get(mode, "listening"))
            self.add_log(f"Listening for {n} commands ({mode.replace('_', ' ')})"
                         f"{' - TEST MODE, nothing is pressed' if self.test_mode.get() else ''}.", "info")
        elif kind == "stopped":
            if self.engine and not self.engine.running and self._pr_cap is not None:
                self._pr_cap = None; self._pr_started_engine = False
                self.pr_btn.configure(text="Start practice"); self.pr_say.configure(text="")
            if self.engine and not self.engine.running:
                self.engine = None
                self.start_btn.configure(text="Start listening", style="Accent.TButton")
                if self.status != "error":
                    self.set_status("stopped")
        elif kind == "error":
            self.add_log(msg[1].replace("\n\n", " "), "error")
            log.error("engine error: %s", msg[1])
            self.set_status("error")
            messagebox.showerror("EVE Voice", msg[1], parent=self)
        elif kind == "teach":
            self.open_teach()
        elif kind == "update":
            _, ver, text, manual = msg
            self.upd_btn.configure(state="normal")
            self.upd_lbl.configure(text=text, style="Ok.TLabel" if ver else "Dim.TLabel")
            if ver:
                self.restart_btn.pack(side="left", padx=10)
                self.add_log(f"Update {ver} downloaded and verified - restart to apply (Help tab).", "setup")
                self.dirty_lbl.configure(text=f"Update {ver} ready - Help > Restart to update")
        elif kind == "capture":
            _, tag, text, conf = msg
            if tag == "practice":
                self._practice_heard(text, conf)
            elif tag == "teach" and self._teach is not None and self._teach.winfo_exists():
                self._teach.heard(text, conf)
        elif kind == "download":
            _, frac, text = msg
            self.dl_bar["value"] = frac
            self.dl_lbl.configure(text=text, style="Dim.TLabel")
        elif kind == "download_done":
            self._downloading = False
            self.model.set_setting("model_path", MODELS[msg[1]]["path"])
            self.vocab = ev.load_vocab(self.model_dir)
            self.dl_lbl.configure(text="Downloaded and switched. Press Save to keep it.", style="Ok.TLabel")
            self.refresh_settings(); self.schedule_restart(full=True)
        elif kind == "download_error":
            self._downloading = False
            self.dl_bar["value"] = 0
            self.dl_lbl.configure(text=f"Download failed: {msg[1]}", style="Bad.TLabel")
            self.refresh_settings()
        elif kind == "calibrate":
            _, spot, done, result = msg

            def finished(box):
                result["ok"] = box is not None
                done.set()
            self.calibrate_spot(spot, finished)

    def add_log(self, text: str, tag: str = "info"):
        self.log_txt.configure(state="normal")
        self.log_txt.insert("end", time.strftime("%H:%M:%S  ") + text + "\n", tag)
        lines = int(self.log_txt.index("end-1c").split(".")[0])
        if lines > 2000:
            self.log_txt.delete("1.0", f"{lines - 2000}.0")
        self.log_txt.see("end")
        self.log_txt.configure(state="disabled")

    def clear_log(self):
        self.log_txt.configure(state="normal"); self.log_txt.delete("1.0", "end")
        self.log_txt.configure(state="disabled")

    def _test_mode_changed(self):
        self.ui_state["test_mode"] = self.test_mode.get()
        if self.engine and self.engine.running:
            self.stop_engine(); self.start_engine()
        else:
            self.set_status(self.status)

    def _update_mode_hint(self):
        m = self.model.setting("mode")
        hint = {"always": "Always listening: just say a command.",
                "push_to_talk": f"Push-to-talk: hold {str(self.model.setting('push_to_talk_key')).title()} while you speak.",
                "wake_word": f"Wake word: say \"{self.model.setting('wake_word')} warp\"."}.get(m, "")
        self.mode_hint.configure(text=hint)

    # ------------------------------------------------------------------ commands tab
    def _group_toggled(self, g):
        self.model.set_group_enabled(g, self.group_vars[g].get())
        self.refresh_commands(); self.schedule_restart()

    def open_dialog(self, title, group="", phrase="", spec=""):
        group = group or (self.filter.get() if self.filter.get() != "All groups" else "")
        d = CommandDialog(self, title, group, phrase, spec)
        self.wait_window(d)
        return d.result

    def _after_dialog(self, res):
        if not res:
            return
        g, p, warns = res
        self.refresh_commands(); self.refresh_spots(); self.select_phrase(p)
        for w in warns:
            self.add_log(f"[warn] {p}: {w}", "skipped")
        self.schedule_restart()

    def add_command(self):
        self._after_dialog(self.open_dialog("Add command"))

    def edit_command(self):
        sel = self.selected()
        if not sel:
            return messagebox.showinfo("EVE Voice", "Pick a command in the list first.", parent=self)
        g, p = sel
        self._after_dialog(self.open_dialog(f"Edit '{p}'", g, p, self.model.get(g, p)))

    def duplicate_command(self):
        sel = self.selected()
        if not sel:
            return
        try:
            new = self.model.duplicate_command(*sel)
        except ValidationError as e:
            return messagebox.showerror("EVE Voice", str(e), parent=self)
        self.refresh_commands(); self.select_phrase(new); self.schedule_restart()
        self.add_log(f"Duplicated as '{new}' - edit it to change the words.", "info")

    def delete_command(self, confirm: bool = True):
        sel = self.selected()
        if not sel:
            return
        if confirm and not messagebox.askyesno("EVE Voice", f"Delete '{sel[1]}'?", parent=self):
            return
        self.model.delete_command(*sel)
        self.refresh_commands(); self.refresh_spots(); self.schedule_restart()

    def test_command(self):
        sel = self.selected()
        if not sel:
            return messagebox.showinfo("EVE Voice", "Pick a command in the list first.", parent=self)
        tm = self.test_mode.get()
        self.add_log(f"[test] Testing '{sel[1]}' once{' (test mode: nothing is pressed)' if tm else ' - click into EVE now'}.",
                     "info")
        threading.Thread(target=fire_test_command, args=(self.model.snapshot(), sel[1], tm, self.events),
                         daemon=True).start()

    # ------------------------------------------------------------------ calibration
    overlay_fn = staticmethod(ev.select_box_overlay)   # swapped out by the GUI smoke test
    input_overlay_fn = staticmethod(lambda master, cb: InputOverlay(master, cb))
    test_fn = staticmethod(lambda *a: fire_test_command(*a))

    def open_teach(self):
        """Teach: 'when I say X, do this ONE input'. Never records a sequence."""
        if self._teach is not None and self._teach.winfo_exists():
            self._teach.lift()
            return self._teach
        if self._pr_cap is not None:
            self.stop_practice()
        self._teach = TeachDialog(self)
        return self._teach

    def calibrate_spot(self, name: str, done=None, countdown_ms: int = 400):
        """Hide this window, show the box-select overlay, store the spot (unsaved until Save)."""
        self.add_log(f"Drag a box over the spot for '{name}' (Esc or right-click cancels).", "setup")
        self.iconify()

        def go():
            box = None
            try:
                box = self.overlay_fn(f"Drag a box for '{name}' - Esc cancels", master=self)
            except Exception as e:
                log.exception("overlay failed")
                self.add_log(f"[error] couldn't open the overlay: {e}", "error")
            self.deiconify(); self.lift()
            if box:
                self.model.set_spot(name, box)
                self.add_log(f"Spot '{name}' set to {box}. Press Save to keep it.", "setup")
                self.refresh_spots(); self.refresh_commands(); self.update_dirty()
            else:
                self.add_log(f"Spot '{name}' unchanged.", "info")
            if done:
                done(box)
            if box:
                self.schedule_restart()
        self.after(countdown_ms, go)

    def _selected_spot(self):
        s = self.spot_tree.selection()
        return s[0] if s else None

    def set_selected_spot(self):
        n = self._selected_spot()
        if not n:
            return messagebox.showinfo("EVE Voice", "Pick a spot in the list first.", parent=self)
        self.calibrate_spot(n)

    def clear_selected_spot(self):
        n = self._selected_spot()
        if n:
            self.model.clear_spot(n); self.refresh_spots(); self.refresh_commands(); self.schedule_restart()

    def new_spot(self):
        n = simpledialog.askstring("EVE Voice", "Name for the new spot (e.g. undock_button):", parent=self)
        if n:
            n = n.strip().replace(" ", "_")
            self.model.set_spot(n, None); self.refresh_spots(); self.update_dirty()

    # ------------------------------------------------------------------ settings tab
    def _set(self, key, value):
        if self._loading:
            return
        if self.model.setting(key) != value:
            self.model.set_setting(key, value)
            self.schedule_restart()

    def _mic_changed(self):
        lbl = self.mic_var.get()
        idx = next((i for i, l in self._mics if l == lbl), None)
        self._set("mic_device", None if idx is None else lbl)   # stored by name: survives re-plugging
        if self.mic.stream:
            self.toggle_mic_test(); self.toggle_mic_test()

    def toggle_mic_test(self):
        if self.mic.stream:
            return self.mic_stop()
        if self.engine and self.engine.running:
            self.mic_err.configure(text="The level shows on the Listen tab while listening.")
            return
        lbl = self.mic_var.get()
        idx = next((i for i, l in self._mics if l == lbl), None)
        err = self.mic.start(idx)
        self.mic_err.configure(text=err or "Talk - the bar should move.")
        self.mic_test_btn.configure(text="Stop test" if self.mic.stream else "Test mic")

    def mic_stop(self):
        self.mic.stop()
        self.mic_test_btn.configure(text="Test mic")

    def _mode_changed(self):
        self._set("mode", self.mode_var.get())
        self._update_mode_hint()

    def _ptt_changed(self, v):
        if v:
            self._set("push_to_talk_key", v); self._update_mode_hint()

    def _wake_changed(self):
        w = ev.normalize_phrase(self.wake_var.get())
        bad = ev.unknown_words(w, self.vocab)
        self.wake_warn.configure(text=(f"Speech model doesn't know '{bad[0]}'" if bad else
                                       ("Wake word can't be empty" if not w else "")))
        if w:
            self._set("wake_word", w)
        try:
            s = float(self.wake_win.get())
            if 1 <= s <= 30:
                self._set("wake_window_seconds", s)
        except ValueError:
            pass
        self._update_mode_hint()

    def _bool_changed(self, key):
        self._set(key, bool(self.bools[key].get()))

    def _num_changed(self, key, var):
        try:
            v = float(var.get())
        except ValueError:
            return
        if 0 <= v <= 10:
            self._set(key, v)

    def _hold_changed(self):
        v = round(float(self.hold_var.get()), 2)
        self.hold_lbl.configure(text=f"{v:.2f} s")
        self._set("key_hold_seconds", v)

    def _conf_changed(self):
        v = round(float(self.conf_var.get()), 2)
        self.conf_lbl.configure(text=f"{v:.0%}")
        self._set("min_confidence", v)

    def _gate_text(self) -> str:
        v = float(self.gate_var.get())
        return "off" if v <= 0.001 else f"{v:.3f}"

    def _gate_changed(self):
        v = round(float(self.gate_var.get()), 3)
        self.gate_lbl.configure(text=self._gate_text())
        self._set("noise_gate", v)

    def _show_held(self):
        h = self._held
        if h is None:
            self.held_bar.pack_forget()
            return
        left = max(0.0, h["until"] - time.monotonic())
        text = {"click": f"Holding {h['label']} - click your target   ({left:.1f}s)",
                "release": f"Holding {h['label']} - say 'release'   ({left:.0f}s)",
                "timed": f"Holding {h['label']}   ({left:.1f}s)"}.get(h["mode"], f"Holding {h['label']}")
        self.held_lbl.configure(text=text)
        if not self.held_bar.winfo_ismapped():
            self.held_bar.pack(fill="x", pady=(10, 0), before=self.log_txt.master)

    def release_held(self):
        app = self.engine.app if self.engine is not None else None
        if app is not None and app.hold.release("Release button"):
            return
        self._held = None
        self._show_held()

    def measure_noise(self):
        """Listen 2 s (from the running listener or the mic test) and put the gate just above it."""
        if not ((self.engine and self.engine.running) or self.mic.stream):
            self.toggle_mic_test()
            if not self.mic.stream:
                return
        self._measure = {"until": time.monotonic() + 2.0, "peak": 0.0}
        self.mic_err.configure(text="Measuring background noise - stay quiet for 2 seconds...", style="Dim.TLabel")

    def _finish_measure(self):
        peak, self._measure = self._measure["peak"], None
        gate = round(min(0.3, peak + 0.02), 3)
        self.gate_var.set(gate); self._gate_changed()
        self.mic_err.configure(text=f"Background peak {peak:.3f} -> noise gate set to {gate:.3f}.", style="Ok.TLabel")

    def keys_helper(self):
        cur = float(self.model.setting("key_hold_seconds") or 0)
        if cur < 0.08:
            self.hold_var.set(0.08); self._hold_changed()
        admin = ev.running_as_admin()
        state = {True: "YES - EVE Voice is running as administrator.",
                 False: "NO - EVE Voice is NOT running as administrator.",
                 None: "unknown (only checked on Windows)."}[admin]
        messagebox.showinfo("Keys sometimes don't register", (
            f"1) Key hold time is now {max(cur, 0.08):.2f} s (EVE misses very short presses).\n\n"
            f"2) Administrator: {state}\n"
            "Windows silently blocks key presses from a normal program to a program running as "
            "administrator. If you start EVE or its launcher as administrator, close this app and "
            "right-click run.bat > Run as administrator.\n\n"
            "3) Use windowed or 'fixed window' mode, keep EVE focused, and turn on the Test button for "
            "one command (Commands tab) to check it.\n\n"
            "4) Still flaky for one command? Edit it > Options > Hold key, e.g. 0.12."), parent=self)

    def _accuracy_changed(self):
        if self._loading:
            return
        key = next(k for k, m in MODELS.items() if m["label"] == self.acc_var.get())
        md = resolve_model_dir(MODELS[key]["path"])
        if model_installed(md) or key == "fast":
            self.model.set_setting("model_path", MODELS[key]["path"])
            self.vocab = ev.load_vocab(self.model_dir)
            self.refresh_settings(); self.schedule_restart(full=True)
            return
        if self._downloading:
            return
        if not messagebox.askyesno("Download the accurate model?", (
                "This downloads the larger English speech model (about 128 MB, ~200 MB unpacked) "
                "from alphacephei.com (the Vosk project) into this folder. It runs offline afterwards.\n\n"
                "It recognises more reliably (especially with game noise and for Teach), and loads in "
                "about a second.\n\nDownload now?"), parent=self):
            self.refresh_settings()
            return
        self._downloading = True
        self.dl_bar["value"] = 0

        def work():
            try:
                download_model(key, APP_DIR, lambda f, t: self.events.put(("download", f, t)))
                self.events.put(("download_done", key))
            except Exception as e:
                log.exception("model download failed")
                self.events.put(("download_error", friendly_error(e)))
        threading.Thread(target=work, daemon=True, name="model-download").start()

    # ------------------------------------------------------------------ next gate tab (opt-in)
    GATE = "next_gate"

    def _build_gate(self):
        t = self.tab_gate
        warn = ttk.LabelFrame(t, text="  Read this first  ", padding=10)
        warn.pack(fill="x")
        ttk.Label(warn, style="Warn.TLabel", wraplength=900, text=(
            "'gate' is the ONE command that looks at your screen. When you say it, the app takes "
            "one picture of just your Overview box (Pillow), finds the yellow next-gate icon and sends ONE left "
            "click on it. It never watches continuously, never reads game memory or files, and saves nothing "
            "(unless you press 'Show what it sees'). Screen reading is a GRAY AREA under the EVE EULA - if you "
            "want to be sure, ask CCP support before using it. It's OFF until you turn it on here.")).pack(anchor="w")
        self.gate_on = tk.BooleanVar()
        ttk.Checkbutton(warn, text="Turn on 'gate' (reads the Overview pixels when said)",
                        variable=self.gate_on, command=self._gate_toggle).pack(anchor="w", pady=(8, 0))
        self.gate_state = ttk.Label(warn, text="", style="Dim.TLabel"); self.gate_state.pack(anchor="w")

        body = ttk.Frame(t); body.pack(fill="both", expand=True, pady=(10, 0))
        body.columnconfigure(0, weight=1); body.columnconfigure(1, weight=1)
        s1 = ttk.LabelFrame(body, text="  1. Where is your Overview?  ", padding=10)
        s1.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=(0, 8))
        ttk.Button(s1, text="Set Overview area", command=self.gate_set_area).pack(anchor="w")
        self.gate_box_lbl = ttk.Label(s1, text="", style="Dim.TLabel"); self.gate_box_lbl.pack(anchor="w", pady=(4, 0))
        ttk.Label(s1, style="Dim.TLabel", wraplength=420, text=(
            "Drag a box over the Overview rows (not the tabs). Move/resize the Overview later? Set it again.")
        ).pack(anchor="w")

        s2 = ttk.LabelFrame(body, text="  2. Pick the yellow  ", padding=10)
        s2.grid(row=0, column=1, sticky="nsew", pady=(0, 8))
        r = ttk.Frame(s2); r.pack(anchor="w")
        ttk.Button(r, text="Pick the yellow colour", command=self.gate_pick).pack(side="left")
        self.gate_swatch = tk.Canvas(r, width=26, height=18, highlightthickness=1,
                                     highlightbackground=COL["line"], bg="#e0ee40")
        self.gate_swatch.pack(side="left", padx=8)
        self.gate_col_lbl = ttk.Label(r, text="", style="Dim.TLabel"); self.gate_col_lbl.pack(side="left")
        ttk.Label(s2, style="Dim.TLabel", wraplength=420, text=(
            "Set a route so a gate shows yellow in the Overview, then click once on that yellow icon.")
        ).pack(anchor="w", pady=(4, 0))

        s3 = ttk.LabelFrame(body, text="  3. Fine tuning (defaults are measured from EVE's icons)  ", padding=10)
        s3.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        self.g_hue = tk.DoubleVar(); self.g_sat = tk.DoubleVar(); self.g_minpx = tk.StringVar()
        ttk.Label(s3, text="Colour tolerance (hue)").grid(row=0, column=0, sticky="w")
        ttk.Scale(s3, from_=3, to=30, variable=self.g_hue, length=150,
                  command=lambda v: self._gate_num("hue_tolerance", round(self.g_hue.get()))).grid(row=0, column=1, padx=6)
        self.g_hue_lbl = ttk.Label(s3, style="Dim.TLabel"); self.g_hue_lbl.grid(row=0, column=2, sticky="w")
        ttk.Label(s3, text="Min colourfulness").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Scale(s3, from_=0.2, to=0.9, variable=self.g_sat, length=150,
                  command=lambda v: self._gate_num("min_saturation", round(self.g_sat.get(), 2))).grid(
            row=1, column=1, padx=6, pady=(4, 0))
        self.g_sat_lbl = ttk.Label(s3, style="Dim.TLabel"); self.g_sat_lbl.grid(row=1, column=2, sticky="w")
        ttk.Label(s3, text="Ignore specks under (pixels)").grid(row=2, column=0, sticky="w", pady=(4, 0))
        ttk.Spinbox(s3, from_=1, to=200, textvariable=self.g_minpx, width=6,
                    command=lambda: self._gate_int("min_pixels", self.g_minpx)).grid(row=2, column=1, sticky="w",
                                                                                     padx=6, pady=(4, 0))
        ttk.Label(s3, style="Dim.TLabel", wraplength=420, text=(
            "Normal (grey) gates are never matched: they have no colour. Two yellow rows far apart = it "
            "clicks nothing and says so.")).grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        s4 = ttk.LabelFrame(body, text="  4. Where to click & check  ", padding=10)
        s4.grid(row=1, column=1, sticky="nsew")
        self.g_where = tk.StringVar(value="icon"); self.g_x = tk.StringVar(value="60")
        ttk.Radiobutton(s4, text="On the yellow icon", value="icon", variable=self.g_where,
                        command=self._gate_where).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Radiobutton(s4, text="On the row, this many pixels from the box's left edge:", value="x",
                        variable=self.g_where, command=self._gate_where).grid(row=1, column=0, sticky="w")
        ttk.Spinbox(s4, from_=0, to=2000, textvariable=self.g_x, width=6,
                    command=self._gate_where).grid(row=1, column=1, sticky="w", padx=6)
        ttk.Button(s4, text="Show what it sees", command=self.gate_preview).grid(row=2, column=0, sticky="w",
                                                                               pady=(10, 0))
        ttk.Label(s4, style="Dim.TLabel", wraplength=420, text=(
            "Takes one picture of the box, marks what it would click and saves what_it_sees.png. "
            "Sends nothing.")).grid(row=3, column=0, columnspan=2, sticky="w")
        self.gate_msg = ttk.Label(s4, text="", style="Dim.TLabel", wraplength=420)
        self.gate_msg.grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

    def refresh_gate(self):
        m = self.model
        import gate_finder
        t = (m.data.get("color_targets") or {}).get(self.GATE) or dict(gate_finder.DEFAULT_TARGET)
        self.gate_on.set(bool(m.setting("allow_screen_read")))
        prob = ev.color_target_problem(t)
        box = t.get("box")
        self.gate_box_lbl.configure(text=f"Overview box: {box}" if box else "Not set yet.",
                                    style="Ok.TLabel" if box else "Warn.TLabel")
        rgb = t.get("rgb") or [224, 238, 64]
        self.gate_swatch.configure(bg="#%02x%02x%02x" % tuple(int(c) for c in rgb[:3]))
        self.gate_col_lbl.configure(
            text=f"hue {gate_finder.hue_of(rgb):.0f} deg, icon {t.get('icon_size', [10, 10])[0]}x"
                 f"{t.get('icon_size', [10, 10])[1]}" + ("" if t.get("sampled") else "  (default - pick yours)"),
            style="Dim.TLabel" if t.get("sampled") else "Warn.TLabel")
        self.g_hue.set(float(t.get("hue_tolerance", 10))); self.g_hue_lbl.configure(text=f"+/- {self.g_hue.get():.0f} deg")
        self.g_sat.set(float(t.get("min_saturation", 0.45))); self.g_sat_lbl.configure(text=f"{self.g_sat.get():.2f}")
        self.g_minpx.set(str(t.get("min_pixels", 12)))
        cx = t.get("click_x")
        self.g_where.set("icon" if cx is None else "x")
        if cx is not None:
            self.g_x.set(str(cx))
        if m.setting("allow_screen_read"):
            self.gate_state.configure(text="ON - 'gate' reads your Overview box when you say it.",
                                      style="Warn.TLabel")
        elif prob:
            self.gate_state.configure(text=f"Off. Before turning it on: {prob}.", style="Dim.TLabel")
        else:
            self.gate_state.configure(text="Off. Set up and ready - tick the box to turn it on.", style="Dim.TLabel")

    def _gate_changed_any(self):
        self.refresh_gate(); self.refresh_commands(); self.update_dirty(); self.schedule_restart()

    def _gate_toggle(self):
        if self._loading:
            return
        on = bool(self.gate_on.get())
        prob = ev.color_target_problem(self.model.color_target(self.GATE))
        if on and prob:
            self.gate_on.set(False)
            messagebox.showinfo("EVE Voice", f"Set it up first: {prob}.", parent=self)
            return
        if on and not messagebox.askyesno("Turn on screen reading?", (
                "'gate' will take one picture of your Overview box each time you say it, and "
                "click the yellow gate once.\n\nThis is a gray area under the EVE EULA. CCP hasn't said "
                "it's OK. Ask CCP support if you want to be sure.\n\nTurn it on?"), parent=self):
            self.gate_on.set(False)
            return
        self.model.set_setting("allow_screen_read", on)
        self.add_log(f"[opt-in] 'gate' screen reading turned {'ON' if on else 'off'}.", "setup")
        self._gate_changed_any()

    def _gate_num(self, key, value):
        if self._loading:
            return
        self.model.set_color_target(self.GATE, **{key: value})
        self.g_hue_lbl.configure(text=f"+/- {self.g_hue.get():.0f} deg")
        self.g_sat_lbl.configure(text=f"{self.g_sat.get():.2f}")
        self.update_dirty(); self.schedule_restart()

    def _gate_int(self, key, var):
        try:
            self._gate_num(key, max(1, int(float(var.get()))))
        except ValueError:
            pass

    def _gate_where(self):
        if self._loading:
            return
        try:
            cx = None if self.g_where.get() == "icon" else max(0, int(float(self.g_x.get())))
        except ValueError:
            return
        self.model.set_color_target(self.GATE, click_x=cx)
        self.update_dirty(); self.schedule_restart()

    def _with_overlay(self, title, heading, hint, then):
        """Hide this window, show OUR overlay (tk events only - no global hooks), hand back the box."""
        self.iconify()

        def go():
            box = None
            try:
                box = self.overlay_fn(title, master=self, heading=heading, hint=hint)
            except Exception as e:
                log.exception("overlay failed")
                self.add_log(f"[error] couldn't open the overlay: {e}", "error")
            self.deiconify(); self.lift()
            then(box)
        self.after(400, go)

    def gate_set_area(self):
        def then(box):
            if box and (box[2] - box[0] >= 20 and box[3] - box[1] >= 20):
                self.model.set_color_target(self.GATE, box=list(box))
                self.add_log(f"Overview area set to {box}. Press Save to keep it.", "setup")
                self._gate_changed_any()
            elif box:
                self.add_log("That box is too small - drag over the whole Overview list.", "skipped")
        self._with_overlay("Overview", "EVE Voice - set Overview area",
                           "Drag a box over your Overview rows. Release to save.   Esc = cancel.", then)

    grab_fn = None   # tests swap this; default is gate_finder.grab_box

    def _grab(self, box):
        import gate_finder
        return (self.grab_fn or gate_finder.grab_box)(box)

    def gate_pick(self):
        def then(box):
            if not box:
                return
            import gate_finder
            x, y = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
            r = 14
            try:
                img = self._grab([x - r, y - r, x + r, y + r])
            except Exception as e:
                self.add_log(f"[error] couldn't read that spot: {e}", "error")
                return
            fields = gate_finder.pick_from_image(img, (r, r), self.model.color_target(self.GATE))
            h, s_, v = colorsys.rgb_to_hsv(*[c / 255 for c in fields["rgb"]])
            if s_ < 0.3:
                self.add_log(f"That spot isn't colourful (picked {fields['rgb']}). Click right on the yellow icon.",
                             "skipped")
                return
            self.model.set_color_target(self.GATE, **fields)
            self.add_log(f"Yellow picked: RGB {fields['rgb']} (hue {h * 360:.0f} deg), icon "
                         f"{fields['icon_size'][0]}x{fields['icon_size'][1]}. Press Save to keep it.", "setup")
            self._gate_changed_any()
        self._with_overlay("yellow", "EVE Voice - pick the yellow",
                           "Click ONCE right on the yellow next-gate icon in your Overview.   Esc = cancel.", then)

    def gate_preview(self):
        import gate_finder
        t = self.model.color_target(self.GATE)
        if not t.get("box"):
            return messagebox.showinfo("EVE Voice", "Set the Overview area first.", parent=self)
        self.iconify()

        def go():
            try:
                img = self._grab(t["box"])
                res = gate_finder.find_target(img, t)
                out = gate_finder.debug_image(img, res)
                path = os.path.join(APP_DIR, "what_it_sees.png")
                out.save(path)
            except Exception as e:
                self.deiconify()
                log.exception("preview failed")
                self.gate_msg.configure(text=f"Couldn't look: {e}", style="Bad.TLabel")
                return
            self.deiconify(); self.lift()
            where = ""
            if res.point is not None:
                where = f" -> would click {t['box'][0] + res.point[0]},{t['box'][1] + res.point[1]}"
            self.gate_msg.configure(text=f"{res.message}{where}. Saved {path}.",
                                    style="Ok.TLabel" if res.point else "Warn.TLabel")
            self.add_log(f"[test] what it sees: {res.message}{where} (nothing sent)", "info")
            self._show_image(path, "What 'gate' sees")
        self.after(400, go)

    def _show_image(self, path, title):
        w = _Toplevel(self); w.title(title); w.configure(bg=COL["bg"])
        try:
            img = tk.PhotoImage(file=path)
            while img.width() > 900 or img.height() > 650:
                img = img.subsample(2)
            lbl = tk.Label(w, image=img, bg=COL["bg"]); lbl.image = img; lbl.pack(padx=10, pady=10)
        except tk.TclError as e:
            ttk.Label(w, text=f"Saved to {path} ({e})").pack(padx=10, pady=10)
        ttk.Label(w, style="Dim.TLabel", text="Grey boxes = yellow shapes seen. Green = chosen row. Red cross = "
                                              "click point.").pack(padx=10, pady=(0, 10))
        self._preview_win = w

    # ------------------------------------------------------------------ clients tab
    def _target_changed(self):
        if self._loading:
            return
        self.model.set_target(mode=self.target_var.get())
        self.win_box.configure(state="normal" if self.target_var.get() == "background_window" else "disabled")
        if self.target_var.get() == "background_window":
            self.refresh_windows()
        self.refresh_commands(); self.schedule_restart()

    def _win_changed(self):
        if self._loading:
            return
        if self.model.target().get("window_title") != self.win_var.get():
            self.model.set_target(window_title=self.win_var.get())
            self.schedule_restart()

    def _api(self):
        try:
            return ev.Win32Api()
        except Exception:
            return None

    def refresh_windows(self):
        api = self._api()
        if api is None:
            self.win_msg.configure(text="Window list only works on Windows.", style="Warn.TLabel")
            return []
        wins = ev.list_eve_windows(api)
        self.win_box.configure(values=[t for _, t in wins])
        self.win_msg.configure(text=f"Found {len(wins)} EVE window(s)." if wins else
                               "No EVE windows found - start and log in your clients, then Refresh.",
                               style="Dim.TLabel")
        return wins

    def send_bg_test(self):
        title = self.win_var.get().strip()
        if not title:
            return messagebox.showinfo("EVE Voice", "Pick the client window first.", parent=self)
        api = self._api()
        if self.test_mode.get() or api is None:
            api = ev.DryRunApi(api)
            self.add_log("[test] background test in dry-run (test mode or not on Windows).", "info")
        try:
            cfg = ev.load_config_dict(self.model.snapshot())
        except ev.ConfigError as e:
            return messagebox.showerror("EVE Voice", str(e), parent=self)
        cfg.target["window_title"] = title
        self.nb.select(self.tab_listen)
        out = self.events
        threading.Thread(target=lambda: ev.run_test_background(cfg, api, log=lambda m: out.put(("log", m))),
                         daemon=True).start()

    # ------------------------------------------------------------------ save / close
    def save(self) -> bool:
        try:
            warns = self.model.save()
        except ValidationError as e:
            messagebox.showerror("EVE Voice", f"Not saved:\n\n{e}", parent=self)
            return False
        except OSError as e:
            log.exception("save failed")
            messagebox.showerror("EVE Voice", f"Couldn't write {self.config_path}:\n\n{e}", parent=self)
            return False
        self.add_log(f"Saved {os.path.basename(self.config_path)} (backup: .bak).", "info")
        for w in warns:
            self.add_log(f"[warn] {w}", "skipped")
        self.refresh_commands(); self.update_dirty()
        return True

    def revert(self, confirm: bool = True):
        if confirm and self.model.dirty and not messagebox.askyesno(
                "EVE Voice", "Throw away your unsaved changes?", parent=self):
            return
        self.model.revert(); self.refresh_all(); self.schedule_restart()
        self.add_log("Reverted to the saved file.", "info")

    def on_close(self):
        if self.model.dirty:
            r = messagebox.askyesnocancel("EVE Voice", "Save your changes before closing?", parent=self)
            if r is None:
                return
            if r and not self.save():
                return
        self.shutdown()

    def shutdown(self):
        if self.engine:
            self.engine.stop(1.0)
        self.mic.stop()
        try:
            if self.state() == "normal":
                self.ui_state["geometry"] = self.geometry()
        except tk.TclError:
            pass
        save_state(self.ui_state, self.state_path)
        self.destroy()

    # ------------------------------------------------------------------ misc
    def _open(self, path):
        if not os.path.exists(path):
            return messagebox.showinfo("EVE Voice", f"{os.path.basename(path)} doesn't exist yet.", parent=self)
        try:
            if sys.platform == "win32":
                os.startfile(path)   # noqa - opens README/log in the user's editor
            else:
                webbrowser.open("file://" + path)
        except Exception as e:
            messagebox.showerror("EVE Voice", str(e), parent=self)

    def first_run_checks(self) -> List[str]:
        probs = []
        if not os.path.isdir(self.model_dir):
            probs.append("The speech model is missing (model folder). Run setup.bat once.")
        mics, err = list_microphones()
        if err:
            probs.append("No microphone found yet: " + err)
        elif len(mics) <= 1:
            probs.append("No microphone found. Plug one in and check Windows privacy > Microphone.")
        return probs

    def welcome(self):
        self.ui_state["welcomed"] = True
        probs = self.first_run_checks()
        msg = ("Welcome to EVE Voice!\n\n"
               "1. Check your keybinds on the Commands tab (they must match EVE).\n"
               "2. Pick your mic on the Settings tab.\n"
               "3. Tick Test mode, press Start listening, say \"warp\".\n\n"
               "One phrase = one key or one click. No macros.")
        if probs:
            msg += "\n\nBefore you start:\n- " + "\n- ".join(probs)
        messagebox.showinfo("EVE Voice", msg, parent=self)

    def _tk_error(self, exc, val, tb):
        log.error("GUI error:\n%s", "".join(traceback.format_exception(exc, val, tb)))
        try:
            messagebox.showerror("EVE Voice", friendly_error(val), parent=self)
        except Exception:
            pass


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="EVE Voice desktop app")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--demo", action="store_true", help="fake microphone (for trying the GUI without a mic)")
    args = ap.parse_args(argv)
    if sys.stdout is None:   # pythonw has no console
        sys.stdout = sys.stderr = open(os.devnull, "w")
    setup_logging()
    if tk is None:
        print("tkinter is missing - reinstall Python from python.org with 'tcl/tk' ticked, "
              "or use run_cli.bat.", file=sys.stderr)
        return 1
    ev.make_dpi_aware()
    try:
        try:
            import eve_update
            eve_update.ensure_user_config(APP_DIR)
        except Exception:
            log.exception("couldn't create commands.json from the defaults")
        win = MainWindow(os.path.abspath(args.config), demo=args.demo, check_updates=not args.demo)
    except Exception as e:
        log.exception("startup failed")
        try:
            r = tk.Tk(); r.withdraw()
            messagebox.showerror("EVE Voice", f"EVE Voice couldn't start:\n\n{e}\n\nDetails: {LOG_PATH}")
            r.destroy()
        except Exception:
            print(f"EVE Voice couldn't start: {e}", file=sys.stderr)
        return 1
    win.mainloop()
    return win.exit_code


if __name__ == "__main__":
    sys.exit(main())
