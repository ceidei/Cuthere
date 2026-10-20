# Tests and compatibility

Released version: **1.0.1 (Windows)**. Candidate: **1.1.0-alpha.4 (cross-platform source, macOS experimental)**. Test date: 2026-10-20.

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
| F8 recording | Hold to record, release to save; playhead and sequence position recorded |
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

This candidate makes the source installable on macOS and completes the English documentation. macOS stays **experimental**: every statement below is automated evidence only. No macOS hardware acceptance is recorded and no green CI run is claimed.

The alpha.4 correction fixes what the published alpha.3 GitHub runs reported: the host's spelling of a project path (`/var` vs `/private/var`, an 8.3 short name on Windows) no longer looks like a different project; the source manifest hashes text from its content, so a fresh checkout matches it; and the workflows run their independent Node, native-import, shell and packaging checks even when the Python suite fails. Alpha.3 fixed the sandboxed two-install restore expectations, recorded the shell entry points as executable with LF line endings, and corrected the permission and product wording.

| Evidence | Windows | macOS |
|---|---|---|
| Source imports on the platform interpreter | yes (existing tests) | automated only: a separate-interpreter simulated-Darwin import check; no real Mac interpreter run is recorded |
| Installer/staging/rollback logic | PowerShell test suite | Python tests against a sandboxed HOME and a fake interpreter/venv |
| Panel/bridge path logic | Node.js tests | Node.js tests with injected `darwin` platform objects |
| Runtime and PyObjC imports | 1.0.1 environment | not performed: the workflow is configured to install and import them on real Darwin, and must be run by the maintainer |
| Real Premiere, real microphone, real Function key | 1.0.1 acceptance (before this candidate) | **not performed** |
| Real agent handoff | 1.0 acceptance (Codex) | not performed |
| macOS CI workflow | not applicable | configured in `.github/workflows/macos.yml`; the published alpha.3 candidate was rejected by the GitHub runners and the reported failures are repaired here, but **no passing run is claimed** |

The macOS candidate is scoped to Apple silicon running macOS 14 or later: official wheel metadata lists `onnxruntime==1.31.0` for macOS 14+ arm64 only, with no macOS x86_64 wheel for Intel Macs, so the installer refuses any other host.

macOS stays experimental until the [real-Mac checklist](../MACOS.md#acceptance-checklist-for-a-real-mac) has been completed and recorded here. Windows 1.0.1 remains the stable reference; this candidate must be re-verified on Windows before it can replace it.

## Known limits

- Transcription can produce same-sound character errors; listen back and correct the text.
- The built-in fixed command covers an explicit range deletion of at most 10 seconds on an ordinary 25 fps sequence.
- Complex notes are analysed by the agent and handled with tools; every result needs review.
- The workflow test used a cached medium model; a first download and run with an empty cache has not completed the same test.
- Other computers, Premiere versions, agents, complex effects, nested sequences, multicam, speed changes and other frame rates need separate verification.
- Importing XML may show an effect-conversion notice; check the report and the affected picture and sound.

For usage see the [README](../../README.md); for fixes see [troubleshooting](TROUBLESHOOTING.md).
