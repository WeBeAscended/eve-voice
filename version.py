"""EVE Voice version + where updates come from. tools/make_release.py rewrites VERSION."""
VERSION = "1.0.2"

# Base URL of the folder that holds latest.json, latest.json.sig and the release zips.
# Placeholder until hosting is decided (Cloudflare Pages / GitHub Releases ...).
# Can also be overridden per install with settings.update_url in commands.json.
UPDATE_URL = "https://raw.githubusercontent.com/WeBeAscended/eve-voice/main/release/"   # fallback when the Releases API fails / is rate-limited

# Ed25519 public key that release signatures are checked against (set by tools/make_release.py)
UPDATE_PUBLIC_KEY = "LC2T8SqLRWsg2olVR/IrO7UkAn3f3AEcEgvKt4Es2Vg="
