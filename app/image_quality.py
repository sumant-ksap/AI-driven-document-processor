"""Cheap local image checks run before the model call.

These catch problems the gate clerk can fix on the spot (rescan) and
also shrink oversized phone photos to stay under API image limits.
"""

from __future__ import annotations

import io

from PIL import Image, ImageFilter, ImageOps, ImageStat

MAX_LONG_EDGE = 2400   # px; larger adds tokens without improving OCR of forms
MIN_SHORT_EDGE = 700   # px; below this, small print on BOLs is usually unreadable
# px long edge; smaller images are enlarged before sending. On a 677x857 BOL with gemma4:e4b,
# no upscaling lost table cells and 1600 truncated addresses; 1200 read both correctly.
UPSCALE_BELOW = 1200
BLUR_THRESHOLD = 12.0  # edge-energy stddev; lower = blurrier (tuned on phone photos of forms)


def prepare_image(data: bytes) -> tuple[bytes, str, list[str]]:
    """Return (jpeg_bytes, media_type, quality_issues)."""
    issues: list[str] = []
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)  # phone photos are often stored rotated
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    w, h = img.size
    if min(w, h) < MIN_SHORT_EDGE:
        issues.append(f"low resolution ({w}x{h}px)")

    gray = img.convert("L")
    brightness = ImageStat.Stat(gray).mean[0]
    if brightness < 60:
        issues.append("image too dark")
    else:
        # Documents are mostly white paper, so judge washout by whether any ink survived,
        # not by average brightness (a clean scan averages ~250).
        hist = gray.histogram()
        dark_fraction = sum(hist[:128]) / (w * h)
        if dark_fraction < 0.002:
            issues.append("image overexposed / washed out")

    sample = gray.copy()
    sample.thumbnail((1200, 1200))
    edges = sample.filter(ImageFilter.FIND_EDGES)
    sw, sh = edges.size
    edges = edges.crop((4, 4, sw - 4, sh - 4))  # FIND_EDGES marks the image border itself
    edge_energy = ImageStat.Stat(edges).stddev[0]
    if edge_energy < BLUR_THRESHOLD:
        issues.append("image appears blurry")

    if max(w, h) > MAX_LONG_EDGE:
        img.thumbnail((MAX_LONG_EDGE, MAX_LONG_EDGE))
    elif max(w, h) < UPSCALE_BELOW:
        # Small screenshots: enlarging gives the vision encoder more pixels per character.
        scale = UPSCALE_BELOW / max(w, h)
        img = img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)

    out = io.BytesIO()
    img.convert("RGB").save(out, format="JPEG", quality=90)
    return out.getvalue(), "image/jpeg", issues
