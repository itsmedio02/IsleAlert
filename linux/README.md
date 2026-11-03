# Isle Alert for Linux

The Linux app (Ubuntu/Debian, GNOME or KDE, Wayland or X11). The icon reading, alert rules and
countdown are shared with the Windows app in `../isle_core.py`.

**Install:** download `isle-alert_<version>_all.deb` from the GitHub release, then
`sudo apt install ./isle-alert_<version>_all.deb` and start **The Isle Alert** from the app menu.

**Start it with the game:** in Steam, The Isle - Properties - Launch Options:
`isle-alert --with-game %command%` (other launch options such as `gamemoderun` go after `--with-game`).
Isle Alert starts the game, starts watching once the game window is open (found with `xwininfo`
from x11-utils), and closes when the game closes.

**Build it yourself:** `sh linux/build_deb.sh` (from the repo root) writes `linux/dist/isle-alert_<version>_all.deb`.
The version comes from `__version__` in `isle_core.py`. GitHub Actions builds it the same way on every push,
and attaches it to the release when a `v*` tag is pushed.

**Run without installing:** `python3 linux/isle_alert_gui.py` (window) or `python3 linux/isle_alert.py --help` (console).
Needs the packages listed under `Depends:` in `package/DEBIAN/control`.

| File | What it is |
|---|---|
| `isle_alert_gui.py` | The window app |
| `isle_alert.py` | Console version + the Linux pieces (screen-share portal, notifications, sounds) |
| `package/` | Everything else that goes in the .deb (launcher, menu entry, icons, control file) |
| `build_deb.sh` | Builds the .deb |
