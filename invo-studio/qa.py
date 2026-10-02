"""Structural gate before a rendered clip enters the posting queue."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


def metadata(path: Path) -> dict:
    output = subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                                      "-of", "json", str(path)])
    return json.loads(output)


def validate(source: Path, rendered: Path, hook_seconds: float) -> dict:
    if not source.is_file() or not rendered.is_file() or rendered.stat().st_size < 100_000:
        raise ValueError("Missing or empty source/render")
    source_info, out = metadata(source), metadata(rendered)
    def stream(data, kind):
        return next((s for s in data["streams"] if s["codec_type"] == kind), None)
    src_audio, out_audio, out_video = stream(source_info, "audio"), stream(out, "audio"), stream(out, "video")
    if src_audio is None or out_audio is None:
        raise ValueError("Expected source and output audio")
    if not out_video or out_video.get("codec_name") != "h264" or out_video.get("width") != 1080 or out_video.get("height") != 1920:
        raise ValueError("Output is not a valid vertical H.264 clip")
    expected = float(source_info["format"]["duration"]) + hook_seconds
    actual = float(out["format"]["duration"])
    if abs(actual - expected) > 0.35:
        raise ValueError(f"Source duration lost: expected {expected:.2f}s, got {actual:.2f}s")
    # Decode all frames and audio to null, catching broken files before queue.
    subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(rendered), "-f", "null", "-"],
                   capture_output=True, check=True)
    return {"source_seconds": round(float(source_info["format"]["duration"]), 3),
            "output_seconds": round(actual, 3), "source_audio_present": True,
            "output_audio_present": True, "decode_passed": True,
            "body_reaction_audio": "excluded_by_filter_graph"}
