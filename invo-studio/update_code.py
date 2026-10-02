"""Update INVO Studio code from the user's GitHub branch before starting.

Only allowlisted code files are replaced. Source clips, reaction recordings,
outputs, credentials and queue state remain on this computer.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASE = "https://raw.githubusercontent.com/ItzmeKenny17/First-Repository/main/invo-studio"
ALLOWED = {
    "dashboard.py", "dashboard.html", "pipeline.py", "render.py", "graphic.py",
    "hook_trim.py", "qa.py", "schedule.py", "youtube_upload.py", "requirements.txt",
    "update_code.py",
}
MAX_FILE_BYTES = 1_000_000


def fetch(name: str) -> bytes:
    request = urllib.request.Request(f"{BASE}/{name}", headers={"User-Agent": "INVO-Studio-Updater/1"})
    with urllib.request.urlopen(request, timeout=15) as response:
        data = response.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"Update file too large: {name}")
    return data


def update() -> None:
    try:
        manifest = json.loads(fetch("manifest.json"))
        files = manifest["files"]
        if not isinstance(files, dict) or set(files) != ALLOWED:
            raise ValueError("Unexpected update file list")
        staged = {}
        for name, expected in files.items():
            if not isinstance(expected, str) or len(expected) != 64:
                raise ValueError(f"Invalid checksum: {name}")
            local = ROOT / name
            if local.is_file() and hashlib.sha256(local.read_bytes()).hexdigest() == expected:
                continue
            content = fetch(name)
            if hashlib.sha256(content).hexdigest() != expected:
                raise ValueError(f"Checksum mismatch: {name}")
            staged[name] = content
        for name, content in staged.items():
            with tempfile.NamedTemporaryFile(dir=ROOT, prefix=".update-", delete=False) as stream:
                stream.write(content)
                temp = Path(stream.name)
            os.replace(temp, ROOT / name)
        (ROOT / ".code-version.json").write_text(json.dumps({"version": manifest["version"]}) + "\n")
        print(f"INVO Studio code up to date (version {manifest['version']}; {len(staged)} changed).")
    except (OSError, ValueError, KeyError, urllib.error.URLError) as exc:
        # An outage should never prevent the installed local dashboard from opening.
        print(f"Code update unavailable; starting installed version. {exc}")


if __name__ == "__main__":
    update()
