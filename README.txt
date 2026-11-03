Isle Alert - hunger / thirst / oxygen alerts for The Isle (Windows 10/11)

1. Right-click the zip - Extract All.
2. Open the IsleAlert folder and double-click IsleAlert.exe.
   Keep everything in that folder together (the .wav files and _internal folder).
3. Tip: right-click IsleAlert.exe - Send to - Desktop (create shortcut).

No Python or other installs needed.

Start it together with the game (optional):
   In Steam, right-click The Isle - Properties - Launch Options, and enter
   "C:\path\to\IsleAlert\IsleAlert.exe" --with-game %command%
   (use the real path to your IsleAlert.exe, in quotes). To get the path:
   hold Shift, right-click IsleAlert.exe - "Copy as path", and paste it in
   place of "C:\path\to\IsleAlert\IsleAlert.exe" (it comes with the quotes).
   Already have launch options? Put them after --with-game. When you press Play,
   Isle Alert opens, starts the game, starts watching by itself once the game
   window is open, and closes when you quit the game.

If Windows shows "Windows protected your PC": click "More info" - "Run anyway".
This appears because the app isn't code-signed; it only reads the game's HUD
pixels and does not touch the game itself.
