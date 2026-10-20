"""release_tool - build signed release assets for autoupdater (project-agnostic).

    from release_tool import build_release, ensure_keypair
    pub_b64 = ensure_keypair("/secure/place/private_key.pem")
    build_release(app_dir, files, "1.2.0", "release/", "/secure/place/private_key.pem",
                  name="my-app", notes="...", repo="OWNER/my-app")

Writes <out>/<name>-<version>.zip (+ app_manifest.json inside), latest.json and
latest.json.sig, and returns a dict with the paths and a suggested `gh release create`.
Needs the 'cryptography' package (signing only happens on the publisher's machine).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import stat
import time
import zipfile
from typing import Iterable, List, Optional


def _key_lib():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    return serialization, Ed25519PrivateKey


def ensure_keypair(private_key_path: str) -> str:
    """Create an Ed25519 key once (chmod 600). Returns the base64 raw PUBLIC key."""
    serialization, Ed25519PrivateKey = _key_lib()
    if not os.path.exists(private_key_path):
        os.makedirs(os.path.dirname(os.path.abspath(private_key_path)), exist_ok=True)
        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        fd = os.open(private_key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
    os.chmod(private_key_path, stat.S_IRUSR | stat.S_IWUSR)
    return public_key_b64(private_key_path)


def _load_private(private_key_path: str):
    serialization, _ = _key_lib()
    with open(private_key_path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def public_key_b64(private_key_path: str) -> str:
    serialization, _ = _key_lib()
    pub = _load_private(private_key_path).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(pub).decode()


def sign_file(private_key_path: str, path: str) -> str:
    with open(path, "rb") as f:
        sig = _load_private(private_key_path).sign(f.read())
    with open(path + ".sig", "w", encoding="ascii") as f:
        f.write(base64.b64encode(sig).decode() + "\n")
    return path + ".sig"


def build_release(app_dir: str, files: Iterable[str], version: str, out_dir: str, private_key_path: str,
                  name: str, notes: str = "", min_updater_version: int = 1,
                  repo: Optional[str] = None) -> dict:
    files = sorted({f.replace("\\", "/") for f in files})
    os.makedirs(out_dir, exist_ok=True)
    zip_name = f"{name}-{version}.zip"
    zip_path = os.path.join(out_dir, zip_name)
    manifest = {"name": name, "version": version, "files": files, "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(os.path.join(app_dir, *f.split("/")), f)
        z.writestr("app_manifest.json", json.dumps(manifest, indent=2))
    with open(zip_path, "rb") as f:
        data = f.read()
    latest = {"version": version, "url": zip_name, "sha256": hashlib.sha256(data).hexdigest(),
              "size": len(data), "notes": notes, "min_updater_version": min_updater_version}
    latest_path = os.path.join(out_dir, "latest.json")
    with open(latest_path, "w", encoding="utf-8") as f:
        json.dump(latest, f, indent=2)
    sig_path = sign_file(private_key_path, latest_path)
    assets = [zip_path, latest_path, sig_path]
    gh = ["gh", "release", "create", f"v{version}"] + [os.path.relpath(a) for a in assets] + \
         (["--repo", repo] if repo else []) + ["--title", f"{name} {version}", "--notes", notes or f"{name} {version}"]
    return {"zip": zip_path, "latest": latest_path, "sig": sig_path, "assets": assets,
            "latest_json": latest, "gh_command": " ".join(shlex.quote(x) for x in gh)}
