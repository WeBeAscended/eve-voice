"""Run: python -m unittest discover -s tests   (no mic, no EVE, no key/mouse libs needed)"""
import json, os, re, sys, shutil, tempfile, unittest
from unittest import mock
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import eve_voice as ev

CFG_PATH = os.path.join(ROOT, "commands.default.json")
SRC_PATH = os.path.join(ROOT, "eve_voice.py")


def _all_on(path):
    """Shipped config with every command switched on and the pre-1.0.3 alternates, so the
    older tests exercise every default phrase. SHIPPED_PATH = the real file (core only)."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    for g in data["groups"].values():
        for ph, spec in list((g.get("commands") or {}).items()) if isinstance(g, dict) else []:
            if isinstance(spec, dict) and spec.pop("enabled", True) is False and list(spec) == ["key"]:
                g["commands"][ph] = spec["key"]
    data["groups"]["windows_travel"]["commands"]["gate"]["also"] = ["select next gate", "next gate", "select gate"]
    data["groups"]["setup"]["commands"]["release"]["also"] = ["let go"]
    import tempfile
    out = os.path.join(tempfile.mkdtemp(prefix="evetest-"), "commands.all_on.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return out


SHIPPED_PATH = CFG_PATH
CFG_PATH = _all_on(SHIPPED_PATH)


def load_raw(path=None):
    with open(path or CFG_PATH, encoding="utf-8") as f:
        return json.load(f)


class FakeClock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


class Recorder:
    """One shared log so we can count EVERY input across keyboard and mouse."""
    def __init__(self): self.calls = []


class MockSender(ev.KeySender):
    name = "mock"
    def __init__(self, rec): self.rec = rec; self.sent = []
    def press(self, modifiers, key, hold):
        self.sent.append("+".join(modifiers + [key])); self.rec.calls.append(("key", tuple(modifiers), key))

    # a held key: the keyDown is the ONE input (counted in rec.calls); its keyUp is tracked
    # separately in .ups so tests can check every down gets exactly one matching up
    def key_down(self, modifiers, key):
        self.press(modifiers, key, 0)
        self.downs = getattr(self, "downs", []) + ["+".join(modifiers + [key])]

    def key_up(self, modifiers, key):
        self.ups = getattr(self, "ups", []) + ["+".join(modifiers + [key])]


class MockMouse(ev.MouseSender):
    name = "mock"
    def __init__(self, rec): self.rec = rec; self.clicks = []
    def click(self, button, at=None):
        self.clicks.append((button, at)); self.rec.calls.append(("mouse", button, at))


def make_app(mode="always", title="EVE - Spence", data=None, **overrides):
    if data is None:
        data = load_raw()
    data.setdefault("settings", {})
    data["settings"]["mode"] = mode
    data["settings"].update(overrides)
    cfg = ev.load_config_dict(data)
    clock = FakeClock()
    ptt = {"down": False}
    gate = ev.TriggerGate(cfg, ptt_pressed=lambda: ptt["down"], clock=clock)
    rec = Recorder()
    sender = MockSender(rec)
    logs = []
    title_fn = title if callable(title) else (lambda: title)
    app = ev.VoiceApp(cfg, sender, gate, title_fn=title_fn, clock=clock, log=logs.append,
                      mouse=MockMouse(rec))
    app.rec = rec
    app.hold.spawn = None                 # tests drive the hold timer with app.hold.tick()
    app.hold.left_button = lambda: False
    return app, sender, clock, ptt, logs


def calibrated_data(box=(100, 200, 300, 220)):
    data = load_raw()
    data["spots"]["next_waypoint"] = list(box)
    data["settings"]["allow_mouse_at"] = True
    return data


def fully_bound_data():
    """Default config with the spot calibrated and every empty placeholder bound to a key,
    so EVERY default phrase is loadable."""
    data = calibrated_data()
    for g in data["groups"].values():
        if isinstance(g, dict):
            g["enabled"] = True
            for k, v in list(g.get("commands", {}).items()):
                if v == "":
                    g["commands"][k] = "x"
                elif isinstance(v, dict) and "mouse_box" in v and v["mouse_box"] is None:
                    v["mouse_box"] = [0, 0, 10, 10]
    return data


# =========================================================================== compliance
class TestCompliance(unittest.TestCase):
    """The user's three rules."""

    def _fire_all(self, data):
        app, _, clock, _, _ = make_app(data=data)
        results = {}
        for phrase, cmd in app.cfg.commands.items():
            if cmd.kind in ("calibrate", "teach", "release"):
                continue
            clock.t += 10
            before = len(app.rec.calls)
            with mock.patch.object(ev.time, "sleep") as slp:
                self.assertIs(app.handle_text(phrase), cmd, phrase)
            results[phrase] = (app.rec.calls[before:], slp.call_count)
        return app, results

    def test_rule1_every_default_command_is_exactly_one_action(self):
        app, results = self._fire_all(fully_bound_data())
        self.assertGreaterEqual(len(results), 50)
        for phrase, (calls, _) in results.items():
            self.assertEqual(len(calls), 1, f"'{phrase}' produced {calls}")
            kind = calls[0][0]
            self.assertEqual(kind, "mouse" if app.cfg.commands[phrase].kind == "mouse" else "key", phrase)

    def test_rule1_shipped_config_as_is(self):
        app, results = self._fire_all(load_raw())
        for phrase, (calls, _) in results.items():
            self.assertEqual(len(calls), 1, phrase)

    def test_rule3_no_sleep_between_actions(self):
        # with mock senders nothing at all may sleep: the only sleep in the send path is the
        # key-hold inside a real sender's single press
        _, results = self._fire_all(fully_bound_data())
        for phrase, (_, sleeps) in results.items():
            self.assertEqual(sleeps, 0, phrase)

    def test_rule1_rejected_forms(self):
        bad = {
            "combo": {"steps": [{"mouse": "left"}, {"key": "s"}]},
            "one step combo": {"steps": [{"key": "s"}]},
            "empty steps": {"steps": []},
            "both": {"key": "s", "mouse": "left"},
            "double": {"mouse": "double"},
            "ctrl click": {"mouse": "left", "modifiers": ["ctrl"]},
            "shift click": {"mouse": "left", "modifiers": "shift"},
        }
        cfg = ev.load_config_dict({"groups": {"g": {"commands": dict(bad, ok="q")}}})
        self.assertEqual(list(cfg.commands), ["ok"])
        text = " ".join(cfg.warnings)
        for phrase in bad:
            self.assertIn(f"'{phrase}'", text)
        self.assertGreaterEqual(text.count("1:1 rule"), len(bad))

    def test_removed_settings_warn(self):
        cfg = ev.load_config_dict({"settings": {"step_delay_seconds": 0.1, "restore_cursor": True},
                                   "groups": {"g": {"commands": {"a": "a"}}}})
        self.assertEqual(sum("removed" in w and "1:1 rule" in w for w in cfg.warnings), 2)
        self.assertNotIn("step_delay_seconds", cfg.settings)
        self.assertNotIn("restore_cursor", cfg.settings)

    def test_default_config_has_no_banned_forms(self):
        raw = json.dumps(load_raw())
        for banned in ('"steps"', '"double"', '"modifiers"', "step_delay", "restore_cursor"):
            self.assertNotIn(banned, raw)

    def test_rule2_source_has_no_memory_hooks_or_logs(self):
        src = open(SRC_PATH, encoding="utf-8").read()
        for bad in ["ReadProcessMemory", "WriteProcessMemory", "OpenProcess", "pymem", "psutil",
                    "win32process", "SetWindowsHookEx", "CreateRemoteThread", "Gamelogs", "Chatlogs",
                    "EVE\\\\logs", "frida", "inject", "LoadLibrary", "screenshot", "ImageGrab", "pytesseract",
                    "keyboard.hook", "is_pressed", "SendInput", "SetForegroundWindow", "SetFocus",
                    "SetCursorPos", "BringWindowToTop", "SetActiveWindow", "AttachThreadInput",
                    "SendMessage", "HWND_BROADCAST"]:
            hits = [l for l in src.splitlines() if bad.lower() in l.lower()
                    and not l.strip().startswith("#") and "1:1" not in l and "Rejected" not in l
                    and "combos" not in l]
            self.assertEqual(hits, [], f"found '{bad}': {hits}")
        # 'steps' only appears in code that REJECTS it - never read/iterated
        self.assertNotIn('["steps"]', src); self.assertNotIn('.get("steps")', src)
        # every Windows API call is on a small read-only/allowlisted set (no process access)
        allowed = {"user32.GetForegroundWindow", "user32.GetWindowTextLengthW", "user32.GetWindowTextW",
                   "user32.GetCursorPos", "user32.GetSystemMetrics", "user32.GetAsyncKeyState",
                   "user32.SetProcessDPIAware", "shcore.SetProcessDpiAwareness",
                   "shell32.IsUserAnAdmin",   # 'keys don't register' helper: am I elevated?
                   # experimental background mode (Win32Api): list windows, post to ONE of them
                   "user32.EnumWindows", "user32.IsWindowVisible", "user32.PostMessageW",
                   "user32.MapVirtualKeyW", "user32.ScreenToClient"}
        used = set(re.findall(r"windll\.(\w+\.\w+)", src)) | {
            "user32." + f for f in re.findall(r"\buser32\.(\w+)\(", src)} | {
            "user32." + f for f in re.findall(r"\bu\.(\w+)[\(.]", src)}
        self.assertTrue(used <= allowed, used - allowed)
        # only file the program opens is the config (read + calibration save)
        opens = re.findall(r"open\(([^,)]+)", src)
        # config file, its temp file, and the speech model's own word list (graph/Gr.fst)
        self.assertTrue(all(a.strip() in ("path", "tmp", "os.path.join(model_dir") for a in opens), opens)

    def test_one_send_site(self):
        src = open(SRC_PATH, encoding="utf-8").read()
        body = src[src.index("    def handle_text"):src.index("    def apply_config")]
        self.assertEqual(body.count("self.sender.press("), 1)
        self.assertEqual(body.count("self.mouse.click("), 1)
        self.assertNotIn("for ", body.split("def _send_one")[1])   # no loop in the send path

    def test_focus_gate_blocks_everything(self):
        app, _, clock, _, _ = make_app(data=fully_bound_data(), title="Discord")
        for phrase in app.cfg.commands:
            clock.t += 10
            app.handle_text(phrase)
        self.assertEqual(app.rec.calls, [])
        self.assertTrue(ev.DEFAULT_SETTINGS["require_eve_focus"])
        self.assertTrue(load_raw()["settings"]["require_eve_focus"])

    def test_calibrate_phrase_sends_no_input(self):
        app, *_ = make_app()
        app.calibrate_fn = lambda spot: True
        app.handle_text("set way point spot")
        self.assertEqual(app.rec.calls, [])

    def test_cooldown_only_drops(self):
        app, _, clock, _, _ = make_app()
        for _ in range(5):
            app.handle_text("warp"); clock.t += 0.1
        self.assertEqual(len(app.rec.calls), 1)


# =========================================================================== config
class TestConfig(unittest.TestCase):
    def test_default_config(self):
        cfg = ev.load_config(CFG_PATH)
        self.assertEqual(len(cfg.warnings), 1, cfg.warnings)   # only: select next needs calibrating
        self.assertIn("next_waypoint", cfg.warnings[0])
        c = cfg.commands
        self.assertEqual(cfg.mode, "always")
        self.assertEqual(c["warp"].key, "d")
        self.assertEqual(c["align"].key, "a")
        self.assertEqual(c["approach"].key, "q")
        self.assertEqual((c["select"].kind, c["select"].mouse, c["select"].target), ("mouse", "left", None))
        for unbound in ("dock", "jump", "select next", "double click"):
            self.assertNotIn(unbound, c)
        # EVE's hold-the-key-then-click actions default to 'hold until you click'
        for p, key in (("lock", "ctrl"), ("lock target", "ctrl"), ("approach", "q"), ("orbit", "w"),
                       ("keep at range", "e"), ("align", "a"), ("warp", "d")):
            self.assertEqual((c[p].key, c[p].hold_mode, c[p].hold_seconds), (key, "click", 10.0), p)
        self.assertEqual(c["release"].kind, "release")
        self.assertIs(cfg.lookup("let go"), c["release"])
        self.assertEqual(c["mid two"].modifiers, ["alt"])
        self.assertEqual(c["stop ship"].modifiers, ["ctrl"])
        self.assertEqual(c["set way point spot"].kind, "calibrate")

    def test_lock_note(self):
        g = load_raw()["groups"]["combat_targeting"]
        self.assertEqual(g["commands"], {"lock": {"key": "ctrl", "hold_until": "click"},
                                         "lock target": {"key": "ctrl", "hold_until": "click"}})
        self.assertIn("Ctrl", g["_note"])
        self.assertIn("Esc > Shortcuts", load_raw()["groups"]["navigation"]["_note"])

    def test_parse_key(self):
        self.assertEqual(ev.parse_key("ctrl+f1"), (["ctrl"], "f1"))
        self.assertEqual(ev.parse_key("Shift+R"), (["shift"], "r"))
        self.assertEqual(ev.parse_key("control+space"), (["ctrl"], "space"))
        self.assertEqual(ev.parse_key("ctrl+alt+f3"), (["ctrl", "alt"], "f3"))
        for bad in ["", "q+w", "ctrl+", "ctrl+ctrl+a", "f99", "banana"]:
            with self.assertRaises(ev.ConfigError, msg=bad):
                ev.parse_key(bad)

    def test_bad_entries_warn_not_crash(self):
        cfg = ev.load_config_dict({"groups": {"g": {"commands": {
            "good": "q", "bad key": "ctrl+banana", "High 1": "f1", "Good": "w"}}}})
        self.assertEqual(cfg.commands["good"].key, "w")  # duplicate -> later wins
        self.assertNotIn("bad key", cfg.commands)
        joined = " ".join(cfg.warnings)
        self.assertIn("banana", joined); self.assertIn("defined twice", joined)
        self.assertIn("spell numbers", joined)

    def test_placeholders_and_disabled(self):
        cfg = ev.load_config_dict({"_x": 1, "groups": {
            "_c": "comment", "a": {"enabled": False, "commands": {"x": "x"}},
            "b": {"commands": {"_note": "hi", "y": "y", "e": "", "ek": {"key": ""}}}}})
        self.assertEqual(list(cfg.commands), ["y"])
        self.assertEqual(cfg.warnings, [])

    def test_invalid_settings(self):
        g = {"g": {"commands": {"a": "a"}}}
        for settings in ({"mode": "sometimes"}, {"mode": "wake_word", "wake_word": ""},
                         {"key_hold_seconds": 2}, {"cooldown_seconds": "x"}):
            with self.assertRaises(ev.ConfigError, msg=settings):
                ev.load_config_dict({"settings": settings, "groups": g})
        with self.assertRaises(ev.ConfigError):
            ev.load_config_dict({"groups": {}})

    def test_grammar(self):
        app, *_ = make_app("always")
        g = ev.build_grammar(app.cfg)
        self.assertEqual(g[-1], "[unk]"); self.assertIn("warp", g); self.assertIn("keep at range", g)
        app, *_ = make_app("wake_word")
        g = ev.build_grammar(app.cfg)
        self.assertIn("computer", g); self.assertIn("computer warp", g); self.assertIn("warp", g)


# =========================================================================== modes
class TestAlwaysMode(unittest.TestCase):
    def test_fires_one_key_each(self):
        app, s, clock, _, _ = make_app()
        for p in ["warp", "stop ship", "mid one", "keep at range"]:
            clock.t += 5; app.handle_text(p)
        self.assertEqual(s.sent, ["d", "ctrl+space", "alt+f1", "e"])

    def test_ignores_unknown_and_combined(self):
        app, s, *_ = make_app()
        for t in ["", "[unk]", "warp [unk]", "warp keep at", "hello there", "keep at warp"]:
            self.assertIsNone(app.handle_text(t))
        self.assertEqual(app.rec.calls, [])

    def test_cooldown_per_phrase(self):
        app, s, clock, *_ = make_app()
        app.handle_text("warp"); clock.t += 0.3; app.handle_text("warp")
        self.assertEqual(s.sent, ["d"])
        clock.t += 1.0; app.handle_text("warp")
        self.assertEqual(s.sent, ["d", "d"])
        app.handle_text("align")
        self.assertEqual(s.sent[-1], "a")

    def test_focus_check(self):
        app, s, _, _, logs = make_app(title="Discord")
        self.assertIsNone(app.handle_text("warp"))
        self.assertEqual(s.sent, []); self.assertIn("isn't focused", logs[-1])
        app, s, *_ = make_app(title=None)
        app.handle_text("warp"); self.assertEqual(s.sent, [])
        app, s, *_ = make_app(title="Discord", require_eve_focus=False)
        app.handle_text("warp"); self.assertEqual(s.sent, ["d"])

    def test_wake_word_not_required_in_always(self):
        app, s, *_ = make_app("always")
        app.handle_text("computer warp")
        self.assertEqual(s.sent, [])


class TestPushToTalk(unittest.TestCase):
    def test_audio_gated_by_key(self):
        app, s, _, ptt, _ = make_app("push_to_talk")
        self.assertFalse(app.gate.audio_enabled())
        ptt["down"] = True
        self.assertTrue(app.gate.audio_enabled())
        app.handle_text("orbit")
        self.assertEqual(s.sent, ["w"])

    def test_feeder_drops_audio_when_released_and_finalises(self):
        class FakeRec:
            def __init__(self): self.buf = []; self.resets = 0
            def AcceptWaveform(self, d): self.buf.append(d); return False
            def Result(self): return '{"text": ""}'
            def FinalResult(self): return json.dumps({"text": "".join(self.buf)})
            def Reset(self): self.buf = []; self.resets += 1
        app, s, _, ptt, _ = make_app("push_to_talk")
        rec = FakeRec(); f = ev.AudioFeeder(rec, app)
        f.feed("dock")
        self.assertEqual(rec.buf, [])
        ptt["down"] = True; f.feed("war"); f.feed("p")
        self.assertEqual(s.sent, [])
        ptt["down"] = False; f.feed("x")
        self.assertEqual(s.sent, ["d"]); self.assertEqual(rec.resets, 1)
        f.feed("y"); self.assertEqual(rec.resets, 1)

    def test_vk_codes_no_hook(self):
        self.assertEqual(ev.vk_code("scroll lock"), 0x91)
        self.assertEqual(ev.vk_code("F13"), 0x7C)
        self.assertEqual(ev.vk_code("x"), ord("X"))
        self.assertEqual(ev.vk_code("right alt"), 0xA5)
        self.assertIsNone(ev.vk_code("banana"))

    def test_always_mode_audio_always_on(self):
        app, *_ = make_app("always")
        self.assertTrue(app.gate.audio_enabled())


class TestWakeWord(unittest.TestCase):
    def test_single_utterance(self):
        app, s, *_ = make_app("wake_word")
        app.handle_text("computer warp")
        self.assertEqual(s.sent, ["d"])

    def test_without_wake_ignored(self):
        app, s, *_ = make_app("wake_word")
        app.handle_text("warp")
        self.assertEqual(s.sent, [])

    def test_window_then_command(self):
        app, s, clock, _, logs = make_app("wake_word", wake_window_seconds=4)
        app.handle_text("computer")
        self.assertIn("[wake]", logs[-1])
        clock.t += 3.0; app.handle_text("approach")
        self.assertEqual(s.sent, ["q"])
        clock.t += 1.0; app.handle_text("orbit")
        self.assertEqual(s.sent, ["q"])

    def test_window_expires(self):
        app, s, clock, *_ = make_app("wake_word", wake_window_seconds=4)
        app.handle_text("computer"); clock.t += 4.5
        app.handle_text("warp")
        self.assertEqual(s.sent, [])

    def test_unk_does_not_consume_window(self):
        app, s, clock, *_ = make_app("wake_word")
        app.handle_text("computer"); app.handle_text("[unk]"); clock.t += 1
        app.handle_text("align")
        self.assertEqual(s.sent, ["a"])

    def test_custom_wake_word(self):
        app, s, *_ = make_app("wake_word", wake_word="Jarvis")
        app.handle_text("jarvis approach"); app.handle_text("computer approach")
        self.assertEqual(s.sent, ["q"])

    def test_wake_select(self):
        app, s, *_ = make_app("wake_word")
        app.handle_text("computer select")
        self.assertEqual(app.mouse.clicks, [("left", None)])


# =========================================================================== mouse
class TestMouse(unittest.TestCase):
    def test_default_mouse_phrases(self):
        app, s, clock, _, _ = make_app()
        for p in ["select", "click", "right click", "middle click"]:
            clock.t += 5
            self.assertIsNotNone(app.handle_text(p), p)
        self.assertEqual(app.mouse.clicks, [("left", None), ("left", None), ("right", None), ("middle", None)])
        self.assertEqual(s.sent, [])

    def test_mouse_respects_focus_and_cooldown(self):
        app, *_ = make_app(title="Chrome")
        app.handle_text("select")
        self.assertEqual(app.mouse.clicks, [])
        app, _, clock, _, _ = make_app()
        app.handle_text("select"); clock.t += 0.2; app.handle_text("select")
        self.assertEqual(len(app.mouse.clicks), 1)

    def test_saved_positions_need_opt_in(self):
        spec = {"g": {"commands": {"ok": "q", "at": {"mouse": "left", "mouse_at": [10, 20]},
                                   "box": {"mouse": "right", "mouse_box": [0, 0, 10, 20]}}}}
        cfg = ev.load_config_dict({"groups": spec})
        self.assertEqual(list(cfg.commands), ["ok"])
        self.assertIn("allow_mouse_at is false", " ".join(cfg.warnings))
        app, *_ = make_app(data={"settings": {"allow_mouse_at": True}, "groups": spec})
        app.handle_text("at"); app.clock.t += 5; app.handle_text("box")
        self.assertEqual(app.mouse.clicks, [("left", (10, 20)), ("right", (5, 10))])

    def test_bad_targets(self):
        cfg = ev.load_config_dict({"settings": {"allow_mouse_at": True}, "groups": {"g": {"commands": {
            "a": {"mouse": "left", "mouse_at": [1, 2, 3]}, "b": {"mouse": "left", "mouse_at": "1,2"},
            "c": {"mouse": "left", "mouse_at": [1, 2], "mouse_box": [1, 1, 2, 2]},
            "d": {"mouse": "left", "mouse_spot": "nope"}, "e": {"mouse": "fourth"}, "ok": "q"}}}})
        self.assertEqual(list(cfg.commands), ["ok"])

    def test_no_mouse_library(self):
        app, _, _, _, logs = make_app()
        app.mouse = None
        self.assertIsNone(app.handle_text("select"))
        self.assertIn("no mouse library", logs[-1])

    def test_pydirectinput_single_click_call(self):
        calls = []
        class P:
            click = staticmethod(lambda **kw: calls.append(kw))
            moveTo = staticmethod(lambda *a: calls.append(("move", a)))
            mouseDown = mouseUp = keyDown = keyUp = staticmethod(lambda *a, **k: calls.append(("other",)))
        m = ev.PyDirectInputMouse.__new__(ev.PyDirectInputMouse); m.p = P
        m.click("left")
        self.assertEqual(calls, [{"button": "left"}])
        calls.clear(); m.click("right", (100, 200))
        self.assertEqual(calls, [{"x": 100, "y": 200, "button": "right"}])   # ONE call, no move-back

    def test_pynput_single_click(self):
        calls = []
        class MS:
            position = (0, 0)
            def click(self, btn, count): calls.append((btn, count))
        class B: left, right, middle = "L", "R", "M"
        m = ev.PynputMouse.__new__(ev.PynputMouse); m.ms = MS(); m.Button = B
        m.click("middle"); m.click("left", (5, 6))
        self.assertEqual(calls, [("M", 1), ("L", 1)])
        self.assertEqual(m.ms.position, (5, 6))   # not restored

    def test_where_helper(self):
        out, sleeps = [], []
        ev.run_where(pos_fn=lambda: (640, 360), title_fn=lambda: "EVE - Spence",
                     sleep=sleeps.append, count=3, log=out.append)
        self.assertEqual(len([l for l in out if "mouse_at: [640, 360]" in l]), 3)


class TestSelectNext(unittest.TestCase):
    def test_shipped_unset(self):
        raw = load_raw()
        self.assertIsNone(raw["spots"]["next_waypoint"])
        self.assertEqual(raw["groups"]["windows_travel"]["commands"]["select next"],
                         {"mouse": "left", "mouse_spot": "next_waypoint"})
        for gone in ("warp to next", "jump to next"):
            self.assertNotIn(gone, json.dumps(raw))

    def test_select_next_then_warp_are_separate(self):
        app, s, clock, _, _ = make_app(data=calibrated_data())
        app.handle_text("select next")
        self.assertEqual(app.rec.calls, [("mouse", "left", (200, 210))])
        clock.t += 1; app.handle_text("warp")
        self.assertEqual(app.rec.calls[-1], ("key", (), "d"))
        self.assertEqual(len(app.rec.calls), 2)

    def test_distinct_phrases(self):
        app, s, clock, _, _ = make_app(data=calibrated_data())
        g = ev.build_grammar(app.cfg)
        self.assertIn("select", g); self.assertIn("select next", g)
        app.handle_text("select")
        clock.t += 5; app.handle_text("select next")
        self.assertEqual(app.mouse.clicks, [("left", None), ("left", (200, 210))])
        self.assertIsNone(app.handle_text("select [unk]"))


# =========================================================================== calibration
class TestCalibration(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "commands.json")
        shutil.copyfile(CFG_PATH, self.path)
        self.addCleanup(shutil.rmtree, self.dir)

    def raw(self):
        return load_raw(self.path)

    def quiet(self, **kw):
        return dict(log=lambda *_: None, sleep=lambda *_: None, **kw)

    def test_box_math(self):
        self.assertEqual(ev.box_center([100, 200, 300, 220]), (200, 210))
        self.assertEqual(ev.box_center([300, 220, 100, 200]), (200, 210))
        self.assertEqual(ev.normalize_box(300, 50, 100, 10), [100, 10, 300, 50])
        self.assertEqual(ev.box_center([-1920, 0, -1820, 40]), (-1870, 20))
        self.assertEqual(ev.resolve_point([5, 6], "x"), (5, 6))

    def test_list_targets(self):
        t = ev.find_calibration_targets(self.raw())
        self.assertEqual(t[0]["spot"], "next_waypoint")
        self.assertEqual(t[0]["users"], ["select next"])
        self.assertFalse(any(x.get("phrase") == "click undock" for x in t))

    def test_cli_pick_and_save_preserves_json(self):
        before = self.raw()
        answers = iter(["1", ""])
        n = ev.calibrate_cli(self.path, None, overlay=lambda t: [100, 200, 300, 220],
                             input_fn=lambda *_: next(answers), **self.quiet())
        self.assertEqual(n, 1)
        after = self.raw()
        before["spots"]["next_waypoint"] = [100, 200, 300, 220]
        before["settings"]["allow_mouse_at"] = True
        self.assertEqual(after, before); self.assertEqual(list(after), list(before))
        self.assertIn("_note", after["groups"]["navigation"])
        with open(self.path, encoding="utf-8") as f:
            self.assertIn('\n  "settings": {\n', f.read())
        self.assertTrue(os.path.exists(self.path + ".bak"))
        cfg = ev.load_config(self.path)
        self.assertEqual(cfg.warnings, [])
        self.assertEqual(cfg.commands["select next"].target, (200, 210))

    def test_cli_by_name(self):
        n = ev.calibrate_cli(self.path, "Select next", overlay=lambda t: [0, 0, 10, 10],
                             input_fn=lambda *_: "", **self.quiet())
        self.assertEqual(n, 1)
        self.assertEqual(self.raw()["spots"]["next_waypoint"], [0, 0, 10, 10])

    def test_cli_command_own_box(self):
        data = self.raw(); data["groups"]["saved_positions_fragile"]["enabled"] = True
        data["groups"]["saved_positions_fragile"]["commands"]["click undock"] = {"mouse": "left", "mouse_at": [1, 1]}
        ev.save_config_data(self.path, data)
        n = ev.calibrate_cli(self.path, "click undock", overlay=lambda t: [10, 10, 30, 30],
                             input_fn=lambda *_: "", **self.quiet())
        self.assertEqual(n, 1)
        spec = self.raw()["groups"]["saved_positions_fragile"]["commands"]["click undock"]
        self.assertEqual(spec, {"mouse": "left", "mouse_box": [10, 10, 30, 30]})
        self.assertEqual(ev.load_config(self.path).commands["click undock"].target, (20, 20))

    def test_cancel_and_unknown(self):
        before = self.raw()
        self.assertEqual(ev.calibrate_cli(self.path, "select next", overlay=lambda t: None,
                                          input_fn=lambda *_: "", **self.quiet()), 0)
        self.assertEqual(ev.calibrate_cli(self.path, "warp", overlay=lambda t: [1, 1, 2, 2],
                                          input_fn=lambda *_: "", **self.quiet()), 0)
        answers = iter(["9"])
        self.assertEqual(ev.calibrate_cli(self.path, None, overlay=lambda t: [1, 1, 2, 2],
                                          input_fn=lambda *_: next(answers), **self.quiet()), 0)
        self.assertEqual(self.raw(), before)

    def test_voice_calibrate_then_reload(self):
        app, s, clock, _, _ = make_app()
        def fake_cal(spot):
            ev.calibrate_targets(self.path, ev._targets_for_name(self.raw(), spot),
                                 overlay=lambda t: [100, 200, 300, 220], log=lambda *_: None)
            return True
        app.calibrate_fn = fake_cal
        self.assertNotIn("select next", app.cfg.commands)
        app.handle_text("set way point spot")
        self.assertTrue(app.config_changed); self.assertEqual(app.rec.calls, [])
        app.apply_config(ev.load_config(self.path))
        clock.t += 5; app.handle_text("select next")
        self.assertEqual(app.rec.calls, [("mouse", "left", (200, 210))])
        self.assertIn("select next", ev.build_grammar(app.cfg))


# =========================================================================== background mode
class StrictApi:
    """Mock of Win32Api. Records every call; any OTHER attribute access fails the test."""
    _ALLOWED = {"enum_windows", "map_virtual_key", "screen_to_client", "post_message"}

    def __init__(self, windows):
        object.__setattr__(self, "windows", windows)
        object.__setattr__(self, "calls", [])
        object.__setattr__(self, "posts", [])

    def __getattribute__(self, name):
        if name.startswith("_") or name in ("windows", "calls", "posts") or name in StrictApi._ALLOWED:
            return object.__getattribute__(self, name)
        raise AssertionError(f"background mode touched forbidden API '{name}'")

    def enum_windows(self):
        self.calls.append("enum_windows"); return list(self.windows)

    def map_virtual_key(self, vk):
        self.calls.append("map_virtual_key"); return {0x79: 0x44, 0x44: 0x20, 0x2E: 0x53}.get(vk, 0x10)

    def screen_to_client(self, hwnd, x, y):
        self.calls.append("screen_to_client"); return x - 100, y - 50

    def post_message(self, hwnd, msg, w, l):
        self.calls.append("post_message"); self.posts.append((hwnd, msg, w, l)); return True


ALT = "EVE - Bravo Alt"


def bg_app(windows, data=None, mode="always", **settings):
    data = data or calibrated_data()
    nav = data["groups"]["navigation"]["commands"]
    nav["warp"], nav["align"] = "d", "a"     # plain taps (hold-until-click is skipped in background mode)
    data["target"] = {"mode": "background_window", "window_title": ALT}
    app, s, clock, ptt, logs = make_app(mode, data=data, title="Discord", **settings)   # foreground isn't EVE
    api = StrictApi(windows)
    app.background = ev.BackgroundTarget(ALT, api, logs.append)
    return app, api, clock, logs


class TestBackground(unittest.TestCase):
    ONE = [(111, "EVE - Main Pilot"), (222, ALT), (333, "Discord")]

    def test_off_by_default(self):
        self.assertEqual(load_raw()["target"]["mode"], "foreground")
        self.assertEqual(ev.load_config(SHIPPED_PATH).target["mode"], "foreground")

    def test_requires_title_and_valid_mode(self):
        g = {"g": {"commands": {"a": "a"}}}
        with self.assertRaises(ev.ConfigError):
            ev.load_config_dict({"target": {"mode": "background_window"}, "groups": g})
        with self.assertRaises(ev.ConfigError):
            ev.load_config_dict({"target": {"mode": "broadcast", "window_title": "x"}, "groups": g})

    def test_every_command_is_one_post_pair_to_one_window(self):
        app, api, clock, _ = bg_app(self.ONE)
        fired = 0
        for phrase, cmd in app.cfg.commands.items():
            if cmd.kind in ("calibrate", "teach", "release"):
                continue
            clock.t += 10
            n = len(api.posts)
            with mock.patch.object(ev.time, "sleep"):
                self.assertIs(app.handle_text(phrase), cmd, phrase)
            new = api.posts[n:]
            self.assertEqual(len(new), 2, phrase)                    # one down + one up
            self.assertEqual({p[0] for p in new}, {222}, phrase)     # exactly ONE window
            fired += 1
        self.assertGreaterEqual(fired, 15)
        self.assertEqual(app.rec.calls, [])                          # never SendInput / pydirectinput
        self.assertEqual(set(api.calls) - StrictApi._ALLOWED, set())

    def test_key_messages_and_lparam(self):
        app, api, *_ = bg_app(self.ONE)
        app.handle_text("test background")   # F10
        (h1, m1, w1, l1), (h2, m2, w2, l2) = api.posts
        self.assertEqual((m1, m2, w1, w2), (ev.WM_KEYDOWN, ev.WM_KEYUP, 0x79, 0x79))
        self.assertEqual(l1, 1 | (0x44 << 16))                       # repeat 1, scan 0x44, not extended
        self.assertEqual(l2, 1 | (0x44 << 16) | (1 << 30) | (1 << 31))
        self.assertEqual(app.cfg.commands["warp"].key, "d")
        self.assertEqual(ev.key_lparams(0x53, True)[0], 1 | (0x53 << 16) | (1 << 24))   # e.g. Delete

    def test_saved_spot_click_is_client_coords(self):
        app, api, *_ = bg_app(self.ONE)
        app.handle_text("select next")                               # spot centre (200, 210)
        self.assertEqual(api.posts, [(222, 0x0201, 0x0001, (160 << 16) | 100), (222, 0x0202, 0, (160 << 16) | 100)])

    def test_zero_or_many_matches_send_nothing(self):
        for wins in ([(111, "EVE - Main Pilot")], [(222, ALT), (223, ALT.upper())], []):
            app, api, _, logs = bg_app(wins)
            self.assertIsNone(app.handle_text("warp"))
            self.assertEqual(api.posts, [], wins)
            self.assertEqual(app.rec.calls, [])
            self.assertIn("nothing sent", logs[-1])

    def test_title_match_is_exact_case_insensitive(self):
        app, api, *_ = bg_app([(5, "eve - bravo alt "), (6, "EVE - Bravo Alt 2"), (7, "EVE - Bravo")])
        app.handle_text("warp")
        self.assertEqual({p[0] for p in api.posts}, {5})

    def test_chords_and_cursor_clicks_skipped(self):
        app, *_ = bg_app(self.ONE)
        for gone in ("mid one", "stop ship", "launch drones", "select", "click"):
            self.assertNotIn(gone, app.cfg.commands)
        self.assertIn("warp", app.cfg.commands); self.assertIn("select next", app.cfg.commands)
        self.assertTrue(any("background_window mode" in w and "skipped" in w for w in app.cfg.warnings))
        bg = ev.BackgroundTarget(ALT, StrictApi(self.ONE), lambda *_: None)
        with self.assertRaises(ev.ConfigError):
            bg.press(222, ["ctrl"], "f1", 0)
        with self.assertRaises(ev.ConfigError):
            bg.click(222, "left", None)

    def test_cooldown_and_wake_word_still_apply(self):
        app, api, clock, _ = bg_app(self.ONE)
        app.handle_text("warp"); clock.t += 0.1; app.handle_text("warp")
        self.assertEqual(len(api.posts), 2)
        app, api, clock, _ = bg_app(self.ONE, mode="wake_word", wake_word="bravo")
        app.handle_text("warp"); self.assertEqual(api.posts, [])
        app.handle_text("bravo warp"); self.assertEqual(len(api.posts), 2)

    def test_two_instances_wake_words_route_to_own_client(self):
        wins = [(111, "EVE - Alpha Main"), (222, ALT)]
        a, a_api, _, _ = bg_app(wins, mode="wake_word", wake_word="alpha")
        a.background = ev.BackgroundTarget("EVE - Alpha Main", a_api, lambda *_: None)
        b, b_api, _, _ = bg_app(wins, mode="wake_word", wake_word="bravo")
        for utterance in ("alpha warp", "bravo align"):   # both instances hear everything
            a.handle_text(utterance); b.handle_text(utterance)
        self.assertEqual([(p[0], p[2]) for p in a_api.posts], [(111, 0x44)] * 2)   # D -> alpha only
        self.assertEqual([(p[0], p[2]) for p in b_api.posts], [(222, 0x41)] * 2)   # A -> bravo only

    def test_blocked_sender_and_list_windows(self):
        with self.assertRaises(RuntimeError):
            ev.BlockedSender().press([], "d", 0)
        with self.assertRaises(RuntimeError):
            ev.BlockedSender().click("left")
        out = []
        n = ev.run_list_windows(StrictApi(self.ONE), log=out.append)
        self.assertEqual(n, 2)
        self.assertTrue(any("'EVE - Bravo Alt'" in l for l in out))
        self.assertFalse(any("Discord" in l for l in out))

    def test_test_background_sends_one_f10(self):
        data = load_raw(); data["target"] = {"mode": "foreground", "window_title": ALT}
        cfg = ev.load_config_dict(data)
        api = StrictApi(self.ONE); sleeps = []
        with mock.patch.object(ev.time, "sleep"):
            ok = ev.run_test_background(cfg, api, log=lambda *_: None, sleep=sleeps.append)
        self.assertTrue(ok); self.assertEqual(sleeps, [1, 1, 1])
        self.assertEqual([(p[0], p[1], p[2]) for p in api.posts], [(222, ev.WM_KEYDOWN, 0x79), (222, ev.WM_KEYUP, 0x79)])
        api2 = StrictApi([(1, ALT), (2, ALT)])
        self.assertFalse(ev.run_test_background(cfg, api2, log=lambda *_: None, sleep=lambda *_: None))
        self.assertEqual(api2.posts, [])

    def test_main_dry_run_background_uses_no_foreground_sender(self):
        import io, contextlib, builtins
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d)
        path = os.path.join(d, "c.json")
        data = load_raw(); data["target"] = {"mode": "background_window", "window_title": ALT}
        ev.save_config_data(path, data)
        inputs = iter(["test background", ""]); orig = builtins.input; builtins.input = lambda *_: next(inputs)
        out = io.StringIO()
        try:
            with mock.patch.object(ev, "make_sender", side_effect=AssertionError("SendInput sender built")), \
                 mock.patch.object(ev, "make_mouse_sender", side_effect=AssertionError("mouse sender built")), \
                 contextlib.redirect_stdout(out):
                rc = ev.main(["--text", "--dry-run", "--config", path])
        finally:
            builtins.input = orig
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("EXPERIMENTAL", out.getvalue())
        self.assertIn("not found - nothing sent", out.getvalue())   # no windows on Linux -> nothing sent


# =========================================================================== senders / CLI
class TestSendersOffline(unittest.TestCase):
    def test_pydirectinput_chord_is_one_press(self):
        calls = []
        class P:
            PAUSE = 0.1; FAILSAFE = True
            keyDown = staticmethod(lambda k: calls.append(("down", k)))
            keyUp = staticmethod(lambda k: calls.append(("up", k)))
        snd = ev.PyDirectInputSender.__new__(ev.PyDirectInputSender); snd.p = P
        with mock.patch.object(ev.time, "sleep") as slp:
            snd.press(["ctrl", "shift"], "f1", 0.05)
        self.assertEqual(calls, [("down", "ctrl"), ("down", "shift"), ("down", "f1"),
                                 ("up", "f1"), ("up", "shift"), ("up", "ctrl")])
        slp.assert_called_once_with(0.05)   # only the hold time of this single press

    def test_text_mode_main_runs(self):
        import io, contextlib, builtins
        inputs = iter(["warp", "select", ""])
        orig = builtins.input; builtins.input = lambda *_: next(inputs)
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                rc = ev.main(["--text", "--dry-run", "--config", CFG_PATH])
        finally:
            builtins.input = orig
        self.assertEqual(rc, 0)
        self.assertIn("would hold down d", out.getvalue())
        self.assertIn("would let go of d", out.getvalue())     # released on exit at the latest
        self.assertIn("would left click at cursor", out.getvalue())


if __name__ == "__main__":
    unittest.main()
