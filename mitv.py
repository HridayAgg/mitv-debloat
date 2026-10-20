#!/usr/bin/env python3
"""
mitv - evidence-first debloater for Xiaomi Mi TV and other Android TV devices.

Design rules this tool follows:

  1. Read-only by default. `scan` and `inspect` never change the TV.
  2. Removal is per-user (`pm uninstall --user 0`), which Android can always
     undo with `cmd package install-existing`. Nothing is deleted from /system.
  3. A hard-coded safety net blocks core packages even if the rule database
     is wrong or a rule was edited by hand.
  4. Every removal is written to a ledger, so `mitv restore` is exact.
  5. Classification can be corroborated by pulling the APK and scanning it for
     ad/analytics SDK fingerprints (`mitv inspect`), rather than trusting
     package names alone.

Usage:
    mitv doctor
    mitv connect 192.168.1.42
    mitv scan
    mitv inspect
    mitv apply --profile light --dry-run
    mitv apply --profile light
    mitv restore --last

Requires: Python 3.8+, adb (Android platform-tools) on PATH.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.1.0"
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATE = Path(os.environ.get("MITV_STATE") or (Path.home() / ".mitv-debloat"))

# ---------------------------------------------------------------------------
# Hard safety net. These patterns are refused by `apply` no matter what the
# rule database says. Removing any of them can brick the TV or kill the remote.
# ---------------------------------------------------------------------------
HARD_BLOCK = [
    r"^android$",
    r"^com\.android\.systemui$",
    r"^com\.android\.settings$",
    r"^com\.android\.tv\.settings$",
    r"^com\.android\.providers\.",
    r"^com\.android\.permissioncontroller$",
    r"^com\.android\.bluetooth$",
    r"^com\.android\.phone$",
    r"^com\.android\.shell$",
    r"^com\.android\.inputmethod\.",
    r"^com\.android\.packageinstaller$",
    r"^com\.google\.android\.gms$",
    r"^com\.google\.android\.gsf$",
    r"^com\.droidlogic\.",
    r"^com\.amlogic\.",
    r"^com\.mitv\.tvservice",
    r"^com\.mitv\.system",
    r"^com\.xiaomi\.mitv\.service",
]
HARD_BLOCK_RE = [re.compile(p) for p in HARD_BLOCK]

PROFILES = ("light", "patchwall", "aggressive")

# Launchers that are themselves ad surfaces / stock homes. A "home replacement"
# gate only passes when the active HOME handler is none of these.
STOCK_HOMES = {
    "com.mitv.tvhome",
    "com.android.tv.settings",
    "com.google.android.tvlauncher",
    "com.google.android.apps.tv.launcherx",
}

PROP_KEYS = [
    "ro.product.manufacturer",
    "ro.product.brand",
    "ro.product.model",
    "ro.product.device",
    "ro.board.platform",
    "ro.build.version.release",
    "ro.build.version.sdk",
    "ro.build.display.id",
    "ro.build.version.incremental",
    "ro.mitv.version",
    "ro.mitv.build.version",
]


class Fail(RuntimeError):
    """User-facing failure: printed without a traceback."""


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def looks_like_mi(blob: str) -> bool:
    return bool(re.search(r"xiaomi|\bmi(tv|box|tvbox)?\b|mitv|patchwall", blob))


# ---------------------------------------------------------------------------
# adb
# ---------------------------------------------------------------------------
class Adb:
    def __init__(self, exe: str = "adb", serial: str = None, verbose: bool = False):
        self.exe = exe
        self.serial = serial
        self.verbose = verbose

    def _run(self, args, timeout=180, check=True):
        argv = [self.exe]
        if self.serial:
            argv += ["-s", self.serial]
        argv += list(args)
        if self.verbose:
            print("+ " + " ".join(argv), file=sys.stderr)
        try:
            cp = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise Fail(
                "'%s' not found. Install Android platform-tools, or pass --adb /path/to/adb."
                % self.exe
            )
        except subprocess.TimeoutExpired:
            raise Fail("adb timed out: %s" % " ".join(argv[1:]))
        out = (cp.stdout or "").strip()
        err = (cp.stderr or "").strip()
        if check and cp.returncode != 0:
            raise Fail("adb failed (%d): %s" % (cp.returncode, err or out))
        return out or err

    def sh(self, command: str, check: bool = True, timeout: int = 180) -> str:
        return self._run(["shell", command], check=check, timeout=timeout)

    def devices(self) -> str:
        return self._run(["devices"])

    def pull(self, remote: str, local: Path) -> None:
        self._run(["pull", remote, str(local)], timeout=300)


# ---------------------------------------------------------------------------
# Device snapshot
# ---------------------------------------------------------------------------
def parse_props(text: str) -> dict:
    props = {}
    for line in text.splitlines():
        m = re.match(r"^\[([^\]]+)\]: \[(.*)\]$", line.strip())
        if m:
            props[m.group(1)] = m.group(2)
    return props


def parse_package_list(text: str) -> dict:
    """`pm list packages -f` -> {package: apk_path}"""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("package:"):
            continue
        body = line[len("package:"):]
        if "=" in body:
            apk, pkg = body.rsplit("=", 1)
        else:
            apk, pkg = "", body
        out[pkg.strip()] = apk.strip()
    return out


def parse_name_list(text: str) -> set:
    names = set()
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            names.add(line[len("package:"):].strip())
    return names


class Snapshot:
    def __init__(self, props, packages, enabled, disabled, ghosts, home):
        self.props = props
        self.packages = packages          # pkg -> apk path (installed for user)
        self.enabled = enabled
        self.disabled = disabled
        self.ghosts = ghosts              # uninstalled-for-user but still present
        self.home = home                  # resolved HOME package, or ""
        self.ts = stamp()

    @property
    def model(self) -> str:
        p = self.props
        bits = [
            p.get("ro.product.manufacturer", ""),
            p.get("ro.product.model", ""),
            p.get("ro.board.platform", ""),
        ]
        return " ".join(b for b in bits if b).strip() or "unknown"

    @property
    def model_key(self) -> str:
        p = self.props
        return " ".join(
            p.get(k, "")
            for k in ("ro.product.manufacturer", "ro.product.brand", "ro.product.model", "ro.board.platform")
        ).lower()

    @property
    def android(self) -> str:
        p = self.props
        rel = p.get("ro.build.version.release", "?")
        sdk = p.get("ro.build.version.sdk", "?")
        return "Android %s (SDK %s)" % (rel, sdk)


def resolve_home(adb: Adb) -> str:
    """Return the component currently handling ACTION_MAIN/CATEGORY_HOME."""
    for attempt in (
        "cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.HOME",
        "cmd package resolve-activity -c android.intent.category.HOME -a android.intent.action.MAIN",
    ):
        try:
            out = adb.sh(attempt, check=False)
        except Fail:
            continue
        comp = ""
        for line in out.splitlines():
            m = re.search(r"([A-Za-z][\w.]*)/([\w.$]+)", line)
            if m:
                comp = m.group(1) + "/" + m.group(2)
        if comp:
            return comp.split("/")[0]
    return ""


def collect(adb: Adb) -> Snapshot:
    props = parse_props(adb.sh("getprop"))
    packages = parse_package_list(adb.sh("pm list packages -f", check=False))
    if not packages:
        raise Fail("No packages returned. Is the TV authorized and awake?")
    enabled = parse_name_list(adb.sh("pm list packages -e", check=False))
    disabled = parse_name_list(adb.sh("pm list packages -d", check=False))
    with_uninstalled = parse_name_list(adb.sh("pm list packages -u", check=False))
    ghosts = (with_uninstalled - set(packages)) if with_uninstalled else set()
    home = resolve_home(adb)
    return Snapshot(props, packages, enabled, disabled, ghosts, home)


# ---------------------------------------------------------------------------
# Rules and classification
# ---------------------------------------------------------------------------
def load_json(name: str) -> dict:
    path = DATA / name
    if not path.exists():
        raise Fail("Missing data file: %s" % path)
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_db():
    db = load_json("packages.json")
    rules = {r["package"]: r for r in db.get("rules", [])}
    return db, rules


def hard_blocked(pkg: str) -> bool:
    return any(rx.search(pkg) for rx in HARD_BLOCK_RE)


def classify(snapshot: Snapshot, rules: dict):
    """Return (items, unknown). Items are dicts ready for JSON/report."""
    items = []
    unknown = []
    for pkg in sorted(snapshot.packages):
        rule = rules.get(pkg)
        installed = pkg in snapshot.enabled or pkg in snapshot.disabled
        if rule is None:
            unknown.append(
                {
                    "package": pkg,
                    "apk": snapshot.packages.get(pkg, ""),
                    "installed": installed,
                    "disabled": pkg in snapshot.disabled,
                }
            )
            continue

        risk = rule.get("risk", "caution")
        note = rule.get("note", "")

        # Model scoping: if a rule declares applies_to, only trust it on a match.
        applies = rule.get("applies_to") or []
        if applies and not any(re.search(p, snapshot.model_key or "", re.I) for p in applies):
            if risk == "safe":
                risk = "caution"
            note = (note + " " if note else "") + "[unconfirmed for %s]" % snapshot.model

        # The safety net always wins.
        if hard_blocked(pkg):
            risk = "protected"
            note = (note + " " if note else "") + "[blocked by hard safety net]"

        items.append(
            {
                "package": pkg,
                "apk": snapshot.packages.get(pkg, ""),
                "name": rule.get("name", pkg),
                "category": rule.get("category", "unknown"),
                "risk": risk,
                "profiles": rule.get("profiles", []),
                "breaks": rule.get("breaks", ""),
                "requires": rule.get("requires", []),
                "note": note.strip(),
                "installed": installed,
                "disabled": pkg in snapshot.disabled,
                "blocked": risk == "protected",
            }
        )

    order = {"safe": 0, "caution": 1, "protected": 2, "unknown": 3}
    items.sort(key=lambda i: (order.get(i["risk"], 9), i["category"], i["package"]))
    return items, unknown


def gate_failures(snapshot: Snapshot, items, home_pkg: str):
    """Return {package: reason} for items whose prerequisites are not met."""
    removing = {i["package"] for i in items}
    blocked = {}
    for item in items:
        for gate in item.get("requires") or []:
            if gate == "home-replacement":
                home = home_pkg or ""
                if not home or home in STOCK_HOMES or home in removing:
                    blocked[item["package"]] = (
                        "no third-party launcher is the active HOME "
                        "(current: %s) - install one first" % (home or "unknown")
                    )
    return blocked


def select(snapshot, items, profile, home_pkg):
    """Pick removable items for a profile, respecting gates and the safety net."""
    idx = PROFILES.index(profile) if profile in PROFILES else 0
    want = set(PROFILES[: idx + 1])
    chosen = []
    skipped = []
    for item in items:
        if item["risk"] == "protected" or item["blocked"]:
            continue
        if not item["installed"]:
            continue
        if not (set(item["profiles"]) & want):
            continue
        chosen.append(item)
    gated = gate_failures(snapshot, chosen, home_pkg)
    final = []
    for item in chosen:
        if item["package"] in gated:
            skipped.append((item, gated[item["package"]]))
        else:
            final.append(item)
    return final, skipped


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def hr(char="-" , width=78):
    return char * width


def render_report(snapshot: Snapshot, items, unknown, path: Path):
    lines = []
    lines.append("# Mi TV debloat report")
    lines.append("")
    lines.append("- generated: `%s`" % snapshot.ts)
    lines.append("- device: **%s**" % snapshot.model)
    lines.append("- android: %s" % snapshot.android)
    for k in ("ro.build.display.id", "ro.build.version.incremental", "ro.board.platform"):
        if snapshot.props.get(k):
            lines.append("- %s: `%s`" % (k, snapshot.props[k]))
    lines.append("- active HOME: `%s`" % (snapshot.home or "unknown"))
    lines.append("- packages: %d installed, %d removed-for-user" % (len(snapshot.packages), len(snapshot.ghosts)))
    lines.append("")

    titles = {
        "safe": "Safe to remove (ads, telemetry, storefront, stubs)",
        "caution": "Removable, but check you do not want it",
        "protected": "Never remove (core system / remote / display)",
    }
    for risk in ("safe", "caution", "protected"):
        group = [i for i in items if i["risk"] == risk]
        lines.append("## %s (%d)" % (titles[risk], len(group)))
        lines.append("")
        if not group:
            lines.append("_none_")
            lines.append("")
            continue
        lines.append("| package | what it is | category | why |")
        lines.append("|---|---|---|---|")
        for i in group:
            why = i["breaks"] or i["note"] or ""
            lines.append("| `%s` | %s | %s | %s |" % (i["package"], i["name"], i["category"], why))
        lines.append("")

    lines.append("## Installed but not in the rule database (%d)" % len(unknown))
    lines.append("")
    lines.append("Run `mitv inspect` to fingerprint these before deciding. Names are not evidence.")
    lines.append("")
    for u in unknown:
        lines.append("- `%s`" % u["package"])
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def print_summary(snapshot: Snapshot, items, unknown):
    counts = {}
    for i in items:
        counts[i["risk"]] = counts.get(i["risk"], 0) + 1
    print("device     : %s" % snapshot.model)
    print("android    : %s" % snapshot.android)
    print("home       : %s" % (snapshot.home or "unknown"))
    print("packages   : %d installed / %d removed-for-user / %d unknown" %
          (len(snapshot.packages), len(snapshot.ghosts), len(unknown)))
    print("classified : %d safe, %d caution, %d protected" %
          (counts.get("safe", 0), counts.get("caution", 0), counts.get("protected", 0)))
    print()


def print_items(items, risk=None, limit=None):
    shown = [i for i in items if risk is None or i["risk"] == risk]
    if limit:
        shown = shown[:limit]
    for i in shown:
        flag = {"safe": "  ", "caution": "!!", "protected": "XX"}.get(i["risk"], "  ")
        what = i["name"]
        if i["breaks"]:
            what += "  -- %s" % i["breaks"]
        print("%s %-42s %s" % (flag, i["package"], what))
    if not shown:
        print("   (none)")


# ---------------------------------------------------------------------------
# APK inspection (static, no execution)
# ---------------------------------------------------------------------------
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def apk_strings(path: Path, limit_bytes=48 << 20) -> str:
    """Extract printable ASCII runs from the APK's dex + manifest.

    This is a string-pool scan, not a decoded manifest. It shows what an APK
    *contains*, which is weaker than `inspect_android_package` style analysis
    but strong enough to spot bundled ad SDKs.
    """
    blobs = []
    with zipfile.ZipFile(str(path)) as zf:
        names = zf.namelist()
        wanted = [n for n in names if re.match(r"^classes\d*\.dex$", n)]
        if "AndroidManifest.xml" in names:
            wanted.append("AndroidManifest.xml")
        budget = limit_bytes
        for name in wanted[:8]:
            if budget <= 0:
                break
            data = zf.read(name)
            budget -= len(data)
            blobs.append(data)
    raw = b"\n".join(blobs)
    runs = re.findall(rb"[\x20-\x7e]{5,}", raw)
    return "\n".join(r.decode("latin-1") for r in runs)


def inspect_apk(path: Path, sdk_map: dict) -> dict:
    text = apk_strings(path)
    hits = []
    for needle, label in sdk_map.items():
        dotted = needle.replace("/", ".")
        if needle in text or dotted in text:
            hits.append({"sdk": label, "needle": dotted})
    perms = sorted(set(re.findall(r"android\.permission\.[A-Z_]+", text)))
    interest = [p for p in perms if p.rsplit(".", 1)[-1] in {
        "INTERNET", "ACCESS_NETWORK_STATE", "ACCESS_WIFI_STATE",
        "READ_PHONE_STATE", "GET_ACCOUNTS", "RECORD_AUDIO", "CAMERA",
        "ACCESS_FINE_LOCATION", "ACCESS_COARSE_LOCATION", "QUERY_ALL_PACKAGES",
        "SYSTEM_ALERT_WINDOW", "WRITE_SECURE_SETTINGS", "READ_EXTERNAL_STORAGE",
    }]
    return {
        "apk": str(path),
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
        "ad_sdks": hits,
        "permissions_of_interest": interest,
        "verdict": (
            "ad/analytics SDK present" if hits
            else "no known ad SDK string found (not proof of absence)"
        ),
    }


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------
def ledger_path() -> Path:
    return STATE / "ledger.jsonl"


def ledger_add(entry: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    with open(ledger_path(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def ledger_read() -> list:
    p = ledger_path()
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def do_doctor(adb: Adb, args) -> int:
    print("mitv %s" % VERSION)
    exe = shutil.which(adb.exe) if not os.path.sep in adb.exe else (adb.exe if os.path.exists(adb.exe) else None)
    if not exe:
        print("[FAIL] adb not found on PATH")
        print("       macOS:   brew install android-platform-tools")
        print("       Debian:  sudo apt install adb")
        print("       Windows: winget install Google.PlatformTools")
        return 2
    print("[ ok ] adb: %s" % exe)
    try:
        print("[ ok ] version: %s" % adb._run(["version"]).splitlines()[0])
    except Fail as exc:
        print("[FAIL] %s" % exc)
        return 2
    devs = adb.devices()
    print("[info] devices:\n%s" % devs)
    if "unauthorized" in devs:
        print("[FAIL] TV shows 'unauthorized' - accept the RSA prompt on the TV.")
        return 2
    if not re.search(r"\tdevice$", devs, re.M):
        print("[warn] no authorized device. Run: mitv connect <TV_IP>")
        return 2
    snap = collect(adb)
    print("[ ok ] connected: %s / %s" % (snap.model, snap.android))
    return 0


def do_connect(adb: Adb, args) -> int:
    target = args.host
    if ":" not in target:
        target += ":5555"
    print("connecting to %s ..." % target)
    out = adb._run(["connect", target], check=False)
    print(out or "(no output)")
    if "cannot connect" in out.lower() or "failed" in out.lower():
        print()
        print("If that failed:")
        print("  - TV and computer must be on the same network, no guest Wi-Fi / AP isolation")
        print("  - enable Developer options -> USB debugging on the TV")
        print("  - Android 11+ TVs need Wireless debugging pairing: adb pair %s" % target)
        print("  - some Mi TV firmware only opens 5555 after `adb tcpip 5555` over USB")
        return 2
    adb.serial = target
    devs = adb.devices()
    print(devs)
    if "unauthorized" in devs:
        print("Accept the 'Allow debugging' prompt on the TV, then rerun.")
        return 2
    return 0


def do_scan(adb: Adb, args) -> int:
    snap = collect(adb)
    db, rules = load_db()
    items, unknown = classify(snap, rules)

    print_summary(snap, items, unknown)

    if args.json:
        blob = {
            "generated": snap.ts,
            "model": snap.model,
            "android": snap.android,
            "home": snap.home,
            "props": snap.props,
            "items": items,
            "unknown": unknown,
        }
        print(json.dumps(blob, indent=2))
        return 0

    print(hr("="))
    print("SAFE to remove")
    print(hr("="))
    print_items(items, "safe")
    print()
    print(hr("="))
    print("CAUTION - removable, but you may want it")
    print(hr("="))
    print_items(items, "caution")
    print()
    print(hr("="))
    print("PROTECTED - never removed by this tool")
    print(hr("="))
    print_items(items, "protected")
    print()

    if unknown and args.unknown:
        print(hr("="))
        print("UNKNOWN - not in the rule database (%d)" % len(unknown))
        print(hr("="))
        for u in unknown:
            print("   %s" % u["package"])
        print()

    if snap.ghosts:
        print("Already removed for this user (%d) - `mitv restore --list` shows them" % len(snap.ghosts))

    if not looks_like_mi(snap.model_key):
        print()
        print("NOTE: this does not look like a Mi TV (%s). Rules marked applies_to"
              " will be downgraded to caution." % snap.model)

    out_dir = STATE / "reports" / snap.ts
    out_dir.mkdir(parents=True, exist_ok=True)
    md = render_report(snap, items, unknown, out_dir / "report.md")
    (out_dir / "snapshot.json").write_text(
        json.dumps(
            {
                "generated": snap.ts,
                "model": snap.model,
                "android": snap.android,
                "home": snap.home,
                "props": snap.props,
                "packages": snap.packages,
                "disabled": sorted(snap.disabled),
                "ghosts": sorted(snap.ghosts),
                "items": items,
                "unknown": unknown,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print()
    print("report: %s" % md)
    return 0


def pull_apks(adb: Adb, pkg: str, dest: Path) -> list:
    out = adb.sh("pm path %s" % pkg, check=False)
    paths = [l.split(":", 1)[1].strip() for l in out.splitlines() if l.startswith("package:")]
    if not paths:
        return []
    dest.mkdir(parents=True, exist_ok=True)
    local = []
    for remote in paths:
        name = "%s__%s" % (pkg, os.path.basename(remote))
        target = dest / name
        try:
            adb.pull(remote, target)
        except Fail:
            continue
        if target.exists():
            local.append(target)
    return local


def do_inspect(adb: Adb, args) -> int:
    snap = collect(adb)
    db, rules = load_db()
    sdk_map = load_json("adsdk.json").get("sdks", {})
    items, unknown = classify(snap, rules)

    targets = list(args.packages or [])
    if not targets:
        targets = [i["package"] for i in items if i["risk"] == "safe"]
    if not targets:
        print("Nothing to inspect. Pass package names explicitly.")
        return 1

    dest = STATE / "apks" / snap.ts
    results = []
    for pkg in targets:
        apks = pull_apks(adb, pkg, dest)
        if not apks:
            print("[skip] %s - no APK path (already removed?)" % pkg)
            continue
        base = apks[0]
        try:
            info = inspect_apk(base, sdk_map)
        except (zipfile.BadZipFile, OSError) as exc:
            print("[fail] %s - %s" % (pkg, exc))
            continue
        info["package"] = pkg
        results.append(info)
        print(hr("="))
        print("%s  (%d bytes, %d apk parts)" % (pkg, info["size"], len(apks)))
        print("sha256: %s" % info["sha256"])
        if info["ad_sdks"]:
            for hit in info["ad_sdks"]:
                print("  AD SDK : %s  (%s)" % (hit["sdk"], hit["needle"]))
        else:
            print("  AD SDK : none found")
        if info["permissions_of_interest"]:
            print("  perms  : %s" % ", ".join(p.rsplit(".", 1)[-1] for p in info["permissions_of_interest"]))
        print("  verdict: %s" % info["verdict"])

    out_dir = STATE / "reports" / snap.ts
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "apk-inspection.json"
    path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print()
    print("evidence: %s" % path)
    return 0


def do_apply(adb: Adb, args) -> int:
    snap = collect(adb)
    db, rules = load_db()
    items, unknown = classify(snap, rules)

    candidates = items
    if args.packages:
        wanted = set(args.packages)
        candidates = [i for i in items if i["package"] in wanted]
        missing = wanted - {i["package"] for i in candidates}
        for m in sorted(missing):
            print("[skip] %s - not installed or not in database" % m)
    else:
        chosen, skipped = select(snap, items, args.profile, snap.home)
        candidates = chosen
        for item, reason in skipped:
            print("[gate] %s kept - %s" % (item["package"], reason))

    if args.skip:
        drop = set(args.skip)
        candidates = [i for i in candidates if i["package"] not in drop]

    removable, refused = [], []
    for item in candidates:
        if hard_blocked(item["package"]):
            refused.append(item)
        elif item["risk"] == "protected":
            refused.append(item)
        elif not item["installed"]:
            continue
        else:
            removable.append(item)

    for item in refused:
        print("[block] %s - protected" % item["package"])

    if not removable:
        print("Nothing to remove.")
        return 0

    print()
    print("Plan (%s):" % ("explicit list" if args.packages else "profile " + args.profile))
    print_items(removable)
    print()
    print("%d package(s) would be removed for user 0. Reversible with `mitv restore`." % len(removable))

    if args.dry_run:
        print("dry run - nothing changed.")
        return 0

    if not args.yes:
        if not sys.stdin.isatty():
            raise Fail("refusing to change the TV without --yes in a non-interactive shell")
        answer = input("Type 'remove' to continue: ").strip().lower()
        if answer != "remove":
            print("aborted.")
            return 1

    batch = stamp()
    removed, failed = [], []
    for item in removable:
        pkg = item["package"]
        out = adb.sh("pm uninstall -k --user 0 %s" % pkg, check=False)
        ok = out.strip().lower().startswith("success")
        print("%s %s  %s" % ("[ ok ]" if ok else "[fail]", pkg, "" if ok else out.strip()[:120]))
        entry = {
            "batch": batch,
            "ts": stamp(),
            "package": pkg,
            "action": "uninstall-user0",
            "result": "success" if ok else "failed",
            "output": out.strip()[:200],
            "apk": item["apk"],
            "model": snap.model,
            "android": snap.android,
        }
        ledger_add(entry)
        (removed if ok else failed).append(pkg)

    print()
    print("removed %d, failed %d" % (len(removed), len(failed)))
    if removed:
        print("undo with: mitv restore --last")
    if failed:
        print("failed packages are usually not uninstallable without root - leave them.")
    return 0 if not failed else 1


def do_restore(adb: Adb, args) -> int:
    rows = ledger_read()
    if not rows:
        print("No ledger at %s - nothing recorded to restore." % ledger_path())
        if args.list:
            print("Note: packages removed outside this tool are not tracked here.")
        return 0

    if args.list:
        seen = {}
        for r in rows:
            seen.setdefault(r["package"], r)
        print("%d package(s) in ledger:" % len(seen))
        for pkg in sorted(seen):
            print("   %s  (%s)" % (pkg, seen[pkg]["result"]))
        return 0

    if args.last:
        batch = rows[-1]["batch"]
        todo = [r for r in rows if r["batch"] == batch and r["result"] == "success"]
    elif args.all:
        todo = [r for r in rows if r["result"] == "success"]
    else:
        raise Fail("choose --last, --all, or --list")

    seen, ordered = set(), []
    for r in reversed(todo):
        if r["package"] in seen:
            continue
        seen.add(r["package"])
        ordered.append(r)

    print("restoring %d package(s) for user 0" % len(ordered))
    ok_count = 0
    for r in ordered:
        pkg = r["package"]
        if args.dry_run:
            print("[plan] %s" % pkg)
            continue
        out = adb.sh("cmd package install-existing --user 0 %s" % pkg, check=False)
        ok = "installed" in out.lower() or out.strip().lower().startswith("success")
        if not ok:
            out2 = adb.sh("pm install-existing --user 0 %s" % pkg, check=False)
            ok = "installed" in out2.lower()
            out = out if ok else (out + " | " + out2)
        print("%s %s  %s" % ("[ ok ]" if ok else "[fail]", pkg, "" if ok else out.strip()[:120]))
        if ok:
            ok_count += 1
            ledger_add({"batch": stamp(), "ts": stamp(), "package": pkg, "action": "restore",
                        "result": "success", "model": r.get("model", "")})

    if not args.dry_run:
        print("restored %d/%d. A reboot applies the change cleanly." % (ok_count, len(ordered)))
    return 0


def do_snapshot(adb: Adb, args) -> int:
    snap = collect(adb)
    out = STATE / "backups" / snap.ts
    out.mkdir(parents=True, exist_ok=True)
    (out / "props.txt").write_text(json.dumps(snap.props, indent=2), encoding="utf-8")
    (out / "packages.txt").write_text("\n".join(sorted(snap.packages)), encoding="utf-8")
    for scope in ("global", "secure", "system"):
        try:
            text = adb.sh("settings list %s" % scope, check=False)
        except Fail:
            text = ""
        (out / ("settings-%s.txt" % scope)).write_text(text, encoding="utf-8")
    (out / "home.txt").write_text(snap.home + "\n", encoding="utf-8")
    print("snapshot written: %s" % out)
    print("restore a single package: mitv restore --list")
    return 0


def do_privacy(adb: Adb, args) -> int:
    """Reduce ad personalisation at the OS level where the ROM supports it."""
    changed = []
    steps = [
        ("secure", "limit_ad_tracking", "1"),
        ("secure", "advertising_id", "00000000-0000-0000-0000-000000000000"),
    ]
    for scope, key, value in steps:
        out = adb.sh("settings put %s %s %s" % (scope, key, value), check=False)
        changed.append("%s/%s -> %s %s" % (scope, key, value, out.strip()))
    for line in changed:
        print("[set ] %s" % line.rstrip())
    print()
    print("Note: a fixed advertising_id is a blunt instrument and some apps ignore")
    print("it. Real ad removal comes from removing the ad-serving packages.")
    if args.dry_run:
        print("(dry run: commands were still sent - settings put is idempotent)")
    return 0


def do_launcher(adb: Adb, args) -> int:
    if args.action == "status":
        home = resolve_home(adb)
        print("active HOME: %s" % (home or "unknown"))
        if home in STOCK_HOMES:
            print("this is a stock launcher (may show sponsored content)")
        return 0
    if args.action == "install":
        if not args.apk:
            raise Fail("launcher install needs --apk path/to/launcher.apk")
        out = adb._run(["install", "-r", "-g", args.apk], check=False, timeout=600)
        print(out)
        return 0 if "success" in out.lower() else 1
    if args.action == "set":
        if not args.component:
            raise Fail("launcher set needs --component pkg/.Activity")
        out = adb.sh("cmd package set-home-activity %s" % args.component, check=False)
        print(out or "done")
        return 0
    raise Fail("unknown launcher action")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--adb", default=os.environ.get("ADB", "adb"), help="adb binary (default: adb)")
    common.add_argument("--serial", default=None, help="device serial or host:port")
    common.add_argument("-v", "--verbose", action="store_true")

    ap = argparse.ArgumentParser(
        prog="mitv",
        description="Evidence-first debloater for Xiaomi Mi TV / Android TV. Read-only by default.",
        epilog="Start with: mitv connect <TV_IP> ; mitv scan ; mitv inspect ; mitv apply --profile light --dry-run",
    )
    ap.add_argument("--version", action="version", version="mitv " + VERSION)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("doctor", parents=[common], help="check adb + device connectivity")
    p.set_defaults(func=do_doctor)

    p = sub.add_parser("connect", parents=[common], help="adb connect to the TV over the network")
    p.add_argument("host", help="TV IP or IP:port")
    p.set_defaults(func=do_connect)

    p = sub.add_parser("scan", parents=[common], help="classify installed packages (read-only)")
    p.add_argument("--json", action="store_true", help="print the full snapshot as JSON")
    p.add_argument("--unknown", action="store_true", help="list packages missing from the database")
    p.set_defaults(func=do_scan)

    p = sub.add_parser("inspect", parents=[common], help="pull APKs and fingerprint ad/analytics SDKs")
    p.add_argument("packages", nargs="*")
    p.set_defaults(func=do_inspect)

    p = sub.add_parser("apply", parents=[common], help="remove packages (dry-run by default)")
    p.add_argument("--profile", choices=PROFILES, default="light")
    p.add_argument("--packages", nargs="*", help="explicit package list instead of a profile")
    p.add_argument("--skip", nargs="*", default=[], help="packages to leave alone")
    p.add_argument("--dry-run", action="store_true", default=False)
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.set_defaults(func=do_apply)

    p = sub.add_parser("restore", parents=[common], help="undo removals recorded in the ledger")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--last", action="store_true")
    g.add_argument("--all", action="store_true")
    g.add_argument("--list", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=do_restore)

    p = sub.add_parser("snapshot", parents=[common], help="back up props, package list and settings")
    p.set_defaults(func=do_snapshot)

    p = sub.add_parser("privacy", parents=[common], help="limit ad tracking at OS level")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=do_privacy)

    p = sub.add_parser("launcher", parents=[common], help="inspect/install/replace the home launcher")
    p.add_argument("action", choices=["status", "install", "set"])
    p.add_argument("--apk")
    p.add_argument("--component")
    p.set_defaults(func=do_launcher)

    return ap


def main(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "cmd", None):
        ap.print_help()
        return 0
    adb = Adb(exe=args.adb, serial=args.serial, verbose=args.verbose)
    try:
        return args.func(adb, args)
    except Fail as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
