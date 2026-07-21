from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
INFRA = ROOT / "infra" / "wsl"


def load_compose() -> dict:
    return yaml.safe_load((INFRA / "compose.yaml").read_text(encoding="utf-8"))


def test_expected_services_and_host_bindings() -> None:
    services = load_compose()["services"]
    assert set(services) == {"postgres", "redis", "api", "web", "litellm"}
    assert services["web"]["ports"] == ["127.0.0.1:3000:3000"]
    assert services["api"]["ports"] == ["127.0.0.1:8080:8080"]
    for internal_service in ("postgres", "redis", "litellm"):
        assert "ports" not in services[internal_service]


def test_persistent_mounts_stay_on_wsl_ext4_paths() -> None:
    services = load_compose()["services"]
    volumes = [
        volume
        for service in services.values()
        for volume in service.get("volumes", [])
    ]
    assert "/srv/ctf-platform/data/postgres:/var/lib/postgresql/data" in volumes
    assert "/srv/ctf-platform/data/redis:/data" in volumes
    assert all(not volume.startswith("/mnt/") for volume in volumes)


def test_database_and_cache_use_internal_network() -> None:
    compose = load_compose()
    assert compose["networks"]["backend"]["internal"] is True
    assert compose["services"]["postgres"]["networks"] == ["backend"]
    assert compose["services"]["redis"]["networks"] == ["backend"]


def test_secrets_are_runtime_variables() -> None:
    compose_text = (INFRA / "compose.yaml").read_text(encoding="utf-8")
    example_text = (INFRA / ".env.example").read_text(encoding="utf-8")
    assert "${POSTGRES_PASSWORD}" in compose_text
    assert "${REDIS_PASSWORD}" in compose_text
    assert "${LITELLM_MASTER_KEY}" in compose_text
    assert "REPLACE_WITH_RANDOM_HEX" in example_text
    assert not (INFRA / ".env").exists()


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
