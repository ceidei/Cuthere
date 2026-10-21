# Cuthere on macOS

> **Status: experimental (1.1.0-alpha.6).** The macOS code paths are written and covered by automated checks; the last recorded macOS arm64 run is [39560295086](https://github.com/ceidei/cuthere/actions/runs/39560295086) for a preceding tree (commit `03dd0ded`). They have **not** run on a real Mac with Premiere Pro, a microphone and a physical key: no microphone capture, Right Option/F8 press, TCC permission prompt, CEP panel load or agent edit is recorded, and this candidate has not been through CI. Use a test project, keep backups, and read [the acceptance checklist](#acceptance-checklist-for-a-real-mac) before trusting it.

## Host scope: Apple silicon on macOS 14 or later

This first macOS candidate is scoped to **Apple silicon (arm64) Macs running macOS 14.0 or later**. The shared pinned runtime cannot be installed on an Intel Mac, and the installer refuses an unsupported CPU or OS version before it creates a folder, environment or preference.

The scope comes from the official wheel metadata of the pinned dependencies:

- [`onnxruntime==1.31.0`](https://pypi.org/project/onnxruntime/1.31.0/#files) publishes macOS wheels only for macOS 14+ arm64 (`cp311`–`cp314`, `macosx_14_0_arm64`). There is no macOS x86_64 wheel.
- [`av==18.1.0`](https://pypi.org/project/av/18.1.0/#files) publishes an arm64 wheel for macOS 14 (`cp311-abi3-macosx_14_0_arm64`). Its Intel wheel does not help while `onnxruntime` has no Intel macOS wheel.
- The optional PyObjC 11.1 frameworks are universal2 wheels, but they only provide the key backend; permissions and hardware acceptance are still required.

The installer prints the detected machine and macOS version in its plan and records the result in `current_install.json` (`host_support`). An unsupported host is rejected with the exact reason and changes nothing.

## What works the same as Windows

Everything that is not a Windows API is shared code:

- one CEP panel, bundle ID `com.autovideoedit.reviewcapture`, folder `AutoVideoEditReviewCapture`, menu entry `Cuthere · 剪这儿`;
- hold-to-record note capture, timeline anchoring, transcript revisions, batch packaging;
- the file bridge (commands/responses/locks) and the host ExtendScript;
- the CLI: `handoff`, `read-batch`, `doctor`, `transcribe`, `select-sequence`, `edit`, `roundtrip`, `bridge-status`, `decision-template`, `shortcut`.

## What is different

| Concern | Windows | macOS |
|---|---|---|
| CEP extension folder (per user) | `%APPDATA%\Adobe\CEP\extensions\AutoVideoEditReviewCapture` | `~/Library/Application Support/Adobe/CEP/extensions/AutoVideoEditReviewCapture` |
| Unsigned dev build enabled by | `HKCU\Software\Adobe\CSXS.12` → `PlayerDebugMode = 1` | user defaults for `com.adobe.CSXS.12`, key `PlayerDebugMode = 1` |
| Runtime (venv, code copies, logs, receipts) | `%LOCALAPPDATA%\AutoVideoEdit\PremiereReview` | `~/Library/Application Support/Cuthere/` |
| Bridge folder | `C:\Users\Public\Documents\ave-bridge` | `~/Library/Application Support/Cuthere/ave-bridge` |
| Global press-and-hold shortcut (Right Option by default on macOS, F8 on Windows) | `RegisterHotKey` + `GetAsyncKeyState` (ctypes) | optional Quartz event tap (PyObjC); macOS asks for Accessibility and, on some systems, Input Monitoring |
| Launch the helper with no console window | `pythonw.exe` | the venv `python3` started by CEP (no tty) |
| Audio APIs | WASAPI via PortAudio | CoreAudio via PortAudio |
| GPU switches in `CSXS/manifest.xml` | keeps `--use-angle=d3d11` and the Vulkan switches | the installer removes the whole `CEFCommandLine` block from its staged copy |

## Install

### Prerequisites

1. **An Apple silicon Mac on macOS 14.0 or later.** The wheel metadata above is the reason; the installer checks it first.
2. **Python 3.11** from [python.org](https://www.python.org/downloads/macos/). The installer insists on exactly 3.11 (it is what the runtime is tested with) and looks for `python3.11` on `PATH`, then the framework location, then `--python /path/to/python3.11`. It will not install Python for you.
3. **Adobe Premiere Pro** with CEP support, installed normally.
4. **FFmpeg** on `PATH` (`brew install ffmpeg`) — needed for media probing and for complex agent edits, not for recording.
5. No `sudo` is needed or wanted: everything is per-user.

Unzip the candidate package into a normal folder (the archive root is the project root):

```bash
unzip cuthere-1.1.0-alpha.6-macos-source.zip -d cuthere-1.1.0-alpha.6-macos-source
cd cuthere-1.1.0-alpha.6-macos-source
```

Check the environment and the plan first:

```bash
./install.sh --check-only    # host scope, Python, FFmpeg, CEP folder, disk
./install.sh                 # DEFAULT: prints the plan, writes nothing
```

The plan shows: host support, target extension folder, runtime folder with a version stamp, venv path, bridge folder, the `PlayerDebugMode` change, the new helper entry points, the macOS manifest change, and every file that will be moved to a backup.

### Apply

```bash
./install.sh --apply
# and, if you want the local transcription model prepared now:
./install.sh --apply --download-model --model medium
```

`--apply`:

1. Creates `~/Library/Application Support/Cuthere/versions/f8-<version>-<stamp>/` with a copy of `engine/`, `services/`, `scripts/`, `docs/` and a `VERSION` file.
2. Creates an isolated virtual environment with `python3.11 -m venv` and installs `requirements-macos.txt` (the pinned common set; `requirements-macos-optional.txt` with PyObjC is attempted next and a failure is recorded, not fatal).
3. Downloads the chosen faster-whisper model **only** with `--download-model`. Without it, a missing cache is reported and the panel is still installed; automatic transcription will say the model is missing until you prepare it.
4. Copies the panel into a staging folder, removes the Windows-only `CEFCommandLine` (Direct3D/Vulkan) block from that copy, and writes `runtime.json` with helper entries, IPC root, bridge folder, model, and an explicit `language` (`--language auto|zh|en|...`, default `auto`). A language recorded by an earlier installation is preserved; `--language` is only needed to change it.
5. Backs up the previous `AutoVideoEditReviewCapture` folder into the runtime backup folder, moves the staged panel into place, and writes `current_install.json`.
6. Enables `PlayerDebugMode` for CEP 12 in per-user defaults, saving the previous value so uninstall can restore it.
7. Rolls the extension folder, the defaults value, the installation record and the `cuthere` command back on failure. If the dependency, model or manifest step fails, the previous panel is still in place and the version folder keeps an `install.log`.

Options:

| Flag | Meaning |
|---|---|
| `--check-only` | Environment and host report only |
| *(none)* | Dry-run plan (default) |
| `--apply` | Perform the installation |
| `--model tiny\|base\|small\|medium` | Transcription model (default `medium`) |
| `--download-model` | Download the model weights (network) |
| `--language auto\|zh\|en\|…` | Language recorded for the panel; `auto` = detect. Omit it to keep an existing setting |
| `--python PATH` | Python 3.11 interpreter to use (also honoured by `install.sh` itself) |
| `--cep-version N` | CSXS version for `PlayerDebugMode` (default `12`) |
| `--runtime-root DIR` | Override the runtime folder (tests, portable setups) |
| `--extension-root DIR` | Override the extension folder (tests, sandbox) |
| `--no-defaults` | Skip the `PlayerDebugMode` change (e.g. if you set it yourself) |
| `--skip-dependencies` | Tests/offline: do not run pip |
| `--skip-pip` | Create the venv without pip/ensurepip (restricted sandboxes) |

Paths containing spaces or non-ASCII characters are supported; every path is quoted and nothing is split on whitespace.

### After installing

1. Quit and reopen Premiere Pro (⌘Q, not just closing the window).
2. Open **Window → Extensions → Cuthere · 剪这儿** (older builds: *Extensions (Legacy)*).
3. The panel should report the recording helper connected. If it does not, read *Connection diagnostics* at the bottom of the panel and run `cuthere doctor`.
4. Select a microphone, record a test note on a **copy** of a project, and check the WAV under `<project>/work/premiere-review/`.

The panel launches the helper itself (`worker_entry.py`) and the visibility helper; there is no separate Windows-style launcher, and no login item is installed. Premiere should be quit before installing or uninstalling so an old helper is not still running.

## Permissions

macOS controls the permissions below at the system level. Cuthere cannot grant them, cannot reset them, and never tries to work around them. Grant the ones the system prompt or the panel's *Connection diagnostics* asks for: the exact set depends on the macOS version and on which process macOS attributes the event tap to.

| Permission | Needed for | Where to grant it | What happens without it |
|---|---|---|---|
| **Microphone** | Recording your notes, started with the recording shortcut or with the panel's **Start**/**Stop** buttons | System Settings → Privacy & Security → Microphone | Capture fails with a visible error; the note is marked `failed`; nothing is recorded silently |
| **Accessibility** | The global recording-shortcut tap (hold-to-talk) | System Settings → Privacy & Security → Accessibility | The panel's *hold to talk* key does nothing; the panel's own **Start**/**Stop** buttons still record |
| **Input Monitoring** | The same tap on newer macOS releases, for some configurations | System Settings → Privacy & Security → Input Monitoring | Same as above |

**Microphone permission is required for recording by either method.** A missing key-monitoring permission only disables hold-to-talk: the panel's own **Start**/**Stop** buttons still record.

Which application you add depends on who owns the process that hosts the key listener. In this design the listener lives inside the Cuthere helper process started by CEP, so macOS may attribute the request to Premiere Pro, to the CEP HTML Engine inside the Premiere bundle, or to the Python interpreter that runs the helper. Check System Settings after the first attempt and add the application that is actually listed; if the prompt never appears:

1. Open the helper's *Connection diagnostics*; a `key_backend: unavailable` or `permission: missing` line means the tap could not be created.
2. Add the app to the permission macOS names — **Accessibility** and/or **Input Monitoring** — with the **+** button.
3. Quit and reopen that application — permission changes apply to newly started processes.

Never edit the TCC database, disable SIP, or grant Full Disk Access as a workaround. If a key-monitoring permission cannot be granted, use the panel buttons for recording and tell the operator that hold-to-talk is unavailable. If **Microphone** permission is denied, recording cannot proceed by either method until the user grants it.

**Nothing in the automated test suite grants or proves a permission.** The tests use fake backends; only the real-hardware checklist below can confirm a key press and a microphone capture. Installing PyObjC does not grant anything either.

## The recording shortcut

**Right Option is the macOS default** (shown in the panel as *右 Option*); **F8 is the Windows default**. Windows keeps F8 so a 1.0 habit and an existing `F8`/`F9` choice keep working, and an explicitly saved old F9 is carried over once (see below).

You can change the shortcut in the panel or from the CLI; both read and write the same file, so a change in one place appears in the other and neither can overwrite the other with a stale value:

| Where | What to do |
|---|---|
| Panel | *录音快捷键 → 设置*, press the key or combination, check the preview, then **保存**. **取消**/Esc leaves the current value; **恢复默认** returns to Right Option. |
| CLI | `cuthere shortcut show`, `cuthere shortcut set RightOption`, `cuthere shortcut set "Ctrl+Shift+K" --dry-run`, `cuthere shortcut reset`, `cuthere shortcut restore-previous`, `cuthere shortcut test-state`, `cuthere shortcut grammar` |

The authoritative file is `~/Library/Application Support/Cuthere/shortcut.json` (`%LOCALAPPDATA%\AutoVideoEdit\PremiereReview\shortcut.json` on Windows). It holds the canonical binding, its display text, who set it and the previous value. `shortcut.last-good.json` next to it keeps the **last value that was successfully in force** (for example F10 after F9 then F10): a truncated or hand-edited `shortcut.json` falls back to it, and the panel says so instead of silently reverting to F8 or to the older F9. The `previous_binding` recorded inside both files remains the separate undo value used by `cuthere shortcut restore-previous`.

**Supported values.** Modifiers `Control`, `Option`, `Shift`, `Command` (aliases `Ctrl`, `Alt`, `Cmd`, and the side-qualified `LeftX`/`RightX` forms such as `RightOption`), plus one main key: `F1`–`F12`, `A`–`Z`, `0`–`9`, `Space`, `Enter`, `Tab`, `Backspace`, `Insert`, `Delete`, `Home`, `End`, `PageUp`, `PageDown`, and the four arrow keys. A binding is either a chord (`Control+Shift+K`) or, **on macOS only**, one side-qualified modifier alone (`RightOption`). Windows additionally refuses `F12` (and any chord whose main key is `F12`) because Microsoft reserves it for debuggers and says not to register it, so the Windows range is `F1`–`F11`; macOS can still bind `F12`. Anything else — an unknown token, two main keys, `Escape` (the recorder's cancel key), `Fn`/Globe — is rejected with a message; it never falls back to F8.

**What is not supported, and why.**

- *Side-qualified modifiers and modifier-only bindings are macOS only.* Windows registers a global hotkey, which cannot express "only the right-hand Alt" and has no event for a modifier on its own. The CLI and the panel refuse those values on Windows instead of registering something that cannot work.
- *`Fn`/Globe and `Escape` are reserved.* `Escape` cancels the recorder, and `Fn` has no dependable event.
- *The pattern is matched exactly.* A plain `F8` binding is not fired by `Command+F8`, and `Shift+F9` is not fired by `Command+Shift+F9`, so a chord you use elsewhere is not stolen. A take ends when the main key **or** one of its required modifiers is released, whichever comes first.

**Right Option and left/right discrimination.** macOS delivers modifiers through `CGEventFlagsChanged`, not key up/down, and `CGEventFlags` only says "some Option is down". Cuthere therefore reads the keycode of the `flagsChanged` event (58 = left Option, 61 = right Option, and the same for the other modifiers), which is the documented way to tell the two sides apart. Pressing **left** Option does not start a take that is bound to Right Option.

When the opposite side is also down, the aggregate flag cannot say which side changed: releasing Right Option while Left Option is held still leaves the Option flag set. For those ambiguous events Cuthere queries the side-specific physical state with `CGEventSourceKeyState` for the changed keycode, so releasing the required side ends the take. The same bounded native read reconciles the modifier sides after hold-to-record was disarmed, a tap was disabled or an edge may have been missed; if the state cannot be established, Cuthere does not guess a press from the aggregate flag.

**Modifier-only chords: the unavoidable limitation.** A take bound to Right Option starts the moment the modifier goes down, because the first key-down cannot know that the modifier is about to become part of a shortcut. If any other key or modifier is pressed while it is held — including punctuation, Escape and keys outside the bindable grammar — the take is cancelled, no review note is created, and the audio file is kept for audit. A modifier pressed while another key or modifier is already held never starts a take. The panel and `cuthere shortcut show` state this limit rather than promising that no such collision can occur.

**Conflicts.** The macOS tap is **listen-only**: it observes the key and does **not** swallow it, so Premiere and other applications still receive it. That is deliberate — recording a note must never change what the edit does — but it means a key that macOS or Premiere also uses will still perform its own action while it also starts a note. Cuthere cannot see every conflict. It warns about widely known system shortcuts (`Control+Space`, `Command+Space`, `Command+Tab`, `Command+Q`, `Command+W`, `Command+L`, `Option+Tab`, `Control+Option+Delete`) in the panel and in `cuthere shortcut show`, and the authoritative list is Premiere's *Edit → Keyboard Shortcuts* dialog plus macOS's own keyboard settings. Pick a key that nothing else uses; if the preview warns, choose another one. *The new-take diagnostic* in the panel's *Connection diagnostics* (`按住录音已就绪` plus `最近一次按下`) and `ptt_key_diagnostic.json` show whether a press actually reached the helper.

**A shortcut is never swapped mid-take.** A change made while a note is recording is reported as pending by the panel and by `cuthere shortcut show`; the take keeps the key it started with and the new binding applies to the next one.

## Function keys and media keys

On MacBooks the top row defaults to media keys, so a physical **F8** may send *Play/Pause* instead of a Function key. Fix it at the keyboard level:

- **System Settings → Keyboard → Keyboard shortcuts → Function keys**: enable *Use F1, F2, etc. keys as standard function keys* (applies to the built-in keyboard, or the external keyboard when connected).
- Or hold **fn** with the key. `Fn` itself cannot be bound.
- F1–F12, letters, digits and the navigation keys are all bindable, so a layout without reliable function keys can use Right Option (the default) or a chord such as `Control+Shift+K` instead.

Also check that Premiere is not using the same key: *Edit → Keyboard Shortcuts* shows conflicts. Cuthere does not remove other applications' shortcuts — if *Play/Pause* still fires, an app or macOS is intercepting the key before Premiere sees it.

## How the macOS key events work (design notes)

- The backend is **PyObjC Quartz** (`Quartz.CGEventTapCreate` with `kCGSessionEventTap`, `kCGHeadInsertEventTap`, `kCGEventTapOptionListenOnly`), imported lazily and only on `sys.platform == "darwin"`. There is no compiled extension in this repository and nothing else to build.
- The tap is created when the panel arms hold-to-record and released when the panel is disarmed, the panel disappears, the worker exits, or recording stops. If macOS denies the tap, the error is written to `ptt_key_diagnostic.json` and the panel shows *hold-to-record unavailable*; it never silently ignores the shortcut.
- The tap mask covers `kCGEventKeyDown`, `kCGEventKeyUp` and `kCGEventFlagsChanged`. Modifier state comes from `flagsChanged` keycodes, so the left and right copies of Option, Control, Command and Shift are distinct; a key event also carries the modifier flags as a fallback if an edge was missed. An ambiguous `flagsChanged` (the opposite side keeps the family flag set) is resolved with `CGEventSourceKeyState` for that exact keycode, as are the sides after a re-arm.
- Every non-modifier key event is observed, not only the bindable grammar: punctuation and Escape cancel a modifier-only take and count as an already-held key. The capture grammar stays bounded, but chord observation does not.
- A complete short press is latched between polls, so a tap that starts and ends between two 20 ms polls still starts and stops one take instead of disappearing.
- Auto-repeat is ignored: holding the key produces one press edge, not a stream of starts.
- Edge cases that end a take: the main key released; a required modifier released; focus loss; the panel closing; the helper exiting; a disabled tap. In every case the audio already captured is saved.
- The listen-only limitation is real and documented above: an observed key is still delivered to Premiere. Cuthere reports the conflict it can see and relies on the user choosing another key; it does not claim to detect every conflict and it does not block the key.
- macOS can disable a tap. On `kCGEventTapDisabledByTimeout` the cached key state is dropped (an active take stops and is saved), the event is counted in `status.json` (`push_to_talk_backend`) and the tap is re-enabled. On `kCGEventTapDisabledByUserInput` the same state reset happens, but re-enabling waits for the next poll with Premiere in front. A dead run loop is reported as a failure instead of recording forever.
- Front application detection uses `NSWorkspace.sharedWorkspace().frontmostApplication()`, whose name and executable path are compared against *Adobe Premiere Pro* (and the CEP HTML Engine inside the Premiere bundle). Recording only starts while Premiere is frontmost.
- Release is guaranteed by: key-up events; the worker loop's *key not down* check; loss of panel heartbeat (focus loss, panel closed, Premiere quit) which stops and saves the recording; and process shutdown (`SIGTERM`/`SIGINT` are handled so the loop exits normally).
- If PyObjC is absent, `install.sh` still installs everything else and the backend reports `unavailable` in `status.json`/`ptt_key_diagnostic.json`; the panel shows it in *Connection diagnostics* instead of pretending hold-to-talk works.

## Transcription

- Weights are downloaded only with `--download-model`. They land in the Hugging Face cache (`~/.cache/huggingface`), not in the Cuthere folders and not in the package.
- The language recorded at install time is passed through for every note; `auto` lets faster-whisper detect it. An existing recorded language is kept. You can transcribe a whole batch by hand:

```bash
"$HOME/Library/Application Support/Cuthere/bin/cuthere" transcribe --project-root "<project folder>" --language auto --model medium
```

- Text corrections are revisions; the original transcript is never overwritten.
- If the model is not cached, the panel reports the transcription error for that note; install again with `--download-model` (or prepare the cache on that machine) and retry.

## CLI wrapper

`install.sh --apply` writes `~/Library/Application Support/Cuthere/bin/cuthere`, a small POSIX shell script that runs the installed CLI with the installed interpreter and bridge folder:

```bash
"$HOME/Library/Application Support/Cuthere/bin/cuthere" --help
"$HOME/Library/Application Support/Cuthere/bin/cuthere" bridge-status
"$HOME/Library/Application Support/Cuthere/bin/cuthere" doctor --project-root "<project folder>" --report-dir "<project folder>/work/premiere-review/doctor"
"$HOME/Library/Application Support/Cuthere/bin/cuthere" handoff --project-root "<project folder>"
"$HOME/Library/Application Support/Cuthere/bin/cuthere" shortcut show
"$HOME/Library/Application Support/Cuthere/bin/cuthere" shortcut set RightOption
```

Add `~/Library/Application Support/Cuthere/bin` to `PATH` if you like; nothing is added to your shell profile automatically.

`bridge-status` is the read-only smoke test: exit `0` means the panel answered a `ping`; exit `2` means it did not (Premiere closed, panel closed, wrong bridge folder, or a timeout, in which case the host state is unknown).

## Files worth knowing

| File | What it tells you |
|---|---|
| `<runtime>/current_install.json` | What is installed, its backups, language, host, model status and optional key-backend status |
| `<runtime>/versions/f8-<version>-<stamp>/install.log` | Install steps, failures and rollback notes |
| `<runtime>/ipc/f8-<version>/status.json` | Panel/helper heartbeat, active recording, key backend health and capability |
| `<runtime>/ipc/f8-<version>/ptt_key_diagnostic.json` | The last key press, or why the tap/backend is unavailable |
| `<runtime>/shortcut.json` | The authoritative recording shortcut, who set it and the previous value (shared by panel, helper and CLI) |
| `<runtime>/shortcut.last-good.json` | The last value that was successfully in force, used when `shortcut.json` is damaged |
| `<project>/work/premiere-review/` | Sessions, notes, WAVs, transcripts and revisions |
| `<project>/work/premiere-review/CURRENT_REVIEW_BATCH.json` | The only batch pointer |

## Uninstall

```bash
./uninstall.sh                        # plan only
./uninstall.sh --apply                # remove this panel and its cuthere command
./uninstall.sh --apply --restore-previous   # restore the previous panel, record and command
./uninstall.sh --apply --purge-runtime      # additionally delete this tool's runtime files
```

Uninstall never deletes `<project>/work/` or `<project>/output/`: your notes, recordings and edit receipts stay in the project. With `--restore-previous` the previous panel, `current_install.json` and the `cuthere` command are put back together, and the defaults value for `PlayerDebugMode` is restored. `--purge-runtime` only removes `versions/f8-*`, `ipc/f8-*` and `bin/cuthere`; unrelated files in a custom runtime root are kept. `--restore-previous` and `--purge-runtime` cannot be combined (run them in two steps), because a restored installation needs its runtime. Restart Premiere afterwards. `uninstall.sh` needs Python 3.10+ (3.11 preferred); it only reads the installation record and moves folders.

## Troubleshooting

**`./install.sh` says the host is not supported.** The candidate requires Apple silicon on macOS 14 or later. `./install.sh --check-only` prints the detected machine and version; Intel Macs are out of scope for the pinned runtime.

**`./install.sh` says Python 3.11 was not found.** Install it from python.org, pass `--python /Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11`, or set `CUTHERE_PYTHON` to that path. `install.sh` honours the same `--python` value it uses to run itself.

**The extension does not appear in Premiere.** Confirm the folder exists at `~/Library/Application Support/Adobe/CEP/extensions/AutoVideoEditReviewCapture/CSXS/manifest.xml`, that `defaults read com.adobe.CSXS.12 PlayerDebugMode` prints `1`, and that you fully quit Premiere (⌘Q) rather than closing the window.

**The panel is blank or the helper never connects.** Run `cuthere doctor`; check `<runtime>/ipc/f8-<version>/status.json` is being updated (its timestamp moves every half second). If the helper process cannot start, run the entry by hand to see the error:

```bash
"$HOME/Library/Application Support/Cuthere/versions/f8-<version>-<stamp>/venv/bin/python" \
  "$HOME/Library/Application Support/Cuthere/versions/f8-<version>-<stamp>/code/services/review_capture/worker_entry.py" \
  --ipc-root "<ipc root from runtime.json>"
```

**The recording shortcut does nothing.** See *The recording shortcut*, *Function keys* and *Permissions*. The panel buttons still record. Run `cuthere shortcut test-state`: it prints the effective binding, the config path, whether the helper is running, and the backend capability. Check `ptt_key_diagnostic.json`: no file at all means the key never reached the helper; `backend: unavailable` means PyObjC or the event tap is the problem. If `status.json` shows `push_to_talk_backend.disabled_events` increasing, macOS is disabling the tap; the take is stopped and saved, and hold-to-talk resumes on the next press (or, for a user-input disable, on the next poll).

**Right Option starts a note and then stops immediately.** Something you pressed next became part of a chord, so Cuthere cancelled that take on purpose; the audio is kept in the capture folder and no note is added. Choose a different key or chord if the modifier is heavily used.

**The shortcut changed by itself, or the panel shows a different key than the CLI.** They read the same `shortcut.json`, so compare `cuthere shortcut show` with `<runtime>/shortcut.json`. `status: recovered` means the file was damaged and the last value that was successfully in force is again active — set it again. An old `F9` saved by the 1.0 panel is migrated once, and only when no `shortcut.json` exists yet.

**Recording is silent or fails immediately.** Check Microphone permission, then the device list in the panel; on some interfaces the *System default* input is not the microphone you expect.

**`cuthere bridge-status` reports a timeout.** Premiere is not running, the panel is closed, or the bridge folder differs between the CLI and `runtime.json`. `--bridge-dir` overrides it for one call.

**Transcription says the model is missing.** The panel was installed without `--download-model` and no cache exists on that machine. Prepare the model (or run the installer again with `--download-model`) and retry; recordings are never deleted.

**A reinstall lost my language choice.** It should not: the previous `runtime.json` is read and its recorded language is kept. If that file was missing, the installer uses `auto`; pass `--language` explicitly to set it again. `previous_runtime.json` in the version folder shows what was seen.

## Acceptance checklist for a real Mac

Complete this on a **copy** of a test project and record results (machine, macOS version, Premiere version, Python version) in [the test record](RELEASE_STATUS.md). Until then the macOS distribution stays experimental.

1. `./install.sh --apply` completes; the receipt (`current_install.json`) exists and records the host as arm64 on macOS 14+; the previous panel (if any) is in the backup folder.
2. Premiere lists **Cuthere · 剪这儿** and the panel shows the helper connected.
3. With the permission granted, hold the real **Right Option** key (the default) for two seconds while Premiere is frontmost: a WAV of roughly two seconds is saved, `ptt_key_diagnostic.json` shows a press, and releasing stops it.
4. Press and hold **left** Option: no take starts. Repeat with F9 after `cuthere shortcut set F9`, and record one note from the panel's button controls.
5. Tap the shortcut very briefly: exactly one short take is saved and no recording stays "stuck".
6. Hold Right Option and then press another letter: that take is cancelled, the panel says so, no note is created, and the next press records normally.
7. Set a chord in the panel (`Control+Shift+K`) and by CLI (`cuthere shortcut set "Ctrl+Shift+K"`), then confirm the panel shows the same value and the helper is armed on it; change it during a take and confirm the change is deferred until the take ends.
8. Switch to another application mid-recording: recording stops and saves; no recording stays "stuck".
9. Kill the helper while recording (Activity Monitor): on the next start the note is marked `interrupted` and the audio file is recoverable.
10. A microphone permission denial is visible in the panel, and granting it later makes recording work without reinstalling.
11. Transcription produces text in the selected language; a manual correction creates a revision.
12. Send a batch, run `cuthere handoff`, and let a local agent do the plan → dry run → publish → read-back cycle on the test project.
13. `./uninstall.sh --apply --restore-previous` restores the previous panel, record and `cuthere` command, and leaves project notes untouched; a following `--purge-runtime` removes only this tool's runtime files.

## Evidence levels for this candidate

The [macOS arm64 run](https://github.com/ceidei/cuthere/actions/runs/39560295086) and [Windows run](https://github.com/ceidei/cuthere/actions/runs/39560295016) pass on commit `03dd0ded`: 299 Python tests, 54 subtests and 25 Node tests on each platform. macOS also passes native dependency installs/imports, installer preflight, shell syntax and 48 sandboxed install/upgrade/restore/purge checks. These runs do not request permissions, record a microphone or operate Premiere. The hardware checklist above remains unperformed.
