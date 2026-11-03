#!/usr/bin/env python3
"""
isle_alert_gui.py - the window version of the Isle alert for Ubuntu (GNOME, Wayland or X11).

A small window that watches the game window (other apps can cover it) and gives you a
desktop notification plus a sound when hunger, thirst or oxygen drops below the level you
set. It only reads the pixels of the HUD icons - nothing is injected into the game.

It reuses the icon reading and alert rules from isle_alert.py, which must sit in the same
folder. Normally you install the .deb and start "The Isle Alert" from the app menu.

To start it together with the game, put this in Steam - The Isle - Properties - Launch Options:
    isle-alert --with-game %command%
Isle Alert then starts the game, starts watching once the game window is open, and closes
when the game closes.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time


def _set_steam_env_aside():
    """Steam hands its launch options library paths meant for the game (LD_LIBRARY_PATH,
    LD_PRELOAD for the overlay...). They can break the system Python/GTK, so restart without
    them; isle_core.game_env() gives them back to the game. Has to run before any import that
    loads libraries. Names: isle_core.STEAM_ONLY_VARS and GAME_ENV_PREFIX."""
    if "--with-game" not in sys.argv or os.environ.get("ISLE_ALERT_REEXEC"):
        return
    os.environ["ISLE_ALERT_REEXEC"] = "1"
    for key in ("LD_LIBRARY_PATH", "LD_PRELOAD", "PYTHONPATH", "PYTHONHOME"):
        if key in os.environ:
            os.environ["ISLE_ALERT_GAME_" + key] = os.environ.pop(key)
    os.execv(sys.executable, [sys.executable] + sys.argv)


_set_steam_env_aside()

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst, Gtk  # noqa: E402

import numpy as np  # noqa: E402

from isle_alert import (CONFIG_DIR, SETTINGS_FILE, SOUNDS, SOUNDS_DIR,  # noqa: E402
                        TOKEN_FILE, Notifier, custom_sounds, due, dump_o2,
                        load_settings_file, open_portal_stream, pipeline_variants)
from isle_core import (__version__, AlertLogic, countdown_text, game_folder,  # noqa: E402
                       level_text, read_frame, split_game_command, start_game, wait_for_game)

DEFAULTS = {"hunger": 40, "thirst": 40, "oxygen": 60, "sounds": {}, "volume": 100, "theme": "system"}
THEMES = (("system", "Same as system"), ("light", "Light"), ("dark", "Dark"))


class ThemeSwitcher:
    """Light / dark for this window only. Uses the light or dark version of the desktop's own
    GTK theme (e.g. Yaru-red / Yaru-red-dark); 'system' puts back whatever the desktop uses."""

    def __init__(self):
        self.settings = Gtk.Settings.get_default()
        self.orig_name = self.settings.get_property("gtk-theme-name") or "Adwaita"
        self.orig_dark = self.settings.get_property("gtk-application-prefer-dark-theme")

    @staticmethod
    def _exists(name):
        dirs = [os.path.expanduser("~/.themes"), os.path.expanduser("~/.local/share/themes"),
                "/usr/share/themes"]
        return any(os.path.isdir(os.path.join(d, name, "gtk-3.0")) for d in dirs)

    def apply(self, mode):
        if mode not in ("light", "dark"):
            name, dark = self.orig_name, self.orig_dark
        else:
            base = self.orig_name[:-5] if self.orig_name.endswith("-dark") else self.orig_name
            if not self._exists(base):
                base = "Adwaita"                       # always there, has a dark variant
            name, dark = base, mode == "dark"
            if dark and self._exists(base + "-dark"):
                name, dark = base + "-dark", False     # the theme's own dark version
        self.settings.set_property("gtk-theme-name", name)
        self.settings.set_property("gtk-application-prefer-dark-theme", dark)
REPEATS = {"Hunger": 0, "Thirst": 0, "Oxygen": 15}      # seconds between repeats; 0 = only once
STATS = ("Hunger", "Thirst", "Oxygen")
CANCELLED = "No window was chosen."
SOUND_PATTERNS = ("*.mp3", "*.wav", "*.ogg", "*.oga", "*.flac", "*.opus", "*.m4a", "*.aac",
                  "*.wma")


def load_settings():
    cfg = dict(DEFAULTS)
    cfg.update(load_settings_file())
    if not isinstance(cfg.get("sounds"), dict):
        cfg["sounds"] = {}
    return cfg


def convert_sound(src, key, timeout=60):
    """Turn any sound file GStreamer can read (MP3, WAV, OGG, FLAC...) into a WAV copy in
    ~/.config/isle-alert/sounds, so it plays reliably and keeps working if the original is
    moved. Returns (path, None) on success or (None, message) on failure."""
    Gst.init(None)
    try:
        SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return None, f"Could not create {SOUNDS_DIR}: {exc}"
    dest = SOUNDS_DIR / f"{key}_{int(time.time() * 1000)}.wav"

    pipe = Gst.Pipeline.new("convert")
    names = ("filesrc", "decodebin", "audioconvert", "audioresample", "capsfilter",
             "wavenc", "filesink")
    els = [Gst.ElementFactory.make(n) for n in names]
    if any(e is None for e in els):
        missing = [n for n, e in zip(names, els) if e is None]
        return None, "Missing GStreamer parts: " + ", ".join(missing)
    filesrc, decode, conv, resample, capsf, enc, sink = els
    filesrc.set_property("location", str(src))
    capsf.set_property("caps", Gst.Caps.from_string(
        "audio/x-raw,format=S16LE,rate=48000,channels=2"))
    sink.set_property("location", str(dest))
    for e in els:
        pipe.add(e)
    filesrc.link(decode)
    conv.link(resample)
    resample.link(capsf)
    capsf.link(enc)
    enc.link(sink)
    found = {"audio": False}

    def on_pad(_decode, pad):
        caps = pad.get_current_caps() or pad.query_caps(None)
        if caps and caps.to_string().startswith("audio/") and not found["audio"]:
            found["audio"] = pad.link(conv.get_static_pad("sink")) == Gst.PadLinkReturn.OK

    decode.connect("pad-added", on_pad)
    pipe.set_state(Gst.State.PLAYING)
    bus, deadline, error = pipe.get_bus(), time.time() + timeout, None
    while True:
        msg = bus.timed_pop_filtered(50 * Gst.MSECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS)
        pump()
        if msg is not None:
            if msg.type == Gst.MessageType.ERROR:
                error = msg.parse_error()[0].message
            break
        if time.time() > deadline:
            error = "It took too long to read the file."
            break
    pipe.set_state(Gst.State.NULL)

    seconds = 0.0
    if dest.exists():
        seconds = max(0, dest.stat().st_size - 44) / (48000 * 2 * 2)
    if error or not found["audio"] or seconds < 0.05:
        try:
            dest.unlink()
        except OSError:
            pass
        reason = error or "No sound was found in it."
        return None, (f"{reason}\n\nMP3, WAV, OGG and FLAC files work. For M4A/AAC "
                      "install gstreamer1.0-libav.")
    return dest, None


def save_settings(cfg):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(cfg), encoding="utf-8")
    except Exception as exc:
        print("could not save settings:", exc, file=sys.stderr)


class GameWatch:
    """The game Isle Alert started (--with-game): is it still running, and is its window open?
    Wayland doesn't let apps see other windows, but Proton games are X11 windows (XWayland),
    which xwininfo can list."""

    def __init__(self, process, command):
        self.process, self.folder = process, game_folder(command)
        self.proc_since = None             # when the game's own .exe was first seen running

    def running(self):
        return self.process.poll() is None   # Steam's wrapper stays until the game has closed

    def window_open(self):
        now = time.time()
        if self._game_process():
            self.proc_since = self.proc_since or now
        else:
            self.proc_since = None
        x11 = x11_game_window()
        if x11:
            return True
        ran = now - self.proc_since if self.proc_since else 0
        # Can't list windows (no xwininfo/X11): give the game time to open it after it
        # starts. Not on the X11 list for long: it may be a Wayland window - try anyway.
        return ran >= (20 if x11 is None else 90)

    def _game_process(self):
        """True if a process of the game's own .exe (run by Wine/Proton) is running."""
        # Wine shows its own path (Z:\mnt\... or a drive letter for the Steam library,
        # e.g. S:\steamapps\common\The Isle\...), so compare from steamapps/ on.
        folder = None
        if self.folder:
            folder = str(self.folder).lower().rstrip("/") + "/"
            if "/steamapps/common/" in folder:
                folder = "/steamapps/common/" + folder.split("/steamapps/common/", 1)[1]
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    exe = f.read().split(b"\0")[0].decode(errors="replace")
            except OSError:
                continue
            exe = exe.replace("\\", "/").lower()
            if not exe.endswith(".exe"):
                continue
            if exe[1:3] == ":/":                       # Wine path: Z:/mnt/... -> /mnt/...
                exe = exe[2:]
            if folder and folder in exe:
                return True
            if not folder and "isle" in exe.rsplit("/", 1)[-1]:
                return True
        return False


XWININFO_LINE = re.compile(r'^\s*0x[0-9a-f]+ "(.*)": \((.*)\)\s+(\d+)x(\d+)')


def x11_game_window():
    """True/False: is the game's window on the X11 (XWayland) window list? None if that
    can't be checked."""
    if not os.environ.get("DISPLAY"):
        return None
    try:
        out = subprocess.run(["xwininfo", "-root", "-tree"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        m = XWININFO_LINE.match(line)
        if not m:
            continue
        name, wm_class, w, h = m.group(1).lower(), m.group(2).lower(), int(m.group(3)), int(m.group(4))
        if w < 640 or h < 480 or "isle alert" in name:
            continue                                   # splash screens, our own window
        if "isle" in name or "isle" in wm_class or "steam_app_376210" in wm_class:
            return True
    return False


def pump():
    """Keep the window alive while we wait for something."""
    while Gtk.events_pending():
        Gtk.main_iteration()


# ------------------------------ capture -------------------------------------
class Capture:
    """Reads the shared game window through PipeWire/GStreamer and feeds AlertLogic."""

    def __init__(self, logic, notifier, interval=3.0, source=None, debug=False):
        self.logic, self.notifier, self.interval = logic, notifier, interval
        self.source = source              # tests only: a GStreamer source instead of the portal
        self.debug = debug                # save isle_o2_debug.png when O2 is seen below 100%
        self.pipeline = None
        self.fd = None                    # our PipeWire connection (pipewiresrc only dups it)
        self.close_session = None
        self.levels = {}
        self.status = "Starting..."
        self.frames = 0
        self.last_frame = time.time()
        self.last_read = self.o2_dump = 0.0
        self.stalled = False
        self.on_stopped = None            # called (main thread) if the capture ends by itself
        self.notify_end = True            # pop-up when it ends (not with --with-game)

    # ---- starting -----------------------------------------------------------
    def start(self):
        """Start capturing. Returns None on success, otherwise a message for the user."""
        Gst.init(None)
        errors = []
        if self.source:
            attempts = [lambda: ("test source",
                                 f"{self.source} ! video/x-raw,format=BGRx ! appsink name=sink")]
        else:
            try:
                open_fd, node, self.close_session = open_portal_stream()
            except GLib.Error as exc:                 # D-Bus problems, no portal running...
                return ("Could not reach the screen-sharing service of your desktop.\n\n"
                        f"{exc.message}\n\nOn Ubuntu this needs xdg-desktop-portal (and "
                        "xdg-desktop-portal-gnome on GNOME).")
            except RuntimeError as exc:               # the picker was cancelled or refused
                text = str(exc)
                return CANCELLED if "code 1" in text else text
            except Exception as exc:
                return f"Could not start the screen sharing:\n{exc}"

            def attempt(i):
                self.fd = open_fd()                   # a fresh PipeWire connection per attempt
                return pipeline_variants(self.fd, node, self.interval)[i]

            attempts = [lambda i=i: attempt(i)
                        for i in range(len(pipeline_variants(-1, node, self.interval)))]

        pipe = None
        for make in attempts:
            try:
                label, desc = make()
            except Exception as exc:
                self._release()
                return f"Could not open the PipeWire connection to the window:\n{exc}"
            pipe = self._try(label, desc, errors)
            if pipe is not None:
                break
            self._close_fd()
        if pipe is None:
            self._release()
            hint = ""
            if any("pipewiresrc" in e for e in errors):
                hint = "\n\nTry:  sudo apt install gstreamer1.0-pipewire"
            return "Could not start the capture:\n" + "\n".join(errors) + hint

        self.pipeline = pipe
        self.last_frame = time.time()
        self.status = "Waiting for frames..."
        bus = pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus)
        return None

    def _try(self, label, desc, errors):
        """Start one pipeline variant. Returns the pipeline, or None (reason added to errors)."""
        try:
            pipe = Gst.parse_launch(desc)
        except GLib.Error as exc:
            errors.append(f"[{label}] could not be built: {exc.message}")
            return None
        sink = pipe.get_by_name("sink")
        for key, value in (("emit-signals", True), ("max-buffers", 1),
                           ("drop", True), ("sync", False)):
            sink.set_property(key, value)
        sink.connect("new-sample", self._on_sample)
        self.frames = 0
        pipe.set_state(Gst.State.PLAYING)
        bus, deadline = pipe.get_bus(), time.time() + 6
        while time.time() < deadline:
            msg = bus.timed_pop_filtered(50 * Gst.MSECOND,
                                         Gst.MessageType.ERROR | Gst.MessageType.EOS)
            pump()                                    # the window stays responsive
            if msg is not None:
                if msg.type == Gst.MessageType.ERROR:
                    err, dbg = msg.parse_error()
                    errors.append(f"[{label}] {err.message}")
                else:
                    errors.append(f"[{label}] the stream ended immediately")
                pipe.set_state(Gst.State.NULL)
                return None
            if self.frames:
                print(f"Capture working ({label})", flush=True)
                return pipe
        print(f"Capture started ({label}), no frames yet", flush=True)
        return pipe          # no error, just no frame yet (game may be idle): keep waiting

    # ---- running (GStreamer thread) ------------------------------------------
    def _on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        now = time.time()
        self.last_frame, self.stalled = now, False
        self.frames += 1
        if not due(now, self.last_read, self.interval):
            return Gst.FlowReturn.OK
        self.last_read = now

        try:
            st = sample.get_caps().get_structure(0)
            w, h, fmt = st.get_value("width"), st.get_value("height"), st.get_value("format")
            buf = sample.get_buffer()
            ok, info = buf.map(Gst.MapFlags.READ)
            if not ok:
                return Gst.FlowReturn.OK
            try:
                stride = info.size // h
                if stride < w * 4:
                    return Gst.FlowReturn.OK
                frame = np.frombuffer(info.data, np.uint8)[: h * stride].reshape(h, stride)
                frame = frame[:, : w * 4].reshape(h, w, 4)[:, :, :3]
                rgb = frame[:, :, ::-1] if fmt.startswith("BGR") else frame
                menu, levels = read_frame(rgb)
                if menu:                              # keep the last real readings on show
                    self.logic.paused()
                    self.status = f"{menu} open - alerts paused"
                    return Gst.FlowReturn.OK
                o2 = levels.get("Oxygen")
                if self.debug and o2 is not None and o2 < 0.999 and now - self.o2_dump > 30:
                    self.o2_dump = now
                    dump_o2(rgb)                      # lets you check what it "saw"
                self.levels = levels
                self.status = f"Watching ({w}x{h})"
                self.logic.update(levels, now)
            finally:
                buf.unmap(info)
        except Exception as exc:                      # never kill the capture thread
            self.status = f"Read error: {exc}"
        return Gst.FlowReturn.OK

    # ---- stopping / problems (main thread) -----------------------------------
    def _on_bus(self, bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, _dbg = msg.parse_error()
            text = f"The capture stopped: {err.message}"
        elif msg.type == Gst.MessageType.EOS:
            text = "The game window was closed - capture ended."
        else:
            return
        self.stop()
        self.status = text
        if self.notify_end:
            self.notifier.send("capture", "Isle alert stopped", text)
        if self.on_stopped:
            self.on_stopped()

    def stop(self):
        if self.pipeline is not None:
            try:
                self.pipeline.get_bus().remove_signal_watch()
            except Exception:
                pass
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
        self._release()

    def _close_fd(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def _release(self):
        """Close our PipeWire connection and end the screen-share session."""
        self._close_fd()
        if self.close_session:
            self.close_session()
            self.close_session = None

    def check_stalled(self):
        now = time.time()
        if self.pipeline and now - getattr(self, "_rate_t", 0) >= 60:   # frames/s, once a minute
            if getattr(self, "_rate_t", 0):
                rate = (self.frames - self._rate_n) / (now - self._rate_t)
                print(f"Receiving {rate:.1f} frames/s from the game window", flush=True)
            self._rate_t, self._rate_n = now, self.frames
        if self.pipeline and not self.stalled and now - self.last_frame > 90:
            self.stalled = True
            self.status = "No frames - is the game minimised or paused?"
            self.notifier.send("stalled", "No frames from the game window",
                               "The game may be minimised or paused - alerts are not working.")


# ------------------------------ the window -----------------------------------
class App(Gtk.Window):
    def __init__(self, source=None, debug=False, game=None):
        super().__init__(title=f"The Isle alert {__version__}")
        self.source, self.debug = source, debug
        # game: a GameWatch when started with --with-game. auto = start watching by itself
        # when the game window opens (switched off when you press Stop).
        self.game, self.auto = game, game is not None
        self.seen = None                  # when the game window was first seen open
        self.retry_at = 0.0
        self.notifier = Notifier()
        self.capture = None
        self.busy = False
        cfg = load_settings()

        self.set_border_width(14)
        self.set_resizable(False)
        grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        self.add(grid)

        # Two tabs keep the window small: alerts (thresholds, live levels) and sounds.
        tabs = Gtk.Notebook()
        grid.attach(tabs, 0, 0, 4, 1)
        alerts = Gtk.Grid(column_spacing=10, row_spacing=8)
        alerts.set_border_width(10)
        tabs.append_page(alerts, Gtk.Label(label="Alerts"))
        sgrid = Gtk.Grid(column_spacing=6, row_spacing=6)
        sgrid.set_border_width(10)
        tabs.append_page(sgrid, Gtk.Label(label="Settings"))

        alerts.attach(Gtk.Label(label="Alert at or below (%)", xalign=0), 1, 0, 1, 1)
        alerts.attach(Gtk.Label(label="Alert in", xalign=0), 4, 0, 1, 1)
        self.spins, self.bars, self.vals, self.etas = {}, {}, {}, {}
        for i, name in enumerate(STATS):
            row = i + 1
            alerts.attach(Gtk.Label(label=name, xalign=0), 0, row, 1, 1)
            spin = Gtk.SpinButton.new_with_range(0, 100, 1)
            spin.set_numeric(True)
            spin.set_value(int(cfg[name.lower()]))
            spin.connect("value-changed", lambda *_: self.save())
            alerts.attach(spin, 1, row, 1, 1)
            bar = Gtk.ProgressBar()
            bar.set_size_request(170, -1)
            bar.set_valign(Gtk.Align.CENTER)
            alerts.attach(bar, 2, row, 1, 1)
            val = Gtk.Label(label="-", xalign=0)
            val.set_width_chars(11)
            alerts.attach(val, 3, row, 1, 1)
            eta = Gtk.Label(label="-", xalign=0)
            eta.set_width_chars(12)
            eta.set_tooltip_text("Time until it reaches your alert level, from how fast it "
                                 "has been dropping over the last few minutes. Once it's "
                                 "below that level, time until it's empty.")
            alerts.attach(eta, 4, row, 1, 1)
            self.spins[name], self.bars[name], self.vals[name] = spin, bar, val
            self.etas[name] = eta

        # ---- alert sounds: pick your own, preview, back to default
        self.sounds = cfg["sounds"]
        self.sound_labels = {}
        for i, name in enumerate(STATS):
            sgrid.attach(Gtk.Label(label=name, xalign=0, width_chars=7), 0, i, 1, 1)
            lab = Gtk.Label(xalign=0)
            lab.set_width_chars(24)
            lab.set_max_width_chars(24)
            lab.set_ellipsize(3)                      # Pango.EllipsizeMode.END
            lab.set_hexpand(True)
            sgrid.attach(lab, 1, i, 1, 1)
            for col, (text, tip, handler) in enumerate((
                    ("Choose…", "Pick a sound file (MP3, WAV, OGG, FLAC)", self.on_choose_sound),
                    ("▶", "Play this sound", self.on_preview_sound),
                    ("Default", "Go back to the built-in sound", self.on_reset_sound))):
                b = Gtk.Button(label=text)
                b.set_tooltip_text(tip)
                b.connect("clicked", handler, name)
                sgrid.attach(b, 2 + col, i, 1, 1)
            self.sound_labels[name] = lab
        self.refresh_sounds()

        row = len(STATS)                              # one volume for all alert sounds
        sgrid.attach(Gtk.Label(label="Volume", xalign=0, width_chars=7), 0, row, 1, 1)
        self.volume = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.volume.set_value(self.notifier.volume)
        self.volume.set_value_pos(Gtk.PositionType.RIGHT)
        self.volume.set_digits(0)
        self.volume.set_hexpand(True)
        self.volume.connect("format-value", lambda _s, v: f"{v:.0f}%")
        self.volume.connect("value-changed", self.on_volume)
        sgrid.attach(self.volume, 1, row, 1, 1)
        b = Gtk.Button(label="▶")
        b.set_tooltip_text("Hear the hunger sound at this volume")
        b.connect("clicked", self.on_preview_sound, "Hunger")
        sgrid.attach(b, 3, row, 1, 1)

        sgrid.attach(Gtk.Label(label="Theme", xalign=0, width_chars=7), 0, row + 1, 1, 1)
        self.theme = Gtk.ComboBoxText()
        for key, text in THEMES:
            self.theme.append(key, text)
        self.theme.set_active_id(cfg.get("theme") if cfg.get("theme") in dict(THEMES) else "system")
        self.theme.set_halign(Gtk.Align.START)
        self.theme.connect("changed", self.on_theme)
        sgrid.attach(self.theme, 1, row + 1, 1, 1)
        self.themes = ThemeSwitcher()
        self.themes.apply(self.theme.get_active_id())

        self.toggle = Gtk.Button(label="Start watching")
        self.toggle.connect("clicked", self.on_toggle)
        grid.attach(self.toggle, 0, 1, 2, 1)
        test = Gtk.Button(label="Test alert")
        test.connect("clicked", self.on_test)
        grid.attach(test, 2, 1, 2, 1)

        forget = Gtk.Button(label="Forget the saved game window (ask again next time)")
        forget.set_relief(Gtk.ReliefStyle.NONE)
        forget.connect("clicked", self.on_forget)
        alerts.attach(forget, 0, len(STATS) + 1, 5, 1)

        self.status = Gtk.Label(label="Not running", xalign=0)
        self.status.set_line_wrap(True)
        self.status.set_max_width_chars(58)
        grid.attach(self.status, 0, 2, 4, 1)
        tip = Gtk.Label(label="When asked, choose the game window. Keep the game running "
                              "and not minimised.", xalign=0)
        tip.set_line_wrap(True)
        tip.set_max_width_chars(58)
        tip.get_style_context().add_class("dim-label")
        grid.attach(tip, 0, 3, 4, 1)

        self.connect("delete-event", self.on_close)
        GLib.timeout_add(500, self.tick)
        if game:
            self.set_status("Waiting for the game window to open...")
            GLib.timeout_add(2000, self.game_tick)

    # ---- helpers ---------------------------------------------------------------
    def thresholds(self):
        return {name: self.spins[name].get_value_as_int() for name in STATS}

    def save(self):
        t = self.thresholds()
        save_settings({"hunger": t["Hunger"], "thirst": t["Thirst"], "oxygen": t["Oxygen"],
                       "sounds": self.sounds, "volume": self.notifier.volume,
                       "theme": self.theme.get_active_id() if hasattr(self, "theme") else "system"})

    # ---- alert sounds ------------------------------------------------------------
    def on_theme(self, combo):
        self.themes.apply(combo.get_active_id())
        self.save()

    def on_volume(self, scale):
        self.notifier.volume = int(round(scale.get_value()))
        self.save()

    def refresh_sounds(self):
        """Update the labels and tell the notifier which files to play."""
        mine = custom_sounds({"sounds": self.sounds})
        for name, lab in self.sound_labels.items():
            if name in mine:
                text = self.sounds[name].get("name") or os.path.basename(mine[name])
                lab.set_text(text)
                lab.set_tooltip_text(text)
            else:
                default = SOUNDS.get(name, "system beep")
                lab.set_text(f"Default ({default})")
                lab.set_tooltip_text(None)
        self.notifier.custom = mine

    def pick_file(self, name):
        """Show a file picker; returns the chosen path or None."""
        dlg = Gtk.FileChooserNative.new(f"Choose the {name.lower()} alert sound", self,
                                        Gtk.FileChooserAction.OPEN, "_Use this sound", "_Cancel")
        flt = Gtk.FileFilter()
        flt.set_name("Sound files")
        flt.add_mime_type("audio/*")
        for pat in SOUND_PATTERNS:
            flt.add_pattern(pat)
            flt.add_pattern(pat.upper())
        dlg.add_filter(flt)
        every = Gtk.FileFilter()
        every.set_name("All files")
        every.add_pattern("*")
        dlg.add_filter(every)
        path = dlg.get_filename() if dlg.run() == Gtk.ResponseType.ACCEPT else None
        dlg.destroy()
        return path

    def set_sound(self, name, src):
        """Use the file src for this alert. Returns None or an error message."""
        self.set_status(f"Reading {os.path.basename(src)}...")
        pump()
        dest, error = convert_sound(src, name)
        if error:
            self.set_status("Sound not changed.")
            return error
        self.sounds[name] = {"file": str(dest), "name": os.path.basename(src)}
        self._remove_copies(name, keep=dest)          # drop the copy of the previous choice
        self.refresh_sounds()
        self.save()
        self.set_status(f"{name} alert will now play {os.path.basename(src)}.")
        return None

    @staticmethod
    def _remove_copies(name, keep=None):
        for p in SOUNDS_DIR.glob(f"{name}_*.wav"):
            if keep is None or p != keep:
                try:
                    p.unlink()
                except OSError:
                    pass

    def on_choose_sound(self, _button, name):
        src = self.pick_file(name)
        if not src:
            return
        error = self.set_sound(name, src)
        if error:
            self.show_error(f"Couldn't use {os.path.basename(src)}", error)
        else:
            self.notifier.play(name)                  # let you hear it straight away

    def on_preview_sound(self, _button, name):
        self.notifier.play(name)

    def on_reset_sound(self, _button, name):
        self.sounds.pop(name, None)
        self._remove_copies(name)
        self.refresh_sounds()
        self.save()
        self.set_status(f"{name} alert is back to the built-in sound.")

    def set_status(self, text):
        self.status.set_text(text)

    def show_error(self, title, text):
        dlg = Gtk.MessageDialog(transient_for=self, modal=True,
                                message_type=Gtk.MessageType.ERROR,
                                buttons=Gtk.ButtonsType.OK, text=title)
        dlg.format_secondary_text(text)
        dlg.run()
        dlg.destroy()

    # ---- buttons ---------------------------------------------------------------
    def on_toggle(self, _button):
        if self.capture:
            self.stop()
            self.auto = False                         # you stopped it: don't start again
        else:
            self.start()
            self.auto = self.game is not None and self.capture is not None

    def start(self, auto=False):
        self.busy = True
        self.toggle.set_sensitive(False)
        self.set_status("Choose the game window in the picker that opens...")
        pump()
        logic = AlertLogic(self.notifier, self.thresholds(), REPEATS, blind_after=300)
        cap = Capture(logic, self.notifier, source=self.source, debug=self.debug)
        cap.notify_end = self.game is None            # with the game: closing it is expected
        error = cap.start()
        self.busy = False
        self.toggle.set_sensitive(True)
        if error and auto:
            self.auto = False                         # don't keep opening the picker
        if error == CANCELLED:                        # you closed the picker: not an error
            self.set_status("Not running - no game window was chosen.")
            return
        if error:
            self.set_status("Not running")
            self.show_error("Could not start watching", error)
            return
        cap.on_stopped = self.on_capture_stopped
        self.capture = cap
        self.toggle.set_label("Stop")
        self.save()

    def stop(self):
        if self.capture:
            self.capture.stop()
            self.capture = None
        self.toggle.set_label("Start watching")
        self.set_status("Stopped")

    def on_capture_stopped(self):
        text = self.capture.status if self.capture else "Stopped"
        self.capture = None
        self.toggle.set_label("Start watching")
        self.set_status(text)
        self.seen, self.retry_at = None, time.time() + 10

    def on_test(self, _button):
        self.notifier.send("Hunger", "Hunger test", "If you can see/hear this, alerts work.")
        GLib.timeout_add(2500, lambda: (self.notifier.send(
            "Thirst", "Thirst test", "If you can see/hear this, alerts work."), False)[1])
        GLib.timeout_add(5000, lambda: (self.notifier.send(
            "Oxygen", "Oxygen test", "If you can see/hear this, alerts work."), False)[1])

    def on_forget(self, _button):
        try:
            TOKEN_FILE.unlink()
            self.set_status("Saved window forgotten - you'll be asked to pick it again.")
        except FileNotFoundError:
            self.set_status("There was no saved window.")

    def on_close(self, *_args):
        if self.busy:                                 # the window picker is open
            return True
        self.save()
        if self.capture:
            self.capture.stop()
            self.capture = None
        if self.game and self.game.running():
            # Steam counts the game as running while Isle Alert is: stay in the background
            # (no window, no alerts) until the game closes.
            self.auto = False
            self.hide()
            return True
        Gtk.main_quit()
        return False

    def game_tick(self):
        """--with-game: start watching once the game window is open, quit after the game."""
        if self.busy:                                 # the window picker is open
            return True
        if not self.game.running():
            print("The game has closed - closing Isle Alert.", flush=True)
            self.save()
            if self.capture:
                self.capture.stop()
                self.capture = None
            Gtk.main_quit()
            return False
        now = time.time()
        if not self.auto or self.capture or now < self.retry_at:
            return True
        if not self.game.window_open():
            self.seen = None
            return True
        if self.seen is None:
            self.seen = now
        elif now - self.seen >= 5:                    # open for a moment: not a splash screen
            self.start(auto=True)
        return True

    # ---- live update -----------------------------------------------------------
    def tick(self):
        cap = self.capture
        if cap:
            cap.logic.th.update(self.thresholds())    # threshold changes apply live
            cap.check_stalled()
            for name in STATS:
                if name not in cap.levels:
                    fraction, text = 0.0, "-"
                elif cap.levels[name] is None:
                    fraction, text = 0.0, "unreadable"
                else:
                    fraction, text = cap.levels[name], level_text(name, cap.levels[name])
                self.bars[name].set_fraction(fraction)
                self.vals[name].set_text(text)
                self.etas[name].set_text(countdown_text(
                    cap.logic.drain, name, cap.levels.get(name), cap.logic.th[name], time.time()))
            if self.capture:
                self.set_status(cap.status)
        return True


def main():
    p = argparse.ArgumentParser(description="The Isle hunger/thirst/O2 alert (window version)")
    p.add_argument("--source", help=argparse.SUPPRESS)     # tests only
    p.add_argument("--debug", action="store_true",
                   help="save isle_o2_debug.png whenever O2 is seen below 100%%")
    p.add_argument("--version", action="version", version=f"Isle Alert {__version__}")
    p.add_argument("--with-game", metavar="COMMAND", help=(
        "start the game with this command (everything after it), watch it and close with it. "
        "Steam launch options: isle-alert --with-game %%command%%"))
    own_args, game_cmd = split_game_command(sys.argv[1:])
    args = p.parse_args(own_args)
    game = None
    if game_cmd is not None:               # started from Steam: the game goes first, always
        try:
            game = GameWatch(start_game(game_cmd), game_cmd)
        except Exception as exc:
            print("Could not start the game:", exc, file=sys.stderr)
            subprocess.run(["notify-send", "Isle Alert could not start the game", str(exc)])
            sys.exit(1)
    try:
        GLib.set_prgname("isle-alert")
        GLib.set_application_name("The Isle Alert")
        Gtk.Window.set_default_icon_name("isle-alert")
        win = App(source=args.source, debug=args.debug, game=game)
        win.show_all()
        Gtk.main()
    except Exception:
        if game is None:
            raise
        import traceback
        traceback.print_exc()
        wait_for_game(game.process)        # don't let Steam think the game closed


if __name__ == "__main__":
    main()
