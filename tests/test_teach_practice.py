"""Teach (one input per phrase, captured on OUR overlay) and Practice. No mic / EVE needed."""
import copy, os, shutil, sys, tempfile, unittest
from collections import OrderedDict
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import eve_voice as ev
import eve_voice_gui as gui
from test_eve_voice import make_app, load_raw, CFG_PATH

ROOT = os.path.dirname(HERE)


class TempModel(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "commands.json")
        shutil.copy(CFG_PATH, self.path)
        self.model = gui.ConfigModel(self.path)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def inputs_for(data, phrase):
    """Load the config like the engine does and count EVERY input one phrase sends."""
    app, *_ = make_app(data=copy.deepcopy(data), require_eve_focus=False)
    app.handle_text(phrase, 0.95)
    return app.rec.calls


class TestTeachPure(TempModel):
    def test_phrase_checks(self):
        errs, warns = gui.check_teach_phrase("warp", self.model, None)
        self.assertTrue(any("already used" in e for e in errs))
        errs, _ = gui.check_teach_phrase("next gate", self.model, None)      # an alternate
        self.assertTrue(any("already used" in e for e in errs), errs)
        errs, warns = gui.check_teach_phrase("warps", self.model, None)
        self.assertEqual(errs, [])
        self.assertTrue(any("sounds a lot like" in w.lower() for w in warns), warns)
        errs, _ = gui.check_teach_phrase("zorblat now", self.model, {"now", "shield"})
        self.assertTrue(any("doesn't know 'zorblat'" in e for e in errs))
        self.assertEqual(gui.check_teach_phrase("shield boost", self.model, None), ([], []))
        self.assertTrue(gui.check_teach_phrase("[unk]", self.model, None)[0])
        self.assertTrue(gui.check_teach_phrase("high 1", self.model, None)[0])

    def test_key_and_chord(self):
        self.assertEqual(gui.teach_spec({"key": "f5"}, "spot", "x", {})[0], "f5")
        spec, spot, box = gui.teach_spec({"key": "alt+f1"}, "cursor", "x", {})
        self.assertEqual((spec, spot, box), ("alt+f1", None, None))
        self.assertIn("press Alt+F1", gui.teach_card("shield boost", {"key": "alt+f1"}, "spot"))

    def test_saved_spot_click(self):
        spec, spot, box = gui.teach_spec({"mouse": "left", "x": 500, "y": 300}, "spot", "Undock Now",
                                         {"taught_undock_now": None})
        self.assertEqual(spot, "taught_undock_now_2")
        self.assertEqual(box, [494, 294, 506, 306])
        self.assertEqual(spec, OrderedDict([("mouse", "left"), ("mouse_spot", spot)]))
        self.assertIn("saved spot (500, 300)", gui.teach_card("undock now", {"mouse": "left", "x": 500, "y": 300},
                                                                 "spot"))

    def test_cursor_click(self):
        spec, spot, box = gui.teach_spec({"mouse": "right", "x": 1, "y": 2}, "cursor", "menu", {})
        self.assertEqual(spec, OrderedDict([("mouse", "right")]))
        self.assertIsNone(spot)
        self.assertIn("wherever your mouse cursor is", gui.teach_card("menu", {"mouse": "right", "x": 1, "y": 2},
                                                                      "cursor"))

    def test_taught_commands_send_exactly_one_input(self):
        cases = [({"key": "f5"}, "spot", [("key", (), "f5")]),
                 ({"key": "ctrl+shift+f9"}, "spot", [("key", ("ctrl", "shift"), "f9")]),
                 ({"mouse": "left", "x": 500, "y": 300}, "spot", [("mouse", "left", (500, 300))]),
                 ({"mouse": "middle", "x": 5, "y": 6}, "cursor", [("mouse", "middle", None)])]
        for captured, mode, want in cases:
            m = gui.ConfigModel(self.path)
            spec, spot, box = gui.teach_spec(captured, mode, "shield boost", m.spots())
            if spot:
                m.set_spot(spot, box)
            m.add_command("taught", "shield boost", spec)
            calls = inputs_for(m.data, "shield boost")
            self.assertEqual(len(calls), 1, (captured, calls))
            self.assertEqual(calls[0][0], want[0][0])
            if want[0][0] == "key":
                self.assertEqual(calls[0], want[0])
            else:
                self.assertEqual(calls[0][1], want[0][1])
                if want[0][2]:
                    self.assertEqual(tuple(calls[0][2]), want[0][2])

    def test_teach_phrase_sends_nothing(self):
        app, *_ = make_app(require_eve_focus=False)
        rec = app.rec
        opened = []
        app.teach_fn = lambda: opened.append(1)
        app.handle_text("teach command", 0.95)
        self.assertEqual(opened, [1])
        self.assertEqual(rec.calls, [])


class TestInputOverlayLogic(unittest.TestCase):
    """Overlay capture uses only its own tk events; this checks the decoding."""

    def run_events(self, *evs):
        got = []
        o = gui.InputOverlay(None, got.append, show=False)
        for e in evs:
            if e[0] == "k":
                o.key(e[1])
            elif e[0] == "r":
                o.key(e[1], release=True)
            else:
                o.pointer(*e[1:])
        return got

    def test_key(self):
        self.assertEqual(self.run_events(("k", "F1")), [{"key": "f1"}])

    def test_chord(self):
        self.assertEqual(self.run_events(("k", "Control_L"), ("k", "Shift_L"), ("k", "a")),
                         [{"key": "ctrl+shift+a"}])

    def test_lone_modifier(self):
        self.assertEqual(self.run_events(("k", "Alt_L"), ("r", "Alt_L")), [{"key": "alt"}])

    def test_clicks(self):
        self.assertEqual(self.run_events(("m", 1, 10, 20)), [{"mouse": "left", "x": 10, "y": 20}])
        self.assertEqual(self.run_events(("m", 3, 1, 2)), [{"mouse": "right", "x": 1, "y": 2}])
        self.assertEqual(self.run_events(("m", 2, 1, 2)), [{"mouse": "middle", "x": 1, "y": 2}])
        self.assertEqual(self.run_events(("m", 4, 1, 2)), [])            # wheel ignored

    def test_cancel_and_only_one_result(self):
        self.assertEqual(self.run_events(("k", "Escape"), ("k", "a")), [None])
        self.assertEqual(self.run_events(("k", "a"), ("k", "b"), ("m", 1, 0, 0)), [{"key": "a"}])


class TestPractice(unittest.TestCase):
    def test_confusion_and_unreliable(self):
        ps = gui.PracticeStats(0.55, {"next gate": "select next gate"})
        for heard, conf in (("lock", 0.9), ("dock", 0.8), ("dock", 0.85)):
            ps.record("lock", heard, conf)
        self.assertEqual(ps.confusions(), [("lock", "dock", 2, 3)])
        self.assertEqual(ps.unreliable(), [("lock", 1 / 3)])
        ps.record("select next gate", "next gate", 0.9)          # alternate counts as right
        ps.record("select next gate", "select next gate", 0.4)   # right but unsure
        self.assertEqual(ps.summary("select next gate")["ok"], 1)
        self.assertEqual(ps.verdict("warp", "[unk]", 0.3), "missed")
        self.assertEqual(ps.verdict("warp", "warp", None), "ok")

    def test_no_flags_when_reliable(self):
        ps = gui.PracticeStats(0.55)
        for _ in range(4):
            ps.record("warp", "warp", 0.95)
        ps.record("warp", "align", 0.7)
        self.assertEqual(ps.unreliable(), [])
        self.assertEqual(ps.confusions(), [])

    def test_suggestions_avoid_confused_and_taken(self):
        s = gui.suggest_alternates("lock", None, ["target"], avoid=["dock"])
        self.assertNotIn("target", s)
        s = gui.suggest_alternates("high one", None, [], avoid=[])
        self.assertIn("gun one", s)
        self.assertIn("high slot one", s)

    def test_add_as_alternate_is_same_single_action(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "c.json"); shutil.copy(CFG_PATH, p)
            m = gui.ConfigModel(p)
            g, ph = m.find("warp")
            m.add_alternate(g, ph, "engage")
            self.assertEqual(gui.spec_alternates(m.get(g, ph)), ["engage"])
            self.assertEqual(inputs_for(m.data, "engage"), inputs_for(m.data, "warp"))
            self.assertEqual(len(inputs_for(m.data, "engage")), 1)
            with self.assertRaises(gui.ValidationError):
                m.add_alternate(g, ph, "align")      # already its own command
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_with_options(self):
        spec = gui.with_options("d", ["engage"], 0.1, 2)
        self.assertEqual(spec, OrderedDict([("key", "d"), ("also", ["engage"]), ("hold", 0.1), ("cooldown", 2)]))
        self.assertEqual(gui.with_options("d", [], None, None), "d")
        self.assertEqual(gui.spec_options(spec), {"also": ["engage"], "hold": 0.1, "cooldown": 2})


class TestScreenReadCompliance(unittest.TestCase):
    def src(self, name):
        with open(os.path.join(ROOT, name), encoding="utf-8") as f:
            return f.read()

    def test_imagegrab_only_in_gate_finder_with_bbox(self):
        for name in ("eve_voice.py", "eve_voice_gui.py"):
            self.assertNotIn("ImageGrab", self.src(name))
        gf = self.src("gate_finder.py")
        calls = [l.strip() for l in gf.splitlines() if "ImageGrab.grab(" in l]
        self.assertEqual(len(calls), 2, calls)
        self.assertTrue(all("bbox=" in c for c in calls), calls)
        for bad in ("ReadProcessMemory", "OpenProcess", "pytesseract", "while True", "threading", "save("):
            self.assertNotIn(bad, gf)

    def test_engine_imports_gate_finder_only_when_opted_in(self):
        src = self.src("eve_voice.py")
        lines = [l for l in src.splitlines() if "import gate_finder" in l or "from gate_finder" in l]
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith("        "))       # inside build_app, not at top level
        body = src[src.index("def build_app("):]
        self.assertIn("gate_finder", body[:body.index("\ndef ", 10)])


@unittest.skipIf(gui.tk is None or not os.environ.get("DISPLAY"), "needs tkinter and a display")
class TestTeachDialog(TempModel):
    def setUp(self):
        super().setUp()
        self.w = gui.MainWindow(self.path, demo=True, state_path=os.path.join(self.dir, "s.json"),
                                show_welcome=False)
        self.overlays = []
        self.w.input_overlay_fn = lambda master, cb: self.overlays.append(cb)
        self.w.update()

    def tearDown(self):
        try:
            self.w.shutdown()
        except Exception:
            pass
        super().tearDown()

    def pump(self, ms=500):
        import time
        end = time.monotonic() + ms / 1000
        while time.monotonic() < end:
            self.w.update(); time.sleep(0.02)

    def test_teach_key_flow_saves_and_backs_up(self):
        d = self.w.open_teach()
        cap = self.w.engine.pending_capture or self.w.engine.app.capture
        self.assertTrue(cap.free and cap.once)          # full vocabulary, listen once, matching paused
        d.heard("shield boost", 0.81)
        self.assertEqual(d.phrase.get(), "shield boost")
        d.to_b(); self.assertEqual(d.stage, "B")
        d.show_overlay(); self.pump(600)
        self.assertEqual(len(self.overlays), 1)
        self.overlays[0]({"key": "alt+f1"})
        self.assertEqual(d.stage, "C")
        self.assertIn('"shield boost"', d.card.cget("text"))
        self.assertTrue(d.save())
        m2 = gui.ConfigModel(self.path)
        self.assertEqual(m2.get("taught", "shield boost"), "alt+f1")
        self.assertTrue(os.path.exists(self.path + ".bak"))
        self.assertIsNone(self.w._teach)

    def test_teach_click_spot_flow(self):
        d = self.w.open_teach()
        d.phrase.set("undock now"); d.to_b()
        d.on_input({"mouse": "left", "x": 300, "y": 200})
        self.assertEqual(d.stage, "B2")
        d.click_mode.set("spot"); d.stage_c()
        self.assertTrue(d.save())
        m2 = gui.ConfigModel(self.path)
        self.assertEqual(m2.get("taught", "undock now")["mouse_spot"], "taught_undock_now")
        self.assertEqual(m2.spots()["taught_undock_now"], [294, 194, 306, 206])

    def test_duplicate_blocks_ok(self):
        d = self.w.open_teach()
        d.heard("warp", 0.9)
        d.to_b()
        self.assertEqual(d.stage, "A")
        self.assertIn("already used", d.check.cget("text"))

    def test_cancel_changes_nothing(self):
        before = copy.deepcopy(self.w.model.data)
        d = self.w.open_teach()
        d.phrase.set("shield boost"); d.to_b(); d.on_input(None)   # overlay Esc -> back to step 2
        self.assertEqual(d.stage, "B")
        d.cancel()
        self.assertEqual(self.w.model.data, before)
        self.pump(300)
        self.assertFalse(self.w.engine and self.w.engine.running)


if __name__ == "__main__":
    unittest.main()
