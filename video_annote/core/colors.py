# video_annote/core/colors.py
"""Stable label colors.

Each label number is mapped once to a palette index that is stored in config.json, so a label keeps
its color across sessions. The first 50 indices use a hand-picked high-contrast palette; beyond that,
deterministic colors are generated that stay as far as possible from every color already in use.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

PALETTE: List[str] = [
    "#E6194B", "#3CB44B", "#FFE119", "#4363D8", "#F58231",
    "#911EB4", "#46F0F0", "#F032E6", "#BCF60C", "#FABEBE",
    "#008080", "#E6BEFF", "#9A6324", "#FFFAC8", "#800000",
    "#AAFFC3", "#808000", "#FFD8B1", "#000075", "#808080",
    "#000000", "#FF4500", "#1E90FF", "#32CD32", "#FFD700",
    "#8A2BE2", "#00CED1", "#FF1493", "#7FFF00", "#FFB6C1",
    "#20B2AA", "#BA55D3", "#B8860B", "#F0E68C", "#A52A2A",
    "#2E8B57", "#BDB76B", "#D2691E", "#4169E1", "#DC143C",
    "#00FA9A", "#9400D3", "#FF8C00", "#2F4F4F", "#ADFF2F",
    "#C71585", "#00BFFF", "#228B22", "#FF6347", "#6A5ACD",
]

RGB = Tuple[int, int, int]


def hex_to_rgb(hex_color: str) -> RGB:
    s = (hex_color or "").strip().lstrip("#")
    if len(s) != 6:
        return (0, 0, 0)
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        return (0, 0, 0)


def rgb_to_hex(rgb: RGB) -> str:
    r, g, b = (max(0, min(int(c), 255)) for c in rgb)
    return f"#{r:02X}{g:02X}{b:02X}"


def rgb_to_hsv(r: int, g: int, b: int) -> Tuple[float, float, float]:
    """r, g, b in 0..255 -> (hue degrees, saturation 0..1, value 0..1)."""
    rf, gf, bf = (max(0.0, min(c / 255.0, 1.0)) for c in (r, g, b))
    mx, mn = max(rf, gf, bf), min(rf, gf, bf)
    diff = mx - mn
    if diff <= 1e-12:
        h = 0.0
    elif mx == rf:
        h = (60.0 * ((gf - bf) / diff) + 360.0) % 360.0
    elif mx == gf:
        h = (60.0 * ((bf - rf) / diff) + 120.0) % 360.0
    else:
        h = (60.0 * ((rf - gf) / diff) + 240.0) % 360.0
    s = 0.0 if mx <= 1e-12 else diff / mx
    return (h, s, mx)


def hsv_to_rgb(h: float, s: float, v: float) -> RGB:
    h = float(h) % 360.0
    s = max(0.0, min(float(s), 1.0))
    v = max(0.0, min(float(v), 1.0))
    c = v * s
    x = c * (1.0 - abs(((h / 60.0) % 2.0) - 1.0))
    m = v - c
    if h < 60.0:
        rp, gp, bp = c, x, 0.0
    elif h < 120.0:
        rp, gp, bp = x, c, 0.0
    elif h < 180.0:
        rp, gp, bp = 0.0, c, x
    elif h < 240.0:
        rp, gp, bp = 0.0, x, c
    elif h < 300.0:
        rp, gp, bp = x, 0.0, c
    else:
        rp, gp, bp = c, 0.0, x
    return tuple(max(0, min(int(round((p + m) * 255.0)), 255)) for p in (rp, gp, bp))  # type: ignore[return-value]


_GOLDEN_ANGLE = 137.50776405003785
# (saturation, value) tiers so that neighbouring generated colors differ in more than hue
_TIERS = ((0.75, 0.95), (0.95, 0.70), (0.50, 0.90), (0.90, 0.50), (0.60, 0.75))
_generated: List[str] = []
_SEARCH_LIMIT = 100  # generated colors chosen by search; later ones use a constant-time sequence


def _distance(a: RGB, b: RGB) -> float:
    return sum((p - q) ** 2 for p, q in zip(a, b)) ** 0.5


def generated_color(gen_index: int) -> str:
    """Deterministic color for palette index ``len(PALETTE) + gen_index``.

    Candidates walk the hue circle by the golden angle across several saturation/value tiers; the
    one farthest (in RGB) from the palette and from all lower generated indices is chosen, so every
    label gets a distinct color. The search is quadratic, so beyond _SEARCH_LIMIT generated colors
    (150+ labels) a plain golden-angle sequence is used instead."""
    if gen_index >= _SEARCH_LIMIT:
        s, v = _TIERS[gen_index % len(_TIERS)]
        # positions past every search candidate (7 * _SEARCH_LIMIT + 48), so no hue is reused
        return rgb_to_hex(hsv_to_rgb(((gen_index + 8 * _SEARCH_LIMIT) * _GOLDEN_ANGLE) % 360.0, s, v))
    while len(_generated) <= gen_index:
        i = len(_generated)
        taken = [hex_to_rgb(h) for h in PALETTE] + [hex_to_rgb(h) for h in _generated]
        best, best_dist = "", -1.0
        for attempt in range(48):
            hue = ((i * 7 + attempt) * _GOLDEN_ANGLE) % 360.0
            s, v = _TIERS[(i + attempt) % len(_TIERS)]
            rgb = hsv_to_rgb(hue, s, v)
            dist = min(_distance(rgb, t) for t in taken)
            if dist > best_dist:
                best, best_dist = rgb_to_hex(rgb), dist
        _generated.append(best)
    return _generated[gen_index]


def color_for_index(idx: int) -> str:
    n = len(PALETTE)
    return PALETTE[idx % n] if idx < n else generated_color(idx - n)


def assign_color_index(step_no: int, color_map: Dict[int, int]) -> int:
    """Return the palette index for ``step_no``, allocating (and recording in ``color_map``) the
    lowest free palette slot, or a generated-color index once the palette is exhausted."""
    idx = color_map.get(int(step_no))
    if idx is not None:
        return int(idx)
    used = {int(v) for v in color_map.values()}
    idx = next((i for i in range(len(PALETTE)) if i not in used), None)
    if idx is None:
        idx = max(max(used) + 1, len(PALETTE))
    color_map[int(step_no)] = idx
    return idx


def color_for_step(step_no: Optional[int], color_map: Optional[Dict[int, int]] = None) -> str:
    """Stable color for a label number. Without a color map, falls back to a palette wrap."""
    if step_no is None:
        return PALETTE[0]
    if color_map is None:
        return PALETTE[int(step_no) % len(PALETTE)]
    return color_for_index(assign_color_index(int(step_no), color_map))
