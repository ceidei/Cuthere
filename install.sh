#!/bin/sh
# Cuthere installer for macOS.
#
#   ./install.sh                 show the plan (writes nothing)
#   ./install.sh --check-only    environment report only
#   ./install.sh --apply         install for the current user
#   ./install.sh --help          all options
#
# Everything runs as the current user: no sudo, nothing in /Library, no changes
# to Homebrew or the system Python. The heavy lifting lives in
# scripts/install_macos.py so it can be tested (plan, staging, rollback) without
# a Mac. See docs/MACOS.md.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
script="$here/scripts/install_macos.py"

if [ "$(uname -s)" != "Darwin" ]; then
    echo "error: this installer targets macOS (uname -s says $(uname -s)). On Windows use install.ps1." >&2
    exit 2
fi

# The interpreter only needs to run the Python installer; it locates and checks
# the Python 3.11 used for the runtime itself (accepting --python too). An
# explicit --python must be honoured here as well: it may be the only 3.11.
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

if [ -n "$python" ]; then
    if [ ! -x "$python" ]; then
        echo "error: --python '$python' is not an executable file." >&2
        echo "       Nothing was changed." >&2
        exit 2
    fi
elif command -v python3.11 >/dev/null 2>&1; then
    python=$(command -v python3.11)
elif [ -x /Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11 ]; then
    python=/Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11
else
    echo "error: Python 3.11 was not found." >&2
    echo "       Install it from https://www.python.org/downloads/macos/ or pass --python /path/to/python3.11." >&2
    echo "       Nothing was changed." >&2
    exit 2
fi

if [ ! -f "$script" ]; then
    echo "error: $script is missing; run this from the unzipped Cuthere folder." >&2
    exit 2
fi

exec "$python" "$script" "$@"
