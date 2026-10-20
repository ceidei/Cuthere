"""Regenerate the repository's public source-checksum manifest.

``FILE_MANIFEST.json`` records the size and SHA-256 of the public source tree.
The per-archive manifest (``output/<package>.manifest.json``) is written by
``build_release.py`` and is authoritative for that ZIP; a manifest that contains
the hash of the archive it is stored in cannot be generated without changing
that hash, so neither repository file is shipped inside a package.

Usage:

    python scripts/update_manifests.py
    python scripts/update_manifests.py --check
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

RELEASE_ROOT = Path(__file__).resolve().parents[1]
if str(RELEASE_ROOT) not in sys.path:
    sys.path.insert(0, str(RELEASE_ROOT))

from scripts import build_release  # noqa: E402

FILE_MANIFEST = RELEASE_ROOT / "FILE_MANIFEST.json"


def version() -> str:
    return (RELEASE_ROOT / "VERSION").read_text(encoding="utf-8").strip()


def is_text(data: bytes) -> bool:
    """Decide from the bytes, not the file name, whether Git treats it as text.

    The suffix is not a reliable signal: ``.gitignore``, ``.gitattributes``,
    ``LICENSE`` and the ``.cjs`` test drivers are text, while a text-suffixed
    file can still be binary. A checkout converts only text line endings, so the
    record must classify the same way: valid UTF-8 without NUL bytes is text.
    """
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def normalized_bytes(path: Path) -> bytes:
    """Hash text with LF endings so the record is checkout-independent.

    This checkout mixes CRLF and LF (Git's autocrlf setting varies by host), and
    a fresh GitHub checkout writes the LF bytes of the committed blob. The
    archive's own manifest hashes the exact bytes in the ZIP; this record is a
    human/tool reference for the source content, not an archive checksum.
    """
    data = path.read_bytes()
    return data.replace(b"\r\n", b"\n") if is_text(data) else data


def file_entry(path: Path, relative: str) -> dict[str, object]:
    data = normalized_bytes(path)
    return {"path": relative, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def public_files() -> list[str]:
    """The public source tree: both package allowlists plus the Python tests.

    Tests are not shipped in the installation archive (they reference both
    platform entry points), but they are part of the repository this manifest
    describes.
    """
    files = set(build_release.select_files("windows")) | set(build_release.select_files("macos"))
    files.update(build_release._iter_tree("tests"))
    return sorted(files)


def _entry(relative: str) -> dict[str, object]:
    return file_entry(RELEASE_ROOT / relative, relative)


def build_file_manifest(files: list[str] | None = None) -> dict:
    files = files or public_files()
    return {
        "product": "Cuthere",
        "version": version(),
        "scope": "public cross-platform source tree; per-archive hashes are in the .manifest.json "
                 "next to each release ZIP",
        "hashing": "text is detected from the content (UTF-8 without NUL bytes) and hashed with LF "
                   "line endings, so the record does not depend on the checkout's core.autocrlf "
                   "setting or on the file suffix; the archive manifest hashes exact ZIP bytes",
        "generated_by": "scripts/update_manifests.py",
        "files": [_entry(relative) for relative in files],
    }


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def generate() -> dict:
    manifest = build_file_manifest()
    write_json(FILE_MANIFEST, manifest)
    return manifest


def check() -> list[str]:
    """Return differences between the committed manifest and the tree."""
    if not FILE_MANIFEST.is_file():
        return ["FILE_MANIFEST.json is missing; run scripts/update_manifests.py"]
    expected = build_file_manifest()
    actual = json.loads(FILE_MANIFEST.read_text(encoding="utf-8"))
    return [] if actual == expected else ["FILE_MANIFEST.json is stale; run scripts/update_manifests.py"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify the manifest without writing")
    args = parser.parse_args(argv)
    if args.check:
        problems = check()
        for problem in problems:
            print(problem)
        return 1 if problems else 0
    manifest = generate()
    print(json.dumps({
        "file_manifest": str(FILE_MANIFEST),
        "version": manifest["version"],
        "files": len(manifest["files"]),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
