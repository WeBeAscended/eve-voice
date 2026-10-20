"""GUI model-layer tests (no display needed; tkinter not required).
Run: python -m unittest discover -s tests"""
import json, os, queue, re, shutil, sys, tempfile, unittest
from collections import OrderedDict
from unittest import mock
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import eve_voice as ev
import eve_voice_gui as gui

CFG_PATH = os.path.join(ROOT, "commands.default.json")
GUI_SRC = os.path.join(ROOT, "eve_voice_gui.py")


def load_json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def drain(q):
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


class TempConfig(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "commands.json")
        shutil.copy(CFG_PATH, self.path)
        self.m = gui.ConfigModel(self.path)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def raw(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f, object_pairs_hook=OrderedDict)


class TestConfigModel(TempConfig):
    def test_loads_shipped_config_and_is_clean(self):
        self.assertFalse(self.m.dirty)
        self.assertIn("navigation", self.m.groups())
        self.assertIn(("navigation", "stop ship", "ctrl+space"), self.m.commands())
        self.assertIn(("navigation", "warp", {"key": "d", "hold_until": "click"}), self.m.commands())
        self.assertNotIn("_note", [p for _, p, _ in self.m.commands()])

    def test_add_edit_delete_duplicate_save_roundtrip(self):
        m = self.m
        m.add_command("navigation", "Warp Home", "h")
        self.assertTrue(m.dirty)
        self.assertEqual(m.get("navigation", "warp home"), "h")
        with self.assertRaises(gui.ValidationError):
            m.add_command("drones", "warp home", "j")              # duplicate phrase
        m.edit_command("navigation", "warp home", "navigation", "go home", "alt+h")
        keys = list(m.data["groups"]["navigation"]["commands"])
        self.assertNotIn("warp home", keys)
        self.assertEqual(m.get("navigation", "go home"), "alt+h")
        m.edit_command("navigation", "go home", "custom_new", "go home", "h")   # move group
        self.assertEqual(m.find("go home"), ("custom_new", "go home"))
        new = m.duplicate_command("navigation", "warp")
        self.assertEqual(new, "warp copy")
        self.assertEqual(m.duplicate_command("navigation", "warp"), "warp copy two")
        order = list(m.data["groups"]["navigation"]["commands"])
        self.assertEqual(order[order.index("warp") + 1], "warp copy two")
        m.delete_command("navigation", "warp copy")
        m.delete_command("navigation", "warp copy two")
        m.save()
        self.assertFalse(m.dirty)
        self.assertTrue(os.path.exists(self.path + ".bak"))
        raw = self.raw()
        self.assertEqual(raw["groups"]["custom_new"]["commands"]["go home"], "h")
        self.assertNotIn("warp copy", raw["groups"]["navigation"]["commands"])
        # every _note / _README key is still there
        orig = load_json(CFG_PATH)
        notes = lambda d: sorted(re.findall(r'"(_\w+)"', json.dumps(d)))
        self.assertEqual(notes(orig), notes(raw))
        self.assertEqual(ev.load_config(self.path).commands["go home"].key, "h")

    def test_edit_in_place_keeps_order(self):
        before = list(self.m.data["groups"]["navigation"]["commands"])
        self.m.edit_command("navigation", "orbit", "navigation", "circle", "w")
        after = list(self.m.data["groups"]["navigation"]["commands"])
        self.assertEqual(after.index("circle"), before.index("orbit"))

    def test_revert(self):
        self.m.delete_command("navigation", "warp")
        self.m.set_setting("mode", "wake_word")
        self.m.revert()
        self.assertFalse(self.m.dirty)
        self.assertEqual(self.m.get("navigation", "warp"), {"key": "d", "hold_until": "click"})

    def test_group_toggle_and_status(self):
        self.m.set_group_enabled("drones", False)
        cfg = self.m.check()
        self.assertEqual(gui.command_status(self.m, cfg, "drones", "launch drones",
                                            self.m.get("drones", "launch drones")), "Off (group off)")
        self.assertEqual(gui.command_status(self.m, cfg, "navigation", "dock", ""), "Not set")
        self.assertEqual(gui.command_status(self.m, cfg, "windows_travel", "select next",
                                            self.m.get("windows_travel", "select next")), "Needs spot")
        self.assertEqual(gui.command_status(self.m, cfg, "navigation", "warp", "d"), "On")
        self.m.set_group_enabled("drones", True)
        self.assertTrue(self.m.group_enabled("drones"))

    def test_spots(self):
        self.m.set_spot("next_waypoint", [10, 20, 30, 40])
        self.assertTrue(self.m.setting("allow_mouse_at"))
        self.assertIn("select next", self.m.spot_users("next_waypoint"))
        cfg = self.m.check()
        self.assertEqual(cfg.commands["select next"].mouse_at, (20, 30))
        self.m.clear_spot("next_waypoint")
        self.assertNotIn("select next", self.m.check().commands)
        # a new mouse_spot is created (unset) when a command refers to it
        self.m.add_command("custom", "click station", gui.build_spec("mouse", button="left", where="spot",
                                                                      spot="undock_button"))
        self.assertIn("undock_button", self.m.spots())

    def test_settings_and_target(self):
        self.m.set_setting("mode", "push_to_talk")
        with self.assertRaises(gui.ValidationError):
            self.m.set_setting("step_delay_seconds", 1)
        self.m.set_target(mode="background_window", window_title="EVE - Alpha")
        with self.assertRaises(gui.ValidationError):
            self.m.set_target(mode="broadcast")
        self.m.save()
        cfg = ev.load_config(self.path)
        self.assertEqual(cfg.mode, "push_to_talk")
        self.assertEqual(cfg.target["window_title"], "EVE - Alpha")

    def test_save_refuses_invalid(self):
        self.m.data["settings"]["mode"] = "telepathy"
        with self.assertRaises(gui.ValidationError):
            self.m.save()
        self.assertEqual(self.raw()["settings"]["mode"], "always")


class TestValidation(unittest.TestCase):
    def v(self, phrase, spec, spots=None, wake=""):
        return gui.validate_command(phrase, spec, spots or {}, wake)

    def test_ok_forms(self):
        self.assertEqual(self.v("Warp", "d")[0], "warp")
        self.v("high one", "alt+f1")
        self.v("select", {"mouse": "left"})
        self.v("select next", {"mouse": "right", "mouse_spot": "next_waypoint"}, {"next_waypoint": None})
        self.v("set it", {"calibrate": "next_waypoint"})

    def test_rejects_non_1to1(self):
        for spec in ({"steps": [{"key": "a"}, {"key": "b"}]}, {"steps": [{"key": "a"}]},
                     {"key": "s", "mouse": "left"}, {"mouse": "double"},
                     {"mouse": "left", "modifiers": ["ctrl"]}, {"mouse": "left", "modifiers": "shift"}):
            with self.assertRaises(gui.ValidationError, msg=spec):
                self.v("bad", spec)

    def test_rejects_empty_and_misc(self):
        for phrase, spec in (("", "d"), ("x", ""), ("x", {"mouse": "left", "mouse_spot": ""}),
                             ("x", {"calibrate": ""}), ("x", "notakey")):
            with self.assertRaises(gui.ValidationError, msg=(phrase, spec)):
                self.v(phrase, spec)
        with self.assertRaises(gui.ValidationError):
            self.v("computer", "d", wake="computer")

    def test_number_hint(self):
        self.assertTrue(self.v("high 1", "f1")[1])

    def test_build_spec_is_one_action(self):
        self.assertEqual(gui.build_spec("key", key="alt+f1"), "alt+f1")
        self.assertEqual(dict(gui.build_spec("mouse", button="middle")), {"mouse": "middle"})
        self.assertEqual(dict(gui.build_spec("mouse", button="left", where="spot", spot="s")),
                         {"mouse": "left", "mouse_spot": "s"})
        with self.assertRaises(gui.ValidationError):
            gui.build_spec("macro")

    def test_describe(self):
        self.assertEqual(gui.describe_spec("d"), "Key: D")
        self.assertEqual(gui.describe_spec("alt+f1"), "Keys: Alt+F1")
        self.assertEqual(gui.describe_spec(""), "Not set - pick a key")
        self.assertEqual(gui.describe_spec({"mouse": "left"}), "Left click at cursor")
        self.assertEqual(gui.describe_spec({"mouse": "left", "mouse_spot": "w"}), "Left click at saved spot: w")
        self.assertIn("no game input", gui.describe_spec({"calibrate": "w"}))
        self.assertEqual(gui.describe_action_short("d"), "pressed D")
        self.assertEqual(gui.describe_action_short("left click"), "left click at cursor")


class TestKeyCapture(unittest.TestCase):
    def test_chords(self):
        c = gui.ChordCapture()
        self.assertIsNone(c.press("Alt_L"))
        self.assertEqual(c.press("F1"), "alt+f1")
        self.assertIsNone(c.release("Alt_L"))
        self.assertIsNone(c.press("Shift_L")); self.assertIsNone(c.press("Control_L"))
        self.assertEqual(c.press("f"), "ctrl+shift+f")
        c.release("Shift_L"); c.release("Control_L")
        self.assertEqual(c.press("d"), "d")
        self.assertEqual(c.press("space"), "space")
        self.assertIsNone(c.press("Super_L"))

    def test_lone_modifier(self):
        c = gui.ChordCapture()
        c.press("Control_L")
        self.assertEqual(c.release("Control_L"), "ctrl")

    def test_captured_keys_are_valid_engine_keys(self):
        for ks in ("d", "F10", "space", "Return", "KP_3", "Prior", "minus"):
            k = gui.keysym_to_key(ks)
            gui.validate_command("x", k, {})   # raises if the engine can't press it

    def test_ptt(self):
        self.assertEqual(gui.ptt_name_from_keysym("Scroll_Lock"), "scroll lock")
        self.assertEqual(gui.ptt_name_from_keysym("F13"), "f13")
        self.assertEqual(gui.ptt_name_from_keysym("Alt_R"), "right alt")
        self.assertIsNone(gui.ptt_name_from_keysym("Super_L"))
        for ks in ("Scroll_Lock", "Caps_Lock", "Pause", "Control_R", "KP_0", "x"):
            self.assertIsNotNone(ev.vk_code(gui.ptt_name_from_keysym(ks)), ks)


def fake_runner(*phrases):
    def run(cfg, app, stop_event, tick, reload_fn, model_dir):
        tick(app, 0.5)
        for p in phrases:
            app.handle_text(p)
    return run


class TestEngine(unittest.TestCase):
    def data(self):
        return load_json(CFG_PATH)

    def run_engine(self, runner, data=None, test_mode=True):
        q = queue.Queue()
        e = gui.Engine(lambda: data or self.data(), q, test_mode, runner=runner)
        with mock.patch("builtins.print"):
            e.start(); e.thread.join(5)
        self.assertFalse(e.running)
        return drain(q)

    def test_events_flow_through_queue(self):
        msgs = self.run_engine(fake_runner("warp", "nonsense", "select"))
        kinds = [m[0] for m in msgs]
        self.assertEqual(kinds[-1], "stopped")
        self.assertIn("started", kinds)
        self.assertIn(("tick", "listening", 0.5), msgs)
        sent = [m for m in msgs if m[0] == "event" and m[1] == "sent"]
        self.assertEqual([(m[2], m[3]) for m in sent], [("warp", "hold d until you click (max 10s)"), ("select", "left click")])

    def test_status_wake_and_ptt(self):
        d = self.data(); d["settings"]["mode"] = "wake_word"
        msgs = self.run_engine(fake_runner(), d)
        self.assertIn(("tick", "wake_wait", 0.5), msgs)
        d["settings"]["mode"] = "push_to_talk"
        with mock.patch.object(ev, "make_ptt_checker", return_value=lambda: False):
            msgs = self.run_engine(fake_runner(), d)
        self.assertIn(("tick", "ptt_wait", 0.5), msgs)

    def test_bad_config_reports_error(self):
        msgs = self.run_engine(fake_runner("warp"), {"settings": {"mode": "x"}, "groups": {}})
        self.assertEqual(msgs[0][0], "error")

    def test_crash_is_friendly(self):
        def boom(*a):
            raise RuntimeError("PortAudio library not found")
        msgs = self.run_engine(boom)
        err = [m for m in msgs if m[0] == "error"][0][1]
        self.assertIn("microphone", err)
        self.assertNotIn("Traceback", err)

    def test_demo_runner_stops(self):
        import threading
        q = queue.Queue()
        e = gui.Engine(self.data, q, True, runner=gui.demo_runner)
        with mock.patch("builtins.print"):
            e.start()
            threading.Event().wait(0.8)
            e.stop()
        msgs = drain(q)
        self.assertEqual(msgs[-1], ("stopped",))
        self.assertTrue(any(m[0] == "event" and m[1] == "sent" and m[2] == "warp" for m in msgs))


class TestFireTest(unittest.TestCase):
    def data(self):
        return load_json(CFG_PATH)

    def test_one_action_after_countdown(self):
        q = queue.Queue(); sleeps = []
        rec = []

        class S(ev.KeySender):
            name = "rec"
            def press(self, mods, key, hold): rec.append((tuple(mods), key))
        d = self.data(); d["settings"]["require_eve_focus"] = False
        with mock.patch.object(ev, "make_sender", return_value=S()), \
                mock.patch.object(ev, "make_mouse_sender", side_effect=RuntimeError("none")):
            gui.fire_test_command(d, "stop ship", False, q, countdown=3, sleep=sleeps.append)
        self.assertEqual(rec, [(("ctrl",), "space")])
        self.assertEqual(sleeps, [1, 1, 1])   # countdown BEFORE the one input, nothing after
        self.assertEqual([m for m in drain(q) if m[0] == "event"], [("event", "sent", "stop ship", "ctrl+space")])

    def test_test_mode_presses_nothing(self):
        q = queue.Queue()
        with mock.patch.object(ev, "make_sender", side_effect=AssertionError("real sender used")), \
                mock.patch("builtins.print"):
            gui.fire_test_command(self.data(), "stop ship", True, q,
                                  countdown=1, sleep=lambda s: None)
        self.assertIn(("event", "sent", "stop ship", "ctrl+space"), drain(q))

    def test_inactive_and_setup(self):
        q = queue.Queue()
        gui.fire_test_command(self.data(), "dock", True, q, countdown=0)
        gui.fire_test_command(self.data(), "set way point spot", True, q, countdown=0)
        logs = [m[1] for m in drain(q)]
        self.assertTrue(any("isn't active" in l for l in logs))
        self.assertTrue(any("setup command" in l for l in logs))


class TestGuiCompliance(unittest.TestCase):
    def test_gui_source_has_no_forbidden_apis(self):
        with open(GUI_SRC, encoding="utf-8") as f:
            src = f.read()
        for bad in ["ReadProcessMemory", "WriteProcessMemory", "OpenProcess", "pymem", "psutil",
                    "win32process", "SetWindowsHookEx", "CreateRemoteThread", "Gamelogs", "Chatlogs",
                    "frida", "LoadLibrary", "pytesseract", "keyboard.hook", "is_pressed", "SendInput",
                    "SetForegroundWindow", "SetCursorPos", "SendMessage", "HWND_BROADCAST", "pynput",
                    "pydirectinput", "windll", "ctypes", "ImageGrab", '["steps"]', '.get("steps")']:
            hits = [l for l in src.splitlines() if bad.lower() in l.lower() and not l.strip().startswith("#")
                    and "pydirectinput\" in low" not in l]
            self.assertEqual(hits, [], f"found '{bad}': {hits}")
        # the GUI never sends input itself: no sender calls outside the engine
        self.assertNotIn(".press(", src.replace("self.cap.press(", "").replace("c.press(", ""))
        self.assertNotIn(".click(", src)
        self.assertEqual(src.count("app.fire("), 1)   # the per-command Test button, one command once
        # the only sleep is the Test button's countdown default (before its single input)
        self.assertEqual(src.count("time.sleep"), 1)
        self.assertIn("countdown: int = 3, sleep=time.sleep", src)
        # only global key access is none: key capture binds to the dialog's own widget
        self.assertNotIn("bind_all", src)

    def test_gui_imports_without_display(self):
        self.assertTrue(hasattr(gui, "ConfigModel"))


@unittest.skipIf(gui.tk is None or not os.environ.get("DISPLAY"), "needs tkinter and a display")
class TestWidgets(TempConfig):
    def setUp(self):
        super().setUp()
        self.state = os.path.join(self.dir, "state.json")
        self.w = gui.MainWindow(self.path, demo=True, state_path=self.state, show_welcome=False)
        self.w.update()

    def tearDown(self):
        try:
            self.w.shutdown()
        except Exception:
            pass
        super().tearDown()

    def test_keycapture_box(self):
        got = []
        b = gui.KeyCaptureBox(self.w, "", on_change=got.append)
        b.feed("Alt_L"); b.feed("F1"); b.feed("Alt_L", release=True)
        self.assertEqual(b.value, "alt+f1")
        self.assertIn("Alt+F1", b.lbl.cget("text"))
        b.clear(); self.assertEqual(b.value, "")

    def test_dialog_validation_blocks_combo(self):
        d = gui.CommandDialog(self.w, "Add")
        d.phrase.set("bad one"); d.kind.set("key")
        d.ok()
        self.assertIsNone(d.result)
        self.assertIn("key", d.err.cget("text").lower())
        d.keybox.feed("g"); d.group.set("navigation")
        d.ok()
        self.assertEqual(d.result[:2], ("navigation", "bad one"))
        self.assertEqual(self.w.model.get("navigation", "bad one"), "g")

    def test_settings_restart_and_close_saves_geometry(self):
        self.w.mode_var.set("wake_word"); self.w._mode_changed()
        self.assertEqual(self.w.model.setting("mode"), "wake_word")
        self.assertTrue(self.w.model.dirty)
        self.w.model.revert(); self.w.shutdown()
        self.assertIn("geometry", load_json(self.state))


if __name__ == "__main__":
    unittest.main()
