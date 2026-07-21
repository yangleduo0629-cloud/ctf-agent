#!/usr/bin/env python3
"""Require upstream-only branches whenever immutable source pins change."""

from __future__ import annotations

import os
import subprocess
import sys

PROTECTED = (
    ".gitmodules",
    "UPSTREAMS.lock.json",
    "THIRD_PARTY_NOTICES.md",
    "sbom/upstreams.cdx.json",
    "third_party/reuse.json",
    "upstream/hexstrike-ai",
    "upstream/pentestgpt",
)


def main() -> int:
    base = os.environ.get("BASE_SHA", "")
    head_ref = os.environ.get("HEAD_REF", "")
    if not base:
        print("BASE_SHA is required", file=sys.stderr)
        return 2
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    changed = set(result.stdout.splitlines())
    protected_changed = sorted(changed.intersection(PROTECTED))
    if protected_changed and not head_ref.startswith("upstream/"):
        print(
            "immutable upstream files changed outside an upstream/* branch: "
            + ", ".join(protected_changed),
            file=sys.stderr,
        )
        return 1
    print(f"upstream PR policy passed; protected changes: {len(protected_changed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
