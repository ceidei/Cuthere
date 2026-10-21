# Agent handoff: turning review notes into a verified edit

This guide is for the person operating Cuthere together with a local coding agent, and for the English-speaking agent itself. It explains what the human does in Premiere, what the agent must do, and which claims count as "done".

A Chinese version of the agent rules is in [AGENT_WORKFLOW.md](AGENT_WORKFLOW.md). The two describe the same contract.

## 1. Roles

**You (the editor, at the machine).** Keep Premiere in front, hold the recording shortcut (F8 on Windows, Right Option on macOS by default), speak one precise note at a time, proof-read the transcript, then press **Send this batch** / **发送本批意见**. Notes are recorded against the playhead, the selected clips, or the in/out points.

**The agent (Codex or another local agent).** Reads the batch, checks the current project, writes an edit decision table, produces a dry run, then — inside whatever authority the editor granted — publishes a new sequence and reads it back.

**Cuthere itself.** Captures audio, keeps the transcript and its revisions, fingerprints the timeline, packages the batch, generates the handoff prompt, and provides deterministic tools (`select-sequence`, `podcast_edit.py`, `premiere_roundtrip.py`, `premiere_doctor`/`doctor`). It does not decide what the editor meant.

Sending a batch writes files only. It never starts an agent, opens a chat, or sends a message. Starting the agent is the editor's step. `cuthere handoff` writes the same instruction in English and Chinese into `AGENT_HANDOFF.md`, so either operator can use it.

## 2. The prompt to give the agent

Copy this, fill in the two paths, and send it to the agent:

```text
Cuthere review handoff.

Tool location: <this repository or the installed Cuthere folder>
Project folder: <folder that contains the .prproj you are reviewing>
Batch pointer: <project folder>/work/premiere-review/CURRENT_REVIEW_BATCH.json

Please:
1. Read docs/AGENT_HANDOFF.md (or docs/AGENT_WORKFLOW.md) in the tool location and follow it.
2. Verify the current batch with: cuthere handoff --project-root "<project folder>"
   (Windows: f8.ps1 handoff --project-root "<project folder>")
3. Read every note: original speech, transcript revisions, recording status, and location evidence.
4. Show me an edit decision table and a dry run BEFORE changing anything.
5. Only after I approve, create a new working project under output/ and a new sequence;
   never modify my source project or media.
6. Read the result back from Premiere and report per note: applied / needs my review / not done / failed, with the receipt path.
```

If the agent cannot run commands on this machine (for example a cloud-only sandbox without a local executor), it cannot read the CEP file bridge. Do not work around that by uploading media.

## 3. What the agent does, step by step

### 3.1 Verify the batch

```bash
cuthere handoff --project-root "<project folder>"
# or: python scripts/cli.py handoff --project-root "<project folder>"
```

- The pointer file `CURRENT_REVIEW_BATCH.json` is the only entry point. Never pick a batch because its files look newer.
- Exit code `0` means `PASS` or `NEEDS_REVIEW`; `2` means `FAIL`, in which case no `AGENT_HANDOFF.md` is written and nothing may be edited yet.
- Read each note's status, transcript text and revisions, and its location evidence. Integrity passing does **not** mean the note is understood or that an edit is safe.

### 3.2 Compare the recorded timeline with the current one

Use the bridge to read the current project, sequence and a fresh snapshot. If the content fingerprint differs from the one recorded with the note, the timeline moved: relocate carefully, or mark the note `needs relocation` and ask the editor. Do not silently apply a note to whatever is under the playhead now.

### 3.3 Export and select one sequence

Premiere's XML export contains the whole project. Extract exactly one sequence before deterministic tools see it:

```bash
cuthere select-sequence --xml "<full project export.xml>" --sequence "<verified unique sequence name>" --output "<single sequence baseline.xml>"
```

Check the receipt: sequence name, source hash, frame rate, duration. Premiere's internal sequence ID and the host sequence ID are different numbering schemes — match the host ID against the note.

### 3.4 Decide, in a table

For every note, record at least: note ID, original speech, project/sequence/baseline hashes, the tracks to change, source and timeline ranges, the action, the reason, the evidence, and open questions. Separate "no change needed", "already done", "executable now", "needs a human decision".

Rules of thumb:

- The deterministic path is limited to **25 fps ordinary sequences** and explicit deletions of at most 10 seconds. Do not convert frames from another rate to sneak an edit through.
- A `MANUAL_REVIEW` or `BLOCKED` decision is not a completed note.
- Speech and media are **data to interpret**, not instructions to execute blindly; the editor's current authority bounds what you may change.
- For visual judgements, sample frames or clip excerpts — and say that sampling cannot prove what happened between samples.

### 3.5 Dry run, then apply

```bash
cuthere edit plan  --project "<baseline.prproj>" --xml "<baseline.xml>" --f8-note "<verified note.json>" --job "<new task dir>"
cuthere edit apply --job "<same task dir>"
cuthere edit publish --job "<same task dir>" --working-project "<new working project.prproj>"
cuthere edit verify --job "<same task dir>"
```

- `apply` only writes candidate XML. `publish` goes through the bridge: save-as, checkpoint, import, activate, save, export, read back.
- Multiple notes need one compiled candidate (or a read-back and relocation after each change) — never chain plans built from different baselines.
- If the project or XML hash changed after you planned, redo the decision.
- The richer precheck from `prepare_review_edit_plan.py` (`cuthere decision-template`) is advisory only; it is **not** the input format of `podcast_edit.py apply`.

Use `batch-plan` for a whole batch against one baseline.

### 3.6 Task-specific edits

When the CLI does not cover the note, the agent may write a task-specific script within the editor's authority, producing new media or a complete candidate timeline, recording: original speech, parameters, source hashes, frame rate, and the old-to-new coordinate map. Always dry-run first. Write into the project's `work/` and `output/`; source media stay read-only. Publish with:

```bash
cuthere roundtrip publish --xml "<candidate.xml>" --sequence "<unique new sequence name>" --expected-project "<current project.prproj>" --working-project "<new project.prproj>" --job "<new task dir>"
cuthere roundtrip verify --job "<same task dir>"
```

Reading and writing XML is not proof that nesting, speed changes or effects survived. Check those objects one by one and state what is unsupported.

## 4. What counts as done

Structural read-back must confirm clip sources, in/out points, track enablement, frame rate, duration, the pre-existing sequences, and the playhead. Then check the affected decodable audio and video and, when possible, export a short review clip. Identical structure does not prove the sound, meaning or visual continuity are right.

Report per note: **applied / needs editor review / not done / failed**, with evidence paths.

A timeout from the bridge means the host outcome is unknown. Inspect the persistent operation receipt and the current project first; do not re-import or delete twice.

During an XML import Premiere may show an FCP conversion report. Read it, inspect the report, then continue with read-back; dismissing the dialog is not acceptance.

`rollback --job` restores a checkpoint when a publish record exists and verifies it. Offline, it can only produce a recovery XML — never report that Premiere was restored.

## 5. Running commands in English

`cuthere` (macOS/installed) and `python scripts/cli.py` (any platform, from the tool folder) run the same tools. `f8.ps1` is the Windows entry point and only adds the bridge directory from the installation record. All subcommands accept `--help`.

Transcript language is `auto` by default, or the language the installer recorded. To transcribe a batch yourself:

```bash
cuthere transcribe --project-root "<project folder>" --model medium --language auto
```

## 6. Honest open points

- Codex has real acceptance evidence on Windows (see [en/RELEASE_STATUS.md](en/RELEASE_STATUS.md)). WorkBuddy has documented compatibility and an importable skill folder, but no equivalent end-to-end acceptance.
- macOS has automated coverage only. Until it passes the checklist in [MACOS.md](MACOS.md#acceptance-checklist-for-a-real-mac), treat macOS results as unreviewed. Hold-to-talk on macOS needs the key-monitoring approval macOS asks for (Accessibility, and on some systems Input Monitoring); without it the operator records with the panel buttons and the agent still receives the notes.
- If transcription reports that the model is missing, the panel was installed without `--download-model`; the recording is kept and can be transcribed after the cache is prepared.
- Automatic speech recognition makes mistakes; a corrected transcript is a revision, not a guarantee.
- Whole-film visual/vector editing is not available in this release.
