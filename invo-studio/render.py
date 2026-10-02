"""Render an INVO-style reaction using a cached, transparent presenter track.

This renderer never publishes anything. It needs an approved source, reaction,
and a manually verified hook end in seconds. It refuses to render without a
real segmentation model; no fake 'background removed' output is produced.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageEnhance
from scipy.ndimage import distance_transform_edt, gaussian_filter, minimum_filter
from graphic import headline_png

FPS = 15
OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920
MATTE_WIDTH = 360
MATTE_HEIGHT = 640
MODEL = "mediapipe-selfie-0"
MATTE_VERSION = "hdr-sdr-crisp-7"


def run(*args: str) -> str:
    result = subprocess.run(args, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"{' '.join(args[:2])} failed:\n{result.stderr[-2500:]}")
    return result.stdout


def duration(path: Path) -> float:
    data = json.loads(run("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)))
    return float(data["format"]["duration"])


def reaction_color_filter(path: Path) -> str:
    """Convert iPhone HLG/BT.2020 video to the SDR pixels used by PNG/MP4."""
    data = json.loads(run("ffprobe", "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=color_transfer", "-of", "json", str(path)))
    stream = data.get("streams", [{}])[0]
    if stream.get("color_transfer") == "arib-std-b67":
        return ("zscale=t=linear:npl=100,format=gbrpf32le,"
                "tonemap=tonemap=hable:desat=0,"
                "zscale=t=bt709:p=bt709:m=bt709:r=full,format=rgb24,")
    return ""


def matte(reaction: Path, cache: Path, length: float, *, body_crop: bool = False) -> Path:
    digest = hashlib.sha256(reaction.read_bytes()).hexdigest()[:20]
    key = f"{digest}-{FPS}-{MATTE_WIDTH}x{MATTE_HEIGHT}-{MODEL}-{MATTE_VERSION}-{'body' if body_crop else 'intro'}"
    folder = cache / key
    manifest = folder / "complete.json"
    frame_count = int(length * FPS + 0.999)
    if manifest.exists() and len(list(folder.glob("*.png"))) >= frame_count:
        return folder
    try:
        import mediapipe as mp
    except ImportError as exc:
        raise RuntimeError("MediaPipe is required for uncached reaction background removal; run DASHBOARD.bat to install dependencies") from exc
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as raw:
        raw_dir = Path(raw)
        run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(reaction),
            "-t", str(length), "-vf", f"{reaction_color_filter(reaction)}fps={FPS},scale={MATTE_WIDTH}:{MATTE_HEIGHT}",
            str(raw_dir / "%06d.png"))
        frames = sorted(raw_dir.glob("*.png"))
        if not frames:
            raise RuntimeError("No reaction frames were decoded")
        with mp.solutions.selfie_segmentation.SelfieSegmentation(model_selection=0) as session:
            for index, frame in enumerate(frames, 1):
                rgb = np.array(Image.open(frame).convert("RGB"), dtype=np.uint8)
                result = session.process(rgb)
                if result.segmentation_mask is None:
                    raise RuntimeError("Offline background removal failed")
                original_alpha = np.clip(result.segmentation_mask * 255, 0, 255).astype(np.uint8)
                if original_alpha.min() >= 200 or original_alpha.max() <= 40:
                    raise RuntimeError("Person mask lacks distinct foreground/background")
                # Preserve the converted SDR skin tone; the source is untouched.
                color = Image.fromarray(rgb, "RGB")
                color = ImageEnhance.Color(color).enhance(0.94)
                color = ImageEnhance.Brightness(color).enhance(0.94)
                pixels = np.dstack((np.asarray(color, dtype=np.uint8), original_alpha))
                # Pull the matte inward by one pixel, then soften the new edge.
                # Copy color from opaque subject pixels into semi-transparent
                # edge pixels so the original white wall cannot create a halo.
                solid = original_alpha >= 245
                if solid.any():
                    nearest = distance_transform_edt(~solid, return_distances=False, return_indices=True)
                    edge = (original_alpha > 0) & (original_alpha < 245)
                    pixels[edge, :3] = pixels[nearest[0][edge], nearest[1][edge], :3]
                alpha = minimum_filter(original_alpha, size=5)
                pixels[:, :, 3] = np.clip(gaussian_filter(alpha.astype(np.float32), sigma=0.6), 0, 255).astype(np.uint8)
                Image.fromarray(pixels, "RGBA").save(folder / f"{index:06}.png")
        manifest.write_text(json.dumps({"reaction_sha256": digest, "fps": FPS, "frames": len(frames)}))
    return folder


def render(source: Path, reaction: Path, hook_end: float, out: Path, cache: Path, max_seconds: float | None,
           headline: str, *, hook_start: float = 0.0, body_reaction: Path | None = None,
           body_start: float | None = None):
    if hook_start < 0 or hook_end <= hook_start or hook_end >= duration(reaction):
        raise ValueError("hook-end must fall inside the reaction recording")
    body_seconds = duration(source)
    if max_seconds is not None and body_seconds > max_seconds:
        raise ValueError(f"source is {body_seconds:.1f}s, longer than permitted {max_seconds}s")
    hook_seconds = hook_end - hook_start
    body_reaction = body_reaction or reaction
    body_start = (hook_end if body_start is None and body_reaction == reaction else body_start or 0.0)
    if duration(body_reaction) < body_start + body_seconds - 0.1:
        raise ValueError("Reaction body is shorter than source; select another body without cutting source")
    intro_matte = matte(reaction, cache, hook_end)
    body_matte = matte(body_reaction, cache, body_start + body_seconds, body_crop=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        first = Path(tmp) / "first.png"
        heading = Path(tmp) / "headline.png"
        headline_png(headline, heading)
        run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
            "-frames:v", "1", str(first))
        filter_graph = (
            f"[0:v]scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,"
            f"fps=30,trim=duration={hook_seconds},setpts=PTS-STARTPTS[still];"
            f"[2:v]trim=duration={hook_seconds},setpts=PTS-STARTPTS,scale=1080:1920:flags=lanczos,fps=30[big];"
            f"[still][big]overlay=0:140:shortest=1:format=auto[cutout_intro];"
            f"[4:v]scale=1080:1920[headline];"
            f"[cutout_intro][headline]overlay=0:0:shortest=1:format=auto,format=yuv420p[intro];"
            f"[3:a]atrim=start={hook_start}:end={hook_end},asetpts=PTS-STARTPTS,apad,atrim=duration={hook_seconds}[introa];"
            f"[1:v]scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,"
            f"fps=30,setpts=PTS-STARTPTS[main];"
            f"[5:v]trim=duration={body_seconds},setpts=PTS-STARTPTS,crop=360:505:0:0,scale=350:491:flags=lanczos,fps=30[small];"
            f"[main][small]overlay=700:1290:eof_action=repeat:format=auto:shortest=1,format=yuv420p[body];"
            f"[1:a]aresample=async=1:first_pts=0,asetpts=PTS-STARTPTS,apad,atrim=duration={body_seconds}[bodya];"
            f"[intro][introa][body][bodya]concat=n=2:v=1:a=1[v][a]"
        )
        run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-loop", "1", "-framerate", "30", "-i", str(first),
            "-i", str(source), "-framerate", str(FPS), "-start_number", str(int(hook_start * FPS) + 1),
            "-i", str(intro_matte / "%06d.png"),
            "-i", str(reaction), "-loop", "1", "-framerate", "30", "-i", str(heading),
            "-framerate", str(FPS), "-start_number", str(int(body_start * FPS) + 1),
            "-i", str(body_matte / "%06d.png"),
            "-filter_complex", filter_graph, "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "-t", str(hook_seconds + body_seconds + 0.1), str(out))
    print(out)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--reaction", required=True, type=Path)
    parser.add_argument("--hook-end", required=True, type=float)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cache", type=Path, default=Path(".matte-cache"))
    parser.add_argument("--max-seconds", type=float)
    parser.add_argument("--headline", required=True)
    parser.add_argument("--hook-start", type=float, default=0)
    parser.add_argument("--body-reaction", type=Path)
    parser.add_argument("--body-start", type=float)
    args = parser.parse_args()
    render(args.source, args.reaction, args.hook_end, args.output, args.cache, args.max_seconds, args.headline,
           hook_start=args.hook_start, body_reaction=args.body_reaction, body_start=args.body_start)


if __name__ == "__main__":
    main()
