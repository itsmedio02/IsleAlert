# Isle Alert dev log

Every change to Isle Alert, newest first. Each version starts with a `## vX.Y.Z` heading.
When a version is tagged, its section here becomes the GitHub release notes, and the Discord bot posts it.

<!--
How to add a version: copy this above the newest entry and fill it in.

## v1.2.0 - 2026-12-01
- **Windows:** what changed on Windows
- **Linux:** what changed on Linux
- **Both:** what changed everywhere
-->

## v1.1.3 - 2026-11-02
- **Linux:** Alert notifications now disappear by themselves after 3 seconds instead of staying on screen until you close them. They still show over the fullscreen game.

## v1.1.2 - 2026-11-02
- **Both:** New **Volume** slider under "Alert sounds", with a ▶ button to preview it. It works for the built-in and your own sounds and is remembered.
- **Windows:** Fixed hunger showing "unreadable" when the game runs in **windowed** mode. The app now ignores the window's title bar and borders.

## v1.1.1 - 2026-11-02
- **Windows:** Isle Alert now comes as a ready-to-run `IsleAlert.exe`. No Python or other installs needed: unzip and run.
