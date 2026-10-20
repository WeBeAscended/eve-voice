"""Signed auto-update: verify, stage, apply, keep user files, merge defaults, roll back.
A local http.server plays the release host. No internet needed."""
import base64, functools, hashlib, http.server, io, json, os, shutil, socket, sys, tempfile, threading
import time, unittest, urllib.error, zipfile
from unittest import mock
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tools"))
import autoupdater as au
import eve_update
import launcher
try:
    import release_tool
    import make_release
    import cryptography  # noqa: F401
    HAVE_CRYPTO = True
except ImportError:  # the box's GUI test venv has no cryptography - signing tests need it
    HAVE_CRYPTO = False

USER_FILES = {"commands.json": None, "commands.json.bak": b"old backup", "gui_state.json": b'{"geometry": "1x1"}',
              "eve_voice.log": b"log line\n", "model/am/final.mdl": b"MODEL", "model-accurate/conf/x": b"BIG",
              "venv/Scripts/python.exe": b"PY", ".defaults_merged.json": None}


class Server:
    def __init__(self, root):
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=root)
        handler.log_message = lambda *a: None
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown(); self.httpd.server_close()


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


@unittest.skipUnless(HAVE_CRYPTO, "needs the cryptography package to sign test releases")
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.key = os.path.join(self.tmp, "keys", "private_key.pem")
        self.pub = release_tool.ensure_keypair(self.key)
        self.app = os.path.join(self.tmp, "app")          # the user's install
        self.src = os.path.join(self.tmp, "src")          # the publisher's next version
        self.host = os.path.join(self.tmp, "host")        # what's uploaded
        os.makedirs(self.host)
        files = make_release.app_files(ROOT)
        for d in (self.app, self.src):
            for f in files:
                os.makedirs(os.path.dirname(os.path.join(d, f)), exist_ok=True)
                shutil.copy2(os.path.join(ROOT, f), os.path.join(d, f))
            make_release.set_version_file(os.path.join(d, "version.py"), "1.0.0", self.pub)
        # installed 1.0.0 has a manifest like a real install
        with open(os.path.join(self.app, au.MANIFEST), "w") as f:
            json.dump({"version": "1.0.0", "files": files + ["old_module.py"]}, f)
        open(os.path.join(self.app, "old_module.py"), "w").write("# removed in 1.1.0\n")
        eve_update.ensure_user_config(self.app)
        user = json.load(open(os.path.join(self.app, "commands.json")))
        user["settings"]["cooldown_seconds"] = 2.5                       # a user change
        del user["groups"]["navigation"]["commands"]["orbit"]            # a user deletion
        user["spots"]["next_waypoint"] = [1, 2, 3, 4]                    # calibration
        self.server = Server(self.host)
        user["settings"]["update_source"] = {"type": "url", "url": self.server.url}
        json.dump(user, open(os.path.join(self.app, "commands.json"), "w"), indent=2)
        for rel, data in USER_FILES.items():
            if data is not None:
                p = os.path.join(self.app, *rel.split("/")); os.makedirs(os.path.dirname(p), exist_ok=True)
                open(p, "wb").write(data)
        self.user_before = self.snapshot_user()

    def tearDown(self):
        self.server.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def snapshot_user(self):
        out = {}
        for rel in USER_FILES:
            p = os.path.join(self.app, *rel.split("/"))
            if os.path.exists(p):
                out[rel] = open(p, "rb").read()
        return out

    def publish(self, version="1.1.0", notes="test", tweak=None, extra=None, files=None, **kw):
        make_release.set_version_file(os.path.join(self.src, "version.py"), version, self.pub)
        with open(os.path.join(self.src, "eve_voice.py"), "a") as f:
            f.write(f"\n# build {version}\n")
        d = json.load(open(os.path.join(self.src, "commands.default.json")))
        d["settings"]["brand_new_setting_for_test"] = True if False else None
        d["settings"].pop("brand_new_setting_for_test")
        d["groups"]["navigation"]["commands"]["warp around"] = "x"          # new command
        d["groups"]["brand_new_group"] = {"commands": {"new thing": "f12"}}
        d["settings"]["cooldown_seconds"] = 0.9                               # changed default
        json.dump(d, open(os.path.join(self.src, "commands.default.json"), "w"), indent=2)
        if tweak:
            tweak(self.src)
        fl = files or make_release.app_files(self.src)
        r = release_tool.build_release(self.src, fl + (extra or []), version, self.host, self.key,
                                       name="eve-voice", notes=notes, **kw)
        return r

    def cfg(self, **kw):
        return eve_update.make_config(self.app, **kw)

    def assertUserFilesUntouched(self):
        now = self.snapshot_user()
        for rel in ("commands.json.bak", "gui_state.json", "eve_voice.log", "model/am/final.mdl",
                    "model-accurate/conf/x", "venv/Scripts/python.exe"):
            self.assertEqual(now.get(rel), self.user_before.get(rel), rel)


class TestVersionAndSignature(unittest.TestCase):
    def test_versions(self):
        self.assertTrue(au.is_newer("1.0.1", "1.0.0"))
        self.assertTrue(au.is_newer("v1.10.0", "1.9.9"))
        self.assertFalse(au.is_newer("1.0.0", "1.0.0"))
        self.assertFalse(au.is_newer("0.9.9", "1.0.0"))
        for bad in ("1.0", "1.0.0-beta", "x"):
            with self.assertRaises(au.UpdateError):
                au.parse_version(bad)

    def test_pure_python_ed25519_rfc8032_vector(self):
        pub = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
        sig = bytes.fromhex("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
        self.assertTrue(au.ed25519_verify_pure(pub, b"", sig))
        self.assertFalse(au.ed25519_verify_pure(pub, b"x", sig))
        self.assertFalse(au.ed25519_verify_pure(pub, b"", sig[:-1] + bytes([sig[-1] ^ 1])))
        b64 = lambda b: base64.b64encode(b).decode()
        with mock.patch.dict(sys.modules, {"cryptography.exceptions": None}):   # force the fallback
            self.assertTrue(au.verify_signature(b64(pub), b"", b64(sig).encode()))
            self.assertFalse(au.verify_signature(b64(pub), b"tampered", b64(sig).encode()))
        self.assertFalse(au.verify_signature("not base64!", b"", b"x"))

    def test_kit_copy_is_identical(self):
        kit = "/workspace/auto-updater-kit"
        if not os.path.isdir(kit):
            self.skipTest("kit folder not present")
        for f, mine in (("autoupdater.py", os.path.join(ROOT, "autoupdater.py")),
                        ("release_tool.py", os.path.join(ROOT, "tools", "release_tool.py"))):
            self.assertEqual(open(os.path.join(kit, f)).read(), open(mine).read(), f)


class TestCheck(Base):
    def test_signature_good_bad_tampered(self):
        r = self.publish()
        rel = au.check(self.cfg())
        self.assertEqual((rel.version, rel.size), ("1.1.0", r["latest_json"]["size"]))
        sig = open(r["sig"]).read()
        # tampered latest.json (e.g. points at another zip) -> rejected
        data = json.load(open(r["latest"])); data["sha256"] = "0" * 64
        json.dump(data, open(r["latest"], "w"), indent=2)
        with self.assertRaisesRegex(au.UpdateError, "signature"):
            au.check(self.cfg())
        # garbage signature
        open(r["sig"], "w").write("AAAA\n")
        with self.assertRaisesRegex(au.UpdateError, "signature"):
            au.check(self.cfg())
        # signed with a DIFFERENT key
        other = os.path.join(self.tmp, "other.pem"); release_tool.ensure_keypair(other)
        self.publish(); release_tool.sign_file(other, r["latest"])
        with self.assertRaisesRegex(au.UpdateError, "signature"):
            au.check(self.cfg())
        self.assertIsNotNone(sig)

    def test_no_downgrade_or_same(self):
        self.publish("1.0.0")
        self.assertIsNone(au.check(self.cfg()))
        self.publish("0.9.0")
        self.assertIsNone(au.check(self.cfg()))
        self.assertIn("no downgrade", open(os.path.join(self.app, au.LOG)).read())

    def test_min_updater_version(self):
        self.publish(min_updater_version=au.UPDATER_VERSION + 1)
        with self.assertRaisesRegex(au.UpdateError, "newer updater"):
            au.check(self.cfg())

    def test_hash_and_size_mismatch(self):
        r = self.publish()
        with open(r["zip"], "ab") as f:
            f.write(b"x")                                 # bigger than signed size
        with self.assertRaisesRegex(au.UpdateError, "bigger|size"):
            au.stage(self.cfg(), au.check(self.cfg()))
        r = self.publish()
        b = bytearray(open(r["zip"], "rb").read()); b[100] ^= 0xFF   # same size, other bytes
        open(r["zip"], "wb").write(bytes(b))
        with self.assertRaisesRegex(au.UpdateError, "sha256"):
            au.stage(self.cfg(), au.check(self.cfg()))
        self.assertIsNone(au.staged(self.cfg()))

    def test_unsafe_zip_paths_rejected(self):
        evil = os.path.join(self.src, "evil.txt"); open(evil, "w").write("x")
        r = self.publish()
        # rebuild the zip by hand with a ../ entry, then sign it properly
        with zipfile.ZipFile(r["zip"], "a") as z:
            z.writestr("../escape.txt", "pwned")
        data = open(r["zip"], "rb").read()
        lj = json.load(open(r["latest"])); lj["sha256"] = hashlib.sha256(data).hexdigest(); lj["size"] = len(data)
        json.dump(lj, open(r["latest"], "w")); release_tool.sign_file(self.key, r["latest"])
        with self.assertRaisesRegex(au.UpdateError, "unsafe path"):
            au.stage(self.cfg(), au.check(self.cfg()))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "escape.txt")))

    def test_offline_is_quiet(self):
        c = self.cfg(); c.source = {"type": "url", "url": f"http://127.0.0.1:{free_port()}/"}; c.fallback_url = None
        t = time.monotonic()
        ver, msg = au.check_and_stage_safe(c)
        self.assertIsNone(ver)
        self.assertIn("offline", msg)
        self.assertLess(time.monotonic() - t, 6)

    def test_github_source_and_rate_limit(self):
        r = self.publish()
        api = "https://api.github.com/repos/me/eve-voice/releases/latest"
        assets = [{"name": os.path.basename(p), "browser_download_url": self.server.url + os.path.basename(p)}
                  for p in r["assets"]]
        real = __import__("urllib.request").request.urlopen

        def fake(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else req
            if url == api:
                return io.BytesIO(json.dumps({"tag_name": "v1.1.0", "assets": assets}).encode())
            return real(req, timeout=timeout)
        c = self.cfg(urlopen=fake); c.source = {"type": "github", "repo": "me/eve-voice"}
        self.assertEqual(au.check(c).zip_url, self.server.url + "eve-voice-1.1.0.zip")

        def limited(req, timeout=None):
            url = req.full_url
            if url.startswith("https://api.github.com"):
                raise urllib.error.HTTPError(url, 403, "rate limit", {}, None)
            return real(req, timeout=timeout)
        c = self.cfg(urlopen=limited); c.source = {"type": "github", "repo": "me/eve-voice"}; c.fallback_url = None
        ver, msg = au.check_and_stage_safe(c)
        self.assertIsNone(ver); self.assertIn("rate limit", msg)
        c.fallback_url = self.server.url                  # static fallback still works
        self.assertEqual(au.check(c).version, "1.1.0")
        c.source = {"type": "github", "repo": "OWNER/eve-voice"}   # placeholder -> fallback
        self.assertEqual(au.check(c).version, "1.1.0")


class TestShippedConfig(unittest.TestCase):
    def test_shipped_sources(self):
        cfg = eve_update.make_config(ROOT)
        self.assertEqual(cfg.source, {"type": "github", "repo": "WeBeAscended/eve-voice"})
        self.assertEqual(cfg.fallback_url, "https://raw.githubusercontent.com/WeBeAscended/eve-voice/main/release/")
        self.assertTrue(cfg.public_key)


class TestApply(Base):
    def stage_new(self, **kw):
        self.publish(**kw)
        c = self.cfg()
        ver, msg = au.check_and_stage_safe(c)
        self.assertEqual(ver, kw.get("version", "1.1.0"), msg)
        return c

    def test_apply_keeps_user_files_and_merges_defaults(self):
        pips = []
        c = self.stage_new()
        c = self.cfg(pip_runner=lambda cmd, p: pips.append(cmd) or 0)
        self.assertEqual(au.apply_staged(c), "1.1.0")
        self.assertEqual(eve_update.read_version(self.app), "1.1.0")
        self.assertIn("# build 1.1.0", open(os.path.join(self.app, "eve_voice.py")).read())
        self.assertFalse(os.path.exists(os.path.join(self.app, "old_module.py")))   # removed upstream
        self.assertTrue(os.path.exists(os.path.join(self.app, au.PREVIOUS, "old_module.py")))
        self.assertTrue(au.is_pending(self.app))
        self.assertEqual(pips, [])                                # requirements unchanged
        self.assertUserFilesUntouched()
        user = json.load(open(os.path.join(self.app, "commands.json")))
        self.assertEqual(user["settings"]["cooldown_seconds"], 2.5)            # user value kept
        self.assertEqual(user["spots"]["next_waypoint"], [1, 2, 3, 4])         # calibration kept
        self.assertNotIn("orbit", user["groups"]["navigation"]["commands"])    # user deletion kept
        self.assertEqual(user["groups"]["navigation"]["commands"]["warp around"], "x")   # new default added
        self.assertIn("brand_new_group", user["groups"])
        self.assertIn("warp around", open(os.path.join(self.app, au.LOG)).read())

    def test_requirements_change_runs_pip_and_failure_restores(self):
        tweak = lambda d: open(os.path.join(d, "requirements.txt"), "a").write("newlib>=1\n")
        self.stage_new(tweak=tweak)
        before = open(os.path.join(self.app, "eve_voice.py")).read()
        c = self.cfg(pip_runner=lambda cmd, p: 1)                # pip fails
        self.assertIsNone(au.apply_staged(c))
        self.assertEqual(open(os.path.join(self.app, "eve_voice.py")).read(), before)
        self.assertEqual(eve_update.read_version(self.app), "1.0.0")
        self.assertIn("1.1.0", au.state(c)["skip_versions"])
        self.assertUserFilesUntouched()
        # next release with the same requirement: pip runs, progress shown
        self.publish("1.1.1", tweak=lambda d: None)
        au.check_and_stage_safe(self.cfg())
        cmds, lines = [], []
        c = self.cfg(pip_runner=lambda cmd, p: (cmds.append(cmd), p("Collecting newlib"), 0)[2])
        self.assertEqual(au.apply_staged(c, lines.append), "1.1.1")
        self.assertEqual(cmds[0][1:4], ["-m", "pip", "install"])
        self.assertIn("Collecting newlib", lines)

    def test_rollback_on_failed_start(self):
        c = self.stage_new()
        au.apply_staged(self.cfg(pip_runner=lambda *a: 0))
        # app never called mark_started -> next launch rolls back
        restored = au.handle_pending(self.cfg())
        self.assertEqual(restored, "1.0.0")
        self.assertEqual(eve_update.read_version(self.app), "1.0.0")
        self.assertTrue(os.path.exists(os.path.join(self.app, "old_module.py")))
        self.assertFalse(au.is_pending(self.app))
        self.assertUserFilesUntouched()
        self.assertIsNone(au.check(self.cfg()))                  # 1.1.0 is skipped from now on
        self.publish("1.1.1")
        self.assertEqual(au.check(self.cfg()).version, "1.1.1")  # a fixed build is offered

    def test_zip_never_overwrites_user_files(self):
        def tweak(d):
            open(os.path.join(d, "commands.json"), "w").write('{"evil": true}')
            os.makedirs(os.path.join(d, "model"), exist_ok=True)
            open(os.path.join(d, "model", "x"), "w").write("evil")
        self.stage_new(tweak=tweak, extra=["commands.json", "model/x"])
        au.apply_staged(self.cfg(pip_runner=lambda *a: 0))
        self.assertNotIn("evil", open(os.path.join(self.app, "commands.json")).read())
        self.assertFalse(os.path.exists(os.path.join(self.app, "model", "x")))


class TestLauncher(Base):
    def ui(self, title, work):
        self.titles.append(title)
        return work(lambda m: None)

    def setUp(self):
        super().setUp()
        self.titles, self.started = [], []

    def test_launcher_applies_then_starts_and_confirms(self):
        self.publish()

        def gui(app_dir, args):
            self.started.append(eve_update.read_version(app_dir))
            au.mark_started(app_dir)                 # what the app does once its window is up
            return 0
        rc = launcher.run(self.app, start_gui=gui, progress_ui=self.ui, pip_runner=lambda *a: 0)
        self.assertEqual((rc, self.started), (0, ["1.1.0"]))
        self.assertEqual(self.titles, ["Updating EVE Voice to 1.1.0..."])
        self.assertFalse(au.is_pending(self.app))
        self.assertUserFilesUntouched()
        # next start: up to date, no window
        launcher.run(self.app, start_gui=gui, progress_ui=self.ui)
        self.assertEqual(self.titles, ["Updating EVE Voice to 1.1.0..."])

    def test_launcher_rolls_back_when_new_version_dies(self):
        self.publish()

        def gui(app_dir, args):
            self.started.append(eve_update.read_version(app_dir))
            if self.started[-1] == "1.1.0":
                return 1                              # crashed before confirming
            au.mark_started(app_dir); return 0
        rc = launcher.run(self.app, start_gui=gui, progress_ui=self.ui, pip_runner=lambda *a: 0)
        self.assertEqual(self.started, ["1.1.0", "1.0.0"])
        self.assertEqual(rc, 0)
        self.assertIn("rolled back 1.1.0 -> 1.0.0", open(os.path.join(self.app, au.LOG)).read())
        self.assertUserFilesUntouched()

    def test_restart_code_applies_update_found_while_running(self):
        def gui(app_dir, args):
            self.started.append(eve_update.read_version(app_dir))
            au.mark_started(app_dir)
            if len(self.started) == 1:              # app staged an update and asked to restart
                self.publish()
                au.check_and_stage_safe(self.cfg())
                return launcher.RESTART_CODE
            return 0
        launcher.run(self.app, start_gui=gui, progress_ui=self.ui, pip_runner=lambda *a: 0)
        self.assertEqual(self.started, ["1.0.0", "1.1.0"])

    def test_launcher_survives_anything(self):
        with mock.patch.object(au, "handle_pending", side_effect=RuntimeError("boom")):
            rc = launcher.run(self.app, start_gui=lambda *a: 0, progress_ui=self.ui)
        self.assertEqual(rc, 0)
        c = self.cfg(); c.source = {"type": "url", "url": f"http://127.0.0.1:{free_port()}/"}
        os.remove(os.path.join(self.app, "commands.json"))
        rc = launcher.run(self.app, start_gui=lambda *a: 0, progress_ui=self.ui)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(os.path.join(self.app, "commands.json")))   # first-run copy

    def test_auto_update_off(self):
        self.publish()
        u = json.load(open(os.path.join(self.app, "commands.json")))
        u["settings"]["auto_update"] = False
        json.dump(u, open(os.path.join(self.app, "commands.json"), "w"))
        launcher.run(self.app, start_gui=lambda *a: 0, progress_ui=self.ui)
        self.assertEqual(eve_update.read_version(self.app), "1.0.0")


class TestMakeRelease(Base):
    def test_cli_builds_signed_assets_without_user_files(self):
        out = os.path.join(self.tmp, "rel")
        open(os.path.join(self.src, "commands.json"), "w").write("{}")       # user file next to code
        with mock.patch("builtins.print") as pr:
            r = make_release.main(["--version", "2.0.0", "--notes", "big", "--key", self.key, "--out", out,
                                   "--repo", "me/eve-voice"], app_dir=self.src)
        names = zipfile.ZipFile(r["zip"]).namelist()
        self.assertIn("launcher.py", names); self.assertIn("app_manifest.json", names)
        self.assertIn("commands.default.json", names)
        for bad in ("commands.json", "gui_state.json", "tests/test_updater.py", "tools/make_release.py"):
            self.assertNotIn(bad, names)
        self.assertFalse(any("private" in n or n.endswith(".pem") for n in names))
        lj = json.load(open(r["latest"]))
        self.assertEqual((lj["version"], lj["url"], lj["notes"]), ("2.0.0", "eve-voice-2.0.0.zip", "big"))
        self.assertTrue(au.verify_signature(self.pub, open(r["latest"], "rb").read(), open(r["sig"], "rb").read()))
        printed = " ".join(str(c) for c in pr.call_args_list)
        self.assertIn("gh release create v2.0.0", printed)
        self.assertEqual(oct(os.stat(self.key).st_mode & 0o777), "0o600")
        with self.assertRaises(SystemExit):
            make_release.main(["--key", os.path.join(self.src, "k.pem"), "--out", out], app_dir=self.src)


if __name__ == "__main__":
    unittest.main()
