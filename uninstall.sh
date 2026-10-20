#!/bin/sh
# Cuthere uninstaller for macOS.
#
#   ./uninstall.sh                            show the plan (writes nothing)
#   ./uninstall.sh --apply                    move this panel out, keep runtimes and notes
#   ./uninstall.sh --apply --restore-previous restore the panel and settings from before
#   ./uninstall.sh --apply --purge-runtime    also delete the Cuthere runtime folders
#
# Project folders are never touched: notes, recordings and receipts stay in
# <project>/work/ and <project>/output/. See docs/MACOS.md.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
script="$here/scripts/install_macos.py"

if [ "$(uname -s)" != "Darwin" ]; then
    echo "error: this uninstaller targets macOS (uname -s says $(uname -s)). On Windows use uninstall.ps1." >&2
    exit 2
fi

# Uninstall only reads records and moves folders, so any Python 3.10+ works.
# Python 3.11 stays the preferred/prepared runtime.
python="${CUTHERE_PYTHON:-}"
next_is_python=0
for arg in "$@"; do
    if [ "$next_is_python" -eq 1 ]; then
        python=$arg
        next_is_python=0
        continue
    fi
    case "$arg" in
        --python) next_is_python=1 ;;
        --python=*) python=${arg#--python=} ;;
    esac
done

if [ -z "$python" ] && command -v python3.11 >/dev/null 2>&1; then
    python=$(command -v python3.11)
elif [ -z "$python" ] && [ -x /Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11 ]; then
    python=/Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11
elif [ -z "$python" ] && command -v python3 >/dev/null 2>&1; then
    python=$(command -v python3)
fi

if [ -z "$python" ] || [ ! -x "$python" ]; then
    echo "error: Python 3.10 or newer was not found; uninstall needs it only to read the installation record." >&2
    echo "       Pass --python /path/to/python3 (or CUTHERE_PYTHON=...)." >&2
    exit 2
fi
if ! "$python" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)' >/dev/null 2>&1; then
    echo "error: $python is older than Python 3.10; pass a newer interpreter with --python." >&2
    exit 2
fi

exec "$python" "$script" --uninstall "$@"
