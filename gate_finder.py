"""
Opt-in "select next gate" helper (OFF by default: settings.allow_screen_read).

In EVE the next gate on your route is shown in YELLOW in the Overview. When - and only
when - you say the command, this:
  1. takes ONE picture of the Overview box you calibrated (nothing is kept),
  2. finds pixels with the yellow's HUE and enough saturation (so brighter / darker
     parts of the icon still match, and the grey normal-gate icon never does),
  3. groups them into rows, keeps rows that contain an icon-sized yellow shape
     (cheap shape check against the icon you picked), ignores tiny specks,
  4. returns ONE point to click: the yellow icon of the top row (or None -> NO click).
No continuous screen watching, no game memory, no game files. The caller sends exactly
one click, or none.

Gray area: finding a target from the screen is closer to what bots do than a fixed spot.
Ask CCP support before relying on it.

Default colour measured from a real EVE Overview "next gate on route" icon:
  outline (224, 238, 64), face (120, 127, 34), brightest (255, 255, 83)
  hue 60-65 deg, saturation 0.67-0.73, brightness 0.38-1.0
  normal (not-on-route) gate icon: grey, saturation <= 0.01 -> never matches
"""
from __future__ import annotations

import colorsys
import sys
from typing import List, Optional, Tuple

DEFAULT_TARGET = {
    "_note": "Opt-in screen reading for 'select next gate'. Set the Overview area and pick the yellow "
             "colour in the app (Next gate tab) before turning it on.",
    "box": None,              # [left, top, right, bottom] of your Overview, screen pixels
    "rgb": [224, 238, 64],    # measured EVE route yellow (hue ~65 deg) - replaced when you pick it
    "sampled": False,         # True once picked from YOUR screen (required)
    "hue_tolerance": 10,      # degrees either side of the picked hue
    "min_saturation": 0.45,   # 0..1 - grey/white things (normal gates, text) are below this
    "min_value": 0.30,        # 0..1 - ignore very dark pixels
    "min_pixels": 12,         # rows with fewer yellow pixels are specks - ignored
    "icon_size": [10, 10],    # yellow icon width/height in pixels (measured when you pick the colour)
    "shape_check": True,      # require an icon-sized yellow shape in the row
    "click_x": None,          # None = on the yellow icon; a number = pixels from the box's left edge
    "ambiguous_gap": 40,      # two yellow rows further apart than this = not sure, click nothing
}


def _t(target: dict, key: str):
    return target.get(key, DEFAULT_TARGET[key])


class Blob:
    def __init__(self, left, top, right, bottom, pixels):
        self.left, self.top, self.right, self.bottom, self.pixels = left, top, right, bottom, pixels

    @property
    def size(self) -> Tuple[int, int]:
        return self.right - self.left + 1, self.bottom - self.top + 1

    @property
    def center(self) -> Tuple[int, int]:
        return (self.left + self.right) // 2, (self.top + self.bottom) // 2

    def __repr__(self):
        return f"Blob(x={self.left}-{self.right}, y={self.top}-{self.bottom}, px={self.pixels})"


class Cluster(Blob):
    """A horizontal band of yellow pixels (one Overview row)."""

    def __init__(self, top, bottom, pixels):
        super().__init__(0, top, 0, bottom, pixels)
        self.blobs: List[Blob] = []
        self.icon: Optional[Blob] = None


class Result:
    def __init__(self, point, status, message, clusters, chosen=None, score=0.0):
        self.point, self.status, self.message = point, status, message
        self.clusters, self.chosen, self.score = clusters, chosen, score


def hue_of(rgb) -> float:
    return colorsys.rgb_to_hsv(*[c / 255.0 for c in rgb])[0] * 360.0


def color_mask(img, rgb, hue_tol: float = 10, min_sat: float = 0.45, min_val: float = 0.30):
    """'L' image, 255 where the pixel has the target's hue (+-hue_tol degrees) and at
    least min_sat saturation and min_val brightness. Done with lookup tables (fast)."""
    from PIL import ImageChops
    h, s, v = img.convert("RGB").convert("HSV").split()   # PIL: all three 0..255
    hc = hue_of(rgb) / 360.0 * 255.0
    ht = hue_tol / 360.0 * 255.0

    def hue_ok(x):
        d = abs(x - hc)
        return min(d, 255 - d) <= ht
    hm = h.point([255 if hue_ok(x) else 0 for x in range(256)])
    sm = s.point([255 if x >= min_sat * 255 else 0 for x in range(256)])
    vm = v.point([255 if x >= min_val * 255 else 0 for x in range(256)])
    return ImageChops.multiply(ImageChops.multiply(hm, sm), vm)


def _blobs_in_band(mask, top, bottom, col_gap=1) -> List[Blob]:
    w = mask.size[0]
    band = mask.crop((0, top, w, bottom + 1))
    bw, bh = band.size
    data = band.tobytes()
    cols = [sum(1 for y in range(bh) if data[y * bw + x]) for x in range(bw)]
    out, start, gap, px = [], None, 0, 0
    for x, n in enumerate(cols + [0] * (col_gap + 1)):
        if n:
            if start is None:
                start, px = x, 0
            px += n
            gap, end = 0, x
        elif start is not None:
            gap += 1
            if gap > col_gap:
                bb = band.crop((start, 0, end + 1, bh)).getbbox()
                out.append(Blob(start, top + bb[1], end, top + bb[3] - 1, px))
                start = None
    return out


def clusters_from_mask(mask, row_gap: int = 3) -> List[Cluster]:
    """Group yellow pixels into row bands (top to bottom), each split into blobs."""
    w, h = mask.size
    data = mask.tobytes()
    counts = [data[y * w:(y + 1) * w].count(255) for y in range(h)]
    out: List[Cluster] = []
    cur, gap = None, 0
    for y, n in enumerate(counts):
        if n:
            if cur is None:
                cur = Cluster(y, y, 0)
            cur.bottom, cur.pixels, gap = y, cur.pixels + n, 0
        elif cur is not None:
            gap += 1
            if gap > row_gap:
                out.append(cur)
                cur, gap = None, 0
    if cur is not None:
        out.append(cur)
    for c in out:
        c.blobs = _blobs_in_band(mask, c.top, c.bottom)
        if c.blobs:
            c.left, c.right = min(b.left for b in c.blobs), max(b.right for b in c.blobs)
    return out


def icon_like(b: Blob, icon_size, slack: float = 0.45) -> bool:
    iw, ih = icon_size
    w, h = b.size
    fill = b.pixels / float(w * h)
    return abs(w / iw - 1) <= slack and abs(h / ih - 1) <= slack and fill >= 0.5   # icons are solid


def find_target(img, target: dict) -> Result:
    """Pure image -> decision. Coordinates in the result are relative to the image."""
    mask = color_mask(img, _t(target, "rgb"), float(_t(target, "hue_tolerance")),
                      float(_t(target, "min_saturation")), float(_t(target, "min_value")))
    clusters = clusters_from_mask(mask)
    min_px = int(_t(target, "min_pixels"))
    shape = bool(_t(target, "shape_check")) and _t(target, "icon_size")
    valid = []
    for c in clusters:
        if c.pixels < min_px:
            continue
        icons = [b for b in c.blobs if icon_like(b, _t(target, "icon_size"))] if shape else []
        if shape and not icons:
            continue
        c.icon = icons[0] if icons else max(c.blobs, key=lambda b: b.pixels)
        valid.append(c)
    if not valid:
        why = "no yellow gate found"
        if any(c.pixels >= min_px for c in clusters):
            why += " (yellow seen, but not icon-shaped)"
        elif clusters:
            why += f" (only {len(clusters)} speck(s))"
        return Result(None, "none", why, clusters)
    largest = max(c.pixels for c in valid)
    strong = [c for c in valid if c.pixels >= 0.5 * largest]
    first = strong[0]                            # top-most strong row
    if len(strong) > 1 and strong[1].top - first.bottom > int(_t(target, "ambiguous_gap")):
        return Result(None, "ambiguous", f"found {len(strong)} yellow rows far apart, not sure, skipped",
                      clusters, None)
    x, _ = first.icon.center
    y = (first.top + first.bottom) // 2
    cx = target.get("click_x")
    if cx is not None and cx != "":
        x = max(0, min(img.size[0] - 1, int(cx)))
    score = first.pixels / float(sum(c.pixels for c in valid))
    return Result((x, y), "found", f"yellow row at y={first.top}-{first.bottom} ({first.pixels} px, "
                                   f"{score:.0%} of the yellow)", clusters, first, score)


def grab_box(box):
    """ONE picture of just this box (screen pixels). Nothing is stored."""
    from PIL import ImageGrab
    l, t, r, b = (int(v) for v in box)
    if sys.platform == "win32":
        return ImageGrab.grab(bbox=(l, t, r, b), all_screens=True)
    return ImageGrab.grab(bbox=(l, t, r, b))


def locate(target: dict, grab=grab_box) -> Tuple[Optional[Tuple[int, int]], str]:
    """Called once per spoken command. -> (screen point, message) or (None, why)."""
    box = target["box"]
    res = find_target(grab(box), target)
    if res.point is None:
        return None, res.message
    x, y = box[0] + res.point[0], box[1] + res.point[1]
    return (x, y), f"{res.message} -> clicking {x},{y}"


def sample_color(img) -> List[int]:
    """Most 'colourful' pixel in a small picture around a click (icons are thin lines)."""
    best, best_score = None, -1.0
    raw = img.convert("RGB").tobytes()
    for i in range(0, len(raw) - 2, 3):
        p = (raw[i], raw[i + 1], raw[i + 2])
        h, s, v = colorsys.rgb_to_hsv(*[c / 255.0 for c in p])
        score = s * 2 + v
        if score > best_score:
            best, best_score = list(p), score
    return best or list(DEFAULT_TARGET["rgb"])


def pick_from_image(img, click: Tuple[int, int], target: Optional[dict] = None) -> dict:
    """'Pick the yellow colour': from a small picture around the click, return the
    target fields to store: rgb, icon_size, sampled=True."""
    t = dict(DEFAULT_TARGET)
    t.update(target or {})
    t["rgb"] = sample_color(img)
    mask = color_mask(img, t["rgb"], float(t["hue_tolerance"]), float(t["min_saturation"]),
                      float(t["min_value"]))
    blobs = [b for c in clusters_from_mask(mask, row_gap=1) for b in c.blobs]
    if blobs:
        cx, cy = click
        near = min(blobs, key=lambda b: (max(b.left - cx, 0, cx - b.right) ** 2 +
                                         max(b.top - cy, 0, cy - b.bottom) ** 2, -b.pixels))
        t["icon_size"] = list(near.size)
    t["sampled"] = True
    return {k: t[k] for k in ("rgb", "icon_size", "sampled")}


def debug_image(img, res: Result):
    """Copy of the box with what it found drawn on it ('Show what it sees')."""
    from PIL import ImageDraw
    out = img.convert("RGB").copy()
    d = ImageDraw.Draw(out)
    for c in res.clusters:
        for b in c.blobs:
            d.rectangle((b.left - 1, b.top - 1, b.right + 1, b.bottom + 1), outline=(140, 140, 140))
    if res.chosen is not None:
        c = res.chosen
        d.rectangle((0, c.top - 2, out.size[0] - 1, c.bottom + 2), outline=(0, 255, 100), width=2)
    if res.point is not None:
        x, y = res.point
        d.line((x - 8, y, x + 8, y), fill=(255, 40, 40), width=2)
        d.line((x, y - 8, x, y + 8), fill=(255, 40, 40), width=2)
    return out
