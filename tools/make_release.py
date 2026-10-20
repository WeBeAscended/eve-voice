#!/usr/bin/env python3
"""Build a signed EVE Voice release (publisher's machine only).

  python tools/make_release.py --bump patch --notes "Fix lock hold"
  python tools/make_release.py --version 1.2.0 --notes "..."
  python tools/make_release.py            # rebuild the current version

Writes release/eve-voice-<ver>.zip, release/latest.json, release/latest.json.sig and
prints the `gh release create` command. The PRIVATE signing key lives OUTSIDE the
app folder (default /workspace/eve-voice-signing/private_key.pem) and is never zipped.
"""
import argparse
import glob
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, APP_DIR)
import release_tool  # noqa: E402

DEFAULT_KEY = os.environ.get("EVE_VOICE_SIGNING_KEY", "/workspace/eve-voice-signing/private_key.pem")
APP_FILES = ["eve_voice.py", "eve_voice_gui.py", "gate_finder.py", "autoupdater.py", "eve_update.py",
             "launcher.py", "version.py", "run.bat", "run_cli.bat", "setup.bat", "requirements.txt",
             "README.md", "commands.default.json"]
APP_GLOBS = ["docs/*.png"]


def app_files(app_dir=APP_DIR):
    files = [f for f in APP_FILES if os.path.isfile(os.path.join(app_dir, f))]
    for g in APP_GLOBS:
        files += [os.path.relpath(p, app_dir).replace("\\", "/") for p in glob.glob(os.path.join(app_dir, g))]
    missing = set(APP_FILES) - set(files)
    if missing:
        raise SystemExit(f"missing app files: {sorted(missing)}")
    return files


def bump(v, part):
    a, b, c = (int(x) for x in v.split("."))
    return {"major": f"{a + 1}.0.0", "minor": f"{a}.{b + 1}.0", "patch": f"{a}.{b}.{c + 1}"}[part]


def set_version_file(path, version=None, public_key=None):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    if version:
        src = re.sub(r'^VERSION\s*=\s*"[^"]*"', f'VERSION = "{version}"', src, flags=re.M)
    if public_key:
        if re.search(r"^UPDATE_PUBLIC_KEY\s*=", src, re.M):
            src = re.sub(r'^UPDATE_PUBLIC_KEY\s*=\s*"[^"]*"', f'UPDATE_PUBLIC_KEY = "{public_key}"', src, flags=re.M)
        else:
            src += f'\n# Ed25519 public key that release signatures are checked against\nUPDATE_PUBLIC_KEY = "{public_key}"\n'
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', src, re.M)
    return m.group(1)


def main(argv=None, app_dir=APP_DIR):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--version", help="set this exact version (semver)")
    g.add_argument("--bump", choices=["major", "minor", "patch"])
    ap.add_argument("--notes", default="")
    ap.add_argument("--key", default=DEFAULT_KEY, help="private key path (created if missing)")
    ap.add_argument("--out", default=os.path.join(app_dir, "release"))
    ap.add_argument("--repo", default="WeBeAscended/eve-voice", help="GitHub OWNER/REPO for the gh command")
    a = ap.parse_args(argv)
    if os.path.abspath(a.key).startswith(os.path.abspath(app_dir) + os.sep):
        raise SystemExit("refusing: keep the private key OUTSIDE the app folder")
    sys.path.insert(0, app_dir)
    import autoupdater
    vfile = os.path.join(app_dir, "version.py")
    with open(vfile, encoding="utf-8") as f:
        cur = re.search(r'^VERSION\s*=\s*"([^"]+)"', f.read(), re.M).group(1)
    new = a.version or (bump(cur, a.bump) if a.bump else cur)
    autoupdater.parse_version(new)
    pub = release_tool.ensure_keypair(a.key)
    set_version_file(vfile, new, pub)
    for old in glob.glob(os.path.join(a.out, "*")):
        if os.path.basename(old).startswith(("eve-voice-", "latest.json")):
            os.remove(old)
    r = release_tool.build_release(app_dir, app_files(app_dir), new, a.out, a.key, name="eve-voice",
                                   notes=a.notes, min_updater_version=autoupdater.UPDATER_VERSION, repo=a.repo)
    # the installer zip carries the same manifest, so the first update knows which files are code
    import json
    with zipfile.ZipFile(r["zip"]) as z:
        manifest = json.loads(z.read("app_manifest.json"))
    with open(os.path.join(app_dir, "app_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"Built EVE Voice {new}")
    for p in r["assets"]:
        print(f"  {p}  ({os.path.getsize(p)} bytes)")
    print(f"sha256 {r['latest_json']['sha256']}")
    print("\nPublish (GitHub Releases):")
    print(f"  {r['gh_command']}")
    print("Or static hosting: upload latest.json, latest.json.sig and the zip to the update_url folder.")
    return r


if __name__ == "__main__":
    main()
