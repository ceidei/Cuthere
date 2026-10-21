# Agent compatibility

Chinese original: [../AGENT_COMPATIBILITY.md](../AGENT_COMPATIBILITY.md).

F8 capture and the note files do not depend on any particular model or agent. To work on the batch, an agent needs access to the same computer that runs Premiere, to the project folder, and the ability to execute local commands (`python`, and on Windows `powershell`). Visual judgement still depends on that agent's model and the media tools it has.

The recording shortcut itself is a per-user setting the agent can read and change through `cuthere shortcut show|set|reset`, which uses the same stored value as the panel and the recording helper.

| Entry point | Current status |
|---|---|
| Codex desktop app / CLI | Real acceptance on Windows: 1.0 completed a physical F8 recording → edit → host read-back on the local Codex desktop environment. See [RELEASE_STATUS.md](RELEASE_STATUS.md). |
| Any agent on macOS | Automated checks pass on an Apple silicon runner, but no agent has completed the cycle on a real Mac: the panel has not been loaded in Premiere and no microphone, recording shortcut or TCC prompt has been exercised. |
| WorkBuddy (local) | Officially supports local skills and script/CLI execution. Handoff documentation is prepared; no equivalent end-to-end edit acceptance has been completed. |
| Other local agents | Can follow the same CLI and file contract; verify each one individually. |
| Cloud-only sandbox | Cannot read the local CEP file bridge; a separate local executor would be required. This version provides no remote transfer. |

Codex can open this source folder and read `AGENTS.md` plus the bundled skill. WorkBuddy can import the `.agents/skills/cuthere/` skill folder according to its own documentation; if the skill is copied elsewhere, first tell the agent where the tool and project folders are. You can also hand `AGENT_HANDOFF.md` directly to any agent and ask it to read `docs/AGENT_HANDOFF.md` (English) or `docs/AGENT_WORKFLOW.md` (中文).

"Send this batch" only produces a file bundle; `handoff` only produces a handoff prompt. Neither starts an agent, opens a new chat or sends a message. A WorkBuddy local-skill trial and a listing in a connector marketplace are different deliverables; this version does not claim to meet marketplace conditions.

WorkBuddy acceptance procedure: in an isolated 25 fps test project — import the instructions → read a batch of notes → state an explicit short-deletion dry run → new candidate → publish through the bridge → read back → roll back, then verify one complex note. Record software/model versions and evidence. Only after that may the status change to verified.

## macOS notes

The CLI contract is identical on macOS; paths differ (`~/Library/Application Support/Cuthere/`). The macOS build is covered by automated checks only: the [macOS run 39449546127](https://github.com/ceidei/cuthere/actions/runs/39449546127) on an Apple silicon runner installs and imports the real Darwin runtime and PyObjC, runs the installer's check-only plan and 48 sandboxed install/upgrade/restore/purge checks, and passes the Python and Node suites. That run starts no agent, opens no microphone, presses no Function key, answers no permission prompt and does not load the panel in Premiere, so a real Mac edit remains unverified — and it predates the configurable recording shortcut, which has no CI run yet. The global shortcut backend needs the key-monitoring permission macOS asks for (Accessibility, and on some systems Input Monitoring) for the process that hosts it; without it the agent can still work from notes recorded with the panel's own buttons. Nothing in this repository can grant those permissions, and no test result implies that they were granted.

## References checked for the 1.0 release (2026-10-20)

- [Codex AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
- [WorkBuddy skills](https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Skills-Market)
- [WorkBuddy connector and CLI](https://open.workbuddy.cn/docs/connector)
