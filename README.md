# mitv — debloat a Xiaomi Mi TV, reversibly

A small, audit-friendly CLI that removes preinstalled apps, PatchWall promo
surfaces and background telemetry from a Mi TV (or any Android TV) over ADB.

It is built around one rule: **nothing is deleted, only uninstalled for your
user**, which Android can always undo. Every removal is recorded so
`mitv restore` puts the TV back exactly as it was.

```
$ mitv scan
device     : Xiaomi Mi TV 4X
android    : Android 9 (SDK 28)
home       : com.mitv.tvhome
packages   : 214 installed / 0 removed-for-user / 37 unknown
classified : 9 safe, 18 caution, 12 protected

==============================================================================
SAFE to remove
==============================================================================
   com.mitv.alwaysonvideo      Always-on video promos  -- Screensaver promo video
   com.mitv.videoworld         PatchWall video shelf / promo feed
   com.mitv.shop               Mi Store / shopping surface
   com.mitv.mtservices         Mi TV services / telemetry
   ...
```

## What it actually does

| | |
|---|---|
| **Removes** | `pm uninstall -k --user 0 <pkg>` — the app disappears for your user, the system image is untouched |
| **Restores** | `cmd package install-existing --user 0 <pkg>` — from a ledger file, exact and complete |
| **Blocks** | A hard-coded safety net refuses core packages (SystemUI, Settings, Play services, Bluetooth, Amlogic TV input, Mi TV core services) even if the rule database is wrong |
| **Proves** | `mitv inspect` pulls an APK and scans its dex/manifest for 40 known ad and analytics SDK fingerprints before you decide |
| **Never** | flashes firmware, roots, unlocks a bootloader, or writes to `/system` |

Removal is per-user, so a factory reset also restores everything. The trade-off
is that firmware updates can bring bloat back — that is what the `ota` rules are
for, and they are opt-in only.

## Quick start

Prerequisites: Python 3.8+, `adb` from Android platform-tools, and the TV and
computer on the same network.

```bash
# 1. get adb
brew install android-platform-tools     # macOS
sudo apt install adb                    # Debian/Ubuntu
winget install Google.PlatformTools     # Windows

# 2. get this repo
git clone https://github.com/HridayAgg/mitv-debloat.git
cd mitv-debloat
./install.sh                            # symlinks ~/.local/bin/mitv

# 3. point it at the TV (TV side: see docs/MI-TV-SETUP.md)
mitv doctor
mitv connect 192.168.1.42

# 4. look before you leap
mitv scan
mitv inspect                            # fingerprints the "safe" candidates
mitv apply --profile light --dry-run

# 5. commit, then verify
mitv apply --profile light
mitv scan

# 6. if anything feels wrong
mitv restore --last
```

Nothing is removed until you either pass `--yes` or type `remove` at the prompt.

## Commands

| command | what it does |
|---|---|
| `mitv doctor` | checks adb, its version, and whether the TV is authorized |
| `mitv connect <ip>` | `adb connect <ip>:5555` with actionable errors |
| `mitv scan [--json] [--unknown]` | read-only classification into safe / caution / protected / unknown; writes a Markdown report |
| `mitv inspect [pkg...]` | pulls APKs, hashes them, scans for ad/analytics SDK strings; writes JSON evidence |
| `mitv apply --profile <p> [--dry-run] [--yes]` | removes the profile's packages, logging each result |
| `mitv apply --packages a b c` | explicit list instead of a profile |
| `mitv restore --last\|--all\|--list` | undo from the ledger |
| `mitv snapshot` | backs up props, package list and all three settings scopes |
| `mitv privacy` | sets `limit_ad_tracking=1` and a null advertising ID |
| `mitv launcher status\|install\|set` | inspect or replace the home launcher |

## Profiles are cumulative

- **`light`** — ads, promos, telemetry, the second app store, game center,
  manual. Nothing you would miss. Start here.
- **`patchwall`** — `light` plus the PatchWall launcher itself. Gated: it will
  not run until a third-party launcher is the active HOME handler.
- **`aggressive`** — `patchwall` plus optional media apps (Netflix, Prime,
  Spotify, OTT preinstalls), the Google TV launcher, Chromecast built-in, Play
  Store, and the OTA updater.

`--skip pkg1 pkg2` leaves individual packages alone.

## Replacing PatchWall (the only way to kill home-screen ads)

The ad tiles live inside the launcher, so they cannot be removed without
removing the launcher. Install a replacement first:

```bash
# sideload a launcher APK you trust, e.g. Projectivy
mitv launcher install --apk ~/Downloads/projectivy.apk
mitv launcher set --component com.spocky.projengmenu/.MainActivity
mitv launcher status            # must not be com.mitv.tvhome
mitv apply --profile patchwall
```

Until `launcher status` shows a non-stock home, the gate keeps PatchWall in
place on purpose.

## State

Everything lives in `~/.mitv-debloat` (override with `MITV_STATE`):

```
~/.mitv-debloat/
├── ledger.jsonl              # every removal and restore, with results
├── reports/<ts>/report.md    # human-readable classification
├── reports/<ts>/snapshot.json
├── reports/<ts>/apk-inspection.json
├── apks/<ts>/                # pulled APKs (delete when done)
└── backups/<ts>/             # props, packages, settings
```

## Limits, stated plainly

- Ads **inside** third-party apps (Hotstar, MX Player, …) stay. Removing an app
  removes its ads; it does not clean another app's ads.
- Some preinstalls are not uninstallable without root. `apply` reports these as
  failures and moves on — that is expected, not a bug.
- Package names differ between Mi TV generations. The database is a prior; the
  device is the truth. `mitv scan` shows what is really installed.
- `inspect` is a string-pool scan, not a decompiler. A miss does not prove an
  APK is clean.
- Firmware updates can restore removed apps.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The suite runs the real CLI against `tests/fake_adb`, covering classification,
the hard block, the launcher gate, dry-run, apply/restore round-trip, and ad-SDK
detection.

## License

MIT.
