#!/usr/bin/env python3
"""Build and smoke-test each pinned upstream in an isolated container."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = json.loads((ROOT / "UPSTREAMS.lock.json").read_text(encoding="utf-8"))
COMPONENTS = {item["id"]: item for item in LOCK["components"]}


def _docker(*args: str, capture: bool = False) -> str:
    result = subprocess.run(
        ["docker", *args], cwd=ROOT, check=False, text=True, capture_output=capture
    )
    if result.returncode:
        detail = (
            (result.stderr or result.stdout or "docker command failed").strip()
            if capture
            else "docker command failed"
        )
        raise RuntimeError(detail)
    return result.stdout.strip() if capture else ""


def _image(component_id: str) -> str:
    return f"ctf-agent-upstream-{component_id}:{COMPONENTS[component_id]['commit'][:12]}"


def build(component_id: str) -> None:
    component = COMPONENTS[component_id]
    _docker(
        "build",
        "--file",
        component["environment"]["definition"],
        "--build-arg",
        f"UPSTREAM_SHA={component['commit']}",
        "--tag",
        _image(component_id),
        ".",
    )


def smoke(component_id: str) -> None:
    build(component_id)
    image = _image(component_id)
    if component_id != "hexstrike-ai":
        _docker("run", "--rm", image)
        return

    name = f"ctf-agent-hexstrike-smoke-{int(time.time())}"
    try:
        _docker("run", "--detach", "--rm", "--name", name, image, capture=True)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            probe = subprocess.run(
                [
                    "docker",
                    "exec",
                    name,
                    "python",
                    "-c",
                    "import json,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:8888/health', timeout=5)); assert d['status']=='healthy'",
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
            )
            if probe.returncode == 0:
                print("HexStrike /health returned healthy")
                return
            time.sleep(3)
        logs = _docker("logs", name, capture=True)
        raise RuntimeError(f"HexStrike health check timed out\n{logs[-4000:]}")
    finally:
        subprocess.run(["docker", "stop", name], cwd=ROOT, check=False, capture_output=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "smoke"))
    parser.add_argument("component", choices=(*COMPONENTS.keys(), "all"))
    args = parser.parse_args()
    targets = list(COMPONENTS) if args.component == "all" else [args.component]
    try:
        for component_id in targets:
            print(f"{args.command}: {component_id}")
            (build if args.command == "build" else smoke)(component_id)
    except (OSError, RuntimeError) as exc:
        print(f"upstream environment: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
