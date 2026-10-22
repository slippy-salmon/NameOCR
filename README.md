# NameOCR

Takes names at the end of a Roblox expedition for you. Open the Esc menu's **People** list, press a key, scroll to the bottom, press the key again, and NameOCR reads every player's username, checks each one against Roblox (and your group), and gives you a space-separated list to copy.

## Install

1. Download **`NameOCR-Setup-x.y.z.exe`** from the [latest release](../../releases/latest).
2. Run it. It asks whether to install just for you (no admin rights needed) or for everyone on the PC, lets you pick the install folder, and adds NameOCR to your Start menu (and desktop, if you tick the box).
3. Windows will probably warn you, because NameOCR isn't from a paid "verified publisher":
   - **"Windows protected your PC"**: click **More info → Run anyway**.
   - **"Smart App Control blocked…"**: Smart App Control blocks every unsigned program, with no button to skip it. It can only be turned off in Windows Security → App & browser control, which is your call. Ask in the Discord if you're unsure.
   - Some antivirus programs may flag it because it listens for its hotkeys while you're in Roblox and takes screenshots of the menu. That's all it does with them (see Privacy below).

Updating: when there's a new version, NameOCR shows a banner with a download link. Run the new installer; your settings are kept.

## First-time setup

Open **⚙ Settings**:

- **General → Saved groups:** paste your group's ID or the link to its page and click **Add**.
- **General → Hotkeys:** the defaults are F8 (start/stop scan) and F9 (copy). F9 also opens Roblox's developer console, so you'll probably want a different copy key.
- **General → Capture area (optional):** with the Esc menu open on People, click **Select area…** and drag a box from just under the tabs to just above Leave / Respawn / Resume. Leaving it on "whole display" also works.

## Using it

1. In Roblox, press Esc, open **People**, switch to **list view**, and scroll to the top.
2. Press your scan key (you'll hear a beep), scroll **slowly** to the bottom, and press it again.
3. NameOCR comes to the front with the names:
   - white = verified, amber = needs a look, red = not in the group (left out of the copy by default)
   - **click a name** to leave it out of the copy or put it back
   - **double-click a username** in the table to fix it by hand
   - **Copy names** (or your copy key) copies the kept names, space-separated
4. Switching the **Group** dropdown rechecks the current list against that group instantly.

If the status line says you scrolled too fast, scroll back over that part before stopping.

**ⓘ How it works** shows the whole process as a flow chart. **Settings → Tuning** has every threshold, with an explanation on each ⓘ; the defaults are tuned to work as they are.

## Privacy

NameOCR only talks to Roblox's public APIs (username lookups, group membership, profile pictures, group names) and GitHub (to check for updates). It never logs in to anything. Screenshots stay in memory unless you turn on **Save scan frames**. Your settings, the log and any saved frames live in `%APPDATA%\NameOCR` (**Settings → Other → Open data folder**).

## Troubleshooting

- **No players found:** make sure the menu is on People in **list view**, and check the capture area.
- **Something went wrong:** send `nameocr.log` from the data folder to whoever gave you NameOCR. It contains the usernames from your scans and nothing else about you.

## Building it yourself

Python 3.12, then `pip install -r requirements.txt` and `python app.py` (with [Tesseract](https://github.com/UB-Mannheim/tesseract/wiki) installed). The installer is built by GitHub Actions: **Actions → Build installer → Run workflow** builds it and publishes a release named after `VERSION` in `config.py` (bump the version first).
