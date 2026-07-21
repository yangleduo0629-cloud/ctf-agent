import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "sandbox" / "runner_entrypoint.py"


def test_entrypoint_captures_and_truncates_both_streams(tmp_path: Path) -> None:
    capture_dir = tmp_path / "capture"
    result = subprocess.run(
        [
            sys.executable,
            str(ENTRYPOINT),
            "--capture-dir",
            str(capture_dir),
            "--limit",
            "1024",
            "--",
            sys.executable,
            "-c",
            "import sys; print('x' * 2000, end=''); print('y' * 2000, end='', file=sys.stderr)",
        ],
        check=False,
    )

    assert result.returncode == 0
    assert (capture_dir / "stdout.bin").read_bytes() == b"x" * 1024
    assert (capture_dir / "stderr.bin").read_bytes() == b"y" * 1024
    assert json.loads((capture_dir / "capture.json").read_text(encoding="utf-8")) == {
        "stderr_truncated": True,
        "stdout_truncated": True,
    }


def test_entrypoint_preserves_child_exit_code(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ENTRYPOINT),
            "--capture-dir",
            str(tmp_path / "capture"),
            "--limit",
            "1024",
            "--",
            sys.executable,
            "-c",
            "raise SystemExit(7)",
        ],
        check=False,
    )
    assert result.returncode == 7
