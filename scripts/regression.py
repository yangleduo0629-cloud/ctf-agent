#!/usr/bin/env python3
"""Run upstream provenance checks and source or container regressions."""

from __future__ import annotations

import argparse
import ast
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args: str) -> None:
    result = subprocess.run(list(args), cwd=ROOT, check=False)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(args)}")


def source_regression() -> None:
    run(sys.executable, "scripts/upstream_guard.py", "verify")
    if shutil.which("uv"):
        run("uv", "lock", "--check")
    run(sys.executable, "-m", "compileall", "-q", "backend", "scripts")
    for relative in (
        "upstream/pentestgpt/pentestgpt_legacy/main.py",
        "upstream/hexstrike-ai/hexstrike_mcp.py",
        "upstream/hexstrike-ai/hexstrike_server.py",
    ):
        path = ROOT / relative
        ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    run(sys.executable, "-m", "unittest", "discover", "-s", "tests/supply_chain", "-p", "test_*.py")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("source", "containers"))
    parser.add_argument("--component", default="all")
    args = parser.parse_args()
    try:
        source_regression()
        if args.mode == "containers":
            run(sys.executable, "scripts/upstream_env.py", "smoke", args.component)
    except RuntimeError as exc:
        print(f"regression: {exc}", file=sys.stderr)
        return 1
    print(f"{args.mode} regression passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
