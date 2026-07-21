from __future__ import annotations

import argparse
import json
import subprocess
import threading
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture-dir", type=Path, required=True)
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("child command is required")
    return args


def drain(source, destination: Path, limit: int, state: dict[str, bool]) -> None:
    written = 0
    with destination.open("wb", buffering=0) as output:
        while chunk := source.read(64 * 1024):
            remaining = max(limit - written, 0)
            if remaining:
                kept = chunk[:remaining]
                output.write(kept)
                written += len(kept)
            if len(chunk) > remaining:
                state["truncated"] = True


def main() -> int:
    args = parse_args()
    args.capture_dir.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        args.command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdout is None or process.stderr is None:
        raise RuntimeError("child output pipes were not created")

    stdout_state = {"truncated": False}
    stderr_state = {"truncated": False}
    threads = [
        threading.Thread(
            target=drain,
            args=(process.stdout, args.capture_dir / "stdout.bin", args.limit, stdout_state),
        ),
        threading.Thread(
            target=drain,
            args=(process.stderr, args.capture_dir / "stderr.bin", args.limit, stderr_state),
        ),
    ]
    for thread in threads:
        thread.start()
    return_code = process.wait()
    for thread in threads:
        thread.join()

    (args.capture_dir / "capture.json").write_text(
        json.dumps(
            {
                "stdout_truncated": stdout_state["truncated"],
                "stderr_truncated": stderr_state["truncated"],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
