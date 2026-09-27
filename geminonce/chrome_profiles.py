"""Using your real Chrome profile.

Chrome 136+ refuses automation of its *default* data dir, but a copy elsewhere works, and the
copied cookies still decrypt (on macOS the key lives in the Keychain, not the profile). So we
mirror your profile (minus caches) into ~/.geminonce/chrome and launch that: same SSO sessions.
"""
from __future__ import annotations

import fnmatch
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

CHROME_DATA = {
    "darwin": Path.home() / "Library/Application Support/Google/Chrome",
    "linux": Path.home() / ".config/google-chrome",
    "win32": Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/User Data",
}.get(sys.platform)
PROFILE_SKIP = ["Service Worker", "File System", "History*", "Favicons*", "Top Sites*", "Visited Links",
                "Sessions", "Current Session", "Current Tabs", "Last Session", "Last Tabs", "*Cache*",
                "Shared Dictionary", "Crashpad", "Singleton*", "BrowserMetrics*"]


def profiles() -> dict[str, str]:
    """{profile dir: 'Name <email>'} from Chrome's Local State."""
    try:
        info = json.loads((CHROME_DATA / "Local State").read_text())["profile"]["info_cache"]
    except (OSError, KeyError, ValueError, TypeError):
        return {}
    return {d: f"{v.get('name', '')} <{v.get('user_name') or 'not signed in'}>" for d, v in info.items()}


def resolve(name: str) -> str:
    """Accept a profile dir ('Profile 2'), display name, or email substring."""
    found = profiles()
    if name in found:
        return name
    hits = [d for d, label in found.items() if name.lower() in label.lower()]
    if len(hits) != 1:
        listing = "\n".join(f"  {d!r}: {label}" for d, label in found.items()) or "  (none found)"
        sys.exit(f"Chrome profile {name!r} is {'ambiguous' if hits else 'not found'}. Profiles:\n{listing}")
    return hits[0]


def mirror(profile: str, dest: Path) -> None:
    """Copy `profile` (minus caches and history) into the user-data dir `dest`."""
    src = CHROME_DATA / profile
    dest.mkdir(parents=True, exist_ok=True)
    dest.chmod(0o700)  # holds live session cookies
    print(f"Copying Chrome profile {profile!r} ({profiles().get(profile, '')}) ...")
    shutil.copy2(CHROME_DATA / "Local State", dest / "Local State")
    if shutil.which("rsync"):
        cmd = ["rsync", "-a", "--delete", *[f"--exclude={p}" for p in PROFILE_SKIP], f"{src}/", str(dest / profile)]
        subprocess.run(cmd, check=False, stderr=subprocess.DEVNULL)  # files changing mid-copy are fine
    else:
        shutil.rmtree(dest / profile, ignore_errors=True)
        shutil.copytree(src, dest / profile, ignore_dangling_symlinks=True,
                        ignore=lambda d, names: [n for n in names if any(fnmatch.fnmatch(n, p) for p in PROFILE_SKIP)])
