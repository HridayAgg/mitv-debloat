# Safety model

## Three independent layers

**1. Per-user removal only.**
`pm uninstall -k --user 0 <pkg>` removes a package for user 0, which is you.
The APK stays in `/system` or `/data`. Android's `cmd package install-existing`
brings it back byte-for-byte. A factory reset also brings it back. Nothing this
tool does is permanent.

**2. A hard-coded blocklist.**
Even if a rule file is wrong, `apply` refuses packages matching these patterns:

```
android                       com.android.systemui
com.android.settings          com.android.tv.settings
com.android.providers.*       com.android.permissioncontroller
com.android.bluetooth         com.android.phone
com.android.shell             com.android.inputmethod.*
com.android.packageinstaller  com.google.android.gms
com.google.android.gsf        com.droidlogic.*
com.amlogic.*                 com.mitv.tvservice*
com.mitv.system               com.xiaomi.mitv.service
```

The Bluetooth one matters more than it looks: remove the Bluetooth stack and a
Bluetooth-only remote stops working, which can leave you with no way to operate
the TV.

**3. A ledger.**
Every `apply` writes one JSON line per package with the batch id, result, model,
Android version and original APK path. `restore --last` replays the most recent
batch; `restore --all` replays everything successful.

## Risk levels

| level | meaning | example |
|---|---|---|
| `safe` | no user-facing feature; ads, promos, telemetry, storefront | `com.mitv.videoworld` |
| `caution` | removable, but a real feature depends on it | Netflix, OTA updater, Play Store |
| `protected` | removing it breaks the TV or the remote | `com.android.systemui` |
| `unknown` | not in the database; no opinion | anything new |

`unknown` packages are **never** touched by a profile. They are listed so you
can `mitv inspect <pkg>` them yourself.

## Rules that are gated

`com.mitv.tvhome` (PatchWall) and `com.google.android.tvlauncher` carry
`requires: ["home-replacement"]`. The gate checks the resolved
`android.intent.category.HOME` handler. If it is still a stock launcher, the
package is skipped with a reason — removing the launcher with nothing to replace
it leaves a black screen you cannot navigate.

## What is genuinely risky

- **OTA updater** (`com.mitv.upgrade`). Removing it stops a firmware update from
  reinstalling bloat. It also stops security patches. This is the one rule where
  "debloat" and "stay patched" actually conflict; it is opt-in for that reason.
- **Play Store** (`com.android.vending`). Removing it breaks licence checks for
  paid apps and stops app updates.
- **Chromecast built-in** (`com.google.android.apps.mediashell`). Removes casting
  from phone to TV.
- **Voice/Assistant** (`com.google.android.katniss`). Marked protected so the
  remote's mic button keeps working.

## Things that look like bloat but are not

| package | why it stays |
|---|---|
| `com.droidlogic.*`, `com.amlogic.*` | Amlogic SoC TV input, HDMI and tuner |
| `com.mitv.tvplayer` | live TV and input switching |
| `com.android.bluetooth` | Bluetooth remote pairing |
| `com.google.android.gms` | account sign-in, Play, every Google-dependent app |
| `com.android.tv.settings` | the only settings UI |

## Honest limits

- This does not remove ads rendered inside third-party apps. It removes apps
  that serve them.
- Some preinstalls return `DELETE_FAILED_INTERNAL_ERROR` without root. The tool
  reports the failure and continues; that package simply stays.
- Firmware updates can reinstall removed packages. Re-run `mitv scan` after an
  update and re-apply.
- `inspect` scans dex strings and the manifest. An SDK loaded at runtime from
  the network, or shipped as a native `.so`, will not appear. A clean result is
  not a guarantee.

## Before you run `apply` for the first time

```bash
mitv scan           # what is actually here
mitv inspect        # what the "safe" ones actually contain
mitv snapshot       # props, package list, settings
mitv apply --profile light --dry-run
```
