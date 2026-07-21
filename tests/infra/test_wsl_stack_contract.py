from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
INFRA = ROOT / "infra" / "wsl"


def load_compose() -> dict:
    return yaml.safe_load((INFRA / "compose.yaml").read_text(encoding="utf-8"))


def test_expected_services_and_host_bindings() -> None:
    services = load_compose()["services"]
    assert set(services) == {
        "postgres",
        "redis",
        "api",
        "sandbox-runner",
        "worker",
        "web",
        "litellm",
    }
    assert services["web"]["ports"] == ["127.0.0.1:3000:3000"]
    assert services["api"]["ports"] == ["127.0.0.1:8080:8080"]
    for internal_service in ("postgres", "redis", "sandbox-runner", "worker", "litellm"):
        assert "ports" not in services[internal_service]
    assert services["api"]["build"] == {
        "context": "../..",
        "dockerfile": "infra/wsl/api/Dockerfile",
        "args": {
            "HTTP_PROXY": "${HTTP_PROXY:-}",
            "HTTPS_PROXY": "${HTTPS_PROXY:-}",
            "NO_PROXY": "${NO_PROXY:-localhost,127.0.0.1}",
        },
    }


def test_persistent_mounts_stay_on_wsl_ext4_paths() -> None:
    services = load_compose()["services"]
    volumes = [
        volume
        for service in services.values()
        for volume in service.get("volumes", [])
    ]
    assert "/srv/ctf-platform/data/postgres:/var/lib/postgresql/data" in volumes
    assert "/srv/ctf-platform/data/redis:/data" in volumes
    assert "/srv/ctf-platform/data/sandboxes:/srv/ctf-platform/data/sandboxes" in volumes
    assert all(not volume.startswith("/mnt/") for volume in volumes)


def test_api_storage_directories_are_writable_by_the_container_group() -> None:
    bootstrap = (INFRA / "scripts" / "bootstrap.sh").read_text(encoding="utf-8")
    deploy = (INFRA / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    application = (INFRA / "api" / "app" / "main.py").read_text(encoding="utf-8")
    for script in (bootstrap, deploy):
        assert "install -d -o ctf-platform -g 65532 -m 2770" in script
    assert "path.is_dir() and os.access(path, os.W_OK)" in application


def test_database_and_cache_use_internal_network() -> None:
    compose = load_compose()
    assert compose["networks"]["backend"]["internal"] is True
    assert compose["services"]["postgres"]["networks"] == ["backend"]
    assert compose["services"]["redis"]["networks"] == ["backend"]
    assert compose["services"]["sandbox-runner"]["networks"] == ["backend"]
    assert compose["services"]["worker"]["networks"] == ["backend"]


def test_secrets_are_runtime_variables() -> None:
    compose_text = (INFRA / "compose.yaml").read_text(encoding="utf-8")
    example_text = (INFRA / ".env.example").read_text(encoding="utf-8")
    assert "${POSTGRES_PASSWORD}" in compose_text
    assert "${REDIS_PASSWORD}" in compose_text
    assert "${LITELLM_MASTER_KEY}" in compose_text
    assert "${CTFD_TOKEN:-}" in compose_text
    assert "${CTFD_PASSWORD:-}" in compose_text
    assert "${SANDBOX_RUNNER_TOKEN}" in compose_text
    assert "REPLACE_WITH_RANDOM_HEX" in example_text
    assert "CTFD_TOKEN=" in example_text
    assert "CTFD_PASSWORD=" in example_text
    assert "SANDBOX_RUNNER_TOKEN=REPLACE_WITH_RANDOM_HEX" in example_text
    assert not (INFRA / ".env").exists()


def test_api_runs_migrations_before_startup() -> None:
    dockerfile = (INFRA / "api" / "Dockerfile").read_text(encoding="utf-8")
    health_check = (INFRA / "scripts" / "health-check.sh").read_text(encoding="utf-8")
    requirements = (INFRA / "api" / "requirements.txt").read_text(encoding="utf-8")
    assert "alembic upgrade head && exec uvicorn" in dockerfile
    assert "COPY backend/ingestion ./backend/ingestion" in dockerfile
    assert "COPY backend/sandbox_runner ./backend/sandbox_runner" in dockerfile
    assert 'schema_revision == "20260721_0003"' in health_check
    assert "python-multipart==0.0.32" in requirements
    assert "aiodocker==0.27.0" in requirements
    assert "websockets==16.1" in requirements
    assert compose_worker_command() == ["python", "-m", "backend.orchestration.worker"]


def test_deploy_waits_for_the_configured_service_count() -> None:
    deploy_script = (INFRA / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert "config --services | wc -l" in deploy_script
    assert "running} -eq ${expected_services}" in deploy_script


def test_sandbox_runner_is_internal_and_docker_socket_isolated() -> None:
    services = load_compose()["services"]
    api_volumes = services["api"]["volumes"]
    runner = services["sandbox-runner"]
    runner_dockerfile = (ROOT / "sandbox" / "Dockerfile.runner").read_text(encoding="utf-8")
    deploy_script = (INFRA / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    assert all("docker.sock" not in volume for volume in api_volumes)
    assert "/var/run/docker.sock:/var/run/docker.sock" in runner["volumes"]
    assert runner["user"] == "0:0"
    assert "ports" not in runner
    assert runner["environment"]["SANDBOX_IMAGE"] == "ctf-sandbox-runner:local"
    assert "@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7" in runner_dockerfile
    assert "USER 65532:65532" in runner_dockerfile
    assert "sandbox/Dockerfile.runner" in deploy_script


def compose_worker_command() -> list[str]:
    return load_compose()["services"]["worker"]["command"]


def test_model_runtime_is_reserved_only() -> None:
    compose = load_compose()
    assert "model-27b" not in compose["services"]
    config = (INFRA / "litellm" / "config.yaml").read_text(encoding="utf-8")
    readme = (INFRA / "README.md").read_text(encoding="utf-8")
    assert "LOCAL_MODEL_API_BASE" in config
    assert "8001" in readme


def test_build_proxy_is_not_baked_into_images() -> None:
    services = load_compose()["services"]
    for service_name in ("api", "web", "litellm"):
        args = services[service_name]["build"]["args"]
        assert set(args) == {"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"}
        assert args["HTTP_PROXY"] == "${HTTP_PROXY:-}"


def test_windows_start_stop_lifecycle_is_present() -> None:
    start_script = (INFRA / "host" / "Start-CtfPlatform.ps1").read_text(encoding="utf-8")
    stop_script = (INFRA / "host" / "Stop-CtfPlatform.ps1").read_text(encoding="utf-8")
    assert "sleep', 'infinity'" in start_script
    assert "health-check.sh --skip-gpu" in start_script
    assert "compose.yaml stop" in stop_script
