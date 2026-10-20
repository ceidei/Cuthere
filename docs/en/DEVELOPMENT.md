# Development and tests

## Running the checks

From the repository root on Windows:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
node tests/premiere_lifecycle.cjs
node --test tests/review_ptt_release.cjs tests/cep_adapter.cjs tests/single_panel.cjs tests/panel_platform.cjs
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

The two GitHub workflows run their Python, Node, native-import, shell and packaging checks independently: each check still runs when an unrelated one fails, and any failed check fails the workflow. `FILE_MANIFEST.json` is verified with `python scripts/update_manifests.py --check`; text is hashed from its content with LF line endings, so a fresh checkout matches it.

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
