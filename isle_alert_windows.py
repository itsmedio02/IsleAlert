#!/usr/bin/env python3
"""
isle_alert_windows.py - hunger / thirst / O2 alert for The Isle on Windows 10/11.

A small window that watches the game window (even when other apps cover it, using
Windows' own Graphics Capture) and gives you a Windows pop-up notification plus a
sound when hunger, thirst or oxygen drops below the level you set.
It only reads the pixels of the HUD icons - nothing is injected into the game.

Install (once):   pip install windows-capture numpy winotify pillow
Put Hungry.wav, Thirsty.wav and Oxygen.wav in the same folder as this script. Hunger and
thirst alert once, when the bar reaches the threshold (40%); they re-arm after you eat/drink.
Oxygen plays its sound every 15 seconds while it stays low.
With --debug, whenever the O2 icon is seen below 100% it saves isle_o2_debug.png next to
the script, so you can check what the program actually saw.
Run:              python isle_alert_windows.py
Other modes:      python isle_alert_windows.py --console --window "The Isle"
                  python isle_alert_windows.py --test screenshot.png
                  python isle_alert_windows.py --notify-test
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

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
    "Hunger": {"x0": 2404, "x1": 2414, "top": 1132, "bottom": 1206,
               "edge": (2343, 1145, 1192)},
    "Thirst": {"x0": 2444, "x1": 2448, "top": 1223, "bottom": 1307,
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
    level = (bot - (band_top + i)) / max(bot - top, 1)
    return float(min(1.0, max(0.0, level / spec.get("full", FULL_FRACTION))))


def dump_o2(rgb):
    """Save the HUD corner as isle_o2_debug.png next to the script (to inspect a false O2 alarm)."""
    try:
        from PIL import Image
        h, w = rgb.shape[:2]
        corner = np.ascontiguousarray(rgb[int(h * 0.6):, int(w * 0.7):])
        Image.fromarray(corner).save(SCRIPT_DIR / "isle_o2_debug.png")
    except Exception:
        pass


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

    def paused(self):
        """A game menu is open: nothing on screen can be trusted, so change nothing."""
        self.streak.clear()
        self.unreadable_since = None

    def update(self, levels, now):
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
                    self.n.send(name, f"{name} is low: {level * 100:.0f}%",
                                self.MESSAGES.get(name, ""))
            elif level > limit + 0.10:                   # recovered -> re-arm
                self.last_alert.pop(name, None)



# ------------------------------ Windows pieces ------------------------------
SETTINGS_FILE = Path(os.environ.get("APPDATA", str(Path.home()))) / "isle-alert" / "settings.json"
DEFAULTS = {"window": "", "hunger": 40, "thirst": 40, "oxygen": 60, "front": False, "sounds": {},
            "volume": 100}
SOUNDS_DIR = SETTINGS_FILE.parent / "sounds"                    # copies of the sounds you picked
REPEATS = {"Hunger": 0, "Thirst": 0, "Oxygen": 15}          # seconds between repeats; 0 = only once
SCRIPT_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
SOUNDS = {"Hunger": "Hungry.wav", "Thirst": "Thirsty.wav",
          "Oxygen": "Oxygen.wav"}                        # keep these next to the script/exe


def load_settings():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    if not isinstance(cfg.get("sounds"), dict):
        cfg["sounds"] = {}
    return cfg


def _volume(cfg):
    try:
        return max(0, min(100, int(cfg.get("volume", 100))))
    except (TypeError, ValueError):
        return 100


def save_settings(cfg):
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(cfg), encoding="utf-8")
    except Exception as exc:
        print("could not save settings:", exc, file=sys.stderr)


def _find_hwnd(title):
    """Handle of the top-level window with this title (exact match first, then partial)."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, title)
    if hwnd:
        return hwnd
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum(h, _lparam):
        n = user32.GetWindowTextLengthW(h)
        if n and user32.IsWindowVisible(h):
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(h, buf, n + 1)
            if title.lower() in buf.value.lower():
                found.append(h)
                return False
        return True

    user32.EnumWindows(enum, 0)
    return found[0] if found else None


def client_crop(title, frame_w, frame_h):
    """(left, top, width, height) of the game's picture inside a captured frame, or None.
    In windowed mode the capture includes the title bar and borders; the HUD positions are
    measured from the game's own picture, so those have to be cut off first."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        hwnd = _find_hwnd(title)
        if not hwnd:
            return None
        client = wintypes.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(client)):
            return None
        origin = wintypes.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(origin))
        frame = wintypes.RECT()                        # the visible window, as Windows captures it
        if ctypes.windll.dwmapi.DwmGetWindowAttribute(
                hwnd, 9, ctypes.byref(frame), ctypes.sizeof(frame)):   # DWMWA_EXTENDED_FRAME_BOUNDS
            user32.GetWindowRect(hwnd, ctypes.byref(frame))
        return crop_box(frame_w, frame_h, (frame.left, frame.top, frame.right, frame.bottom),
                        (origin.x, origin.y, client.right, client.bottom))
    except Exception:
        return None


def crop_box(frame_w, frame_h, window, client):
    """Pure maths for client_crop. window = (left, top, right, bottom) on screen,
    client = (x, y, width, height) on screen. Windows may report these in scaled (DPI)
    units, so everything is converted to the frame's own pixels."""
    wl, wt, wr, wb = window
    cx, cy, cw, ch = client
    if wr - wl <= 0 or wb - wt <= 0 or cw <= 0 or ch <= 0:
        return None
    sx, sy = frame_w / (wr - wl), frame_h / (wb - wt)
    left, top = int(round((cx - wl) * sx)), int(round((cy - wt) * sy))
    width, height = int(round(cw * sx)), int(round(ch * sy))
    left, top = max(0, left), max(0, top)
    width, height = min(width, frame_w - left), min(height, frame_h - top)
    if width < frame_w * 0.5 or height < frame_h * 0.5:
        return None                                    # nonsense: use the whole frame
    if (left, top, width, height) == (0, 0, frame_w, frame_h):
        return None                                    # fullscreen / borderless: nothing to cut
    return left, top, width, height


def window_titles():
    """Titles of all visible windows (for the drop-down)."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        titles = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def enum(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd):
                n = user32.GetWindowTextLengthW(hwnd)
                if n:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    user32.GetWindowTextW(hwnd, buf, n + 1)
                    titles.append(buf.value)
            return True

        user32.EnumWindows(enum, 0)
        return sorted(set(titles), key=str.lower)
    except Exception:
        return []


def custom_sounds(cfg):
    """{alert: file} for the sounds you picked in the window (only files that still exist)."""
    out = {}
    sounds = cfg.get("sounds")
    for key, entry in (sounds.items() if isinstance(sounds, dict) else ()):
        path = entry.get("file") if isinstance(entry, dict) else entry
        if isinstance(path, str) and Path(path).is_file():
            out[key] = path
    return out


def _wav_ok(path):
    """True for an ordinary (PCM) WAV file, which winsound plays directly."""
    import wave
    try:
        with wave.open(str(path)) as w:
            return w.getnframes() > 0
    except Exception:
        return False


def _mci(cmd):
    """Send one command to Windows' built-in media player (MCI). Raises on errors."""
    import ctypes
    buf = ctypes.create_unicode_buffer(256)
    err = ctypes.windll.winmm.mciSendStringW(cmd, buf, 255, 0)
    if err:
        msg = ctypes.create_unicode_buffer(256)
        ctypes.windll.winmm.mciGetErrorStringW(err, msg, 255)
        raise RuntimeError(msg.value or f"error {err}")
    return buf.value


_mci_count = [0]


def _mci_alias():
    _mci_count[0] += 1
    return f"isle{os.getpid()}n{_mci_count[0]}"


def mci_length(path):
    """Length in seconds of a sound Windows can play (MP3, WMA...). Raises if it can't."""
    alias = _mci_alias()
    _mci(f'open "{path}" type mpegvideo alias {alias}')
    try:
        _mci(f"set {alias} time format milliseconds")
        return int(_mci(f"status {alias} length") or 0) / 1000
    finally:
        try:
            _mci(f"close {alias}")
        except Exception:
            pass


def play_mci(path, volume=100):
    """Play an MP3/WMA/... file in the background. Returns the thread playing it."""
    import threading

    def run():
        alias = _mci_alias()
        try:
            _mci(f'open "{path}" type mpegvideo alias {alias}')
        except Exception as exc:
            print("could not play", path, "-", exc, file=sys.stderr)
            return
        try:
            _mci(f"set {alias} time format milliseconds")
            if volume < 100:
                try:
                    _mci(f"setaudio {alias} volume to {int(_gain(volume) * 1000)}")
                except Exception:
                    pass                               # plays at full volume instead
            length = int(_mci(f"status {alias} length") or 0)
            _mci(f"play {alias} from 0 to {length}")
            time.sleep(length / 1000 + 0.2)
        except Exception as exc:
            print("could not play", path, "-", exc, file=sys.stderr)
        finally:
            try:
                _mci(f"close {alias}")
            except Exception:
                pass

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def check_sound(path):
    """None if Windows can play this file as an alert sound, otherwise why not."""
    path = Path(path)
    if path.suffix.lower() == ".wav" and _wav_ok(path):
        return None
    try:
        if mci_length(path) > 0.05:
            return None
        return "No sound was found in it."
    except Exception as exc:
        return f"Windows couldn't play it ({exc})."


def _gain(volume):
    """Slider 0-100 -> loudness factor. Squared, so the slider feels even to the ear."""
    return max(0.0, min(1.0, volume / 100.0)) ** 2


def quieter_wav(path, volume):
    """The WAV file's bytes with its samples turned down to this volume, or None if it
    can't be done (then it plays at full volume)."""
    import io
    import wave
    try:
        with wave.open(str(path)) as w:
            params, raw = w.getparams(), w.readframes(w.getnframes())
        g, width = _gain(volume), params.sampwidth
        if width == 1:                                   # 8-bit: unsigned, centred on 128
            a = np.frombuffer(raw, np.uint8).astype(np.float32) - 128
            out = (np.clip(a * g, -128, 127) + 128).astype(np.uint8).tobytes()
        elif width == 2:
            a = np.frombuffer(raw, "<i2").astype(np.float32)
            out = np.clip(a * g, -32768, 32767).astype("<i2").tobytes()
        elif width == 3:                                 # 24-bit: widen to 32, scale, narrow
            b = np.frombuffer(raw, np.uint8).reshape(-1, 3)
            a = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) |
                 (b[:, 2].astype(np.int32) << 16))
            a = np.where(a >= 1 << 23, a - (1 << 24), a)
            a = np.clip(a * g, -(1 << 23), (1 << 23) - 1).astype(np.int32)
            out = a.astype("<i4").view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
        elif width == 4:
            a = np.frombuffer(raw, "<i4").astype(np.float64)
            out = np.clip(a * g, -(1 << 31), (1 << 31) - 1).astype("<i4").tobytes()
        else:
            return None
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setparams(params)
            w.writeframes(out)
        return buf.getvalue()
    except Exception as exc:
        print("could not change the volume of", path, "-", exc, file=sys.stderr)
        return None


class WinNotifier:
    """Windows toast notification + the alert's sound."""

    def __init__(self, custom=None, volume=100):
        self.on_alert = None                  # optional callback (used by the window)
        self.custom = dict(custom or {})      # sounds you picked replace the built-in ones
        self.threads = {}
        self.volume = volume                  # 0-100, for all alert sounds
        self._quiet = {}                      # (file, size, mtime, volume) -> WAV bytes

    def _play_wav(self, winsound, path):
        if self.volume >= 100:
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
            return
        if self.volume <= 0:
            return
        st = path.stat()
        key = (str(path), st.st_size, st.st_mtime, self.volume)
        if key not in self._quiet:
            self._quiet = {key: quieter_wav(path, self.volume)}    # keep only the newest
        data = self._quiet[key]
        if data is None:
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
            return
        import threading                               # winsound can't play memory async
        threading.Thread(target=winsound.PlaySound, args=(data, winsound.SND_MEMORY),
                         daemon=True).start()

    def _file_for(self, key):
        mine = self.custom.get(key)
        if mine and Path(mine).is_file():
            return Path(mine)
        wav = SCRIPT_DIR / SOUNDS[key] if key in SOUNDS else None
        if wav and wav.exists():
            return wav
        if wav:
            print(f"{wav.name} not found next to the script - using the default sound",
                  file=sys.stderr)
        return None

    def play(self, key):
        """Play the sound for this alert. An MP3 that is still playing is not started again."""
        try:
            import winsound
            path = self._file_for(key)
            if path is None:
                winsound.PlaySound("SystemExclamation", winsound.SND_ALIAS | winsound.SND_ASYNC)
            elif path.suffix.lower() == ".wav" and _wav_ok(path):
                self._play_wav(winsound, path)
            elif self.volume > 0:
                t = self.threads.get(key)
                if t is None or not t.is_alive():
                    self.threads[key] = play_mci(path, self.volume)
        except Exception as exc:
            print("could not play the sound:", exc, file=sys.stderr)

    def send(self, key, title, body):
        print(f"[ALERT] {title} - {body}", flush=True)
        self.play(key)
        try:
            from winotify import Notification, audio
            toast = Notification(app_id="The Isle alert", title=title, msg=body, duration="long")
            toast.set_audio(audio.Silent, loop=False)     # the beep above is the sound
            toast.show()
        except Exception as exc:
            print("Toast notification failed (pip install winotify):", exc, file=sys.stderr)
        if self.on_alert:
            self.on_alert()


class Watcher:
    """Captures the game window in a background thread and feeds AlertLogic."""

    def __init__(self, title, logic, interval, notifier, debug=False):
        self.title, self.logic, self.interval = title, logic, interval
        self.notifier, self.debug = notifier, debug
        self.levels, self.status = {}, "Starting..."
        self.last_frame, self.last_read = time.time(), 0.0
        self.stalled, self.control = False, None
        self.crop, self.crop_for, self.crop_time = None, None, 0.0

    def _on_frame(self, frame):
        now = time.time()
        self.last_frame, self.stalled = now, False
        # Frames come about once per interval (see start), so allow a little slack: a frame
        # a few ms early must not be skipped, or readings would come twice as far apart.
        if now - self.last_read < self.interval * 0.8:
            return
        self.last_read = now
        buf = np.asarray(frame.frame_buffer)           # BGRA
        if buf.ndim != 3:
            buf = buf.reshape(frame.height, frame.width, 4)
        rgb = buf[:, :, 2::-1]                         # BGR -> RGB (a view, no copy)
        fh, fw = rgb.shape[:2]
        if (fw, fh) != self.crop_for or now - self.crop_time > 5:
            self.crop = client_crop(self.title, fw, fh)  # windowed: cut off title bar/borders
            self.crop_for, self.crop_time = (fw, fh), now
        if self.crop:
            x, y, cw, ch = self.crop
            rgb = rgb[y:y + ch, x:x + cw]
        menu, levels = read_frame(rgb)
        if menu:                                       # keep the last real readings on show
            self.logic.paused()
            self.status = f"{menu} open - alerts paused"
            return
        self.levels = levels
        o2 = self.levels.get("Oxygen")
        if self.debug and o2 is not None and o2 < 0.999 and now - getattr(self, "o2_dump", 0) > 30:
            self.o2_dump = now
            dump_o2(rgb)                                 # lets you check what it "saw"
        self.status = f"Watching ({rgb.shape[1]}x{rgb.shape[0]})"
        if self.debug:
            print({k: None if v is None else round(v * 100) for k, v in self.levels.items()},
                  flush=True)
        self.logic.update(self.levels, now)

    def _attach(self, capture):
        @capture.event
        def on_frame_arrived(frame, control):
            try:
                self._on_frame(frame)
            except Exception as exc:                   # never kill the capture thread
                self.status = f"Read error: {exc}"

        @capture.event
        def on_closed():
            self.control = None
            self.status = "The game window was closed - capture ended."
            self.notifier.send("capture", "Isle alert stopped", "The game window was closed.")

    def start(self):
        try:
            from windows_capture import WindowsCapture
        except ImportError:
            raise RuntimeError("The 'windows-capture' package is missing.\n"
                               "Run:  pip install windows-capture")
        errors = []
        # Only one frame per interval is read, and every frame Windows delivers is a full
        # copy of the game window (~15 MB at 1440p), so ask for no more than that.
        # Windows versions without this option fall through to the plain variants.
        every_ms = max(100, int(self.interval * 1000))
        for extra in ({"cursor_capture": False, "draw_border": False,
                       "minimum_update_interval": every_ms},
                      {"cursor_capture": False, "draw_border": False}, {}):
            try:                                       # newer options first, plain as a fallback
                capture = WindowsCapture(window_name=self.title, **extra)
                self._attach(capture)
                self.control = capture.start_free_threaded()
                self.last_frame = time.time()
                self.status = "Waiting for frames..."
                return
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
        raise RuntimeError("Could not capture that window.\n" + "\n".join(dict.fromkeys(errors)))

    def stop(self):
        if self.control:
            try:
                self.control.stop()
            except Exception:
                pass
            self.control = None

    def check_stalled(self):
        if self.control and not self.stalled and time.time() - self.last_frame > 90:
            self.stalled = True
            self.status = "No frames - is the game minimised or paused?"
            self.notifier.send("stalled", "No frames from the game window",
                               "The game may be minimised or paused - alerts are not working.")



def save_png(rgb, path):
    from PIL import Image
    Image.fromarray(rgb).save(path)
    print("saved", path)


def run_test(args):
    from PIL import Image
    rgb = np.array(Image.open(args.test).convert("RGB"))
    menu, levels = read_frame(rgb)
    print(f"Game menu: {menu or 'none (normal play)'}")
    if menu:
        levels = measure_all(rgb)                        # show what it would read anyway
    for name, level in levels.items():
        print(f"{name}: {'not on screen / unreadable' if level is None else f'{level * 100:.0f}%'}")
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
    out = Path(args.test).with_name("isle_alert_check.png")
    save_png(img[int(h * 0.7):, int(w * 0.8) - 200:], out)


def run_gui(args):
    import shutil
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    cfg = load_settings()
    sounds = cfg["sounds"]
    notifier = WinNotifier(custom_sounds(cfg), volume=_volume(cfg))
    flag = {"front": False}
    notifier.on_alert = lambda: flag.update(front=True)
    state = {"watcher": None}

    root = tk.Tk()
    root.title("The Isle alert")
    root.resizable(False, False)
    frm = ttk.Frame(root, padding=12)
    frm.grid()

    # Two tabs keep the window small: alerts (window, thresholds, live levels) and sounds.
    tabs = ttk.Notebook(frm)
    tabs.grid(row=0, column=0, columnspan=4, sticky="we")
    alerts = ttk.Frame(tabs, padding=10)
    sound_tab = ttk.Frame(tabs, padding=10)
    tabs.add(alerts, text="Alerts")
    tabs.add(sound_tab, text="Sounds")

    ttk.Label(alerts, text="Game window").grid(row=0, column=0, sticky="w")
    win_var = tk.StringVar(value=cfg["window"])
    combo = ttk.Combobox(alerts, textvariable=win_var, width=40)
    combo.grid(row=0, column=1, columnspan=2, sticky="we")

    def refresh_windows():
        titles = window_titles()
        combo["values"] = titles
        if not win_var.get():
            for t in titles:
                if "isle" in t.lower():
                    win_var.set(t)
                    break

    ttk.Button(alerts, text="Refresh", command=refresh_windows).grid(row=0, column=3, padx=(6, 0))
    refresh_windows()

    ttk.Label(alerts, text="Alert at or below (%)").grid(row=1, column=1, sticky="w", pady=(10, 2))
    stats = {}
    for i, name in enumerate(("Hunger", "Thirst", "Oxygen")):
        r = 2 + i
        ttk.Label(alerts, text=name).grid(row=r, column=0, sticky="w", pady=2)
        var = tk.IntVar(value=cfg[name.lower()])
        ttk.Spinbox(alerts, from_=0, to=100, width=5, textvariable=var).grid(row=r, column=1, sticky="w")
        bar = ttk.Progressbar(alerts, length=150, maximum=100)
        bar.grid(row=r, column=2, padx=6)
        lab = ttk.Label(alerts, text="-", width=11)
        lab.grid(row=r, column=3, sticky="w")
        stats[name] = (var, bar, lab)

    # ---- alert sounds: pick your own, preview, back to default
    box = sound_tab
    sound_labels = {}
    for i, name in enumerate(("Hunger", "Thirst", "Oxygen")):
        ttk.Label(box, text=name, width=8).grid(row=i, column=0, sticky="w", pady=2)
        lab = ttk.Label(box, text="", width=30)
        lab.grid(row=i, column=1, sticky="w")
        sound_labels[name] = lab
        ttk.Button(box, text="Choose...", command=lambda n=name: choose_sound(n)
                   ).grid(row=i, column=2, padx=(6, 0))
        ttk.Button(box, text="\u25b6", width=3, command=lambda n=name: notifier.play(n)
                   ).grid(row=i, column=3, padx=(4, 0))
        ttk.Button(box, text="Default", command=lambda n=name: reset_sound(n)
                   ).grid(row=i, column=4, padx=(4, 0))

    ttk.Label(box, text="Volume", width=8).grid(row=3, column=0, sticky="w", pady=(8, 0))
    vol_var = tk.IntVar(value=notifier.volume)
    vol_lab = ttk.Label(box, text=f"{notifier.volume}%", width=5)

    def on_volume(value):
        notifier.volume = int(round(float(value)))
        vol_lab.config(text=f"{notifier.volume}%")

    vol = ttk.Scale(box, from_=0, to=100, orient="horizontal", length=190,
                    variable=vol_var, command=on_volume)
    vol.grid(row=3, column=1, sticky="w", pady=(8, 0))
    vol_lab.grid(row=3, column=2, sticky="w", padx=(6, 0), pady=(8, 0))
    ttk.Button(box, text="\u25b6", width=3, command=lambda: notifier.play("Hunger")
               ).grid(row=3, column=3, padx=(4, 0), pady=(8, 0))
    vol.bind("<ButtonRelease-1>", lambda _e: persist())

    front_var = tk.BooleanVar(value=bool(cfg["front"]))
    ttk.Checkbutton(alerts, text="Also pop this window to the front on an alert",
                    variable=front_var).grid(row=5, column=0, columnspan=4, sticky="w", pady=(10, 0))
    status = ttk.Label(frm, text="Not running", foreground="#555")
    status.grid(row=2, column=0, columnspan=4, sticky="w", pady=(8, 0))

    def refresh_sounds():
        mine = custom_sounds({"sounds": sounds})
        for name, lab in sound_labels.items():
            if name in mine:
                text = sounds[name].get("name") or os.path.basename(mine[name])
            else:
                text = f"Default ({SOUNDS.get(name, 'system sound')})"
            lab.config(text=text if len(text) <= 30 else text[:28] + "\u2026")
        notifier.custom = mine

    def remove_copies(name, keep=None):
        for p in SOUNDS_DIR.glob(f"{name}_*"):
            if keep is None or p != keep:
                try:
                    p.unlink()
                except OSError:
                    pass                               # still playing: removed next time

    def choose_sound(name):
        src = filedialog.askopenfilename(
            parent=root, title=f"Choose the {name.lower()} alert sound",
            filetypes=[("Sound files", "*.wav *.mp3 *.wma *.m4a *.aac *.flac"),
                       ("All files", "*.*")])
        if not src:
            return
        problem = check_sound(src)
        if problem:
            messagebox.showerror(f"Couldn't use {os.path.basename(src)}",
                                 f"{problem}\n\nWAV and MP3 files always work.")
            return
        try:
            SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
            dest = SOUNDS_DIR / f"{name}_{int(time.time() * 1000)}{Path(src).suffix.lower()}"
            shutil.copyfile(src, dest)                 # keeps working if the original moves
        except OSError as exc:
            messagebox.showerror("Couldn't copy the sound", str(exc))
            return
        sounds[name] = {"file": str(dest), "name": os.path.basename(src)}
        remove_copies(name, keep=dest)
        refresh_sounds()
        persist()
        status.config(text=f"{name} alert will now play {os.path.basename(src)}.")
        notifier.play(name)                            # let you hear it straight away

    def reset_sound(name):
        sounds.pop(name, None)
        remove_copies(name)
        refresh_sounds()
        persist()
        status.config(text=f"{name} alert is back to the built-in sound.")

    refresh_sounds()

    def thresholds():
        out = {}
        for name, (var, _bar, _lab) in stats.items():
            try:
                out[name] = max(0, min(100, int(var.get())))
            except (tk.TclError, ValueError):
                out[name] = DEFAULTS[name.lower()]
        return out

    def persist():
        t = thresholds()
        save_settings({"window": win_var.get().strip(), "hunger": t["Hunger"],
                       "thirst": t["Thirst"], "oxygen": t["Oxygen"], "front": front_var.get(),
                       "sounds": sounds, "volume": notifier.volume})

    def stop():
        if state["watcher"]:
            state["watcher"].stop()
            state["watcher"] = None
        toggle.config(text="Start watching")
        status.config(text="Stopped")

    def start():
        title = win_var.get().strip()
        if not title:
            messagebox.showwarning("Pick the game window", "Choose The Isle's window first.")
            return
        logic = AlertLogic(notifier, thresholds(), REPEATS, blind_after=300)
        watcher = Watcher(title, logic, args.interval, notifier, debug=args.debug)
        try:
            watcher.start()
        except Exception as exc:
            messagebox.showerror("Could not start",
                                 f"{exc}\n\nIs the game running, and not minimised?")
            return
        state["watcher"] = watcher
        persist()
        toggle.config(text="Stop")

    toggle = ttk.Button(frm, text="Start watching",
                        command=lambda: stop() if state["watcher"] else start())
    toggle.grid(row=1, column=0, columnspan=2, sticky="we", pady=(10, 0))
    def test_alert():                                   # hunger, thirst, then oxygen sound
        notifier.send("Hunger", "Hunger test", "If you can see/hear this, alerts work.")
        root.after(2500, lambda: notifier.send(
            "Thirst", "Thirst test", "If you can see/hear this, alerts work."))
        root.after(5000, lambda: notifier.send(
            "Oxygen", "Oxygen test", "If you can see/hear this, alerts work."))

    ttk.Button(frm, text="Test alert", command=test_alert
               ).grid(row=1, column=2, columnspan=2, sticky="we", pady=(10, 0), padx=(6, 0))

    def bring_front():
        root.deiconify()
        root.lift()
        root.attributes("-topmost", True)
        root.after(4000, lambda: root.attributes("-topmost", False))

    def tick():
        w = state["watcher"]
        if w:
            w.logic.th.update(thresholds())               # threshold changes apply live
            w.check_stalled()
            for name, (_var, bar, lab) in stats.items():
                if name not in w.levels:
                    bar["value"], text = 0, "-"
                elif w.levels[name] is None:
                    bar["value"], text = 0, "unreadable"
                else:
                    bar["value"], text = w.levels[name] * 100, f"{w.levels[name] * 100:.0f}%"
                lab.config(text=text)
            status.config(text=w.status)
            if w.control is None and state["watcher"]:     # window closed under us
                toggle.config(text="Start watching")
                state["watcher"] = None
        if flag["front"]:
            flag["front"] = False
            if front_var.get():
                bring_front()
        root.after(500, tick)

    def on_close():
        persist()
        stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    tick()
    root.mainloop()


def run_console(args):
    if not args.window:
        sys.exit('Give the game window title, e.g.  --window "The Isle"')
    cfg = load_settings()
    notifier = WinNotifier(custom_sounds(cfg), volume=_volume(cfg))
    logic = AlertLogic(notifier,
                       {"Hunger": args.hunger, "Thirst": args.thirst, "Oxygen": args.oxygen},
                       REPEATS, blind_after=300)
    watcher = Watcher(args.window, logic, args.interval, notifier, debug=args.debug)
    watcher.start()
    print(f"Watching '{args.window}'. Alerting at hunger <= {args.hunger}%, thirst <= "
          f"{args.thirst}%, oxygen <= {args.oxygen}%. Ctrl+C to stop.", flush=True)
    try:
        while watcher.control:
            time.sleep(2)
            watcher.check_stalled()
    except KeyboardInterrupt:
        pass
    watcher.stop()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--console", action="store_true", help="run without the window")
    p.add_argument("--window", help="game window title (console mode)")
    p.add_argument("--below", type=int, default=40, help="hunger/thirst threshold %% (console mode)")
    p.add_argument("--hunger", type=int)
    p.add_argument("--thirst", type=int)
    p.add_argument("--oxygen", type=int, default=60)
    p.add_argument("--interval", type=float, default=3.0, help="seconds between readings")
    p.add_argument("--debug", action="store_true",
                   help="print every reading and save isle_o2_debug.png (also in the window)")
    p.add_argument("--test", metavar="PNG", help="measure a screenshot file and exit")
    p.add_argument("--notify-test", action="store_true", help="send a test alert and exit")
    args = p.parse_args()
    try:                                   # real pixel sizes, so windowed-mode cropping is exact
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
    args.hunger = args.below if args.hunger is None else args.hunger
    args.thirst = args.below if args.thirst is None else args.thirst

    if args.test:
        run_test(args)
    elif args.notify_test:
        cfg = load_settings()
        n = WinNotifier(custom_sounds(cfg), volume=_volume(cfg))
        n.send("Hunger", "Hunger test", "If you can see/hear this, alerts work.")
        time.sleep(2.5)
        n.send("Thirst", "Thirst test", "If you can see/hear this, alerts work.")
        time.sleep(2.5)
        n.send("Oxygen", "Oxygen test", "If you can see/hear this, alerts work.")
    elif args.console:
        run_console(args)
    else:
        run_gui(args)


if __name__ == "__main__":
    main()
