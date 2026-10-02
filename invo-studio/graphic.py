"""TikTok Sans hook on a single red sticker over the source's first frame."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
# This is the bundled TikTok Sans Bold font, not a system-font substitute.
FONT = ROOT / "assets" / "fonts" / "TikTokSans-Bold.ttf"
# Sampled from the user's original in-app red sticker reference.
STICKER_RED = (232, 63, 65, 255)


def headline_png(words: str, output: Path) -> None:
    words = " ".join(words.upper().strip().split())[:100]
    if not words:
        raise ValueError("A verified opening headline is required")
    canvas = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    parts = words.split()
    best = None
    for size in range(72, 59, -2):
        font = ImageFont.truetype(str(FONT), size)
        if draw.textlength(words, font=font) <= 960:
            best = (words,)
            break
        # The native reference fills the first line before wrapping the rest.
        for split in range(len(parts) - 1, 0, -1):
            lines = (" ".join(parts[:split]), " ".join(parts[split:]))
            if all(draw.textlength(line, font=font) <= 960 for line in lines):
                best = lines
                break
        if best:
            break
    if best is None:
        raise ValueError("Opening hook is too long")
    lines = best
    widths = [draw.textlength(line, font=font) for line in lines]
    line_step = int(size * 1.43)
    card_height = int(size * 1.56)
    # On the 480px reference the joined sticker begins at y=137 and ends at
    # y=230. These are the matching coordinates on our 1080px output.
    y0 = 310 if len(lines) == 2 else 365
    for index, line in enumerate(lines):
        width = widths[index]
        top = y0 + index * line_step
        draw.rounded_rectangle((540-width/2-38, top, 540+width/2+38, top+card_height),
                               radius=14, fill=STICKER_RED)
    for index, line in enumerate(lines):
        draw.text((540, y0+8+index*line_step), line, font=font, anchor="ma", fill="white")
    canvas.save(output)
