"""Recognition refinements + opt-in 'select next gate'. No mic / EVE needed."""
import json, os, sys, unittest
from unittest import mock
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import eve_voice as ev
from test_eve_voice import make_app, load_raw, fully_bound_data, MockSender, MockMouse, Recorder


def gate_data(enabled=True, box=(100, 300, 400, 600)):
    data = fully_bound_data()
    data["settings"]["allow_screen_read"] = enabled
    t = data["color_targets"]["next_gate"]
    t["box"], t["sampled"] = list(box), True
    return data


def events(app):
    got = []
    app.on_event = lambda k, p, d: got.append((k, p, d))
    return got


class TestConfidence(unittest.TestCase):
    def test_parse_result_uses_lowest_word(self):
        js = json.dumps({"text": "keep at range", "result": [
            {"word": "keep", "conf": 0.99}, {"word": "at", "conf": 0.62}, {"word": "range", "conf": 0.9}]})
        self.assertEqual(ev.parse_result(js), ("keep at range", 0.62))
        self.assertEqual(ev.parse_result('{"text": "warp"}'), ("warp", None))
        self.assertEqual(ev.parse_result("garbage"), ("", None))

    def test_below_threshold_sends_nothing_and_logs(self):
        app, s, clock, _, logs = make_app(min_confidence=0.7)
        got = events(app)
        self.assertIsNone(app.handle_text("warp", 0.62))
        self.assertEqual(app.rec.calls, [])
        self.assertTrue(any("heard 'warp' (62%) - not sure, skipped" in l for l in logs), logs)
        self.assertEqual(got[0][0], "unsure")
        clock.t += 10
        self.assertIsNotNone(app.handle_text("warp", 0.9))
        self.assertEqual(app.rec.calls, [("key", (), "d")])

    def test_threshold_setting(self):
        self.assertEqual(ev.DEFAULT_SETTINGS["min_confidence"], 0.7)
        app, *_ = make_app(min_confidence=0.5)
        self.assertIsNotNone(app.handle_text("warp", 0.62))
        with self.assertRaises(ev.ConfigError):
            make_app(min_confidence=1.5)

    def test_low_confidence_wake_word_doesnt_open_window(self):
        app, *_ = make_app("wake_word")
        app.handle_text("computer", 0.4)   # default threshold 0.55
        self.assertFalse(app.gate.wake_active())

    def test_unmatched_logged_with_closest(self):
        app, _, _, _, logs = make_app()
        got = events(app)
        app.handle_text("warp keep at", 0.95)
        app.handle_text("[unk]", 0.5)
        app.handle_text("orbit [unk]", 0.8)
        kinds = [g[0] for g in got]
        self.assertEqual(kinds, ["unmatched"] * 3)
        self.assertRegex(got[0][2], r"closest command: '(warp|keep at range)'")
        self.assertIn("95%", got[0][2])
        self.assertIn("not a command", got[1][2])
        self.assertIn("partly", got[2][2])
        self.assertEqual(app.rec.calls, [])

    def test_log_unmatched_off(self):
        app, _, _, _, logs = make_app(log_unmatched=False)
        app.handle_text("warp keep at", 0.9)
        self.assertEqual([l for l in logs if "heard" in l], [])


class FakeRec:
    """Kaldi recogniser stand-in: buffers chunks; never endpoints by itself."""
    def __init__(self, text="warp", conf=0.95):
        self.buf, self.text, self.conf, self.finals, self.resets = [], text, conf, 0, 0
    def AcceptWaveform(self, d): self.buf.append(d); return False
    def Result(self): return '{"text": ""}'
    def PartialResult(self): return json.dumps({"partial": self.text[:2] if self.buf else ""})
    def FinalResult(self):
        self.finals += 1
        loud = any(any(b) for b in self.buf)
        self.buf = []
        if not loud:
            return '{"text": ""}'
        return json.dumps({"text": self.text, "result": [{"word": w, "conf": self.conf} for w in self.text.split()]})
    def Reset(self): self.buf = []; self.resets += 1


LOUD = (b"\x00\x40" * 800)     # peak 0.5
QUIET = (b"\x10\x00" * 800)    # peak ~0.0005
HUM = (b"\x00\x08" * 800)      # peak ~0.06 (background hum)


class TestFeeder(unittest.TestCase):
    def test_fast_endpoint_fires_after_silence(self):
        app, s, *_ = make_app(endpoint_silence_seconds=0.35)
        rec = FakeRec()
        parts = []
        f = ev.AudioFeeder(rec, app, chunk_seconds=0.05, on_partial=parts.append)
        for _ in range(4):
            f.feed(LOUD)
        self.assertEqual(s.sent, [])                     # acts on FINAL only
        self.assertIn("wa", parts)                        # partial shown as 'hearing...'
        for _ in range(6):
            f.feed(QUIET)
        self.assertEqual(s.sent, [])                      # 0.30 s of quiet: not yet
        f.feed(QUIET)
        self.assertEqual(s.sent, ["d"])                   # 0.35 s -> final -> ONE press
        self.assertEqual(rec.finals, 1)

    def test_noise_gate_feeds_silence(self):
        app, s, *_ = make_app(noise_gate=0.1)
        rec = FakeRec()
        f = ev.AudioFeeder(rec, app)
        f.feed(HUM)
        self.assertEqual(rec.buf, [bytes(len(HUM))])      # hum below the gate became silence
        for _ in range(10):
            f.feed(HUM)
        self.assertEqual(s.sent, [])                      # hum never counts as speech
        f.feed(LOUD)
        self.assertEqual(rec.buf[-1], LOUD)

    def test_ptt_still_gates_audio(self):
        app, s, _, ptt, _ = make_app("push_to_talk")
        rec = FakeRec()
        f = ev.AudioFeeder(rec, app)
        f.feed(LOUD)
        self.assertEqual(rec.buf, [])


class TestCapture(unittest.TestCase):
    def test_practice_capture_never_sends(self):
        app, s, *_ = make_app()
        heard = []
        app.capture = ev.Capture(lambda t, c: heard.append((t, c)), once=False)
        for p in ("warp", "align", "select"):
            app.hear(p, 0.9)
        self.assertEqual(heard, [("warp", 0.9), ("align", 0.9), ("select", 0.9)])
        self.assertEqual(app.rec.calls, [])
        app.capture = None
        app.hear("warp", 0.9)
        self.assertEqual(app.rec.calls, [("key", (), "d")])

    def test_teach_capture_uses_free_recogniser_once(self):
        app, s, *_ = make_app()
        main, free = FakeRec("warp"), FakeRec("warp to the gate", 0.8)
        made = []
        f = ev.AudioFeeder(main, app, free_rec_fn=lambda: made.append(1) or free)
        heard = []
        app.capture = ev.Capture(lambda t, c: heard.append((t, c)), free=True, once=True)
        for _ in range(3):
            f.feed(LOUD)
        for _ in range(8):
            f.feed(QUIET)
        self.assertEqual(heard, [("warp to the gate", 0.8)])
        self.assertIsNone(app.capture)
        self.assertNotIn(LOUD, main.buf)                   # command recogniser didn't get the speech
        self.assertEqual(s.sent, [])
        for _ in range(3):
            f.feed(LOUD)
        for _ in range(8):
            f.feed(QUIET)
        self.assertEqual(s.sent, ["d"])                    # back to normal commands

    def test_capture_timeout(self):
        clock = [0.0]
        app, *_ = make_app()
        heard = []
        app.capture = ev.Capture(lambda t, c: heard.append(t), timeout=5, clock=lambda: clock[0])
        f = ev.AudioFeeder(FakeRec(), app)
        clock[0] = 6
        f.feed(QUIET)
        self.assertEqual(heard, [None])
        self.assertIsNone(app.capture)


class TestAlternates(unittest.TestCase):
    def data(self):
        d = load_raw()
        d["groups"]["navigation"]["commands"]["warp"] = {"key": "d", "also": ["warp to", "Engage"]}
        return d

    def test_alternate_fires_same_single_input(self):
        app, s, clock, _, _ = make_app(data=self.data())
        self.assertEqual(app.cfg.aliases, {"warp to": "warp", "engage": "warp", "let go": "release"})
        for p in ("warp", "warp to", "engage"):
            clock.t += 10
            n = len(app.rec.calls)
            self.assertIs(app.handle_text(p, 0.9), app.cfg.commands["warp"])
            self.assertEqual(app.rec.calls[n:], [("key", (), "d")])

    def test_alternates_share_cooldown(self):
        app, s, *_ = make_app(data=self.data())
        app.handle_text("warp"); app.handle_text("engage")
        self.assertEqual(s.sent, ["d"])

    def test_alternates_in_grammar(self):
        cfg = ev.load_config_dict(self.data())
        g = ev.build_grammar(cfg)
        self.assertIn("warp to", g); self.assertIn("engage", g); self.assertEqual(g.count("warp"), 1)
        d = self.data(); d["settings"]["mode"] = "wake_word"
        g = ev.build_grammar(ev.load_config_dict(d))
        self.assertIn("computer engage", g)

    def test_conflicting_alternate_ignored(self):
        d = self.data()
        d["groups"]["navigation"]["commands"]["align"] = {"key": "a", "also": ["orbit", "engage"]}
        cfg = ev.load_config_dict(d)
        self.assertIn(cfg.aliases.get("engage"), ("warp", "align"))     # first one wins, one action
        self.assertNotIn("orbit", cfg.aliases)
        self.assertTrue(any("'orbit' is already its own command" in w for w in cfg.warnings))
        self.assertTrue(any("'engage' is already an alternate of" in w for w in cfg.warnings))

    def test_every_alternate_is_one_action(self):
        d = fully_bound_data()
        for g in d["groups"].values():
            for k, v in list(g.get("commands", {}).items()):
                if isinstance(v, str) and v:
                    g["commands"][k] = {"key": v, "also": [f"{k} please"]}
        app, s, clock, _, _ = make_app(data=d)
        self.assertGreater(len(app.cfg.aliases), 40)
        for alt, main in app.cfg.aliases.items():
            clock.t += 10
            n = len(app.rec.calls)
            with mock.patch.object(ev.time, "sleep") as slp:
                app.handle_text(alt, 0.95)
            want = 0 if app.cfg.commands[main].kind == "release" else 1   # 'let go' = a keyUp only
            self.assertEqual(len(app.rec.calls) - n, want, alt)
            self.assertEqual(slp.call_count, 0)

    def test_options_never_add_actions(self):
        for bad in ({"key": "d", "also": ["x"], "mouse": "left"}, {"also": ["x"]},
                    {"mouse": "left", "hold": 0.1}, {"key": "d", "hold": 2}, {"key": "d", "also": 5}):
            with self.assertRaises(ev.ConfigError, msg=bad):
                ev.Command("p", bad, "g")


class TestPerCommandTiming(unittest.TestCase):
    def test_hold_and_cooldown_override(self):
        d = load_raw()
        d["groups"]["navigation"]["commands"]["warp"] = {"key": "d", "hold": 0.12, "cooldown": 3}
        holds = []

        class S(ev.KeySender):
            def press(self, m, k, hold): holds.append((k, hold))
        app, _, clock, _, _ = make_app(data=d)
        app.sender = S()
        app.handle_text("warp"); app.handle_text("stop ship")
        self.assertEqual(holds, [("d", 0.12), ("space", 0.05)])
        clock.t += 1
        app.handle_text("warp")                     # within its own 3 s cooldown
        clock.t += 2.5
        app.handle_text("warp")
        self.assertEqual([h for h in holds if h[0] == "d"], [("d", 0.12), ("d", 0.12)])


class TestScreenTarget(unittest.TestCase):
    def test_off_by_default_and_inactive(self):
        cfg = ev.load_config(os.path.join(os.path.dirname(HERE), "commands.default.json"))
        self.assertFalse(cfg.allow_screen_read)
        self.assertNotIn("select next gate", cfg.commands)
        self.assertFalse(ev.DEFAULT_SETTINGS["allow_screen_read"])

    def test_needs_calibration_before_enabling(self):
        d = fully_bound_data(); d["settings"]["allow_screen_read"] = True
        cfg = ev.load_config_dict(d)
        self.assertNotIn("select next gate", cfg.commands)
        self.assertTrue(any("Overview area" in w for w in cfg.warnings))
        d["color_targets"]["next_gate"]["box"] = [0, 0, 100, 100]
        cfg = ev.load_config_dict(d)
        self.assertTrue(any("Pick the yellow" in w for w in cfg.warnings))

    def test_found_is_one_click_at_found_point(self):
        app, s, clock, _, logs = make_app(data=gate_data())
        seen = []
        app.locator = lambda t: (seen.append(t["box"]) or ((150, 320), "yellow row at y=10-20"))
        for p in ("select next gate", "next gate", "select gate"):
            clock.t += 10
            n = len(app.rec.calls)
            self.assertIsNotNone(app.handle_text(p, 0.95))
            self.assertEqual(app.rec.calls[n:], [("mouse", "left", (150, 320))])
        self.assertEqual(seen, [[100, 300, 400, 600]] * 3)   # looked at the box once per command

    def test_not_found_or_ambiguous_is_zero_inputs(self):
        for msg in ("no yellow gate found", "found 2 yellow rows far apart, not sure, skipped"):
            app, s, _, _, logs = make_app(data=gate_data())
            got = events(app)
            app.locator = lambda t, m=msg: (None, m)
            self.assertIsNone(app.handle_text("select next gate", 0.95))
            self.assertEqual(app.rec.calls, [])
            self.assertTrue(any(msg in g[2] for g in got if g[0] == "skipped"))

    def test_no_screen_look_without_focus(self):
        app, *_ = make_app(data=gate_data(), title="Discord")
        app.locator = mock.Mock()
        app.handle_text("select next gate")
        app.locator.assert_not_called()
        self.assertEqual(app.rec.calls, [])

    def test_build_app_wires_locator_only_when_opted_in(self):
        with mock.patch("builtins.print"):
            cfg = ev.load_config_dict(fully_bound_data())
            self.assertIsNone(ev.build_app(cfg, dry_run=True, use_ptt=False, log=lambda m: None).locator)
            cfg = ev.load_config_dict(gate_data())
            import gate_finder
            self.assertIs(ev.build_app(cfg, dry_run=True, use_ptt=False, log=lambda m: None).locator,
                          gate_finder.locate)

    def test_background_mode_skips_screen_clicks(self):
        d = gate_data(); d["target"] = {"mode": "background_window", "window_title": "EVE - Alt"}
        cfg = ev.load_config_dict(d)
        self.assertNotIn("select next gate", cfg.commands)
        self.assertTrue(any("screen-found click" in w for w in cfg.warnings))


class TestTeachPhrase(unittest.TestCase):
    def test_teach_phrase_sends_nothing(self):
        app, *_ = make_app()
        called = []
        app.teach_fn = lambda: called.append(1)
        app.handle_text("teach command", 0.9)
        self.assertEqual(called, [1])
        self.assertEqual(app.rec.calls, [])


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------- the user's real recording
import wave
DATA = os.path.join(HERE, "data")


def _model_dir():
    for d in (os.environ.get("EVE_VOICE_MODEL", ""), os.path.join(os.path.dirname(HERE), "model"),
              "/tmp/vosk-model-small-en-us-0.15"):
        if d and os.path.isdir(os.path.join(d, "am")):
            return d
    return None


class TestUserRecording(unittest.TestCase):
    """Regression on tests/data/user_warp_x4.wav ('warp' x4, 16 kHz mono)."""

    def setUp(self):
        with open(os.path.join(DATA, "user_warp_x4.expected.json")) as f:
            self.exp = json.load(f)
        wf = wave.open(os.path.join(DATA, "user_warp_x4.wav"))
        self.rate, self.raw = wf.getframerate(), wf.readframes(wf.getnframes())

    def chunks(self):
        n = self.rate // 20 * 2
        for i in range(0, len(self.raw), n):
            yield (i + n) / 2 / self.rate, self.raw[i:i + n]

    def test_default_noise_gate_separates_speech_from_background(self):
        gate = ev.DEFAULT_SETTINGS["noise_gate"]
        for u in self.exp["utterances"]:
            peak = max(ev.chunk_level(c) for t, c in self.chunks() if u["start"] <= t <= u["end"] + 0.05)
            self.assertGreater(peak, gate * 4, u)
        quiet = [ev.chunk_level(c) for t, c in self.chunks()
                 if all(not (u["start"] - 0.4 <= t <= u["end"] + 0.35) for u in self.exp["utterances"])]
        self.assertLess(max(quiet), gate)

    @unittest.skipIf(_model_dir() is None, "needs vosk + a model (set EVE_VOICE_MODEL)")
    def test_full_pipeline_four_warps_fast(self):
        try:
            import vosk
        except ImportError:
            self.skipTest("vosk not installed")
        vosk.SetLogLevel(-1)
        d = load_raw(); d["settings"]["require_eve_focus"] = False
        app, s, clock, _, logs = make_app(data=d)
        times = []
        app.sender.press = lambda m, k, h: times.append((clock.t, k))
        rec = vosk.KaldiRecognizer(vosk.Model(_model_dir()), self.rate, json.dumps(ev.build_grammar(app.cfg)))
        rec.SetWords(True)
        f = ev.AudioFeeder(rec, app, chunk_seconds=0.05)
        for t, c in self.chunks():
            clock.t = t
            f.feed(c)
        f._final(rec, rec.FinalResult())
        self.assertEqual([k for _, k in times], self.exp["expected_presses"])
        for (t, _), u in zip(times, self.exp["utterances"]):
            self.assertLessEqual(t - u["end"], self.exp["max_latency_after_word_end_s"], (t, u))
        self.assertEqual([l for l in logs if "not sure" in l], [])
