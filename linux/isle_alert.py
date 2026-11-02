#!/usr/bin/env python3
"""
isle_alert.py - hunger/thirst alert for The Isle on Ubuntu (Wayland or X11).

It watches ONE window (the game) through the desktop's screen-share portal, so
other apps can sit on top of the game and it still works. It only looks at the
pixels of the hunger (stomach), thirst (drop) and O2 icons and sends a desktop
notification (plus a sound) when a bar falls below your threshold.
It never touches the game's files or memory.

Usage
  python3 isle_alert.py                 # watch the game, alert below 40%
  python3 isle_alert.py --below 50      # alert earlier (e.g. at 50%)
  python3 isle_alert.py --hunger 30 --thirst 50 --oxygen 70
  python3 isle_alert.py --test shot.png # check the reading on a screenshot
  python3 isle_alert.py --notify-test   # check notifications + sounds only

Put Hungry.wav, Thirsty.wav and Oxygen.wav in the same folder as this script. Hunger and
thirst alert once, when the bar reaches the threshold (40%); they re-arm after you eat/drink.
Oxygen plays its sound every 15 seconds while it stays low.
With --debug, whenever the O2 icon is seen below 100% it saves isle_o2_debug.png next to
the script, so you can check what the program actually saw.
"""
import argparse
import json
import os
import shutil
import subprocess
import threading
import sys
import time
from fractions import Fraction
from pathlib import Path

import numpy as np

# Reading the HUD icons, the alert rules, the countdown and the version are shared with the
# Windows app and live in isle_core.py: next to this file once installed, one folder up when
# run straight from the repo.
if not (Path(__file__).resolve().parent / "isle_core.py").exists():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import isle_core  # noqa: E402
from isle_core import (__version__, AlertLogic, format_eta, level_text, read_frame,  # noqa: E402
                       run_test, save_png)

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "isle-alert"
TOKEN_FILE = CONFIG_DIR / "portal_token"
SETTINGS_FILE = CONFIG_DIR / "settings.json"     # thresholds + the sounds you picked (window app)
SOUNDS_DIR = CONFIG_DIR / "sounds"               # WAV copies of the sounds you picked
SOUND = "/usr/share/sounds/freedesktop/stereo/dialog-warning.oga"   # default beep
SCRIPT_DIR = Path(__file__).resolve().parent
SOUNDS = {"Hunger": "Hungry.wav", "Thirst": "Thirsty.wav",
          "Oxygen": "Oxygen.wav"}                                   # keep these next to the script


def dump_o2(rgb):
    """Save isle_o2_debug.png next to the script (or in ~/.config/isle-alert if read-only)."""
    return isle_core.dump_o2(rgb, [SCRIPT_DIR, CONFIG_DIR])


# ------------------------------ notifications ------------------------------
def load_settings_file():
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def custom_sounds(data=None):
    """{alert: file} for the sounds picked in the window app (only files that still exist)."""
    data = load_settings_file() if data is None else data
    out = {}
    sounds = data.get("sounds")
    for key, entry in (sounds.items() if isinstance(sounds, dict) else ()):
        path = entry.get("file") if isinstance(entry, dict) else entry
        if isinstance(path, str) and Path(path).is_file():
            out[key] = path
    return out


def settings_volume(data=None):
    """Alert volume 0-100 saved by the window app (100 if never set)."""
    data = load_settings_file() if data is None else data
    try:
        return max(0, min(100, int(data.get("volume", 100))))
    except (TypeError, ValueError):
        return 100


NOTIFY_SECONDS = 3   # notifications close themselves after this long


def close_notification(nid: str) -> None:
    """Close a desktop notification. GNOME keeps 'critical' ones up until they're closed,
    whatever timeout was asked for, so the app closes them itself."""
    try:
        subprocess.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
                        "--object-path", "/org/freedesktop/Notifications",
                        "--method", "org.freedesktop.Notifications.CloseNotification", nid],
                       capture_output=True, timeout=5)
    except Exception:
        pass


class Notifier:
    def __init__(self):
        self.ids = {}
        self.custom = custom_sounds()         # sounds you picked in the app replace the defaults
        self.volume = settings_volume()       # 0-100, for all alert sounds
        self.playing = {}
        self.have_notify = shutil.which("notify-send") is not None
        self.player = next((p for p in ("paplay", "pw-play", "aplay") if shutil.which(p)), None)
        if not self.have_notify:
            print("notify-send not found: sudo apt install libnotify-bin", file=sys.stderr)

    def _sound_for(self, key):
        mine = self.custom.get(key)
        if mine and Path(mine).is_file():
            return mine
        name = SOUNDS.get(key)
        if name and (SCRIPT_DIR / name).exists():
            return str(SCRIPT_DIR / name)
        if name:
            print(f"{name} not found next to the script - using the default sound", file=sys.stderr)
        return SOUND if Path(SOUND).exists() else None

    def play(self, key):
        """Play the sound for this alert. If it is still playing it is not started again,
        so a long sound never piles up on itself."""
        sound = self._sound_for(key)
        if not (self.player and sound) or self.volume <= 0:
            return
        prev = self.playing.get(key)
        if prev is not None and prev.poll() is None:
            return
        try:
            self.playing[key] = subprocess.Popen(self._command(sound), stderr=subprocess.DEVNULL)
        except Exception as exc:                          # never crash on a sound
            print("could not play the sound:", exc, file=sys.stderr)

    def _command(self, sound):
        """Player command line at the chosen volume. The slider is squared so it feels
        even to the ear (50% is clearly quieter, not just a little)."""
        gain = (self.volume / 100.0) ** 2
        if self.volume >= 100 or self.player == "aplay":    # aplay has no volume option
            return [self.player, sound]
        if self.player == "paplay":
            return [self.player, f"--volume={int(gain * 65536)}", sound]
        return [self.player, f"--volume={gain:.3f}", sound]  # pw-play: 0.0-1.0

    def send(self, key, title, body):
        print(f"[ALERT] {title} - {body}", flush=True)
        self.play(key)
        if not self.have_notify:
            return
        # critical urgency so GNOME still shows it over a fullscreen/borderless game
        cmd = ["notify-send", "-a", "The Isle alert", "-u", "critical", "-t", str(NOTIFY_SECONDS * 1000),
               "-i", "dialog-warning", "-p"]
        if key in self.ids:
            cmd += ["-r", self.ids[key]]
        try:
            out = subprocess.run(cmd + [title, body], capture_output=True, text=True, timeout=5)
            if out.stdout.strip().isdigit():
                self.ids[key] = out.stdout.strip()
                t = threading.Timer(NOTIFY_SECONDS, close_notification, args=(self.ids[key],))
                t.daemon = True
                t.start()
        except Exception as exc:                          # never crash on a notification
            print("notify-send failed:", exc, file=sys.stderr)


# ------------------------------ screen-share portal -------------------------
def open_portal_stream():
    """Ask the desktop for a screen-share of ONE window. Returns (open_fd, node_id, close).

    open_fd() gives a new PipeWire connection fd (the caller must close it, pipewiresrc
    only duplicates it); close() ends the screen-share session."""
    import gi
    from gi.repository import Gio, GLib

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    dest, path, iface = ("org.freedesktop.portal.Desktop",
                         "/org/freedesktop/portal/desktop",
                         "org.freedesktop.portal.ScreenCast")
    sender = bus.get_unique_name()[1:].replace(".", "_")
    counter = [0]

    def v(sig, val):
        return GLib.Variant(sig, val)

    def request(method, sig, args, options):
        """Call a portal method that answers through a Request/Response signal."""
        counter[0] += 1
        token = f"isle{os.getpid()}x{counter[0]}"
        options = dict(options, handle_token=v("s", token))
        req_path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        loop, result = GLib.MainLoop(), {}

        def on_response(conn, snd, obj, ifc, sig_name, params, data):
            result["code"], result["data"] = params.unpack()
            loop.quit()

        sub = bus.signal_subscribe(dest, "org.freedesktop.portal.Request", "Response",
                                   req_path, None, Gio.DBusSignalFlags.NONE, on_response, None)
        bus.call_sync(dest, path, iface, method,
                      GLib.Variant(sig, tuple(args) + (options,)),
                      None, Gio.DBusCallFlags.NONE, -1, None)
        loop.run()                                       # waits while you pick the window
        bus.signal_unsubscribe(sub)
        if result["code"] != 0:
            raise RuntimeError(f"{method} was cancelled or refused (code {result['code']})")
        return result["data"]

    session = request("CreateSession", "(a{sv})", [],
                      {"session_handle_token": v("s", f"isle{os.getpid()}s")})["session_handle"]

    select = {"types": v("u", 2),            # 2 = a single window
              "multiple": v("b", False),
              "cursor_mode": v("u", 1),      # hide the mouse cursor
              "persist_mode": v("u", 2)}     # remember the choice between runs
    if TOKEN_FILE.exists():
        select["restore_token"] = v("s", TOKEN_FILE.read_text().strip())
    request("SelectSources", "(oa{sv})", [session], select)
    started = request("Start", "(osa{sv})", [session, ""], {})

    if started.get("restore_token"):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(started["restore_token"])
    streams = started.get("streams") or []
    if not streams:
        raise RuntimeError("No window was shared.")
    node_id = streams[0][0]

    def open_fd():
        """A fresh PipeWire connection (one per pipeline attempt)."""
        reply, fds = bus.call_with_unix_fd_list_sync(
            dest, path, iface, "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (session, {})),
            GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, -1, None, None)
        return fds.get(reply.unpack()[0])

    def close():
        """End the screen-share session (also turns off the desktop's sharing indicator)."""
        try:
            bus.call_sync(dest, session, "org.freedesktop.portal.Session", "Close",
                          None, None, Gio.DBusCallFlags.NONE, -1, None)
        except Exception:
            pass

    return open_fd, node_id, close


def pipeline_variants(fd, node, interval):
    """Capture pipelines to try, best first: (label, gst-launch description).

    Only one frame per reading interval is analysed, so the first ones ask the compositor
    for no more than that - every frame is a full-window copy (~15 MB at 1440p)."""
    num, den = Fraction(1 / max(interval, 0.1)).limit_denominator(100).as_integer_ratio()
    src = f"pipewiresrc fd={fd} path={node} always-copy=true"
    raw = "video/x-raw,format=BGRx"
    slow = f"{raw},max-framerate={num}/{den}"
    return [
        ("low frame rate", f"{src} ! {slow} ! appsink name=sink"),
        ("low frame rate, videoconvert", f"{src} ! videoconvert ! {slow} ! appsink name=sink"),
        ("plain", f"{src} ! {raw} ! appsink name=sink"),
        ("with videoconvert", f"{src} ! videoconvert ! {raw} ! appsink name=sink"),
        ("no always-copy", f"pipewiresrc fd={fd} path={node} ! videoconvert ! {raw} ! "
                           "appsink name=sink"),
    ]


def due(now, last_read, interval):
    """Time for a new reading? A little slack, so frames arriving at exactly the reading
    rate (see pipeline_variants) aren't skipped because one came a few ms early."""
    return now - last_read >= interval * 0.8


def run_live(args):
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst

    Gst.init(None)
    print("A window picker will open - choose the game window (The Isle).", flush=True)
    open_fd, node, close_session = open_portal_stream()

    notifier = Notifier()
    logic = AlertLogic(notifier,
                       {"Hunger": args.hunger, "Thirst": args.thirst, "Oxygen": args.oxygen},
                       {"Hunger": args.repeat, "Thirst": args.repeat, "Oxygen": args.oxygen_repeat},
                       blind_after=300)
    state = {"last_frame": time.time(), "last_read": 0.0, "stalled": False,
             "dumped": False, "frames": 0}

    def on_sample(sink):
        sample = sink.emit("pull-sample")
        now = time.time()
        state["last_frame"], state["stalled"] = now, False
        state["frames"] += 1
        if not due(now, state["last_read"], args.interval):
            return Gst.FlowReturn.OK
        state["last_read"] = now

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
            if menu != state.get("menu"):
                state["menu"] = menu
                print(f"{menu} open - alerts paused" if menu else "Back in the game - watching",
                      flush=True)
            if menu:
                logic.paused()
                return Gst.FlowReturn.OK
            o2 = levels.get("Oxygen")
            if args.debug and o2 is not None and o2 < 0.999 and now - state.get("o2_dump", 0) > 30:
                state["o2_dump"] = now
                dump_o2(rgb)                             # lets you check what it "saw"
            if args.debug:
                print({k: None if x is None else level_text(k, x) for k, x in levels.items()},
                      f"({w}x{h})", flush=True)
            logic.update(levels, now)
            if args.debug:
                etas = {k: format_eta(logic.drain.eta(k, now)) for k in levels}
                print("  empty in:", {k: v for k, v in etas.items() if v} or "not known yet",
                      flush=True)
                if not state["dumped"]:
                    state["dumped"] = True
                    save_png(np.ascontiguousarray(rgb), Path.home() / "isle_alert_frame.png")
        finally:
            buf.unmap(info)
        return Gst.FlowReturn.OK

    def try_start(label, desc):
        """Start one pipeline variant. Returns the pipeline, or None after printing why not."""
        try:
            pipe = Gst.parse_launch(desc)
        except GLib.Error as exc:
            print(f"  [{label}] could not be built: {exc.message}", flush=True)
            return None
        sink = pipe.get_by_name("sink")
        sink.set_property("emit-signals", True)
        sink.set_property("max-buffers", 1)
        sink.set_property("drop", True)
        sink.set_property("sync", False)
        sink.connect("new-sample", on_sample)
        state["frames"] = 0
        pipe.set_state(Gst.State.PLAYING)
        bus, deadline = pipe.get_bus(), time.time() + 6
        while time.time() < deadline:
            msg = bus.timed_pop_filtered(200 * Gst.MSECOND,
                                         Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if msg is not None:
                if msg.type == Gst.MessageType.ERROR:
                    err, dbg = msg.parse_error()
                    print(f"  [{label}] failed: {err.message}\n      {dbg}", flush=True)
                else:
                    print(f"  [{label}] the stream ended immediately (EOS)", flush=True)
                pipe.set_state(Gst.State.NULL)
                return None
            if state["frames"]:
                print(f"Capture working ({label}).", flush=True)
                return pipe
        print(f"  [{label}] started but no frames arrived yet - is the game window drawing?",
              flush=True)
        return pipe

    pipeline = fd = None
    for i in range(len(pipeline_variants(-1, node, args.interval))):
        fd = open_fd()                                # fresh PipeWire connection each time
        label, desc = pipeline_variants(fd, node, args.interval)[i]
        pipeline = try_start(label, desc)
        if pipeline is not None:
            break
        os.close(fd)
    if pipeline is None:
        close_session()
        sys.exit("Could not start the capture pipeline (details above).\n"
                 "If it says 'no element pipewiresrc': sudo apt install gstreamer1.0-pipewire")

    loop = GLib.MainLoop()

    def on_bus(bus, msg):
        if msg.type in (Gst.MessageType.ERROR, Gst.MessageType.EOS):
            notifier.send("capture", "Isle alert stopped",
                          "The shared window was closed or the capture ended.")
            loop.quit()

    pipeline.get_bus().add_signal_watch()
    pipeline.get_bus().connect("message", on_bus)

    def watchdog():
        if time.time() - state["last_frame"] > 90 and not state["stalled"]:
            state["stalled"] = True
            notifier.send("stalled", "No frames from the game window",
                          "The game may be minimised or paused - alerts are not working.")
        return True

    GLib.timeout_add_seconds(10, watchdog)
    print(f"Watching. Alerting at hunger <= {args.hunger}%, thirst <= {args.thirst}%, "
          f"oxygen <= {args.oxygen}%. Ctrl+C to stop.", flush=True)
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    pipeline.set_state(Gst.State.NULL)
    os.close(fd)
    close_session()


# ------------------------------ helpers / test modes ------------------------


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--below", type=int, default=40, help="alert at or below this %% (default 40)")
    p.add_argument("--hunger", type=int, help="hunger threshold %% (overrides --below)")
    p.add_argument("--thirst", type=int, help="thirst threshold %% (overrides --below)")
    p.add_argument("--oxygen", type=int, default=60,
                   help="oxygen threshold %% (default 60; the O2 bar reads reliably up to ~80)")
    p.add_argument("--oxygen-repeat", type=int, default=15,
                   help="seconds between repeat oxygen alerts (default 15)")
    p.add_argument("--repeat", type=int, default=0,
                   help="seconds between repeat hunger/thirst alerts (default 0 = play only once)")
    p.add_argument("--interval", type=float, default=3.0, help="seconds between readings (default 3)")
    p.add_argument("--debug", action="store_true",
                   help="print every reading, save one frame and isle_o2_debug.png")
    p.add_argument("--test", metavar="PNG", help="measure a screenshot file and exit")
    p.add_argument("--version", action="version", version=f"Isle Alert {__version__}")
    p.add_argument("--notify-test", action="store_true", help="send a test notification and exit")
    args = p.parse_args()
    args.hunger = args.below if args.hunger is None else args.hunger
    args.thirst = args.below if args.thirst is None else args.thirst

    if args.test:
        run_test(args.test)
    elif args.notify_test:
        n = Notifier()
        n.send("Hunger", "Hunger test", "If you can see/hear this, alerts work.")
        time.sleep(2.5)
        n.send("Thirst", "Thirst test", "If you can see/hear this, alerts work.")
        time.sleep(2.5)
        n.send("Oxygen", "Oxygen test", "If you can see/hear this, alerts work.")
    else:
        run_live(args)


if __name__ == "__main__":
    main()
