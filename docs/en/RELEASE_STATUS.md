# Tests and compatibility

Released version: **1.0.1 (Windows)**. Candidate: **1.1.0-alpha.6 (cross-platform source, macOS experimental)**. Test date: 2026-10-21.

## Tested environment

| Component | Tested configuration |
|---|---|
| Operating system | Windows 11 Pro |
| Editing software | Adobe Premiere Pro 25.6.4 |
| Python | 3.11 |
| Agent | Codex desktop app with local command execution |
| Transcription | faster-whisper, medium model |
| Test sequence | 25 fps, video and two-channel audio |

## What was verified

| Item | Scope |
|---|---|
| Install and connection | Isolated Python environment, Premiere panel, recording helper and edit connection |
| F8 recording (Windows) | Hold to record, release to save; playhead and sequence position recorded |
| Automatic transcription | Original audio, Chinese transcript and revision record saved |
| Note handoff | A real one-note batch passed the integrity check and was read by Codex |
| Picture and sound edit | Frames 125–150 of the video and both audio channels deleted together; 12 s / 300 frames became 11 s / 275 frames |
| Write-back and read-back | New working project and sequence; media, tracks, frame rate and duration checked |
| Export check | Exported picture frame count, audio and level compared |
| Recovery | Panel reconnection and project checkpoint restore |
| 1.0.1 interface | Extension name, dark scrollbars and connection state |

The full recording and edit test used 1.0.0. 1.0.1 verified the interface, installation and connection, and did not repeat the full recording/edit test. The machine-readable record is [VALIDATION.json](../../VALIDATION.json).

Automated checks cover the Python tests, the Node panel/bridge tests, the bridge host contract and install/restore filesystem tests; see [development and tests](DEVELOPMENT.md) for the commands.

## 1.1.0-alpha.4 candidate

This candidate makes the source installable on macOS and completes the English documentation. macOS stays **experimental**: the automated suites pass on both GitHub runners (recorded below), but no macOS hardware acceptance is recorded.

The alpha.4 correction fixes what the published alpha.3 GitHub runs reported: the host's spelling of a project path (`/var` vs `/private/var`, an 8.3 short name on Windows) no longer looks like a different project; the source manifest hashes text from its content, so a fresh checkout matches it; and the workflows run their independent Node, native-import, shell and packaging checks even when the Python suite fails. Alpha.3 fixed the sandboxed two-install restore expectations, recorded the shell entry points as executable with LF line endings, and corrected the permission and product wording.

### Recorded automated runs

Both platform workflows **succeeded** on the functional candidate commit [`7bb307a2d88e6dfa0cfc0353f776d7af30080fd8`](https://github.com/ceidei/cuthere/commit/7bb307a2d88e6dfa0cfc0353f776d7af30080fd8) of [ceidei/cuthere](https://github.com/ceidei/cuthere):

| Run | Runner | Result | What it covers |
|---|---|---|---|
| [Windows run 39449546296](https://github.com/ceidei/cuthere/actions/runs/39449546296) | `windows-latest` | success | 204 Python tests; 15 Node panel and bridge tests; PowerShell syntax and the install/restore filesystem cases; the Windows allowlist dry run; `update_manifests.py --check` |
| [macOS run 39449546127](https://github.com/ceidei/cuthere/actions/runs/39449546127) | `macos-15` (arm64) | success | 204 Python tests on a real Darwin interpreter with the arm64 and macOS 14 assertions; 15 Node tests; installation and import of the macOS runtime and PyObjC; `install_macos.py --check-only`; `bash -n` on the three shell entry points; 48 sandboxed install/upgrade/restore/purge checks with 0 failures; the macOS allowlist dry run; `update_manifests.py --check` |

Those runs are automated evidence only. No step loads the panel in Premiere, presses a physical F8/F9 or Right Option key, answers a TCC permission prompt or captures from a microphone on either platform. The 1.0.0 and 1.0.1 Windows acceptance above was recorded on real hardware and remains the reference for actual editing behaviour; the CI runs neither repeat nor extend it. The two runs belong to commit `7bb307a2`, which is the code before the configurable recording shortcut; this candidate needs its own runs.

## 1.1.0-alpha.6 candidate: shortcut review repairs

This candidate fixes the five concrete shortcut defects found in independent review; it does not add a feature or widen the supported scope. macOS remains **experimental** and no CI run exists for this candidate yet.

- **macOS unknown keys and modifier chords.** Any real non-modifier key, including punctuation, Escape and keys outside the bindable grammar, cancels a modifier-only take and counts as an already-held key. A modifier-only binding no longer starts when another modifier or key is already held (both orders are covered). When the opposite side keeps the family flag set, a required side's release is resolved with `CGEventSourceKeyState` instead of the aggregate `CGEventFlags`; the same bounded native read reconciles the sides after a re-arm, and an unknown side state is never guessed into a take.
- **Windows registration failure.** A refused `RegisterHotKey` is visible as `registration_failed` in the panel, the CLI and `ptt_key_diagnostic.json`; it is not reported as armed/applied and does not start a take. An active take stops and keeps its audio if the registration is lost, and the explicit observe-only diagnostic mode is reported separately. Windows now refuses `F12` and chords whose base key is `F12` (Microsoft reserves it for debuggers); macOS still allows `F12`.
- **Live readback.** A helper is "running" only when its `status.json` heartbeat is at most 10 seconds old and its configuration path and platform match the requested ones. Stale, malformed or foreign files no longer claim a live helper or prove a new setting applied. The CLI reports configured, effective, applied and pending separately: a successful write is saved, not necessarily applied, and a status written before the write is not post-write acknowledgment.
- **Corruption recovery.** `shortcut.last-good.json` now holds the last value that was successfully in force (F10 after F9 then F10), not the previous value (F9). A warm or freshly constructed reader recovers that value from a truncated or malformed current file, keeps explicit diagnostics, and leaves `restore-previous` and `previous_binding` as the separate undo history.
- **Packaging.** The alpha.6 source archive is built from a fresh committed-blob export and every entry is compared with `git cat-file`; per-archive exact hashes and the LF-normalised repository manifest remain separate. The alpha.5 archive is preserved unchanged.

Automated locally on the Windows development host: **298 Python tests passed** (1 skipped: the directory-symlink alias test needs a privilege this host does not have), **54 subtests passed**, and **25 Node panel/bridge tests passed**. These cover the negative cases above, including the exact modifier/key orderings, a refused `RegisterHotKey` with a physically down key, stale/mismatched status files, and warm/fresh corruption recovery. They do not press a physical key or capture a microphone, and there is no CI run for this candidate.

## 1.1.0-alpha.5 candidate: a configurable recording shortcut

macOS now defaults to **Right Option**; Windows keeps **F8**. The shortcut can be recorded, saved, cancelled and reset in the panel, or changed with `cuthere shortcut show|set|reset|restore-previous|test-state|grammar`. The panel, the recording helper and the command line share one per-user setting, `<runtime>/shortcut.json`: a CLI change appears in an open panel and takes effect immediately, and the old panel `localStorage` preference is migrated exactly once, only when that file does not exist yet, so it can never overwrite a newer value.

Supported values are `Control`/`Option`/`Shift`/`Command` (including `Left`/`Right` spellings) plus one main key (`F1`–`F12`, `A`–`Z`, `0`–`9`, `Space`, `Enter`, `Tab`, `Backspace`, `Insert`, `Delete`, `Home`, `End`, `PageUp`, `PageDown` and the four arrows); macOS additionally allows one side-qualified modifier on its own. `Escape` and `Fn`/Globe cannot be bound, Windows cannot bind side-qualified or modifier-only values, and an unsupported value is rejected with a message instead of silently becoming F8. Changing the shortcut during a take is reported as pending and applies to the next take, never mid-recording.

- Automated checks run locally on the Windows development host: 273 Python tests passed (1 skipped: the directory-symlink alias test needs a privilege this host does not have), 50 subtests passed, and 25 Node panel/bridge tests passed. They cover the parser/validation matrix (Python and the panel JavaScript read the same test vectors), fresh defaults, migration of an explicit old F9, persistence/reset/invalid input, a damaged file keeping the last good value, macOS left-vs-right Option and `flagsChanged` release, both key/modifier release orders, short taps and auto-repeat, a modifier-only take cancelled when another key turns it into a chord (no note is created, the audio is kept), a chord pressed in another application neither starting nor cancelling a take, the Windows F8 regression and chord registration, a CLI or bridge change picked up by the running helper, no mid-take swap, and the panel value and the CLI converging on one setting.
- Not performed: pressing Right Option on a real Mac, a real Windows/Premiere chord, a real microphone, and the real interception/conflict behaviour of these keys with Premiere in front. The tests use synthetic key events and cannot stand in for hardware acceptance. The GitHub workflows have not been run for this candidate's commit yet.

| Evidence | Windows | macOS |
|---|---|---|
| Source imports on the platform interpreter | yes (existing tests and CI) | yes in CI: a real Darwin interpreter on the arm64 runner imports the runtime and PyObjC |
| Installer/staging/rollback logic | PowerShell test suite (CI) | Python tests against a sandboxed HOME, plus 48 sandboxed install/upgrade/restore/purge checks in CI |
| Panel/bridge path logic | Node.js tests (CI) | Node.js tests with injected `darwin` platform objects (CI) |
| Configurable recording shortcut | parser, persistence, shared setting and Windows registration covered by tests (local; the Windows run for this candidate is pending) | parser and panel recorder covered by tests; left/right Option exercised with synthetic `flagsChanged` events, a physical press is **not performed** |
| Shortcut review repairs (alpha.6) | unknown-key/chord edges, registration collision, live-readback freshness/identity, last-good recovery and committed-blob packaging covered locally | native side-state and stale/re-arm edges covered with a fake Quartz surface; no physical Mac press |
| Runtime and PyObjC imports | 1.0.1 environment | installed and imported in CI on the arm64 runner; no event tap, microphone stream or permission request is created |
| Real Premiere, real microphone, real Function key | 1.0.1 acceptance (before this candidate) | **not performed** |
| Real agent handoff | 1.0 acceptance (Codex) | not performed |
| Real Mac hardware: installer, TCC prompt, CEP load, Premiere edit | not applicable | **not performed** |

The macOS candidate is scoped to Apple silicon running macOS 14 or later: official wheel metadata lists `onnxruntime==1.31.0` for macOS 14+ arm64 only, with no macOS x86_64 wheel for Intel Macs, so the installer refuses any other host. No Intel Mac support is claimed.

macOS stays experimental until the [real-Mac checklist](../MACOS.md#acceptance-checklist-for-a-real-mac) has been completed and recorded here. Windows 1.0.1 remains the stable reference; this candidate must be re-verified on Windows before it can replace it.

## Known limits

- Transcription can produce same-sound character errors; listen back and correct the text.
- The built-in fixed command covers an explicit range deletion of at most 10 seconds on an ordinary 25 fps sequence.
- Complex notes are analysed by the agent and handled with tools; every result needs review.
- The workflow test used a cached medium model; a first download and run with an empty cache has not completed the same test.
- Other computers, Premiere versions, agents, complex effects, nested sequences, multicam, speed changes and other frame rates need separate verification.
- Importing XML may show an effect-conversion notice; check the report and the affected picture and sound.

For usage see the [README](../../README.md); for fixes see [troubleshooting](TROUBLESHOOTING.md).
