"""
isle_core.py - the part of Isle Alert that Windows and Linux share.

Reading the HUD icons from a frame of the game, spotting game menus, the alert rules, the
"Alert in" countdown and the version number. isle_alert_windows.py (Windows) and
linux/isle_alert.py + linux/isle_alert_gui.py (Linux) import everything from here, so a fix
made here reaches both apps.
"""
import os
import tempfile
from pathlib import Path

import numpy as np

__version__ = "1.3.0"     # also the GitHub tag (v1.3.0) and the Linux package version

# --------------------------------------------------------------------------
# Where the icons are. Measured on a 2560x1440 screenshot of the game and
# scaled by window height, anchored to the bottom-right corner, so other
# resolutions should work too (as long as the in-game UI scale is unchanged).
#   x0/x1 : narrow column band through the middle of the icon
#   top   : y of the top of the icon outline
#   bottom: y of the bottom of the icon outline
# --------------------------------------------------------------------------
REF_W, REF_H = 2560, 1440
ICONS = {
    # Stomach / drop: the fill is a flat pale colour clipped at a horizontal line.
    # "edge": a side of the icon's hexagon (x, y from, y to) that must be a sharp line.
    # The game scales the fill over the icon's whole image box, which is taller than the
    # visible stomach/drop: "zero"/"hundred" are the y of the fill line at 0% and 100%.
    # Hunger was fitted to the game's own hover read-out (70.55/50.7/48.92%, within 0.3%).
    # Thirst uses the same box, centred on its hexagon (95 px lower) - not yet checked.
    # "full_at": the highest visible fill row; a line there means "this full or more".
    "Hunger": {"x0": 2404, "x1": 2414, "top": 1132, "bottom": 1206,
               "zero": 1213.3, "hundred": 1126.4, "full_at": 1137,
               "edge": (2343, 1145, 1192)},
    "Thirst": {"x0": 2444, "x1": 2448, "top": 1223, "bottom": 1307,
               "zero": 1308.3, "hundred": 1221.4, "full_at": 1228,
               "edge": (2499, 1238, 1290)},
    # O2: the whole hexagon fills with teal. The strip sits left of the "O2" text, so it
    # can see the fill line from the bottom up to about 80%; above that it reads 100%.
    # The icon is invisible while oxygen is at 100%, so "not on screen" is read as 100%.
    "Oxygen": {"x0": 2201, "x1": 2209, "top": 1145, "bottom": 1227,
               "band_top": 1163, "band_bottom": 1208, "min_ref": 80, "teal": True,
               "saturate": True, "full": 1.0, "hidden_level": 1.0,
               "present": [(2194, 1170, 2201, 1203), (2271, 1170, 2278, 1203)],   # lit hexagon edges
               "glyph": (2207, 1163, 2261, 1204)},                                # the "O2" text
}
FULL_FRACTION = 0.93   # the fill never quite reaches the outline; treat this as 100%
COLOUR_TOL = 45        # how far a pixel may differ from the fill colour

# ------------------------------ reading the icons --------------------------
def _to_px(w, h, x, y):
    """Reference (2560x1440) coordinates -> pixels of a w x h frame, bottom-right anchored."""
    s = h / REF_H
    return int(round(w - (REF_W - x) * s)), int(round(h - (REF_H - y) * s))


def _box(rgb, b):
    h, w = rgb.shape[:2]
    ax0, ay0 = _to_px(w, h, b[0], b[1])
    ax1, ay1 = _to_px(w, h, b[2], b[3])
    return rgb[max(ay0, 0):ay1 + 1, max(ax0, 0):max(ax1, ax0 + 1)].astype(np.int16)


def _icon_visible(rgb, spec):
    """True only if both lit cyan hexagon edges AND the pale glyph are there."""
    for b in spec["present"]:                        # tall thin boxes on the hexagon's sides
        px = _box(rgb, b)
        if px.size == 0:
            return False
        cyan = ((px[..., 1] >= 120) & (px[..., 2] >= 120) &
                (px[..., 1] - px[..., 0] >= 30) & (px[..., 2] - px[..., 0] >= 30))
        if cyan.any(axis=1).mean() < 0.85:           # a real edge runs the whole height
            return False
    if "glyph" in spec:
        px = _box(rgb, spec["glyph"])
        if px.size == 0:
            return False
        pale = (px[..., 0] >= 170) & (px[..., 1] >= 200) & (px[..., 2] >= 200)
        if pale.mean() < 0.10:                       # the real "O2" text covers ~29% of this box
            return False
    return True


def measure(rgb, spec):
    """Fill level 0.0-1.0 of one icon, or None if it isn't on screen / can't be read."""
    h, w = rgb.shape[:2]
    x0, band_top = _to_px(w, h, spec["x0"], spec.get("band_top", spec["top"]))
    x1, band_bot = _to_px(w, h, spec["x1"], spec.get("band_bottom", spec["bottom"]))
    _, top = _to_px(w, h, 0, spec["top"])
    _, bot = _to_px(w, h, 0, spec["bottom"])
    x1 = max(x1, x0 + 1)
    if x0 < 0 or band_top < 0 or band_bot >= h or band_bot - band_top < 12:
        return None

    if "edge" in spec and not _icon_edge_ok(rgb, spec):
        return None                                  # blurred or covered: a menu is open

    # The icon must really be on screen: its shape has to be there, not just something bright.
    if "present" in spec and not _icon_visible(rgb, spec):
        return spec.get("hidden_level")              # icon not drawn (O2: means 100%)

    band = rgb[band_top:band_bot + 1, x0:x1].astype(np.int16)    # rows x cols x 3
    n = band.shape[0]
    if "present" not in spec and band[: max(3, n // 8)].mean(axis=2).max() < 110:
        return None                                  # glyph outline missing: HUD hidden

    col = np.median(band, axis=1)                    # one colour per row
    r0, r1 = int(n * 0.86), int(n * 0.93)
    ref = np.median(col[r0:r1 + 1], axis=0)          # fill colour near the bottom
    if ref.mean() < spec.get("min_ref", 150):
        return 0.0                                   # bottom is dark -> bar (almost) empty
    if spec.get("teal") and not (ref[1] > ref[0] + 50 and ref[2] > ref[0] + 50):
        return None                                  # not the fill colour -> not the icon

    i = r0                                           # walk up while it's still "fill"
    while i > 0 and np.abs(col[i - 1] - ref).max() <= COLOUR_TOL:
        i -= 1
    if i == 0 and spec.get("saturate"):
        return 1.0                                   # fill line is above this strip
    if "zero" in spec:                               # calibrated against the game's numbers
        s = h / REF_H
        line = band_top + i                          # first filled row
        if line <= h - (REF_H - spec["full_at"]) * s + 1:
            return 1.0                               # at the top of the glyph: (nearly) full
        zero = h - (REF_H - spec["zero"]) * s
        hundred = h - (REF_H - spec["hundred"]) * s
        return float(min(1.0, max(0.0, (zero - line) / (zero - hundred))))
    level = (bot - (band_top + i)) / max(bot - top, 1)
    return float(min(1.0, max(0.0, level / spec.get("full", FULL_FRACTION))))


def dump_o2(rgb, folders):
    """Save the HUD corner as isle_o2_debug.png (to inspect a false O2 alarm), in the first of
    `folders` that can be written to. Returns the saved path, or None."""
    try:
        from PIL import Image
        h, w = rgb.shape[:2]
        corner = np.ascontiguousarray(rgb[int(h * 0.6):, int(w * 0.7):])
        for folder in folders:
            try:
                folder = Path(folder)
                folder.mkdir(parents=True, exist_ok=True)
                if not os.access(folder, os.W_OK):
                    continue
                out = folder / "isle_o2_debug.png"
                Image.fromarray(corner).save(out)
                return out
            except OSError:
                continue
    except Exception:
        pass
    return None


# ------------------------------ menus & sharp edges --------------------------
def _line_fraction(rgb, horizontal, c, a0, a1, s):
    """How much of the stretch a0..a1 carries a thin, sharp, bright cyan line along row c
    (horizontal) or column c. A few pixels of misplacement are allowed. Blurred lines,
    soft gradients and plain bright areas do not count."""
    h, w = rgb.shape[:2]
    t = max(2, int(round(3 * s)))                    # search +-t around c
    d = max(2, int(round(4 * s)))                    # contrast measured d px either side
    lo, hi = c - t - d, c + t + d + 1
    if lo < 0 or hi > (h if horizontal else w):
        return 0.0
    idx = np.arange(max(a0, 0), min(a1, w if horizontal else h), max(1, int(round(4 * s))))
    if idx.size == 0:
        return 0.0
    cut = rgb[lo:hi, idx] if horizontal else rgb[idx, lo:hi].transpose(1, 0, 2)
    cut = cut.astype(np.int16)                       # (across, along, 3)
    g, r = cut[..., 1], cut[..., 0]
    k = g[d:d + 2 * t + 1].argmax(axis=0) + d        # brightest pixel near c, per position
    cols = np.arange(g.shape[1])
    p = g[k, cols]
    ok = ((p >= 120) & (p - r[k, cols] >= 30) &
          (p - g[k - d, cols] >= 35) & (p - g[k + d, cols] >= 35))
    return float(ok.mean())


def _icon_edge_ok(rgb, spec):
    """The hexagon around the icon must be a sharp line. In the Escape menu the HUD is
    blurred and in the Status Report it is covered - then the icon can't be trusted."""
    h, w = rgb.shape[:2]
    ex, ey0, ey1 = spec["edge"]
    x, y0 = _to_px(w, h, ex, ey0)
    _, y1 = _to_px(w, h, ex, ey1)
    return _line_fraction(rgb, False, x, y0, y1, h / REF_H) >= 0.7


# Escape menu: the outlines of its buttons (RESUME ... QUIT) on the left of the screen.
ESC_LINES = (814, 897, 914, 998, 1114, 1198, 1214, 1297, 1314, 1398)
# Status Report (and its Group / Mutations / Nest tabs): full-width frame lines.
FRAME_TOP, FRAME_BOTTOM = (9,), (1297, 1322, 1404)


def detect_menu(rgb):
    """Name of the game menu on screen, or None during normal play."""
    h, w = rgb.shape[:2]
    s = h / REF_H
    x0, x1 = int(70 * s), int(440 * s)
    hits = sum(_line_fraction(rgb, True, int(round(h - (REF_H - y) * s)), x0, x1, s) >= 0.8
               for y in ESC_LINES)
    if hits >= 7:
        return "Escape menu"
    rows = ([int(round(y * s)) for y in FRAME_TOP] +
            [int(round(h - (REF_H - y) * s)) for y in FRAME_BOTTOM])
    hits = sum(_line_fraction(rgb, True, y, int(w * 0.06), int(w * 0.94), s) >= 0.8
               for y in rows)
    if hits >= 3:
        return "Status report"
    return None


def measure_all(rgb):
    levels = {name: measure(rgb, spec) for name, spec in ICONS.items()}
    if levels.get("Hunger") is None and levels.get("Thirst") is None:
        levels["Oxygen"] = None                      # whole HUD hidden: O2 can't be judged
    return levels


def read_frame(rgb):
    """(menu name or None, levels). Nothing is measured while a game menu is open."""
    menu = detect_menu(rgb)
    if menu:
        return menu, {name: None for name in ICONS}
    return None, measure_all(rgb)


class DrainTracker:
    """Estimates how long each stat has left before it reaches a level (0% or your alert level).

    The icon can only be read to whole pixels (~1.2% each), so instead of fitting a line through
    every (stepped) reading, it notes the moments the fill line drops to a new pixel row. Those
    moments are exact: the rate comes from them, and between them the countdown just runs down.
    A new lower reading must be seen twice in a row (a single odd frame is ignored); a jump up
    means you ate / drank / got air, and it starts over."""

    WINDOW = {"Hunger": 600, "Thirst": 600, "Oxygen": 45}    # seconds of drops used for the rate
    MIN_STEPS = 2                                            # pixel drops needed before estimating
    RISE = 0.03                                              # a jump up this big = refilled
    CONFIRM = 2                                              # readings in a row to accept a drop
    SMOOTH = 0.35                                            # how fast a new rate takes over

    def __init__(self):
        self.level = {}      # name -> accepted (debounced) level
        self.pending = {}    # name -> (lower level waiting for confirmation, count)
        self.drops = {}      # name -> [(time, level)] moments the level stepped down
        self.smooth = {}     # name -> smoothed rate

    def _reset(self, name, level):
        self.level[name] = level
        self.pending.pop(name, None)
        self.drops[name] = []
        self.smooth.pop(name, None)

    def add(self, levels, now):
        for name, level in levels.items():
            if level is None:
                continue                                     # unreadable: keep what we have
            cur = self.level.get(name)
            if level >= 0.999 or cur is None or level > cur + self.RISE:
                self._reset(name, level)                     # full, first reading, or refilled
                continue
            if level >= cur - 1e-6:                          # same, or flickered up a pixel
                self.pending.pop(name, None)
                continue
            p_level, count = self.pending.get(name, (None, 0))
            count = count + 1 if p_level is not None and abs(p_level - level) < 1e-6 else 1
            self.pending[name] = (level, count)
            if count < self.CONFIRM:
                continue
            self.pending.pop(name, None)
            self.level[name] = level
            d = self.drops.setdefault(name, [])
            d.append((now, level))
            keep = self.WINDOW.get(name, 600)
            while len(d) > self.MIN_STEPS + 1 and d[0][0] < now - keep:
                d.pop(0)
            r = self._raw_rate(name)
            if r:                                            # smoothed, so one early or late
                prev = self.smooth.get(name)                 # pixel doesn't jolt the countdown
                self.smooth[name] = r if prev is None else prev + self.SMOOTH * (r - prev)

    def rate(self, name):
        """Smoothed level lost per second (None if not enough drops seen yet)."""
        return self.smooth.get(name) if len(self.drops.get(name) or []) >= self.MIN_STEPS + 1 else None

    def _raw_rate(self, name):
        """Level lost per second, from the recent pixel drops (None if not enough yet)."""
        d = self.drops.get(name) or []
        if len(d) < self.MIN_STEPS + 1:
            return None
        t = np.array([p[0] for p in d], dtype=float)
        v = np.array([p[1] for p in d], dtype=float)
        slope = np.polyfit(t - t[-1], v, 1)[0]
        return -slope if slope < 0 else None

    def eta(self, name, now=None, target=0.0):
        """Seconds until this stat drops to target (0.0-1.0, default 0%), or None if it
        can't be told yet."""
        r = self.rate(name)
        if not r:
            return None
        t_last, l_last = self.drops[name][-1]
        since = max(0.0, (now if now is not None else t_last) - t_last)
        # the level was exactly l_last at t_last; it can't be below the next pixel yet
        step = (self.drops[name][0][1] - l_last) / (len(self.drops[name]) - 1)
        current = max(l_last - r * since, l_last - step)
        return max(0.0, (current - target) / r)


def format_eta(seconds):
    """'~40 s', '~12 min', '~1 h 05 min'."""
    if seconds is None:
        return ""
    s = int(round(seconds))
    if s < 60:
        return f"~{s} s"
    m = (s + 30) // 60
    return f"~{m} min" if m < 60 else f"~{m // 60} h {m % 60:02d} min"


def countdown_text(drain, name, level, threshold, now):
    """What the window shows next to a bar: time until the alert level (threshold in %),
    or once it's at/below that level, time until empty. "-" while it can't be told."""
    if level is None:
        return "-"
    if level > threshold / 100.0:
        return format_eta(drain.eta(name, now, threshold / 100.0)) or "-"
    eta = format_eta(drain.eta(name, now))
    return f"empty {eta}" if eta else "-"


class AlertLogic:
    """Alert when a stat drops below its threshold, repeat while it stays low."""

    MESSAGES = {"Hunger": "Your dinosaur needs to eat soon.",
                "Thirst": "Your dinosaur needs to drink soon.",
                "Oxygen": "Your dinosaur is running out of air - get to the surface!"}

    CONFIRM = {"Hunger": 2, "Thirst": 2, "Oxygen": 2}   # low readings in a row before alerting

    def __init__(self, notifier, thresholds, repeats, blind_after):
        self.n, self.th, self.repeats, self.blind_after = notifier, thresholds, repeats, blind_after
        self.last_alert = {}
        self.streak = {}
        self.unreadable_since = None
        self.told_blind = False
        self.drain = DrainTracker()

    def paused(self):
        """A game menu is open: nothing on screen can be trusted, so change nothing."""
        self.streak.clear()
        self.unreadable_since = None

    def update(self, levels, now):
        self.drain.add(levels, now)
        if all(v is None for k, v in levels.items() if k != "Oxygen"):   # O2 can't tell us
            self.streak.clear()
            self.unreadable_since = self.unreadable_since or now
            if now - self.unreadable_since > self.blind_after and not self.told_blind:
                self.told_blind = True
                self.n.send("blind", "Can't see the HUD icons",
                            "Is the HUD hidden, or is this the wrong window?")
            return
        self.unreadable_since, self.told_blind = None, False

        for name, level in levels.items():
            if level is None:
                self.streak[name] = 0
                if name == "Oxygen":                     # icon gone again = air is fine
                    self.last_alert.pop(name, None)
                continue
            limit = self.th[name] / 100.0
            if level > limit:
                self.streak[name] = 0
            if level <= limit:
                self.streak[name] = self.streak.get(name, 0) + 1
                if self.streak[name] < self.CONFIRM.get(name, 1):
                    continue
                last, every = self.last_alert.get(name), self.repeats.get(name, 0)
                if last is None or (every and now - last >= every):    # every=0 -> only once
                    self.last_alert[name] = now
                    eta = format_eta(self.drain.eta(name, now))
                    self.n.send(name, f"{name} is low: {level_text(name, level)}" +
                                (f" - empty in {eta}" if eta else ""),
                                self.MESSAGES.get(name, ""))
            elif level > limit + 0.10:                   # recovered -> re-arm
                self.last_alert.pop(name, None)


def full_floor(name):
    """For hunger/thirst: the % at which the fill line reaches the top of the visible glyph.
    Above it the icon looks full, so readings there mean 'at least this much'."""
    spec = ICONS.get(name, {})
    if "full_at" not in spec:
        return None
    return round((spec["zero"] - spec["full_at"]) / (spec["zero"] - spec["hundred"]) * 100)


def level_text(name, level):
    """How a reading is shown: '51%', or '88%+' when the icon looks full."""
    if level is None:
        return "unreadable"
    floor = full_floor(name)
    if floor is not None and level >= 0.999:
        return f"{floor}%+"
    return f"{level * 100:.0f}%"


# ------------------------------ test mode ----------------------------------
def save_png(rgb, path):
    from PIL import Image
    Image.fromarray(rgb).save(path)
    print("saved", path)


def run_test(path):
    """--test: measure a screenshot and print what it reads. The check image (the HUD corner
    with the sampled strips in red) goes to the temp folder, not next to your screenshot."""
    from PIL import Image
    rgb = np.array(Image.open(path).convert("RGB"))
    menu, levels = read_frame(rgb)
    print(f"Game menu: {menu or 'none (normal play)'}")
    if menu:
        levels = measure_all(rgb)                        # show what it would read anyway
    for name, level in levels.items():
        print(f"{name}: {'not on screen / unreadable' if level is None else level_text(name, level)}")
    h, w = rgb.shape[:2]
    img = rgb.copy()
    for spec in ICONS.values():                          # draw the sampled bands in red
        x0, bt = _to_px(w, h, spec["x0"], spec.get("band_top", spec["top"]))
        x1, bb = _to_px(w, h, spec["x1"], spec.get("band_bottom", spec["bottom"]))
        x1 = max(x1, x0 + 1)
        if x0 < 2 or bt < 0 or bb >= h:
            continue
        img[bt:bb + 1, x0 - 2:x0] = (255, 0, 0)
        img[bt:bb + 1, x1:x1 + 2] = (255, 0, 0)
    out = Path(tempfile.gettempdir()) / "isle_alert_check.png"
    save_png(img[int(h * 0.7):, int(w * 0.8) - 200:], out)
    return levels
