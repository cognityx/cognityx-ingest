from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path)
    args = parser.parse_args()
    fixture = Path(__file__).resolve().parent
    manifest = json.loads((fixture / "fixture_manifest.json").read_text(encoding="utf-8"))

    errors: list[str] = []
    for item in manifest["synthetic_source_truth"]:
        path = fixture / item["path"]
        if not path.is_file():
            errors.append(f"missing delta source: {path}")
        elif sha256(path) != item["sha256"]:
            errors.append(f"delta source hash mismatch: {path}")

    sums = {}
    for line in (fixture / "sha256sums.txt").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, rel = line.split("  ", 1)
        sums[rel] = digest
    for rel, digest in sums.items():
        path = fixture / rel
        if not path.is_file():
            errors.append(f"missing packed file: {rel}")
        elif sha256(path) != digest:
            errors.append(f"packed file hash mismatch: {rel}")

    if args.repo_root:
        base = manifest["base_fixture"]
        base_path = args.repo_root / base["path"]
        if not base_path.is_file():
            errors.append(f"missing authoritative base fixture: {base_path}")
        elif sha256(base_path) != base["sha256"]:
            errors.append(
                f"authoritative base fixture hash mismatch: {base_path}; "
                f"expected {base['sha256']}, got {sha256(base_path)}"
            )

    if errors:
        print("Fixture verification FAILED")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Fixture verification PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
