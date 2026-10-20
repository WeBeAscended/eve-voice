"""autoupdater - signed, rollback-safe self-update for small Python desktop apps.

Project-agnostic: everything app-specific is passed in an UpdaterConfig
(app name, folder, version, Ed25519 public key, where releases live, which
user files must never be touched). Drop this one file into any app.

Release layout (built by release_tool.py):
  latest.json      {"version", "url", "sha256", "size", "notes", "min_updater_version"}
  latest.json.sig  base64 Ed25519 signature of the exact latest.json bytes
  <name>-<version>.zip   app code only, with app_manifest.json listing its files

Sources:
  {"type": "github", "repo": "OWNER/REPO"}  -> api.github.com .../releases/latest assets
  {"type": "url", "url": "https://host/path/"} -> plain static folder
  A GitHub failure (offline, rate limit, no release) falls back to `fallback_url`.

Flow: check() -> stage() (download + verify sha256/size + extract to staging)
      -> apply_staged() at the NEXT start, before the app imports its code
         (backs up the old code to previous_version/, sets a pending flag)
      -> the app calls mark_started() once it's up; if it never does,
         handle_pending() on the next start rolls back automatically.
Nothing here raises into the caller from the *_safe helpers; errors are logged.
"""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

UPDATER_VERSION = 1
MANIFEST = "app_manifest.json"
STAGING = "update_staging"
PREVIOUS = "previous_version"
PENDING = "update_pending.json"
STATE = "update_state.json"
LOG = "update.log"
MAX_JSON = 1_000_000
MAX_ZIP = 500_000_000
# never written or deleted by the updater, whatever a release contains
ALWAYS_PRESERVE = (STAGING, STAGING + "/*", PREVIOUS, PREVIOUS + "/*", PENDING, STATE, LOG,
                   "venv", "venv/*", ".venv", ".venv/*", ".git", ".git/*", "*.part")


class UpdateError(Exception):
    pass


@dataclass
class UpdaterConfig:
    app_name: str                         # e.g. "eve-voice" (zip asset prefix)
    app_dir: str                          # folder the app runs from
    current_version: str                  # "1.0.0"
    public_key: str                       # base64 raw 32-byte Ed25519 public key
    source: Optional[dict] = None         # {"type": "github", "repo": ...} or {"type": "url", "url": ...}
    fallback_url: Optional[str] = None    # static folder used if the GitHub source fails
    preserve: Sequence[str] = ()          # globs (relative, '/' separators) of user files/folders
    requirements: Optional[str] = "requirements.txt"
    python: str = sys.executable          # used for 'pip install -r' when requirements change
    timeout: float = 5.0
    on_applied: Optional[Callable[["UpdaterConfig", str], None]] = None   # e.g. merge new defaults
    log: Optional[Callable[[str], None]] = None
    urlopen: Optional[Callable] = None    # tests
    pip_runner: Optional[Callable] = None  # tests: (cmd, progress) -> returncode
    user_agent: str = "autoupdater/1"


@dataclass
class Release:
    version: str
    zip_url: str
    sha256: str
    size: int
    notes: str = ""
    raw: dict = field(default_factory=dict)


# ----------------------------------------------------------------------------- versions
def parse_version(v: str) -> Tuple[int, int, int]:
    m = re.fullmatch(r"\s*v?(\d+)\.(\d+)\.(\d+)\s*", str(v))
    if not m:
        raise UpdateError(f"not a version: {v!r}")
    return tuple(int(x) for x in m.groups())  # type: ignore


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


# ----------------------------------------------------------------------------- Ed25519 verify
_p = 2 ** 255 - 19
_q = 2 ** 252 + 27742317777372353535851937790883648493
_d = -121665 * pow(121666, _p - 2, _p) % _p
_SQRT_M1 = pow(2, (_p - 1) // 4, _p)


def _ed_add(P, Q):
    A = (P[1] - P[0]) * (Q[1] - Q[0]) % _p
    B = (P[1] + P[0]) * (Q[1] + Q[0]) % _p
    C = 2 * P[3] * Q[3] * _d % _p
    D = 2 * P[2] * Q[2] % _p
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _p, G * H % _p, F * G % _p, E * H % _p)


def _ed_mul(s, P):
    Q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            Q = _ed_add(Q, P)
        P = _ed_add(P, P)
        s >>= 1
    return Q


def _ed_x(y, sign):
    if y >= _p:
        return None
    x2 = (y * y - 1) * pow(_d * y * y + 1, _p - 2, _p) % _p
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_p + 3) // 8, _p)
    if (x * x - x2) % _p:
        x = x * _SQRT_M1 % _p
    if (x * x - x2) % _p:
        return None
    return _p - x if (x & 1) != sign else x


def _ed_point(s):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _ed_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _p)


_GY = 4 * pow(5, _p - 2, _p) % _p
_G = (_ed_x(_GY, 0), _GY, 1, _ed_x(_GY, 0) * _GY % _p)


def ed25519_verify_pure(public: bytes, msg: bytes, sig: bytes) -> bool:
    """RFC 8032 verify in plain Python (fallback when 'cryptography' isn't installed)."""
    if len(public) != 32 or len(sig) != 64:
        return False
    A, R = _ed_point(public), _ed_point(sig[:32])
    if A is None or R is None:
        return False
    s = int.from_bytes(sig[32:], "little")
    if s >= _q:
        return False
    h = int.from_bytes(hashlib.sha512(sig[:32] + public + msg).digest(), "little") % _q
    P, Q = _ed_mul(s, _G), _ed_add(R, _ed_mul(h, A))
    return (P[0] * Q[2] - Q[0] * P[2]) % _p == 0 and (P[1] * Q[2] - Q[1] * P[2]) % _p == 0


def verify_signature(public_key_b64: str, data: bytes, sig_text: bytes) -> bool:
    try:
        pub = base64.b64decode(public_key_b64, validate=True)
        sig = base64.b64decode(sig_text.strip(), validate=True)
    except Exception:
        return False
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return ed25519_verify_pure(pub, data, sig)
    try:
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, data)
        return True
    except (InvalidSignature, ValueError):
        return False


# ----------------------------------------------------------------------------- small helpers
def _log(cfg: UpdaterConfig, msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{cfg.app_name}] {msg}"
    try:
        with open(os.path.join(cfg.app_dir, LOG), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if cfg.log:
        try:
            cfg.log(msg)
        except Exception:
            pass


def _p_(cfg, *parts) -> str:
    return os.path.join(cfg.app_dir, *parts)


def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def is_preserved(cfg: UpdaterConfig, rel: str) -> bool:
    """True for user files/folders the updater must never write or delete.
    Patterns are fnmatch globs ('*' also crosses '/'); a plain name also covers its folder."""
    rel = rel.replace("\\", "/").strip("/")
    for pat in tuple(cfg.preserve) + ALWAYS_PRESERVE:
        pat = pat.replace("\\", "/").strip("/")
        if fnmatch.fnmatch(rel, pat) or rel.startswith(pat + "/"):
            return True
    return False


def _safe_rel(name: str) -> Optional[str]:
    n = name.replace("\\", "/")
    if not n or n.startswith("/") or re.match(r"^[A-Za-z]:", n):
        return None
    parts = [p for p in n.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return "/".join(parts)


def state(cfg: UpdaterConfig) -> dict:
    return _read_json(_p_(cfg, STATE), {}) or {}


def _save_state(cfg: UpdaterConfig, st: dict) -> None:
    _write_json(_p_(cfg, STATE), st)


# ----------------------------------------------------------------------------- fetching
def _open(cfg, url, accept=None):
    req = urllib.request.Request(url, headers={"User-Agent": cfg.user_agent,
                                               **({"Accept": accept} if accept else {})})
    return (cfg.urlopen or urllib.request.urlopen)(req, timeout=cfg.timeout)


def _get(cfg, url, limit=MAX_JSON, accept=None) -> bytes:
    with _open(cfg, url, accept) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise UpdateError(f"{url} is too big")
    return data


def _from_github(cfg: UpdaterConfig, repo: str) -> Tuple[bytes, bytes, Dict[str, str]]:
    api = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        rel = json.loads(_get(cfg, api, accept="application/vnd.github+json"))
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise UpdateError("GitHub rate limit reached - skipping this check")
        if e.code == 404:
            raise UpdateError(f"no published release for {repo}")
        raise UpdateError(f"GitHub API error {e.code}")
    assets = {a.get("name"): a.get("browser_download_url") for a in rel.get("assets") or []
              if isinstance(a, dict)}
    if "latest.json" not in assets or "latest.json.sig" not in assets:
        raise UpdateError("the latest GitHub release has no latest.json / latest.json.sig asset")
    return _get(cfg, assets["latest.json"]), _get(cfg, assets["latest.json.sig"]), assets


def _from_url(cfg: UpdaterConfig, base: str) -> Tuple[bytes, bytes, Dict[str, str]]:
    base = base if base.endswith("/") else base + "/"
    return (_get(cfg, urllib.parse.urljoin(base, "latest.json")),
            _get(cfg, urllib.parse.urljoin(base, "latest.json.sig")), {"__base__": base})


def _fetch_manifest(cfg: UpdaterConfig):
    src = cfg.source or ({"type": "url", "url": cfg.fallback_url} if cfg.fallback_url else None)
    if not src:
        raise UpdateError("no update source configured")
    try:
        if src.get("type") == "github":
            repo = str(src.get("repo") or "")
            if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or repo.upper().startswith("OWNER/"):
                raise UpdateError(f"GitHub repo not set ({repo or 'empty'})")
            return _from_github(cfg, repo)
        if src.get("type") == "url":
            return _from_url(cfg, str(src.get("url") or ""))
        raise UpdateError(f"unknown update source type {src.get('type')!r}")
    except (UpdateError, urllib.error.URLError, OSError, ValueError) as e:
        if cfg.fallback_url and src.get("type") != "url":
            _log(cfg, f"{_why(e)}; trying {cfg.fallback_url}")
            return _from_url(cfg, cfg.fallback_url)
        raise


def _why(e: Exception) -> str:
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return f"offline or unreachable ({e.reason})"
    return str(e) or e.__class__.__name__


def check(cfg: UpdaterConfig) -> Optional[Release]:
    """Newer, correctly signed release, or None if up to date. Raises UpdateError/OSError."""
    data, sig, assets = _fetch_manifest(cfg)
    if not verify_signature(cfg.public_key, data, sig):
        raise UpdateError("latest.json signature is NOT valid - ignoring this release")
    try:
        info = json.loads(data)
        version, url = str(info["version"]), str(info["url"])
        sha, size = str(info["sha256"]).lower(), int(info["size"])
    except (ValueError, KeyError, TypeError):
        raise UpdateError("latest.json is malformed")
    if int(info.get("min_updater_version", 1)) > UPDATER_VERSION:
        raise UpdateError(f"{version} needs a newer updater - please reinstall manually once")
    if not is_newer(version, cfg.current_version):
        if parse_version(version) < parse_version(cfg.current_version):
            _log(cfg, f"published {version} is older than installed {cfg.current_version} - no downgrade")
        return None
    if version in state(cfg).get("skip_versions", []):
        _log(cfg, f"{version} was rolled back before - skipping it")
        return None
    name = url.rsplit("/", 1)[-1]
    if "__base__" in assets:
        zip_url = urllib.parse.urljoin(assets["__base__"], url)
    elif name in assets:
        zip_url = assets[name]
    else:
        raise UpdateError(f"release asset {name} not found")
    return Release(version, zip_url, sha, size, str(info.get("notes") or ""), info)


# ----------------------------------------------------------------------------- staging
def staged(cfg: UpdaterConfig) -> Optional[dict]:
    s = _read_json(_p_(cfg, STAGING, "staged.json"))
    if not s:
        return None
    try:
        if not is_newer(s["version"], cfg.current_version):
            return None
    except (UpdateError, KeyError):
        return None
    return s if os.path.isdir(_p_(cfg, STAGING, s["version"])) else None


def stage(cfg: UpdaterConfig, rel: Release) -> str:
    """Download the zip, check size + sha256, extract to update_staging/<version>/."""
    if rel.size > MAX_ZIP:
        raise UpdateError("release is too big")
    root = _p_(cfg, STAGING)
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)
    h, got, buf = hashlib.sha256(), 0, io.BytesIO()
    with _open(cfg, rel.zip_url) as r:
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            got += len(chunk)
            if got > rel.size:
                raise UpdateError("download is bigger than latest.json says - rejected")
            h.update(chunk)
            buf.write(chunk)
    if got != rel.size:
        raise UpdateError(f"download size {got} != {rel.size} - rejected")
    if h.hexdigest() != rel.sha256:
        raise UpdateError("download sha256 doesn't match the signed latest.json - rejected")
    dest = os.path.join(root, rel.version)
    with zipfile.ZipFile(buf) as z:
        try:
            man = json.loads(z.read(MANIFEST))
        except KeyError:
            raise UpdateError("release zip has no app_manifest.json")
        if str(man.get("version")) != rel.version:
            raise UpdateError("release zip version doesn't match latest.json")
        files = []
        for info in z.infolist():
            if info.is_dir():
                continue
            rel_name = _safe_rel(info.filename)
            if rel_name is None:
                raise UpdateError(f"unsafe path in zip: {info.filename!r}")
            if rel_name != MANIFEST and is_preserved(cfg, rel_name):
                _log(cfg, f"zip contains user file {rel_name} - ignored")
                continue
            out = os.path.join(dest, *rel_name.split("/"))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with z.open(info) as src, open(out, "wb") as f:
                shutil.copyfileobj(src, f)
            if rel_name != MANIFEST:
                files.append(rel_name)
    _write_json(os.path.join(root, "staged.json"), {"version": rel.version, "notes": rel.notes, "files": files})
    _log(cfg, f"staged {rel.version} ({len(files)} files)")
    return dest


def check_and_stage_safe(cfg: UpdaterConfig) -> Tuple[Optional[str], str]:
    """(staged version or None, human message). Never raises."""
    try:
        s = staged(cfg)
        if s:
            return s["version"], f"Update {s['version']} is ready - restart to apply."
        rel = check(cfg)
        if rel is None:
            return None, f"Up to date ({cfg.current_version})."
        stage(cfg, rel)
        return rel.version, f"Update {rel.version} is ready - restart to apply."
    except Exception as e:
        msg = _why(e)
        _log(cfg, f"update check: {msg}")
        return None, f"No update: {msg}"


# ----------------------------------------------------------------------------- apply / rollback
def _manifest_files(path: str) -> Optional[List[str]]:
    m = _read_json(path)
    if not isinstance(m, dict) or not isinstance(m.get("files"), list):
        return None
    return [f for f in (_safe_rel(x) for x in m["files"]) if f]


def _sha_file(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _copy(src: str, dst: str) -> None:
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    tmp = dst + ".upd-tmp"
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def _run_pip(cfg: UpdaterConfig, progress) -> None:
    req = _p_(cfg, cfg.requirements)
    cmd = [cfg.python, "-m", "pip", "install", "-r", req, "--disable-pip-version-check"]
    if cfg.pip_runner:
        rc = cfg.pip_runner(cmd, progress)
    else:
        flags = 0x08000000 if sys.platform == "win32" else 0   # CREATE_NO_WINDOW
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             cwd=cfg.app_dir, creationflags=flags)
        for line in p.stdout:
            if progress and line.strip():
                progress(line.strip()[:120])
        rc = p.wait()
    if rc != 0:
        raise UpdateError(f"pip install failed (exit {rc})")


def apply_staged(cfg: UpdaterConfig, progress: Optional[Callable[[str], None]] = None) -> Optional[str]:
    """Install a staged update over the app code. Backs up the old code first; on any
    failure restores it. Returns the new version, or None. Never raises."""
    s = staged(cfg)
    if not s:
        return None
    new_v, src_root = s["version"], _p_(cfg, STAGING, s["version"])
    say = progress or (lambda m: None)
    new_files = [f for f in s.get("files", []) if not is_preserved(cfg, f)]
    old_files = _manifest_files(_p_(cfg, MANIFEST))
    if old_files is None:
        old_files = [f for f in new_files if os.path.exists(_p_(cfg, *f.split("/")))]
    old_files = [f for f in old_files if not is_preserved(cfg, f)]
    prev = _p_(cfg, PREVIOUS)
    try:
        say(f"Backing up {cfg.current_version}...")
        shutil.rmtree(prev, ignore_errors=True)
        os.makedirs(prev)
        kept = []
        for f in old_files:
            p = _p_(cfg, *f.split("/"))
            if os.path.isfile(p):
                _copy(p, os.path.join(prev, *f.split("/")))
                kept.append(f)
        if os.path.isfile(_p_(cfg, MANIFEST)):
            _copy(_p_(cfg, MANIFEST), os.path.join(prev, MANIFEST))
        _write_json(os.path.join(prev, ".backup.json"), {"version": cfg.current_version, "files": kept})
        req_changed = bool(cfg.requirements) and (
            _sha_file(_p_(cfg, cfg.requirements)) != _sha_file(os.path.join(src_root, cfg.requirements)))
        say(f"Installing {new_v}...")
        for f in new_files:
            _copy(os.path.join(src_root, *f.split("/")), _p_(cfg, *f.split("/")))
        _copy(os.path.join(src_root, MANIFEST), _p_(cfg, MANIFEST))
        for f in set(old_files) - set(new_files):
            try:
                os.remove(_p_(cfg, *f.split("/")))
            except OSError:
                pass
        if req_changed and os.path.isfile(_p_(cfg, cfg.requirements)):
            say("Libraries changed - installing them (this can take a minute)...")
            _run_pip(cfg, say)
        if cfg.on_applied:
            try:
                cfg.on_applied(cfg, new_v)
            except Exception as e:
                _log(cfg, f"post-update step failed (update kept): {e}")
        _write_json(_p_(cfg, PENDING), {"version": new_v, "previous": cfg.current_version,
                                        "time": time.time()})
        shutil.rmtree(_p_(cfg, STAGING), ignore_errors=True)
        _log(cfg, f"applied {cfg.current_version} -> {new_v}")
        cfg.current_version = new_v
        return new_v
    except Exception as e:
        _log(cfg, f"applying {new_v} failed: {_why(e)} - restoring {cfg.current_version}")
        _restore(cfg, bad_files=new_files)
        _skip(cfg, new_v)
        shutil.rmtree(_p_(cfg, STAGING), ignore_errors=True)
        return None


def _restore(cfg: UpdaterConfig, bad_files: Optional[List[str]] = None) -> Optional[str]:
    prev = _p_(cfg, PREVIOUS)
    meta = _read_json(os.path.join(prev, ".backup.json"))
    if not meta:
        return None
    good = [f for f in meta.get("files", []) if not is_preserved(cfg, f)]
    if bad_files is None:
        bad_files = _manifest_files(_p_(cfg, MANIFEST)) or []
    for f in set(bad_files) - set(good):
        if not is_preserved(cfg, f):
            try:
                os.remove(_p_(cfg, *f.split("/")))
            except OSError:
                pass
    for f in good:
        _copy(os.path.join(prev, *f.split("/")), _p_(cfg, *f.split("/")))
    if os.path.isfile(os.path.join(prev, MANIFEST)):
        _copy(os.path.join(prev, MANIFEST), _p_(cfg, MANIFEST))
    cfg.current_version = meta.get("version", cfg.current_version)
    return cfg.current_version


def _skip(cfg: UpdaterConfig, version: str) -> None:
    st = state(cfg)
    st.setdefault("skip_versions", [])
    if version not in st["skip_versions"]:
        st["skip_versions"].append(version)
    _save_state(cfg, st)


def rollback(cfg: UpdaterConfig, reason: str) -> Optional[str]:
    """Put the previous version back (keeps user files). Returns the restored version."""
    pend = _read_json(_p_(cfg, PENDING), {}) or {}
    bad = pend.get("version") or cfg.current_version
    restored = _restore(cfg)
    if restored:
        _skip(cfg, bad)
        _log(cfg, f"rolled back {bad} -> {restored}: {reason}")
    else:
        _log(cfg, f"wanted to roll back {bad} ({reason}) but there is no backup")
    try:
        os.remove(_p_(cfg, PENDING))
    except OSError:
        pass
    return restored


def handle_pending(cfg: UpdaterConfig) -> Optional[str]:
    """Call first thing at launch: a pending flag means the last update never confirmed
    a good start -> roll back. Returns the restored version or None."""
    if os.path.exists(_p_(cfg, PENDING)):
        return rollback(cfg, "the new version didn't start properly")
    return None


def mark_started(app_dir: str) -> None:
    """The app calls this once it started fine: the update is now kept."""
    try:
        os.remove(os.path.join(app_dir, PENDING))
    except OSError:
        pass


def is_pending(app_dir: str) -> bool:
    return os.path.exists(os.path.join(app_dir, PENDING))


# ----------------------------------------------------------------------------- JSON config merge
def merge_new_defaults(user_path: str, default_path: str, seen_path: str) -> List[str]:
    """Add settings/entries that are NEW in the default config (not in the defaults seen
    last time) and missing from the user's file. Never changes or deletes a user value,
    and never re-adds something the user deleted. Returns the added key paths."""
    new = _read_json(default_path)
    if not isinstance(new, dict):
        return []
    seen = _read_json(seen_path, {}) or {}
    user = _read_json(user_path)
    added: List[str] = []
    if isinstance(user, dict):
        from collections import OrderedDict
        with open(user_path, encoding="utf-8") as f:
            user = json.load(f, object_pairs_hook=OrderedDict)

        def walk(n, s, u, path):
            for k, v in n.items():
                here = f"{path}/{k}" if path else k
                if k not in u:
                    if k not in s:
                        u[k] = json.loads(json.dumps(v))
                        added.append(here)
                elif isinstance(v, dict) and isinstance(u[k], dict):
                    walk(v, s.get(k, {}) if isinstance(s.get(k), dict) else {}, u[k], here)
        walk(new, seen if isinstance(seen, dict) else {}, user, "")
        if added:
            shutil.copy2(user_path, user_path + ".before-update")   # keep the app's own .bak alone
            _write_json(user_path, user)
    _write_json(seen_path, new)
    return added
