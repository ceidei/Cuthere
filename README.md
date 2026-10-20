# Cuthere · 剪这儿

**Say the note in Premiere. Let an agent make the edit.**

Hold **F8** while you watch the cut and speak. Cuthere keeps the recording, the transcript and the exact timeline position together, then hands the batch to a local coding agent that plans the change, builds a new sequence and reads the result back from Premiere.

Current version: **1.1.0-alpha.3** — Windows 1.0.1 is the released path; macOS is an experimental candidate. The macOS candidate has automated checks only; it has not been exercised on real Mac hardware with Premiere, a microphone or F8.
[Download](https://github.com/ceidei/cuthere/releases/latest) · [Tests and compatibility](docs/en/RELEASE_STATUS.md) · [中文说明](README.zh-CN.md)

## Features

- **Comment while watching**: F8/F9 hold-to-record, release to save; a note is tied to the playhead, the selected clips or an in/out range.
- **Transcribe locally**: speech becomes text on your own machine, with playback and manual corrections.
- **Hand a batch to an agent**: every note keeps its audio, text and position so they can be processed together.
- **Edit per note**: the agent combines the project, the relevant media and the context to plan changes and calls editing tools to produce a new version.
- **Keep the original cut**: work happens in a new working project and sequence, with read-back checks and rollback.

Notes can concern sound, picture or the relationship between them. What can actually be finished depends on the agent, the media it can read and the editing actions available; see the [agent workflow](docs/en/AGENT_WORKFLOW.md).

## Install

### Requirements

| Component | Requirement |
|---|---|
| Operating system | Windows 10/11 |
| Editing software | Adobe Premiere Pro with CEP support; tested with 25.6.4 |
| Python | 3.11 with the `py` launcher |
| Media processing | FFmpeg on `PATH`, used by the agent to inspect and process media |
| Agent | Codex or another local agent that can access files and run commands |

### Install with an agent

Download and unzip the [release package](https://github.com/ceidei/cuthere/releases/latest), give the folder to a local Codex session and send:

> Read README.md, install Cuthere for me, prepare the transcription model, and check recording, transcription and the Premiere connection. Use a separate test project and keep backups of the existing project and extensions.

Save your work, restart Premiere when asked, choose a microphone and record one test note with F8. The first installation needs a network connection for dependencies and the transcription model.

### Manual install

In the unzipped folder, run PowerShell:

```powershell
# Show the installation plan
.\install.ps1
# Install and download the default transcription model
.\install.ps1 -Apply -DownloadModel
```

An existing model cache makes `-DownloadModel` optional. After installing, restart Premiere and open **Window → Extensions → Cuthere · 剪这儿**; older builds list it under *Extensions (Legacy)*. Wait until the recording helper and the edit connection are ready.

The installer uses a per-user Python environment and backs up any previous installation. This version is an unsigned CEP extension, so installation enables the matching development-extension setting for the current user. See [uninstall and restore](docs/en/TROUBLESHOOTING.md#uninstall-and-restore-the-previous-panel).

### macOS (experimental)

The candidate source package unpacks to a folder containing `install.sh`:

```bash
unzip cuthere-1.1.0-alpha.3-macos-source.zip -d cuthere-1.1.0-alpha.3-macos-source
cd cuthere-1.1.0-alpha.3-macos-source

./install.sh --check-only   # Python 3.11, FFmpeg, CEP folder, disk, host scope
./install.sh                # DEFAULT: prints the plan, writes nothing
./install.sh --apply        # install for the current user
```

This candidate is scoped to **Apple silicon (arm64) Macs running macOS 14 or later**. The pinned runtime cannot be installed on an Intel Mac, so the installer refuses an unsupported CPU or OS version before changing anything. See [docs/MACOS.md](docs/MACOS.md) for permissions, Function-key conflicts and troubleshooting.

Recording needs macOS **Microphone** permission whichever way a take is started: the F8/F9 key or the panel's own Start/Stop buttons. Hold-to-talk also needs the key-monitoring permission macOS asks for — **Accessibility**, and on some systems **Input Monitoring**. Without key monitoring the panel buttons still record; without microphone permission nothing records. Cuthere cannot grant these permissions; grant the ones the system prompt or the panel's Connection diagnostics asks for.

## Use

1. Open a saved Premiere project and the Cuthere panel.
2. Play or park the playhead where something needs changing, hold **F8**, say the note, release.
3. Listen back and proof-read the text, then record more notes.
4. Press **Send this batch** to write the handoff files, then run:

   ```powershell
   .\f8.ps1 handoff --project-root 'D:\Projects\Example'
   ```

5. Give the printed `AGENT_HANDOFF.md` path to a local agent and say: **"The notes are done. Apply this batch, produce a new version and verify the result."**

`--project-root` is the folder that contains the `.prproj` you were reviewing. The agent lists its plan first and then processes the notes. Keep the Cuthere panel open while it works.

Sending a batch writes local files; it does not start an agent by itself. Recording and transcription run on this machine by default. Once a batch is handed to an agent, how its data is processed depends on the agent, model and tools you chose.

## Supported scope

- **Tested workflow**: Windows, Premiere Pro 25.6.4, Python 3.11 and the Codex desktop app; recording, automatic transcription, note handoff, edit write-back, read-back and rollback.
- **Built-in fixed command**: one explicit range of at most 10 seconds on a 25 fps ordinary sequence.
- **Complex notes**: the agent inspects the relevant picture and sound, writes a plan and calls tools; the result still needs editorial review.
- **Other agents**: a documented file and CLI contract exists; products such as WorkBuddy have not completed the same end-to-end test. See [compatibility](docs/en/AGENT_COMPATIBILITY.md).

Transcription can make mistakes, so proof-read the text first. Other frame rates, complex effects, nested sequences, multicam and speed-changed projects need separate verification. The full test scope is in [the test record](docs/en/RELEASE_STATUS.md). Planned work for 2.0 covers reusable shot analysis, material retrieval and better cross-shot planning and verification. See [the changelog](CHANGELOG.md).

## Help and contributing

- [Installation and runtime troubleshooting](docs/en/TROUBLESHOOTING.md)
- [Agent workflow](docs/en/AGENT_WORKFLOW.md)
- [Development and tests](docs/en/DEVELOPMENT.md)
- [Report an issue](https://github.com/ceidei/cuthere/issues)

Include the software versions, reproduction steps and expected versus actual results. Remove personal paths, client information and keys before sharing logs.

## Licence

[MIT](LICENSE). Premiere, agent products and third-party dependencies keep their own terms. This is an independent third-party extension. See [licensing and privacy](docs/en/LICENSING_AND_PRIVACY.md) and [third-party notices](THIRD_PARTY_NOTICES.md).
