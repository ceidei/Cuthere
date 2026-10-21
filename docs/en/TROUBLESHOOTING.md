# Installation and runtime troubleshooting

macOS-specific items (Function keys, Accessibility/Input Monitoring/Microphone permissions) are in [the macOS guide](../MACOS.md#troubleshooting).

## The panel is blank after restarting Premiere

Save your project, quit Premiere completely, reopen it and select **Window → Extensions → Cuthere · 剪这儿**. Check the recording-helper and edit-connection status in the panel. If Premiere was started by an automated launcher or by a script, close it and start it the way you normally do.

## Holding the recording shortcut records nothing

Keep Premiere in front and confirm the panel shows the helper connected, then check the selected microphone and input level. Start with the system default input, then pick a specific microphone and channel if playback is silent.

Run `cuthere shortcut show` and compare the binding with the key you pressed: the default is F8 on Windows and Right Option on macOS, and an unsupported value is rejected rather than silently replaced. Notes and recordings live under `<project>/work/premiere-review/`. Listen to the WAV first to separate a microphone problem from a transcription problem. The helper writes `ptt_key_diagnostic.json` every time an armed press is seen; if the file never appears, the key never reached the helper (another application owns it, the panel is not ready, or — on macOS — the Function key is not being sent; see the [macOS guide](../MACOS.md)).

On Windows, a diagnostic file showing `state: registration_failed` means `RegisterHotKey` could not take the combination (another application owns it or Windows reserves it). Choose another combination or close the application holding it; Cuthere will not start a take on a shortcut it did not register. Windows refuses F12 and chords whose base key is F12; use F1–F11 or another key.

`live.helper_running` in `cuthere shortcut show` is true only while the helper's heartbeat is at most 10 seconds old and its `shortcut_config` and platform match the request. An old `status.json` left by an exited helper is reported as stale and cannot prove that a setting applied. `saved` means the file was written; `applied`/`pending` say whether a live helper has actually taken it. If `shortcut.json` is truncated or hand-edited into invalid JSON, Cuthere recovers the last value that was successfully in force from `shortcut.last-good.json`; `status: recovered` marks that recovery.

## There is a recording but no transcript

Check that the transcription model is available. From the tool folder:

```powershell
.\install.ps1 -Apply -DownloadModel
```

Look at that note's `transcription/job.json` and give the error to the agent. The recording is kept and the text can be corrected. If the text is in the wrong language, check the language recorded in the runtime configuration (`auto` by default; an explicitly recorded value is preserved).

## The agent receives no notes

Press **Send this batch** in the panel first, then run:

```powershell
.\f8.ps1 handoff --project-root 'D:\Projects\Example'
```

Use the folder that contains the `.prproj`, give the printed `AGENT_HANDOFF.md` path to the agent and make sure it may access that folder. A non-zero exit means the batch failed verification: read the report under `work/premiere-review/intake-*` first.

## The edit connection drops

Keep the Cuthere panel open. Closing the panel pauses the connection; reopen it and wait for the status to recover. If it stays disconnected, check the diagnostics at the bottom of the panel.

## The import stalls or times out

Look for a conversion-report or media-relink dialog in Premiere. After handling it, have the agent inspect the current timeline and the persisted operation receipt before deciding what to do next. A timeout can happen after Premiere has already started the work — read back first, then retry.

## Uninstall and restore the previous panel

```powershell
# Windows: show the plan, then uninstall
.\uninstall.ps1
.\uninstall.ps1 -Apply
# Uninstall and restore the version from before the installation (needs a backup)
.\uninstall.ps1 -Apply -RestorePrevious
```

```bash
# macOS
./uninstall.sh                                 # plan only
./uninstall.sh --apply                          # uninstall
./uninstall.sh --apply --restore-previous       # uninstall and restore the previous version
```

Uninstall keeps the project, its source media and the notes stored in the project folder. Restart Premiere afterwards. On macOS, `--purge-runtime` only removes this tool's own runtime files and must be run separately from `--restore-previous`; combining them is refused.

## Reporting a problem

Include the operating system, Premiere, Python and Cuthere versions, the steps and the error text. Remove personal paths, client information and keys before sharing logs; the original media and project are not needed.
