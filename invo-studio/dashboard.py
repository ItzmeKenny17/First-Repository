"""Local render console. Loopback only; publishing remains separate."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from schedule import ZONE, next_eligible

ROOT = Path(__file__).resolve().parent
STATE, CONTROL, ACCOUNTS = (ROOT / name for name in ("state.json", "control.json", "accounts.json"))
AUTO_RUNTIME = ROOT / "auto_runtime.json"
SOURCES, OUTPUT, THUMBS = ROOT / "assets/sources", ROOT / "output", ROOT / ".thumbnails"
HOSTS = {"127.0.0.1:8787", "localhost:8787"}
# TikTok and Instagram publishers are not implemented in this local build.
FULL_PUBLISHER_AVAILABLE = False
LOCK = threading.RLock()
WORKER = {"running": False, "started": None, "ended": None, "exit_code": None,
          "requested": 0, "log": []}
SOURCE_CACHE = {"key": None, "files": []}


def read(path: Path, default: dict) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default.copy()


def write(path: Path, data: dict) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def log(message: str) -> None:
    with LOCK:
        WORKER["log"] = (WORKER["log"] + [message.rstrip()])[-40:]


def missing_dependencies() -> list[str]:
    missing = [name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None]
    missing += [name for name in ("mediapipe", "PIL", "scipy") if importlib.util.find_spec(name) is None]
    return missing


def source_catalog() -> list[Path]:
    files = sorted((p for p in SOURCES.iterdir() if p.is_file() and p.suffix.lower() in
                    {".mp4", ".mov", ".mkv"}), key=lambda p: (" (" in p.stem, p.name.lower())) if SOURCES.exists() else []
    key = tuple((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in files)
    with LOCK:
        if key == SOURCE_CACHE["key"]:
            return SOURCE_CACHE["files"]
        unique = {}
        for path in files:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for part in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(part)
            unique.setdefault(digest.hexdigest(), path)
        SOURCE_CACHE.update(key=key, files=list(unique.values()))
        return SOURCE_CACHE["files"]


def render_worker(count: int, source: str | None) -> None:
    cmd = [sys.executable, "-u", "pipeline.py", "--dry-run", "--limit", str(count)]
    if source:
        cmd += ["--source", source]
    log(f"Render started · {count} clip{'s' if count != 1 else ''}" + (f" · {source}" if source else ""))
    try:
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", bufsize=1)
        assert proc.stdout is not None
        for line in proc.stdout:
            log(line)
        code = proc.wait()
        log("Render finished" if code == 0 else f"Render stopped with error code {code}")
    except Exception as exc:
        code = -1
        log(f"Render could not start: {exc}")
    with LOCK:
        WORKER.update(running=False, ended=datetime.now(timezone.utc).isoformat(), exit_code=code)


def start_render(count: int, source: str | None) -> bool:
    with LOCK:
        if WORKER["running"]:
            return False
        WORKER.update(running=True, started=datetime.now(timezone.utc).isoformat(),
                      ended=None, exit_code=None, requested=count, log=[])
        threading.Thread(target=render_worker, args=(count, source), daemon=True).start()
        return True


def auto_loop() -> None:
    """Render one local batch per Phoenix day while the dashboard is running."""
    while True:
        try:
            control = read(CONTROL, {})
            today = datetime.now(ZONE).date().isoformat()
            previous = read(AUTO_RUNTIME, {}).get("last_auto_day")
            if (not missing_dependencies() and control.get("auto_render_enabled", True) and control.get("automation_enabled", True)
                    and control.get("render_enabled", True) and previous != today and snapshot()["counts"]["backlog"]):
                count = min(5, max(1, int(control.get("auto_render_daily_limit", 5))))
                if start_render(count, None):
                    write(AUTO_RUNTIME, {"last_auto_day": today})
        except (OSError, ValueError, KeyError) as exc:
            log(f"Automatic render check failed: {exc}")
        time.sleep(30)


def snapshot() -> dict:
    state = read(STATE, {"jobs": {}, "last_run": None})
    control = read(CONTROL, {"automation_enabled": True, "render_enabled": True, "posting_enabled": False})
    accounts = read(ACCOUNTS, {})
    outputs = {p.name: p for p in OUTPUT.glob("*.mp4") if p.is_file()}
    jobs, used = [], set()
    for key, raw in reversed(list(state.get("jobs", {}).items())):
        name = Path(raw.get("output") or "").name
        job = {"id": key[:12], "source": raw.get("source", ""), "reaction": raw.get("reaction", ""),
               "headline": raw.get("headline", ""), "status": raw.get("status", "UNKNOWN"),
               "error": raw.get("error", ""), "started": raw.get("started_utc"),
               "finished": raw.get("finished_utc"), "scheduled": raw.get("scheduled_utc"),
               "published": raw.get("published_utc"), "urls": {k: raw[k] for k in
                   ("youtube_url", "tiktok_url", "instagram_url", "facebook_url") if raw.get(k)},
               "file": name if name in outputs else None}
        if job["file"] and job["status"] not in {"QA_FAILED", "render_failed"}:
            used.add(job["source"])
        jobs.append(job)
    source_files = source_catalog()
    backlog = [{"name": p.name, "size_mb": round(p.stat().st_size / 1048576, 1)}
               for p in source_files if p.name not in used]
    library = [{"file": name, "size_mb": round(path.stat().st_size / 1048576, 1),
                "headline": next((j["headline"] for j in jobs if j["file"] == name), ""),
                "status": next((j["status"] for j in jobs if j["file"] == name), "Legacy preview")}
               for name, path in sorted(outputs.items(), key=lambda item: item[1].stat().st_mtime, reverse=True)]
    posted = [j for j in jobs if j["published"] and j["urls"]]
    rendered = [j for j in jobs if j["file"] and j["status"].upper() in
                {"RENDERED", "REVIEW_PENDING", "QUEUED", "SCHEDULED_DRAFT", "PAUSED"}]
    connections = {name: {"handle": info.get("handle", info.get("profile_url", "")),
                           "connection": info.get("connection", "NOT_CONNECTED")}
                   for name, info in accounts.items() if name != "invo_submission"}
    ready = FULL_PUBLISHER_AVAILABLE and control.get("posting_enabled", False) and all(
        accounts.get(k, {}).get("connection") == "CONNECTED" for k in ("youtube", "instagram", "tiktok"))
    next_post = None
    if ready and control.get("ramp_start_date"):
        try:
            last = max((datetime.fromisoformat(j["published"]) for j in posted), default=None)
            start = datetime.fromisoformat(control["ramp_start_date"]).date()
            next_post = next_eligible(datetime.now(timezone.utc), start, last)
        except ValueError:
            pass
    drafts = []
    if not ready:
        now = datetime.now(timezone.utc)
        candidate = now
        for job in rendered[:5]:
            slot = next_eligible(candidate, now.astimezone(ZONE).date())
            if not slot:
                break
            drafts.append({"at": slot.isoformat(), "file": job["file"]})
            candidate = slot
    with LOCK:
        worker = {**WORKER, "log": WORKER["log"][-16:]}
    return {"time": datetime.now(timezone.utc).isoformat(), "last_run": state.get("last_run"),
            "control": control, "worker": worker, "connections": connections, "posting_ready": bool(ready),
            "missing_dependencies": missing_dependencies(),
            "auto_last_day": read(AUTO_RUNTIME, {}).get("last_auto_day"),
            "next_post": next_post.isoformat() if next_post else None, "drafts": drafts,
            "counts": {"sources": len(source_files), "backlog": len(backlog), "rendered": len(rendered),
                       "scheduled": sum(bool(j["scheduled"]) for j in jobs), "posted": len(posted),
                       "failed": sum(j["status"] in {"QA_FAILED", "render_failed"} for j in jobs)},
            "backlog": backlog, "jobs": jobs, "library": library}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, content: bytes, mime: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code: int, value: dict) -> None:
        self._send(code, json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _file(self, base: Path, name: str) -> Path | None:
        decoded = unquote(name)
        if not decoded or Path(decoded).name != decoded or "\\" in decoded:
            return None
        path = base / decoded
        return path if path.is_file() and path.suffix.lower() in {".mp4", ".mov", ".mkv"} else None

    def do_GET(self):
        if self.headers.get("Host") not in HOSTS:
            self.send_error(403)
            return
        route = urlparse(self.path).path
        if route == "/":
            self._send(200, (ROOT / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
        elif route == "/api/state":
            self._json(200, snapshot())
        elif route.startswith("/video/"):
            path = self._file(OUTPUT, route[len("/video/"):])
            if path:
                self._video(path)
            else:
                self.send_error(404)
        elif route.startswith("/thumb/"):
            kind, sep, name = route[len("/thumb/"):].partition("/")
            path = self._file(OUTPUT if kind == "output" else SOURCES, name) if sep and kind in {
                "output", "source"} else None
            if not path:
                self.send_error(404)
                return
            THUMBS.mkdir(exist_ok=True)
            key = hashlib.sha256(f"{path}:{path.stat().st_mtime_ns}".encode()).hexdigest()[:20]
            thumb = THUMBS / f"{key}.jpg"
            if not thumb.exists():
                try:
                    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "0.5", "-i", str(path),
                                    "-frames:v", "1", "-vf", "scale=270:-2", str(thumb)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if thumb.exists():
                self._send(200, thumb.read_bytes(), "image/jpeg")
            else:
                self.send_error(404)
        else:
            self.send_error(404)

    def _video(self, path: Path) -> None:
        total = path.stat().st_size
        start, end, code = 0, total - 1, 200
        header = self.headers.get("Range", "")
        if header:
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", header)
            if not match:
                self.send_error(416)
                return
            start = int(match[1])
            end = min(int(match[2]), total - 1) if match[2] else total - 1
            if start >= total or start > end:
                self.send_error(416)
                return
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()
        try:
            with path.open("rb") as media:
                media.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = media.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if self.headers.get("Host") not in HOSTS or self.headers.get("Origin") != "http://" + self.headers["Host"]:
            self.send_error(403)
            return
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self.send_error(415)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 2048:
                raise ValueError("Invalid request size")
            body = json.loads(self.rfile.read(size))
        except ValueError:
            self._json(400, {"error": "Invalid JSON request"})
            return
        route = urlparse(self.path).path
        if route == "/api/render":
            count, source = body.get("count", 1), body.get("source")
            if type(count) is not int or count not in {1, 3, 5} or (source and
                    (type(source) is not str or not self._file(SOURCES, source))):
                self._json(400, {"error": "Choose 1, 3, or 5 videos and a valid source"})
                return
            if source and count != 1:
                self._json(400, {"error": "A selected source renders once"})
                return
            control = read(CONTROL, {})
            missing = missing_dependencies()
            if missing:
                self._json(409, {"error": "Install or add to PATH: " + ", ".join(missing)})
                return
            if not control.get("automation_enabled", True) or not control.get("render_enabled", True):
                self._json(409, {"error": "Rendering is paused. Resume first."})
                return
            if not start_render(count, source):
                self._json(409, {"error": "A render is already running"})
                return
            self._json(202, {"message": "Render started"})
        elif route == "/api/auto":
            enabled = body.get("enabled")
            if type(enabled) is not bool:
                self._json(400, {"error": "Invalid automatic render setting"})
                return
            control = read(CONTROL, {})
            control["auto_render_enabled"] = enabled
            control["auto_render_daily_limit"] = 5
            write(CONTROL, control)
            self._json(200, {"message": "Automatic daily rendering enabled" if enabled else
                       "Automatic daily rendering disabled"})
        elif route == "/api/control":
            action = body.get("action")
            if action not in {"pause", "resume"}:
                self._json(400, {"error": "Unknown action"})
                return
            control = read(CONTROL, {})
            control["automation_enabled"] = action == "resume"
            control["render_enabled"] = action == "resume"
            # Resume affects only rendering; platform publishing stays off.
            write(CONTROL, control)
            self._json(200, {"message": "Rendering resumed" if action == "resume" else
                       "Paused after the current video"})
        else:
            self.send_error(404)


if __name__ == "__main__":
    print("INVO Studio: http://127.0.0.1:8787", flush=True)
    server = ThreadingHTTPServer(("127.0.0.1", 8787), Handler)
    threading.Thread(target=auto_loop, daemon=True).start()
    server.serve_forever()
