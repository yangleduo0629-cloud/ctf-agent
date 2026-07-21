#!/usr/bin/env python3
"""Prepare one upstream revision on an update-only branch."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import upstream_guard

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "UPSTREAMS.lock.json"
NOTICE_PATH = ROOT / "THIRD_PARTY_NOTICES.md"
REUSE_PATH = ROOT / "third_party" / "reuse.json"


def _run(*args: str, cwd: Path = ROOT) -> str:
    result = subprocess.run(
        list(args), cwd=cwd, check=False, capture_output=True, text=True, encoding="utf-8"
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def require_update_branch(branch: str) -> None:
    if not branch.startswith("upstream/"):
        raise RuntimeError("upstream changes require an upstream/* branch")


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def _copyright_from(path: Path) -> str:
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if re.match(r"(?i)^copyright\b", line.strip()):
            return line.strip()
    raise RuntimeError(f"no copyright statement found in {path.relative_to(ROOT)}")


def prepare(component_id: str, ref: str) -> str:
    branch = _run("git", "branch", "--show-current")
    require_update_branch(branch)
    if _run("git", "status", "--porcelain"):
        raise RuntimeError("working tree must be clean before preparing an upstream update")

    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    component = next((item for item in lock["components"] if item["id"] == component_id), None)
    if component is None:
        raise RuntimeError(f"unknown component: {component_id}")
    old_sha = component["commit"]
    old_copyright = component["license"]["copyright"]

    if component["integration"] == "fork-base":
        _run("git", "fetch", "--no-tags", "upstream", ref)
        new_sha = _run("git", "rev-parse", "FETCH_HEAD")
        if new_sha != old_sha:
            _run("git", "merge", "--no-commit", "--no-ff", new_sha)
    else:
        checkout = ROOT / component["local_path"]
        _run("git", "fetch", "--no-tags", "origin", ref, cwd=checkout)
        new_sha = _run("git", "rev-parse", "FETCH_HEAD", cwd=checkout)
        _run("git", "checkout", "--detach", new_sha, cwd=checkout)

    if new_sha == old_sha:
        raise RuntimeError(f"{component_id} is already pinned to {new_sha}")

    component["commit"] = new_sha
    new_copyright = _copyright_from(ROOT / component["license"]["path"])
    component["license"]["copyright"] = new_copyright
    lock["generated_at"] = (
        datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    _write_json(LOCK_PATH, lock)

    generated_paths: list[str] = []
    if component_id == "veria-ctf-agent":
        _run("uv", "lock", "--python", "3.14")
        generated_paths.append("uv.lock")
    elif component_id == "hexstrike-ai":
        requirements = "upstream/hexstrike-ai/requirements.txt"
        resolved_lock = "third_party/locks/hexstrike-requirements.lock.txt"
        _run(
            "uv",
            "pip",
            "compile",
            requirements,
            "--python-version",
            "3.12",
            "--python-platform",
            "linux",
            "--output-file",
            resolved_lock,
        )
        digest = hashlib.sha256((ROOT / requirements).read_bytes()).hexdigest()
        metadata_path = ROOT / "third_party" / "locks" / "hexstrike-requirements.lock.meta.json"
        _write_json(
            metadata_path,
            {
                "schema_version": 1,
                "source_component": component_id,
                "source_commit": new_sha,
                "input_path": requirements,
                "input_sha256": digest,
                "generator": "uv 0.11.29",
            },
        )
        generated_paths.extend(
            [resolved_lock, str(metadata_path.relative_to(ROOT)).replace("\\", "/")]
        )

    notice = NOTICE_PATH.read_text(encoding="utf-8")
    notice = notice.replace(old_sha, new_sha).replace(old_copyright, new_copyright)
    NOTICE_PATH.write_text(notice, encoding="utf-8", newline="\n")

    reuse = json.loads(REUSE_PATH.read_text(encoding="utf-8"))
    for entry in reuse["entries"]:
        if entry["source_component"] == component_id:
            entry["source_commit"] = new_sha
    _write_json(REUSE_PATH, reuse)

    upstream_guard.generate_sbom()
    paths = [
        "UPSTREAMS.lock.json",
        "THIRD_PARTY_NOTICES.md",
        "third_party/reuse.json",
        "sbom/upstreams.cdx.json",
        *generated_paths,
    ]
    if component["integration"] == "git-submodule":
        paths.append(component["local_path"])
    _run("git", "add", "--", *paths)
    return new_sha


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=("veria-ctf-agent", "pentestgpt", "hexstrike-ai"))
    parser.add_argument("ref", help="upstream branch, tag, or commit to fetch")
    args = parser.parse_args()
    try:
        sha = prepare(args.component, args.ref)
    except RuntimeError as exc:
        print(f"upstream update: {exc}", file=sys.stderr)
        return 1
    print(f"prepared {args.component} at {sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
