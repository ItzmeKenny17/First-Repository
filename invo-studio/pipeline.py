"""Deterministic batch queue. Requires explicit publishing credentials to upload.

Run with --dry-run to render and report without contacting any platform.
State is persisted as JSON so an uncertain upload is never blindly repeated.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

from render import render

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
STATE = ROOT / "state.json"
REPORT = ROOT / "report.html"
VIDEO_TYPES = {".mp4", ".mov", ".mkv"}


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(part)
    return digest.hexdigest()


def video_fingerprint(path: Path) -> str:
    # Normalize sampled decoded frames: container metadata and harmless
    # re-encodes of an identical recording must not create fake new reactions.
    proc = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-an", "-vf",
                           "fps=3,scale=90:160", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"],
                          capture_output=True, check=True)
    return hashlib.sha256(proc.stdout).hexdigest()


def unique_videos(folder: Path, *, visual=False):
    found = {}
    # Prefer the original filename over copied names such as "7 (1).mp4".
    for path in sorted(folder.iterdir(), key=lambda p: (bool(re.search(r" \(\d+\)$", p.stem)), p.name.lower())):
        if path.is_file() and path.suffix.lower() in VIDEO_TYPES:
            found.setdefault(video_fingerprint(path) if visual else sha(path), path)
    return list(found.items())


def read_state():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"jobs": {}, "last_run": None}


def save(state):
    temp = STATE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(STATE)


def report(state):
    import html
    rows = []
    for key, job in reversed(list(state["jobs"].items())):
        vals = [key[:10], job.get("source", ""), job.get("reaction", ""), job.get("status", ""),
                job.get("youtube_url", ""), job.get("error", "")]
        rows.append("<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in vals) + "</tr>")
    REPORT.write_text("<!doctype html><meta charset='utf-8'><title>INVO status</title>"
        "<style>body{font:16px system-ui;background:#121923;color:#eef;padding:30px}table{border-collapse:collapse;width:100%}"
        "th,td{padding:9px;border:1px solid #384656;text-align:left}a{color:#80ccff}</style>"
        "<h1>INVO run status</h1><p>Last run: " + html.escape(str(state.get("last_run"))) + "</p>"
        "<table><tr><th>Pair</th><th>Source</th><th>Reaction</th><th>Status</th><th>YouTube</th><th>Problem</th></tr>"
        + "".join(rows) + "</table>", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--source", help="Render this source filename from the bank")
    args = parser.parse_args()
    if not 1 <= args.limit <= 20:
        parser.error("daily limit must be 1..20")
    if (ASSETS / "reactions" / "bank.json").exists():
        return run_bank(args, parser)
    reactions_file = ASSETS / "reactions.json"
    if not reactions_file.exists():
        parser.error("assets/reactions.json is required with manually verified hook_end times")
    config = json.loads(reactions_file.read_text(encoding="utf-8"))
    hooks = config.get("hook_end_by_filename", {})
    headlines = config.get("headline_by_filename", {})
    allowed = {tuple(pair) for pair in config.get("allowed_pairs", [])}
    sources = unique_videos(ASSETS / "sources")
    reactions = unique_videos(ASSETS / "reactions", visual=True)
    state = read_state()
    if not args.dry_run and not os.environ.get("YOUTUBE_REFRESH_TOKEN"):
        parser.error("Publishing requires YOUTUBE_REFRESH_TOKEN; use --dry-run to render only")
    if not args.dry_run:
        attempts = [datetime.fromisoformat(j["upload_attempt_utc"])
                    for j in state["jobs"].values() if j.get("upload_attempt_utc")]
        if attempts and datetime.now(timezone.utc) - max(attempts) < timedelta(hours=2):
            parser.error("Two-hour gap has not elapsed since the last upload attempt")
    if not args.dry_run:
        from youtube_upload import verify
        for old_job in state["jobs"].values():
            if old_job.get("youtube_id") and old_job.get("status") == "youtube_processing":
                try:
                    current = verify(old_job["youtube_id"])
                    old_job["status"] = "published_youtube" if current == "public" else f"youtube_{current}"
                except Exception as exc:
                    old_job["error"] = str(exc)[:1000]
        save(state)
    made = 0
    # Alternate source first, then reaction to avoid twenty posts from one source.
    for reaction_hash, reaction in reactions:
        if reaction.name not in hooks or reaction.name not in headlines:
            print(f"SKIP {reaction.name}: set its hook_end and headline in reactions.json")
            continue
        for source_hash, source in sources:
            if made >= args.limit:
                break
            if allowed and (source.name, reaction.name) not in allowed:
                continue
            key = hashlib.sha256((source_hash + reaction_hash).encode()).hexdigest()
            if key in state["jobs"] and state["jobs"][key].get("status") != "render_failed":
                continue
            video = ROOT / "output" / f"{key[:16]}.mp4"
            job = state["jobs"][key] = {"source": source.name, "reaction": reaction.name,
                                        "status": "rendering", "output": str(video.relative_to(ROOT))}
            save(state)
            try:
                render(source, reaction, float(hooks[reaction.name]), video, ROOT / ".matte-cache", None,
                       headlines[reaction.name])
                job["status"] = "rendered"
                save(state)
                if not args.dry_run:
                    # Persist the uncertain state BEFORE calling the remote API.
                    # A crash cannot automatically create a duplicate upload.
                    job["status"] = "uploading_or_uncertain"
                    job["upload_attempt_utc"] = datetime.now(timezone.utc).isoformat()
                    save(state)
                    from youtube_upload import upload, verify
                    youtube_id = upload(video, title=f"Reaction: {source.stem[:70]} #Shorts",
                                        description="", privacy="public")
                    job["youtube_id"] = youtube_id
                    job["youtube_url"] = f"https://www.youtube.com/shorts/{youtube_id}"
                    status = verify(youtube_id)
                    job["status"] = "published_youtube" if status == "public" else f"youtube_{status}"
                    # Other platform links and Discord submission remain gated.
                    save(state)
            except Exception as exc:
                job["error"] = str(exc)[:1000]
                if job["status"] != "uploading_or_uncertain":
                    job["status"] = "render_failed"
                save(state)
            made += 1
    from datetime import datetime, timezone
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    save(state)
    report(state)
    print(f"Processed {made} unique combinations. Open {REPORT} for status.")


def run_bank(args, parser):
    """Use prepared hook and silent reaction tracks; keep upload disabled by default."""
    from render import duration
    from qa import validate

    bank = json.loads((ASSETS / "reactions" / "bank.json").read_text())
    hooks = [r for r in bank if r.get("active") and r.get("hook_path") and
             r.get("processing_status") == "READY"]
    bodies = [r for r in hooks if r.get("body_path")]
    if not hooks or not bodies:
        parser.error("Prepared hook and silent body clips are required")
    control_path = ROOT / "control.json"
    control = json.loads(control_path.read_text()) if control_path.exists() else {
        "render_enabled": True, "posting_enabled": False, "automation_enabled": True}
    if not control.get("automation_enabled", True) or not control.get("render_enabled", True):
        print("PAUSED: rendering is disabled; queue and history preserved")
        return
    if not args.dry_run and not control.get("posting_enabled", False):
        parser.error("Publishing is paused in control.json")
    if not args.dry_run and not os.environ.get("YOUTUBE_REFRESH_TOKEN"):
        parser.error("Publishing requires YouTube authorization")
    state = read_state()
    if not args.dry_run:
        attempts = [datetime.fromisoformat(j["upload_attempt_utc"]) for j in state["jobs"].values()
                    if j.get("upload_attempt_utc")]
        if attempts and datetime.now(timezone.utc) - max(attempts) < timedelta(hours=2):
            parser.error("Two-hour gap has not elapsed since last upload attempt")
        if any(j.get("status") in {"POSTING", "POSTED_PENDING_VERIFY"} for j in state["jobs"].values()):
            parser.error("An upload is active or uncertain; reconcile it before another post")
    print("Indexing unique sources…", flush=True)
    sources = unique_videos(ASSETS / "sources", visual=True)
    made = 0
    for source_hash, source in sources:
        if made >= args.limit:
            break
        if args.source and source.name != args.source:
            continue
        control = json.loads(control_path.read_text())
        if not control.get("automation_enabled", True) or not control.get("render_enabled", True):
            print("PAUSED: finish current render, then stop")
            break
        # A source is one finished clip. Changing the reaction must not fill
        # the queue with re-edits of the same footage.
        if any(j.get("source") == source.name and j.get("status") not in {"QA_FAILED", "render_failed"}
               and (ROOT / j.get("output", "missing")).is_file() for j in state["jobs"].values()):
            continue
        length = duration(source)
        count = lambda rid: sum(j.get("reaction") == rid for j in state["jobs"].values())
        choices = sorted(hooks, key=lambda r: (count(r["reaction_id"]), r["reaction_id"]))
        selected = None
        for hook in choices:
            compatible = sorted((b for b in bodies if b["processed_duration"] >= length + 0.1),
                                key=lambda b: (sum(j.get("body_reaction") == b["reaction_id"]
                                                   for j in state["jobs"].values()), b["reaction_id"]))
            for body in compatible:
                key = hashlib.sha256(f"{source_hash}|{hook['reaction_id']}|{body['reaction_id']}".encode()).hexdigest()
                previous = state["jobs"].get(key)
                if not previous or previous.get("status") in {"QA_FAILED", "render_failed"} or not (
                    ROOT / previous.get("output", "missing")
                ).is_file():
                    selected = (hook, body, key)
                    break
            if selected:
                break
        if not selected:
            continue
        hook, body, key = selected
        print(f"Rendering {source.name} with {hook['reaction_id']} + {body['reaction_id']}", flush=True)
        hook_video = ROOT / hook["hook_path"]
        body_video = ROOT / body["body_path"]
        from hook_trim import speech_window
        hook_start, hook_end = speech_window(hook_video, duration(hook_video))
        seconds = hook_end - hook_start
        video = ROOT / "output" / f"INVO_{source_hash[:8]}_{hook['reaction_id']}_{body['reaction_id']}_{key[:8]}.mp4"
        job = state["jobs"][key] = {"source": source.name, "reaction": hook["reaction_id"],
                                     "body_reaction": body["reaction_id"], "status": "RENDERING",
                                     "output": str(video.relative_to(ROOT)), "headline": hook["headline"],
                                     "started_utc": datetime.now(timezone.utc).isoformat(),
                                     "render_style": "TikTok Sans Bold / red sticker"}
        save(state)
        try:
            render(source, hook_video, hook_end, video, ROOT / ".matte-cache", None,
                   hook["headline"], hook_start=hook_start, body_reaction=body_video, body_start=0)
            job["status"] = "QA_CHECK"
            save(state)
            job["qa"] = validate(source, video, seconds)
            job["status"] = "RENDERED" if args.dry_run else "QUEUED"
            job["finished_utc"] = datetime.now(timezone.utc).isoformat()
            save(state)
            if not args.dry_run:
                # Re-read after rendering: the emergency stop can be pressed
                # while a long background-removal job is still running.
                control = json.loads(control_path.read_text())
                if not control.get("automation_enabled") or not control.get("posting_enabled"):
                    job["status"] = "PAUSED"
                    save(state)
                    break
                job["status"] = "POSTING"
                job["upload_attempt_utc"] = datetime.now(timezone.utc).isoformat()
                save(state)
                from youtube_upload import upload, verify
                youtube_id = upload(video, title=f"{hook['headline'][:70]} #Shorts",
                                    description="", privacy="public")
                job["youtube_id"] = youtube_id
                job["youtube_url"] = f"https://www.youtube.com/shorts/{youtube_id}"
                status = verify(youtube_id)
                job["status"] = "POSTED" if status == "public" else "POSTED_PENDING_VERIFY"
                if status == "public":
                    job["published_utc"] = datetime.now(timezone.utc).isoformat()
                save(state)
        except Exception as exc:
            job["error"] = str(exc)[:1200]
            job["status"] = "POSTED_PENDING_VERIFY" if job["status"] == "POSTING" else "QA_FAILED"
            job["finished_utc"] = datetime.now(timezone.utc).isoformat()
            save(state)
        made += 1
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    save(state)
    report(state)
    print(f"Processed {made} bank items. Open {REPORT} for status.")


if __name__ == "__main__":
    main()
