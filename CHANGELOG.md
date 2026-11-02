# Isle Alert dev log

Every change to Isle Alert, newest first. Each version starts with a `## vX.Y.Z` heading.
When a version is tagged, its section here becomes the GitHub release notes, and the Discord bot posts it.

<!--
How to add a version: copy this above the newest entry and fill it in.

## vX.Y.Z - YYYY-MM-DD
- **Windows:** what changed on Windows
- **Linux:** what changed on Linux
- **Both:** what changed everywhere
-->

## v1.3.0 - 2026-11-02
- **Both:** Hunger now matches the game's own number. It used to read too high, by about 2% at half and up to 7% around 70%. It was checked against the in-game hover value (within 0.3%). Thirst got the same fix; it should be just as accurate, but that hasn't been checked against the game yet.
- **Both:** New **Alert in** column: an estimate of how long until hunger, thirst or oxygen reaches the alert level you set, based on how fast it has been dropping. Once it's below that level it shows the time until empty instead (e.g. "empty ~14 min"). It appears after a minute or two of watching and starts over when you eat, drink or surface. Low-stat alerts now say how long until empty too, e.g. "Hunger is low: 38% - empty in ~14 min".
- **Both:** Above about 88% the stomach icon looks full, so the app can't tell 90% from 100%. It now shows **88%+** there instead of pretending it's 100% (thirst: 92%+).
- **Both:** The version number is shown in the window's title bar, and `--version` prints it.
- **Linux:** The `.deb` is now built on GitHub and attached to every release, next to the Windows zip.
- **Both:** `--test screenshot.png` saves its check image in your temp folder instead of next to your screenshot.

## v1.2.0 - 2026-11-02
- **Both:** The window is now split into two tabs, **Alerts** and **Settings**, so it takes up less space. Start/Stop, Test alert and the status line stay visible on both tabs.
- **Both:** New **Theme** option in Settings: Light, Dark, or the same as your system. It's remembered.

## v1.1.3 - 2026-11-02
- **Linux:** Alert notifications now disappear by themselves after 3 seconds instead of staying on screen until you close them. They still show over the fullscreen game.

## v1.1.2 - 2026-11-02
- **Both:** New **Volume** slider under "Alert sounds", with a ▶ button to preview it. It works for the built-in and your own sounds and is remembered.
- **Windows:** Fixed hunger showing "unreadable" when the game runs in **windowed** mode. The app now ignores the window's title bar and borders.

## v1.1.1 - 2026-11-02
- **Windows:** Isle Alert now comes as a ready-to-run `IsleAlert.exe`. No Python or other installs needed: unzip and run.
