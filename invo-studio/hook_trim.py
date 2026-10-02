"""Keep the longest spoken hook, joining pauses under 0.18 seconds."""
from pathlib import Path
import re
import subprocess


def speech_window(path: Path, length: float) -> tuple[float, float]:
    result = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "info", "-i", str(path),
                             "-af", "silencedetect=noise=-42dB:d=0.08", "-f", "null", "-"],
                            text=True, capture_output=True, check=True)
    bounds = [(0.0, "end")] + [(float(t), kind) for kind, t in
                               re.findall(r"silence_(start|end): ([0-9.]+)", result.stderr)] + [(length, "start")]
    bounds.sort()
    spans = []
    start = None
    for t, kind in bounds:
        if kind == "end":
            start = t
        elif start is not None:
            if t - start >= .08:
                spans.append((start, t))
            start = None
    joined = []
    for span in spans:
        if joined and span[0] - joined[-1][1] < .18:
            joined[-1] = (joined[-1][0], span[1])
        else:
            joined.append(span)
    voiced = [span for span in joined if span[1] - span[0] >= .35]
    if not voiced:
        raise ValueError(f"No spoken hook: {path.name}")
    start, end = max(voiced, key=lambda pair: pair[1] - pair[0])
    # Preserve the quiet trailing consonant and 0.10s after the last audible
    # sample. A louder silence threshold clipped the end of these three takes.
    return round(max(0, start - .04), 3), round(min(length - .02, end + .10), 3)
