"""1.0.1: 'warp gate' at any pace (one breath = split in order, or a pause = two utterances),
holds up to 10 s. warp_gate_*.wav = your 'warp' (from user_warp_x4.wav) + a synthetic 'gate'."""
import json, os, sys, unittest, wave
from unittest import mock
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
import eve_voice as ev
from test_eve_voice import make_app
from test_recognition import gate_data, _model_dir

GATE = ("mouse", "left", (150, 320))


def gate_app():
    app, s, clock, _, logs = make_app(data=gate_data())
    app.locator = lambda t: ((150, 320), "yellow row")
    return app, s, clock


class TestHoldRobust(unittest.TestCase):
    def test_only_fresh_click_or_release_or_key_ends_hold(self):
        app, s, clock = gate_app()
        btn = {"down": True}                       # a click already in progress when the hold starts
        app.hold.left_button = lambda: btn["down"]
        app.handle_text("warp", 0.95)
        for down in (True, False):                 # old click finishing: ignored
            btn["down"] = down; clock.t += 0.01; app.hold.tick()
        f = ev.AudioFeeder(mock.Mock(), app)
        f._partial_fire("scan", {}); f._partial_fire("scan", {})        # partial mishear: no effect
        f._partial_fire("release", {}); f._partial_fire("release", {})
        self.assertIsNotNone(app.hold.active)
        btn["down"] = True; clock.t += 0.01; app.hold.tick()             # fresh press...
        self.assertIsNotNone(app.hold.active)
        btn["down"] = False; clock.t += 0.01; app.hold.tick()            # ...and release
        self.assertIsNotNone(app.hold.active)                            # still down for EVE's frame
        clock.t += 0.2; app.hold.tick()
        self.assertIsNone(app.hold.active)
        self.assertEqual(getattr(s, "ups", []), ["d"])


class TestWarpGate(unittest.TestCase):
    def check(self, app, s):
        # D down, ONE click while D is still down, then D let go (hold-until-click). Nothing else.
        self.assertEqual(app.rec.calls, [("key", (), "d"), GATE])
        self.assertEqual(getattr(s, "ups", []), [])     # NOT let go between/at the click
        app.clock.t += 0.2; app.hold.tick()             # the hold thread lets go after CLICK_SETTLE
        self.assertEqual(s.ups, ["d"])
        self.assertIsNone(app.hold.active)

    def test_one_breath_and_with_pause(self):
        with mock.patch.object(ev.time, "sleep", side_effect=AssertionError("no sleeps between inputs")):
            app, s, clock = gate_app()
            self.assertEqual(app.cfg.segment("warp gate"), ["warp", "gate"])
            app.handle_text("warp gate", 0.95)
            self.check(app, s)
            app, s, clock = gate_app()
            app.handle_text("warp", 0.95); clock.t += 1.3; app.handle_text("gate", 0.95)
            self.check(app, s)

    def test_timed_hold_survives_click_and_partial_not_double_fired(self):
        d = gate_data(); d["groups"]["navigation"]["commands"]["warp"] = {"key": "d", "hold_seconds": 10}
        app, s, clock, _, _ = make_app(data=d)
        app.locator = lambda t: ((150, 320), "yellow row")
        self.assertEqual(app.cfg.commands["warp"].hold_seconds, 10.0)
        f = ev.AudioFeeder(mock.Mock(), app)
        f._partial_fire("warp gate", {}); f._partial_fire("warp gate", {})   # stable partial -> fires
        f._final(f.rec, json.dumps({"text": "warp gate"}))                     # final: nothing again
        self.assertEqual(app.rec.calls, [("key", (), "d"), GATE])
        self.assertIsNotNone(app.hold.active)                 # timed hold keeps going after the click
        clock.t += 10.01; app.hold.tick()
        self.assertEqual(s.ups, ["d"])
        with self.assertRaises(ev.ConfigError):
            ev.Command("x", {"key": "d", "hold_seconds": 10.5}, "g")

    @unittest.skipIf(_model_dir() is None, "needs vosk + a model")
    def test_recordings_fast_and_paused(self):
        try:
            import vosk
        except ImportError:
            self.skipTest("vosk not installed")
        vosk.SetLogLevel(-1)
        model = vosk.Model(_model_dir())
        for name in ("warp_gate_fast.wav", "warp_gate_paused.wav"):
            app, s, clock = gate_app()
            app.cfg.settings["require_eve_focus"] = False
            wf = wave.open(os.path.join(HERE, "data", name))
            rate, raw = wf.getframerate(), wf.readframes(wf.getnframes())
            rec = vosk.KaldiRecognizer(model, rate, json.dumps(ev.build_grammar(app.cfg)))
            rec.SetWords(True); rec.SetPartialWords(True)
            f = ev.AudioFeeder(rec, app, chunk_seconds=0.05)
            n = rate // 20 * 2
            for i in range(0, len(raw), n):
                clock.t = 1000 + i / 2 / rate
                f.feed(raw[i:i + n])
            f._final(rec, rec.FinalResult())
            self.assertEqual(app.rec.calls, [("key", (), "d"), GATE], name)


if __name__ == "__main__":
    unittest.main()
