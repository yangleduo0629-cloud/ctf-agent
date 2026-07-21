import os
from pathlib import Path

from fastapi import FastAPI

from backend.sandbox_runner.engine import DockerSandboxRunner
from backend.sandbox_runner.internal_api import InternalRunnerRuntime
from backend.sandbox_runner.internal_api import router as runner_router

app = FastAPI(title="CTF Sandbox Runner", version="0.1.0")
app.state.runner = InternalRunnerRuntime(
    engine=DockerSandboxRunner(
        image=os.environ.get("SANDBOX_IMAGE", "ctf-sandbox-runner:local"),
        artifacts_root=Path(os.environ["ARTIFACTS_DIR"]),
        work_root=Path(os.environ["SANDBOX_WORK_ROOT"]),
        max_output_bytes=int(os.environ.get("SANDBOX_MAX_OUTPUT_BYTES", "1048576")),
        max_archive_bytes=int(os.environ.get("SANDBOX_MAX_ARCHIVE_BYTES", "268435456")),
        max_archive_files=int(os.environ.get("SANDBOX_MAX_ARCHIVE_FILES", "100")),
    ),
    token=os.environ["SANDBOX_RUNNER_TOKEN"],
)
app.include_router(runner_router)
