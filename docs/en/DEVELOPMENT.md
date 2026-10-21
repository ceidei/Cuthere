# Development and tests

## Running the checks

From the repository root on Windows:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
node tests/premiere_lifecycle.cjs
node --test tests/review_ptt_release.cjs tests/cep_adapter.cjs tests/single_panel.cjs tests/panel_platform.cjs tests/panel_shortcut.cjs
```

The installation and restore check uses a separate temporary directory:

```powershell
.\tests\panel_install.ps1 -TestRoot 'D:\CuthereTests\fresh-run'
```

On macOS:

```bash
python3.11 -m pytest tests -q
sh tests/shell/installer_tests.sh          # sandboxed folders; no real extension folder or preferences
./install.sh                               # plan only by default
```

`-TestRoot` must point at a directory that does not exist yet. These checks use synthetic data and must never operate on a real project or the current Premiere installation. They cover file formats, installation/restore logic and interface behaviour; changes that involve a microphone, keys, Premiere or an agent still need the full workflow checked in a separate test project.

## Continuous integration

Two workflows run on every push to `main` and `feature/macos-english` and on pull requests. Their checks are independent: each still runs when an unrelated one fails, and any failed check fails the workflow. Nothing uses `continue-on-error`; a check whose own dependency install failed is skipped by name instead of silently passing.

| Workflow | Runner | Checks |
|---|---|---|
| `windows.yml` | `windows-latest` | Python tests, Node panel/bridge tests, PowerShell syntax and the install/restore filesystem cases, the Windows allowlist dry run, `update_manifests.py --check` |
| `macos.yml` | `macos-15` (Apple silicon) | Python tests on a real Darwin interpreter, Node tests, macOS runtime and PyObjC install/import, `install_macos.py --check-only`, shell syntax, the sandboxed install/upgrade/restore/purge suite, the macOS allowlist dry run, `update_manifests.py --check` |

The functional candidate commit [`7bb307a2d88e6dfa0cfc0353f776d7af30080fd8`](https://github.com/ceidei/cuthere/commit/7bb307a2d88e6dfa0cfc0353f776d7af30080fd8) passed both: [Windows run 39449546296](https://github.com/ceidei/cuthere/actions/runs/39449546296) with 204 Python and 15 Node tests, and [macOS run 39449546127](https://github.com/ceidei/cuthere/actions/runs/39449546127) with 204 Python tests, 15 Node tests, successful runtime/PyObjC imports and 48 sandboxed installer checks with 0 failures. CI does not exercise a microphone, a physical Function key, a TCC prompt or Premiere loading the panel; see [tests and compatibility](RELEASE_STATUS.md) for what remains manual.

`FILE_MANIFEST.json` is verified with `python scripts/update_manifests.py --check`; text is hashed from its content with LF line endings, so a fresh checkout matches it. CI only checks the manifest and never regenerates it, so run `python scripts/update_manifests.py` yourself after changing a shipped file.

## Release packages

Source packages are built from an allowlist with a leak scan and written to `output/`:

```powershell
python scripts/build_release.py --platform macos
python scripts/build_release.py --platform windows
```

The archives do not contain the test suite, run records or model weights. The `.sha256` and `.manifest.json` next to each ZIP are authoritative for that archive; the repository-level `FILE_MANIFEST.json` only describes the public source tree.

## Submitting changes

- Keep changes focused and include reproduction steps or test results.
- Use synthetic media and example paths.
- Do not commit recordings, projects, client material, model weights or credentials.
- After changing public files, update their size and SHA-256 in `FILE_MANIFEST.json`.
- State which checks actually ran and which environments remain unverified.

## Agent interface

Handoff, decision, write-back and verification steps are in the [agent workflow](AGENT_WORKFLOW.md). Keep source media and the original project read-only, and write results into a new working project and task directory.
