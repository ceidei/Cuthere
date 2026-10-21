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

## 1.1.0-alpha.6: configurable recording shortcuts

The defaults are Right Option on macOS and F8 on Windows. Change, save or reset the recording key in the panel, or use `cuthere shortcut` from a local agent. Both interfaces share one setting. Changes made during a take apply after recording ends.

### Automated checks

These runs cover commit [`03dd0ded`](https://github.com/ceidei/cuthere/commit/03dd0dedb8e4b6acb26f406063efb84b3e2adf4c):

| Platform | Result | Coverage |
|---|---|---|
| [Windows](https://github.com/ceidei/cuthere/actions/runs/39560295016) | Passed | 299 Python tests, 54 subtests, 25 Node tests, PowerShell install/restore cases and the release file manifest |
| [macOS arm64](https://github.com/ceidei/cuthere/actions/runs/39560295086) | Passed | The same Python and Node suites, native dependency installation/imports, installer preflight, shell syntax, 48 sandboxed install/upgrade/restore/purge checks and the release file manifest |

Shortcut checks cover left/right modifiers, chord release, short taps and repeat, foreground changes, registration failure, setting synchronization and corruption recovery. They use synthetic key events and test directories; they do not establish physical keyboard or Premiere acceptance.

### Compatibility and remaining hardware tests

- macOS experiment: Apple silicon, macOS 14 or later. Intel Macs are not supported.
- **Not performed:** physical Right Option, microphone capture, system permission prompts, Premiere panel loading and agent editing on a real Mac.
- Custom Windows chords have not been accepted in a live Premiere session. The historical Windows 1.0.0/1.0.1 acceptance remains limited to the scope above.
- The Mac event tap observes rather than suppresses keys. Premiere or another application may also respond. Fn/Globe and Escape cannot be bound; Windows additionally refuses F12, side-specific modifiers and modifier-only bindings.

macOS remains experimental until the [hardware checklist](../MACOS.md#acceptance-checklist-for-a-real-mac) is completed. [Experimental download](https://github.com/ceidei/cuthere/releases/tag/v1.1.0-alpha.6) · [Development and tests](DEVELOPMENT.md).
