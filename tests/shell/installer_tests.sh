#!/bin/sh
# Shell-level checks for the macOS entry points. Runs on macOS (and any POSIX
# shell with python3); it never touches a real CEP folder, network, preference
# domain or system Python: every path is redirected into a temporary directory.
#
#   sh tests/shell/installer_tests.sh
#
# What this covers: syntax of both shell scripts, refusal on non-Darwin hosts,
# the dry-run default, two sandboxed installs on paths with spaces and non-ASCII
# characters (--no-defaults), the generated CLI wrapper, the helper's status
# file, a full --restore-previous that returns the *first* installation (panel,
# config, record and wrapper), the restore+purge guard, an explicit
# --purge-runtime that must keep unrelated files, and then a separate
# single-install transaction that restores the pre-install panel.
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$root"

pass=0
fail=0
check() {
    # check <description> <command...>
    description=$1
    shift
    if "$@" >/dev/null 2>&1; then
        pass=$((pass + 1))
        echo "ok - $description"
    else
        fail=$((fail + 1))
        echo "not ok - $description"
    fi
}

# Resolve the interpreter to an absolute executable path: install.sh requires an
# executable *file* for --python, so a bare "python3" would only work if the
# checkout happened to contain it. Prefer 3.11, which the installer requires.
python=""
if command -v python3.11 >/dev/null 2>&1; then
    python=$(command -v python3.11)
elif command -v python3 >/dev/null 2>&1; then
    python=$(command -v python3)
elif command -v python >/dev/null 2>&1; then
    python=$(command -v python)
fi
if [ -z "$python" ]; then
    echo "error: a python3 interpreter is needed for these checks" >&2
    exit 2
fi
case "$python" in
    /*) ;;
    *) python=$(CDPATH= cd -- "$(dirname -- "$python")" && pwd)/$(basename -- "$python") ;;
esac
if [ ! -x "$python" ]; then
    echo "error: '$python' is not an executable file" >&2
    exit 2
fi

# 1. Syntax of the shell scripts (does not execute them).
for script in install.sh uninstall.sh; do
    check "syntax: $script" sh -n "$script"
done
check "syntax: tests/shell/installer_tests.sh" sh -n "$0"

# 2. On a non-Darwin host both scripts must refuse and change nothing.
if [ "$(uname -s)" != "Darwin" ]; then
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    check "install.sh refuses a non-Darwin host" sh -c '! ./install.sh --apply --runtime-root "$1" --extension-root "$2"' sh "$tmp/runtime" "$tmp/ext"
    check "uninstall.sh refuses a non-Darwin host" sh -c '! ./uninstall.sh --apply --runtime-root "$1" --extension-root "$2"' sh "$tmp/runtime" "$tmp/ext"
    check "nothing was created" test ! -e "$tmp/ext"
    echo "# shell checks: $pass passed, $fail failed (non-Darwin host: install/uninstall run checks skipped)"
    [ "$fail" -eq 0 ] || exit 1
    exit 0
fi

# 3. macOS-only checks against a sandboxed install root. The paths deliberately
#    contain spaces and non-ASCII characters: every path is passed as one
#    argument and quoted by the scripts.
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
ext="$tmp/CEP extensions/测试"
runtime="$tmp/runtime with space 测试"
mkdir -p "$ext"

plan_out="$tmp/plan.json"
check "dry-run is the default" sh -c './install.sh --python "$1" --runtime-root "$2" --extension-root "$3" > "$4"' \
    sh "$python" "$runtime" "$ext" "$plan_out"
check "plan output is a PLAN" grep -q '"mode": "PLAN"' "$plan_out"
check "plan states the Apple silicon / macOS 14 scope" grep -q 'Apple silicon' "$plan_out"
check "dry-run wrote no install record" test ! -e "$runtime/current_install.json"
check "dry-run installed no panel" test ! -e "$ext/AutoVideoEditReviewCapture"

# A previous panel makes the backup path real.
mkdir -p "$ext/AutoVideoEditReviewCapture/CSXS"
printf '<ExtensionManifest ExtensionBundleId="com.autovideoedit.reviewcapture"/>' > "$ext/AutoVideoEditReviewCapture/CSXS/manifest.xml"
printf 'previous panel' > "$ext/AutoVideoEditReviewCapture/previous.txt"

# First installation: --no-defaults keeps the sandbox away from real preferences.
check "install (sandboxed, spaces and non-ASCII)" sh -c \
    './install.sh --apply --skip-pip --skip-dependencies --no-defaults --python "$1" --runtime-root "$2" --extension-root "$3"' \
    sh "$python" "$runtime" "$ext"
check "panel installed" test -f "$ext/AutoVideoEditReviewCapture/runtime.json"
check "runtime.json records the platform" grep -q '"platform": "darwin"' "$ext/AutoVideoEditReviewCapture/runtime.json"
check "receipt written" test -f "$runtime/current_install.json"
check "cli wrapper is executable" test -x "$runtime/bin/cuthere"
check "cli wrapper runs from a path with spaces" "$runtime/bin/cuthere" --help
check "previous panel backed up" sh -c 'ls "$1"/backups/*/previous.txt' sh "$runtime"

first_record="$tmp/first-record.json"
cp "$runtime/current_install.json" "$first_record"
first_wrapper="$tmp/first-wrapper.sh"
cp "$runtime/bin/cuthere" "$first_wrapper"
first_runtime=$("$python" -c 'import json,sys;print(json.load(open(sys.argv[1]))["runtime"])' "$first_record")

# The helper must start with the installed interpreter and write its status to
# the (quoted) IPC path.
panel_runtime="$ext/AutoVideoEditReviewCapture/runtime.json"
launcher=$("$python" -c 'import json,sys;print(json.load(open(sys.argv[1]))["launcher"])' "$panel_runtime")
worker=$("$python" -c 'import json,sys;print(json.load(open(sys.argv[1]))["worker_entry"])' "$panel_runtime")
ipc=$("$python" -c 'import json,sys;print(json.load(open(sys.argv[1]))["ipc_root"])' "$panel_runtime")
check "helper starts (--once)" "$launcher" "$worker" --ipc-root "$ipc" --once
check "helper wrote its status" grep -q 'push_to_talk_capability' "$ipc/status.json"

# Capture the first installation's panel configuration: the second install
# replaces it, and a full restore must bring exactly this file back.
first_panel_config="$tmp/first-panel-runtime.json"
cp "$panel_runtime" "$first_panel_config"
check "first panel config targets the first installation" grep -Fq "$first_runtime" "$first_panel_config"

# Second installation over the first, still with --no-defaults: the previous
# record and wrapper must be preserved for a full restore.
check "second install (--no-defaults)" sh -c \
    './install.sh --apply --skip-pip --skip-dependencies --no-defaults --python "$1" --runtime-root "$2" --extension-root "$3"' \
    sh "$python" "$runtime" "$ext"
second_record="$runtime/current_install.json"
previous_record=$("$python" -c 'import json,sys;r=json.load(open(sys.argv[1]));print(r["previous_install_record"] or "")' "$second_record")
check "previous install record preserved with --no-defaults" test -n "$previous_record"
check "previous install record exists" test -f "$previous_record"
previous_runtime=$("$python" -c 'import json,sys;print(json.load(open(sys.argv[1]))["runtime"])' "$previous_record")
check "previous record describes the first installation" test "$previous_runtime" = "$first_runtime"
second_runtime=$("$python" -c 'import json,sys;print(json.load(open(sys.argv[1]))["runtime"])' "$second_record")
check "upgraded wrapper targets the current runtime" grep -Fq "$second_runtime" "$runtime/bin/cuthere"

# Uninstall plan writes nothing, then a full restore of the *first*
# installation: its panel (and runtime.json), its install record and its CLI
# wrapper. The pre-install dummy panel belongs to an earlier transaction, so it
# must not be the restored panel here.
uninstall_plan="$tmp/uninstall-plan.json"
check "uninstall plan writes nothing" sh -c \
    './uninstall.sh --python "$1" --runtime-root "$2" --extension-root "$3" > "$4"' \
    sh "$python" "$runtime" "$ext" "$uninstall_plan"
check "uninstall plan is a PLAN" grep -q '"mode": "PLAN"' "$uninstall_plan"
check "uninstall with full restore" sh -c \
    './uninstall.sh --apply --restore-previous --python "$1" --runtime-root "$2" --extension-root "$3"' \
    sh "$python" "$runtime" "$ext"
check "first installation's panel restored" cmp -s "$panel_runtime" "$first_panel_config"
check "restored panel config targets the first installation" grep -Fq "$first_runtime" "$panel_runtime"
check "restored panel is Cuthere's panel, not the pre-install dummy" test ! -f "$ext/AutoVideoEditReviewCapture/previous.txt"
restored_runtime=$("$python" -c 'import json,sys;print(json.load(open(sys.argv[1]))["runtime"])' "$runtime/current_install.json")
check "previous install record restored" test "$restored_runtime" = "$first_runtime"
check "restored record is the first install record" cmp -s "$runtime/current_install.json" "$first_record"
check "previous cli wrapper restored" cmp -s "$runtime/bin/cuthere" "$first_wrapper"

# Restore and purge cannot be combined; the refusal must change nothing.
record_before=$(cat "$runtime/current_install.json")
check "restore+purge is refused" sh -c \
    '! ./uninstall.sh --apply --restore-previous --purge-runtime --python "$1" --runtime-root "$2" --extension-root "$3"' \
    sh "$python" "$runtime" "$ext"
check "refused restore+purge changed nothing" sh -c 'test "$(cat "$1")" = "$2"' sh "$runtime/current_install.json" "$record_before"

# An explicit purge removes Cuthere's artifacts and keeps unrelated files.
mkdir -p "$runtime/notes"
printf 'keep me' > "$runtime/notes/keep.txt"
printf '#!/bin/sh\n' > "$runtime/bin/other-tool"
printf 'keep me' > "$runtime/unrelated.txt"
check "purge uninstall" sh -c \
    './uninstall.sh --apply --purge-runtime --python "$1" --runtime-root "$2" --extension-root "$3"' \
    sh "$python" "$runtime" "$ext"
check "purge kept an unrelated file" test -f "$runtime/unrelated.txt"
check "purge kept an unrelated notes folder" test -f "$runtime/notes/keep.txt"
check "purge kept another tool in bin" test -f "$runtime/bin/other-tool"
check "purge removed the generated command" test ! -e "$runtime/bin/cuthere"
check "purge removed the runtime versions" test ! -e "$runtime/versions"
check "purge removed the ipc folder" test ! -e "$runtime/ipc"

# A separate single-install transaction returns all the way to the pre-install
# panel. It gets its own sandbox so the two-install transaction above keeps its
# own expectations: this one restores the dummy panel (with no Cuthere
# runtime.json), removes the install record and leaves no generated command.
ext_one="$tmp/CEP single restore 测试"
runtime_one="$tmp/runtime single restore 测试"
mkdir -p "$ext_one/AutoVideoEditReviewCapture/CSXS"
printf '<ExtensionManifest ExtensionBundleId="com.autovideoedit.reviewcapture"/>' > "$ext_one/AutoVideoEditReviewCapture/CSXS/manifest.xml"
printf 'dummy panel from before Cuthere' > "$ext_one/AutoVideoEditReviewCapture/previous.txt"
check "single sandboxed install over a dummy panel" sh -c \
    './install.sh --apply --skip-pip --skip-dependencies --no-defaults --python "$1" --runtime-root "$2" --extension-root "$3"' \
    sh "$python" "$runtime_one" "$ext_one"
check "single install backed up the dummy panel" sh -c 'ls "$1"/backups/*/previous.txt' sh "$runtime_one"
check "uninstall restores the pre-install panel" sh -c \
    './uninstall.sh --apply --restore-previous --python "$1" --runtime-root "$2" --extension-root "$3"' \
    sh "$python" "$runtime_one" "$ext_one"
check "pre-install panel file restored" sh -c 'test "$(cat "$1")" = "$2"' \
    sh "$ext_one/AutoVideoEditReviewCapture/previous.txt" 'dummy panel from before Cuthere'
check "restored pre-install panel has no Cuthere config" test ! -f "$ext_one/AutoVideoEditReviewCapture/runtime.json"
check "no installation record remains after the final restore" test ! -e "$runtime_one/current_install.json"
check "no cli wrapper remains after the final restore" test ! -e "$runtime_one/bin/cuthere"

echo "# shell checks: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
