# Rust Blaster visual wash bot

A **desktop screen macro** for Roblox Rust Blaster. It detects brown/orange rust *pixels* in the playfield, moves the **pink in-game wash cursor** onto a thick rusty patch, holds the wash beam briefly, then takes another screenshot and repeats. It does **not** use a skeleton-key template, so it can work on other item shapes. It also looks for an X on a centered popup; if a likely popup has no recognizable X, it **pauses instead of guessing or clicking a purchase button**.

**Mouse-motion fix:** Roblox can ignore a cursor teleport even though it accepts a mouse click. On Windows this version sends relative mouse-motion events via the standard Win32 `SendInput` API and checks successive screenshots to confirm the *game's* pink cursor actually reached the rust **before clicking**. If motion is ignored, it pauses instead of spraying the wrong place. No ordinary desktop macro can guarantee that a game which blocks synthetic input will accept it.

The defaults were tuned against the supplied 1902×1002 reference screenshot. This is not a Roblox exploit or an in-game script. It has not been tested against a live game in this environment, so **check the preview and run the no-click mouse test before enabling washing**. Only use automation where permitted by the game's rules.

## Install (Windows 10/11 or macOS; Python 3.10+)

Open a terminal in this repository. On Windows PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m rust_blaster_bot --inspect tests\fixtures\rust_key_reference.jpg --output preview.png
```

On macOS, replace `py -3` with `python3`, and `.\.venv\Scripts\python.exe` with `.venv/bin/python`. Allow Screen Recording and Accessibility/Input Monitoring for your terminal/Python in macOS Privacy settings. Offline `--inspect` and the tests also work on Linux, but live mouse control is supported on Windows/macOS.

## Before starting

1. Open Rust Blaster, make the game **visible** and focus its window. Keep the whole playfield on screen. For a windowed game, specify its desktop rectangle with `--region x,y,width,height`; otherwise the first monitor is captured. `--list-monitors` shows available indices.
2. Check what the bot would see **without clicking**:

   ```powershell
   .\.venv\Scripts\python.exe -m rust_blaster_bot --preview preview.png
   ```

   The cyan box is the allowed washing area, orange highlights are detected rust, the green cross is the next target, **the small blue marker labeled `cursor` is the detected pink wash ring**, and magenta marks a recognized popup/X. The green cross must be on rust, **not** on UI, pumpkins or clean metal. The included sample can also be inspected offline with the install command above.
3. **Verify movement without clicking anything.** With rusty item visible, run:

   ```powershell
   .\.venv\Scripts\python.exe -m rust_blaster_bot --test-mouse mouse-check
   ```

   Focus Roblox and press **F8** once. The bot sends mouse movement, never presses the mouse button, then exits after saving `mouse-check-before.png` and `mouse-check-after.png`. The blue `cursor` marker in the *after* image should be near the green rust target. If it does not move, **do not run normal washing**: check that the game has focus, the captured monitor/`--region` is correct, and Python has input permissions. If the game rejects software-generated mouse motion, the bot will report that rather than blindly click. Share the two screenshots if you want help diagnosing your setup.
4. When the movement test succeeds, run:

   ```powershell
   .\.venv\Scripts\python.exe -m rust_blaster_bot
   ```

   The bot starts **PAUSED**. Press **F8** to start/pause and **F7** or **Ctrl+C** to stop. Moving the pointer to the **upper-left corner of the captured game rectangle** also stops it. The mouse button is released when paused, stopped or focus is lost. In fallback OS-cursor mode it also pauses if you move the pointer by hand; in game-cursor mode Roblox may lock/recenter the OS pointer, so use F8/F7 to interrupt. On a Mac, you may need the `Fn` key for F7/F8.

If the pink ring is not visible on your screen, the bot pauses rather than making unverified clicks. `--no-reticle-check` is an **unsafe fallback** that verifies only the OS cursor; use it only when you have independently confirmed that your game responds to those movements. It is **not** a workaround for Roblox blocking software input.

The macro waits when there is no rust; if the game spawns another item, it starts cleaning that one. It does not buy upgrades, press reward/claim buttons, or advance paid prompts.

## Popups

A **central dark, navy or light dialog** with a visible contrasting X is recognized by shape; the same X must be seen in two consecutive frames before clicking. It will try to close the same stubborn dialog at most twice. An unrecognized central modal pauses the bot to avoid spraying/clicking through it. Other popup designs, particularly small, off-center or unusually colored ones, may need a template. A single screenshot cannot establish the appearance of every possible popup.

To teach an additional close button, capture a screenshot while that popup is showing (this command never clicks):

```powershell
.\.venv\Scripts\python.exe -m rust_blaster_bot --screenshot popup.png
```

Crop **just the X button** (include its background/border) into e.g. `close.png`, at least 8×8 pixels, using an image editor. Then run with `--close-template close.png`. You can repeat that option for different popup styles. Templates are searched only in the central part of the captured game, must match at least 91%, and require two-frame confirmation. If a popup is not detected, do not leave the macro unattended: pause, capture its screenshot and add a template. Never crop an OK, Buy, or Claim button as a close template.

## Tuning

```powershell
# Window starts at desktop pixel (80,40), measures 1500×850:
.\.venv\Scripts\python.exe -m rust_blaster_bot --region 80,40,1500,850 --preview preview.png

# Narrow the safe playfield / adjust color for another lighting or item:
.\.venv\Scripts\python.exe -m rust_blaster_bot --roi 0.25,0.22,0.87,0.91 --hue 3,27 --value 20,205 --preview preview.png

# Slower/longer wash beam; use only after inspecting the preview:
.\.venv\Scripts\python.exe -m rust_blaster_bot --pulse 0.5 --region 80,40,1500,850
```

`--roi` uses **left,top,right,bottom as fractions of the captured game image**; it should exclude the upgrade sidebar, top banners, right toolbar and decorations at the bottom. `--hue` uses OpenCV's 0–179 hue scale (not degrees). `--sat-min` and `--value` are on the 0–255 scale. The detector rechecks the screenshot after every pulse, skips patches that do not respond, and pauses after repeated unsuccessful pulses. If it keeps aiming at clean metal, reduce the color range or make the ROI smaller. If it misses rust, adjust the range while inspecting a screenshot of **that item**.

On Windows, the foreground window title must contain `Roblox` by default. If the game is running in a differently named window, set `--window-title NAME`; `--no-focus-check` is available but removes an important safety guard. The screenshot and mouse coordinates must use the same display scaling (the program requests per-monitor DPI awareness on Windows). On macOS the relative input path uses `pynput`; allow Screen Recording, Accessibility and Input Monitoring for Python/your terminal.

If `--test-mouse` reports that the ring did not move, changing the rust color thresholds or making pulses longer will **not** fix the input problem. Please provide the before/after screenshots, OS, screen scaling and whether Roblox is fullscreen or windowed. Some game/client configurations ignore injected movement altogether; this program does not bypass such restrictions. If the ring **does** align but the rust does not clear, try a longer `--pulse 0.8` while watching one item; the bot pauses after repeated pulses with no visual progress. A short recording showing a *manual* successful wash would help adapt the beam action if this game's controls require a drag rather than holding the button.

## Tests

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

The tests use a small version of the provided screenshot plus synthetic **non-key items and popups**, a simulated game that locks the OS pointer while moving the pink ring, and a fake Win32 `SendInput` driver. They verify that the bot never clicks unless the in-game cursor aligns, releases the button on interruption, and stops after no progress. They do not simulate the game's actual beam physics or guarantee that every popup theme can be recognized.
