"""Held keys: still ONE input (one keyDown ... one keyUp of the same key)."""
import copy, os, sys, time, unittest
from unittest import mock
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import eve_voice as ev
from test_eve_voice import make_app, load_raw, bg_app, ALT, fully_bound_data


def app_with(spec_by_phrase=None, title="EVE - Spence", **kw):
    d = load_raw()
    nav = d["groups"]["navigation"]["commands"]
    for p, spec in (spec_by_phrase or {}).items():
        nav[p] = spec
    app, s, clock, _, logs = make_app(data=d, title=title, **kw)
    s.downs, s.ups = [], []
    return app, s, clock, logs


class TestParse(unittest.TestCase):
    def cmd(self, spec):
        return ev.Command("x", spec, "g")

    def test_modes_and_caps(self):
        c = self.cmd({"key": "d", "hold_seconds": 3})
        self.assertEqual((c.hold_mode, c.hold_seconds, c.key_spec), ("timed", 3.0, "hold d for 3s"))
        self.assertEqual(self.cmd({"key": "q", "hold_seconds": 10}).hold_seconds, 10.0)
        c = self.cmd({"key": "ctrl", "hold_until": "click"})
        self.assertEqual((c.hold_mode, c.hold_seconds), ("click", 10.0))
        c = self.cmd({"key": "q", "hold_until": "release"})
        self.assertEqual((c.hold_mode, c.hold_seconds), ("release", 30.0))
        self.assertEqual(self.cmd({"key": "q", "hold_until": "click", "hold_max": 12}).hold_seconds, 12.0)
        self.assertEqual(self.cmd("d").hold_mode, "tap")
        for bad in ({"key": "d", "hold_seconds": 10.5}, {"key": "d", "hold_seconds": 0.05},
                    {"key": "d", "hold_until": "click", "hold_max": 31},
                    {"key": "d", "hold_until": "release", "hold_max": 0.1},
                    {"key": "d", "hold_seconds": 2, "hold_until": "click"},
                    {"key": "d", "hold": 0.1, "hold_seconds": 2},
                    {"key": "d", "hold_until": "forever"}, {"key": "d", "hold_max": 3},
                    {"mouse": "left", "hold_seconds": 2}, {"mouse": "left", "hold_until": "click"},
                    {"key": "d", "hold": 0.6}):            # taps keep their own 0.5 s cap
            with self.assertRaises(ev.ConfigError, msg=bad):
                self.cmd(bad)


class TestHolds(unittest.TestCase):
    def test_timed_one_down_one_up(self):
        app, s, clock, logs = app_with({"warp": {"key": "d", "hold_seconds": 3}})
        self.assertIsNotNone(app.handle_text("warp"))
        self.assertEqual((s.downs, s.ups), (["d"], []))
        self.assertIn("  holding D for 3.0s", logs)
        clock.t += 2.9; app.hold.tick()
        self.assertEqual(s.ups, [])
        clock.t += 0.2; app.hold.tick(); app.hold.tick()
        self.assertEqual(s.ups, ["d"])
        self.assertIn("  released D (time's up)", logs)
        self.assertEqual(len(app.rec.calls), 1)          # the keyDown; nothing else was sent

    def test_until_click_waits_for_users_own_click(self):
        app, s, clock, logs = app_with()
        seq = iter([False, False, True, True, False])
        app.hold.left_button = lambda: next(seq)
        app.handle_text("lock")                             # hold ctrl until click
        self.assertEqual(s.downs, ["ctrl"])
        app.hold.tick(); app.hold.tick(); app.hold.tick()   # up, down, down (user pressing)
        self.assertEqual(s.ups, [])
        app.hold.tick()                                     # user let go of the button
        self.assertEqual(s.ups, [])                         # still held so EVE sees Ctrl+click
        clock.t += 0.1; app.hold.tick(); self.assertEqual(s.ups, [])
        clock.t += 0.06; app.hold.tick()                    # CLICK_SETTLE passed
        self.assertEqual(s.ups, ["ctrl"])
        self.assertIn("  released Ctrl (you clicked)", logs)
        self.assertEqual([c for c in app.rec.calls if c[0] == "mouse"], [])   # the tool never clicks

    def test_until_click_button_already_down_at_start(self):
        app, s, clock, _ = app_with()
        seq = iter([True, True, False, True, False])
        app.hold.left_button = lambda: next(seq)
        app.handle_text("approach")
        app.hold.tick(); app.hold.tick()        # still down from before, then up: only arms
        self.assertEqual(s.ups, [])
        app.hold.tick(); app.hold.tick()        # a real click
        clock.t += 0.2; app.hold.tick()
        self.assertEqual(s.ups, ["q"])

    def test_until_click_timeout(self):
        app, s, clock, logs = app_with()
        app.handle_text("orbit")
        clock.t += 9.9; app.hold.tick()
        self.assertEqual(s.ups, [])
        clock.t += 0.2; app.hold.tick()
        self.assertEqual(s.ups, ["w"])
        self.assertIn("  released W (safety limit reached)", logs)

    def test_release_phrase_and_let_go(self):
        app, s, clock, logs = app_with({"keep at range": {"key": "e", "hold_until": "release"}})
        app.handle_text("keep at range")
        clock.t += 20; app.hold.tick()
        self.assertEqual(s.ups, [])
        self.assertIsNotNone(app.handle_text("release"))
        self.assertEqual(s.ups, ["e"])
        self.assertEqual(len(app.rec.calls), 1)
        clock.t += 5
        self.assertIsNone(app.handle_text("let go"))       # nothing held -> nothing at all
        self.assertEqual((len(app.rec.calls), s.ups), (1, ["e"]))
        app.handle_text("align"); clock.t += 5
        app.handle_text("let go")
        self.assertEqual(s.ups, ["e", "a"])

    def test_release_mode_safety_max(self):
        app, s, clock, _ = app_with({"warp": {"key": "d", "hold_until": "release", "hold_max": 8}})
        app.handle_text("warp"); clock.t += 8.01; app.hold.tick()
        self.assertEqual(s.ups, ["d"])

    def test_new_hold_releases_previous_first(self):
        app, s, clock, logs = app_with()
        app.handle_text("lock"); clock.t += 1
        app.handle_text("approach")
        self.assertEqual(s.downs, ["ctrl", "q"])
        self.assertEqual(s.ups, ["ctrl"])
        self.assertIn("  released Ctrl (you started holding Q)", logs)
        self.assertEqual(app.hold.holding(), "Q")

    def test_other_input_while_holding_releases_first(self):
        # held Ctrl + our next key/click would combine into a chord / ctrl+click: never
        app, s, clock, logs = app_with()
        app.handle_text("lock"); app.handle_text("high one")
        self.assertEqual((s.sent, s.ups), (["ctrl", "f1"], ["ctrl"]))
        self.assertIn("  released Ctrl (before 'high one')", logs)
        app.handle_text("warp"); app.handle_text("select")       # a tool click: D stays down for it
        self.assertEqual(s.ups, ["ctrl"])
        self.assertEqual(app.rec.calls[-1][0], "mouse")
        clock.t += 0.2; app.hold.tick()                          # ...and is let go just after
        self.assertEqual(s.ups, ["ctrl", "d"])

    def test_focus_loss_releases(self):
        title = {"t": "EVE - Spence"}
        app, s, clock, logs = app_with(title=lambda: title["t"])
        app.handle_text("warp")
        app.hold.tick(); self.assertEqual(s.ups, [])
        title["t"] = "Discord"; app.hold.tick()
        self.assertEqual(s.ups, [])                     # a short focus blip doesn't let go
        title["t"] = "EVE - Spence"; clock.t += 0.3; app.hold.tick()
        title["t"] = "Discord"; clock.t += 0.3; app.hold.tick()
        self.assertEqual(s.ups, [])
        clock.t += 0.5; app.hold.tick()
        self.assertEqual(s.ups, ["d"])
        self.assertIn("  released D (EVE lost focus)", logs)

    def test_config_change_stop_and_exit_release(self):
        app, s, clock, _ = app_with()
        app.handle_text("warp")
        app.apply_config(app.cfg)                       # mode switch / settings change
        self.assertEqual(s.ups, ["d"])
        clock.t += 5; app.handle_text("warp")
        ev.release_all_holds("app closing")             # what atexit runs
        self.assertEqual(s.ups, ["d", "d"])
        self.assertFalse(app.hold.release())            # idempotent: never a second keyUp

    def test_events_for_status_bar(self):
        app, s, clock, _ = app_with()
        got = []
        app.on_event = lambda k, p, d: got.append((k, p, d))
        app.handle_text("lock"); app.handle_text("release")
        self.assertIn(("holding", "lock", "Ctrl|click|10"), got)
        self.assertIn(("released", "lock", "Ctrl|you said 'release'"), got)

    def test_every_default_command_down_matches_up(self):
        app, s, clock, _, _ = make_app(data=fully_bound_data())
        s.downs, s.ups = [], []
        for phrase, cmd in app.cfg.commands.items():
            if cmd.kind in ("calibrate", "teach", "release"):
                continue
            clock.t += 10
            n = len(app.rec.calls)
            with mock.patch.object(ev.time, "sleep") as slp:
                app.handle_text(phrase)
            self.assertEqual(len(app.rec.calls) - n, 1, phrase)
            self.assertEqual(slp.call_count, 0 if cmd.hold_mode != "tap" or cmd.kind != "key" else 0)
            app.hold.tick()
        app.hold.release("done")
        self.assertGreaterEqual(len(s.downs), 7)
        self.assertEqual(s.downs, s.ups)                # every keyDown got exactly its own keyUp

    def test_non_blocking_real_thread(self):
        app, s, clock, _ = app_with({"warp": {"key": "d", "hold_seconds": 0.2}})
        app.hold.clock = time.monotonic
        app.hold.spawn = ev._spawn_thread
        t0 = time.monotonic()
        app.handle_text("warp")
        self.assertLess(time.monotonic() - t0, 0.05)      # listener isn't blocked
        self.assertEqual(s.ups, [])
        deadline = time.monotonic() + 2
        while not s.ups and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(s.ups, ["d"])
        self.assertGreaterEqual(time.monotonic() - t0, 0.19)


class TestBackgroundHold(unittest.TestCase):
    ONE = [(222, ALT)]

    def test_timed_hold_is_down_now_up_later(self):
        d = fully_bound_data()
        d["groups"]["navigation"]["commands"]["dock"] = {"key": "g", "hold_seconds": 2}
        app, api, clock, logs = bg_app(self.ONE, data=d)
        app.hold.spawn = None
        app.handle_text("dock")
        self.assertEqual([p[1] for p in api.posts], [ev.WM_KEYDOWN])
        clock.t += 2.1; app.hold.tick()                  # no focus check in background mode
        self.assertEqual([(p[0], p[1], p[2]) for p in api.posts],
                         [(222, ev.WM_KEYDOWN, 0x47), (222, ev.WM_KEYUP, 0x47)])
        self.assertEqual(app.rec.calls, [])

    def test_hold_until_click_skipped_in_background(self):
        app, *_ = bg_app(self.ONE, data=fully_bound_data())
        self.assertNotIn("lock", app.cfg.commands)
        self.assertTrue(any("hold until click" in w for w in app.cfg.warnings))


class TestCompliance(unittest.TestCase):
    def test_left_button_read_is_a_poll_not_a_hook(self):
        with open(os.path.join(os.path.dirname(HERE), "eve_voice.py"), encoding="utf-8") as f:
            src = f.read()
        body = src[src.index("def make_left_button_checker"):src.index("_LIVE_HOLDS")]
        self.assertIn("GetAsyncKeyState", body)
        self.assertNotIn("Hook", body)
        self.assertEqual(ev.make_left_button_checker()(), None if sys.platform != "win32" else False)


if __name__ == "__main__":
    unittest.main()
