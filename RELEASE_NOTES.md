**For Windows 10 and 11 (64-bit).** Download **`NameOCR-Setup-…-x64.exe`** below and run it. Windows may say "Windows protected your PC". Click **More info → Run anyway** (NameOCR isn't signed by a paid publisher). See the README for setup.

Already have NameOCR? Just run the new installer over it; your settings are kept.

New in 1.0.3:
- **Better player tracking.** Players are followed by how their row looks, so nobody is dropped or counted twice, even in lobbies where people keep leaving and rejoining, or when voice-chat icons appear
- **More accurate names.** One scoring model now weighs every possible account on both the username and the display name at once. More names come out right and fewer are guessed: anything the screen genuinely can't settle is marked "needs a look" with the reason. Also handles names where every O/0 was misread (like BL00DYL0V3RSS), and always checks an account's lookalike twins before verifying it
- **No more giving up when Roblox is busy.** NameOCR waits out Roblox's lookup limit by itself, shows each name as soon as it's checked (with its group status), and **Stop checking** ends it early. Big lobbies take a minute or two
- **Fewer lookups.** Lookalike spellings are tried in order of how likely they are, roughly halving the requests per scan
- **Safer copying.** Names still being checked, and names whose account wasn't found, are left out of the copy
- **Clearer colours.** Names left out by default keep their amber/red colour; grey now only means you left a name out yourself
- **Explanations.** New options in Settings → Other: **Show details when hovering a name** (why it was decided that way) and **Show confidence scores**. Both off by default
- **Simpler Tuning.** One slider, **Lookalike spellings per name**, replaces Rate limit wait, Trust username and the tracking settings

From 1.0.2:
- **More accurate names.** Better at names with a lost underscore plus an O/0 mix-up, and at telling apart real accounts whose names look identical on screen (l/I/1, O/0, S/5, missing underscores). It decides by display name or profile picture, and when neither settles it, the player is marked "needs a look" with both accounts named instead of guessing
- **Faster checks.** Lookalike spellings are tried most-likely-first and stop as soon as the answer is clear; rescanning the same server within 30 minutes reuses the names it already looked up (group membership is always checked fresh)
- **Roblox busy?** NameOCR keeps retrying for up to a minute (new **Rate limit wait** setting). If Roblox still won't answer, the status line says so and the affected players are marked "needs a look"
- **Skipped-player marker.** An amber ⋯ in the list shows where you scrolled too fast and players may have been missed
- **Auto-stop.** A scan you forget to stop ends by itself after 5 minutes (setting)
- **Stays small.** The log is capped at about 1 MB, and only the newest 20 scans' saved frames are kept
- **Clean uninstall.** Uninstalling now also removes your settings, log and saved frames
- **Simpler Tuning.** Removed three settings that are now fixed at their best values (Readings per name, Lookalike limit, Two-slip search); added Trust username, Rate limit wait and Auto-stop

From 1.0.1:
- The installer works like a normal one: choose where to install, install for just you or everyone, and choose whether to add a desktop shortcut

From 1.0.0, the first public version:
- Scans the Esc menu's People list while you scroll and reads every username
- Checks each name against Roblox, fixing common misreads (O/0, S/5, l/I/1, missing underscores…)
- Group membership check, with saved groups and instant switching
- Click names to leave them out; copy the rest space-separated
- Tuning settings with explanations, and a "How it works" chart
