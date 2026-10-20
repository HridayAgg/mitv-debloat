#!/usr/bin/env bash
# Symlink mitv into a bin directory. No dependencies beyond python3.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bindir="${MITV_BIN_DIR:-$HOME/.local/bin}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required but not on PATH" >&2
  exit 1
fi

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' || {
  echo "python 3.8+ required, found $(python3 -V)" >&2
  exit 1
}

mkdir -p "$bindir"
chmod +x "$here/mitv.py"
ln -sf "$here/mitv.py" "$bindir/mitv"

echo "linked $bindir/mitv -> $here/mitv.py"
case ":$PATH:" in
  *":$bindir:"*) ;;
  *) echo "note: $bindir is not on PATH. Add it:"
     echo "      echo 'export PATH=\"$bindir:\$PATH\"' >> ~/.bashrc" ;;
esac

if command -v adb >/dev/null 2>&1; then
  echo "adb: $(command -v adb)"
else
  echo
  echo "adb is not installed yet. Pick one:"
  echo "  macOS    brew install android-platform-tools"
  echo "  Debian   sudo apt install adb"
  echo "  Windows  winget install Google.PlatformTools"
fi

"$bindir/mitv" --version

cat <<'EOF'

Next:
  mitv connect <TV_IP>      # TV setup: docs/MI-TV-SETUP.md
  mitv scan
  mitv inspect
  mitv apply --profile light --dry-run
EOF
