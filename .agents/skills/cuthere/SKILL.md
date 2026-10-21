---
name: cuthere
description: "Process time-anchored Premiere review notes captured with the Cuthere hold-to-record shortcut (F8 on Windows, Right Option on macOS by default, user-configurable), make edit decisions, and publish verified revisions. Use for an existing review batch, not for whole-library automatic video understanding."
---

# Cuthere

Locate the Cuthere installation/source directory and the directory containing the current Premiere project. When this skill stays inside the source repository, the repository root is three levels above this skill folder. If the host copied the skill elsewhere, use the user-supplied tool directory or CUTHERE_HOME (F8_REVIEW_EDIT_HOME remains a legacy alias).

Read docs/AGENT_WORKFLOW.md in the tool directory before processing a batch. Run scripts/cli.py handoff with the actual project directory to verify the current batch. The recording shortcut is the editor's own setting: inspect or change it with `scripts/cli.py shortcut show|set|reset`, which shares one stored value with the panel and the recording helper. Follow the document for decision tables, dry-run, supported edit operations, bridge publication, media checks and rollback.

Preserve source media, original projects, audio notes and provenance. Carry forward existing user authorization; generate new working versions. Distinguish a candidate XML from an edit actually imported and verified in Premiere. Complete each note with its result, remaining uncertainty and receipt path.

The installed CLI directly supports limited 25fps edits. Complex audio/picture requests require the agent to inspect the affected media and create a checked plan; the product does not supply general whole-video understanding. A cloud-only agent needs a separate local executor. For installation or host compatibility, consult README.md and docs/AGENT_COMPATIBILITY.md in the tool directory.
