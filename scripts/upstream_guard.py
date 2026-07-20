#!/usr/bin/env python3
"""Validate immutable upstream pins and generate the source-component SBOM."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tomllib
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "UPSTREAMS.lock.json"
REUSE_PATH = ROOT / "third_party" / "reuse.json"
CI_ACTIONS_PATH = ROOT / "third_party" / "ci-actions.json"
CONTAINER_IMAGES_PATH = ROOT / "third_party" / "container-images.json"
NOTICE_PATH = ROOT / "THIRD_PARTY_NOTICES.md"
SBOM_PATH = ROOT / "sbom" / "upstreams.cdx.json"
HEXSTRIKE_LOCK = ROOT / "third_party" / "locks" / "hexstrike-requirements.lock.txt"
HEXSTRIKE_LOCK_META = ROOT / "third_party" / "locks" / "hexstrike-requirements.lock.meta.json"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PINNED_ACTION_RE = re.compile(r"^\s*uses:\s*([^\s@]+)@([^\s#]+)", re.MULTILINE)


class GuardError(RuntimeError):
    pass


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GuardError(f"cannot read {path.relative_to(ROOT)}: {exc}") from exc


def _git(*args: str, cwd: Path = ROOT, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise GuardError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def _normalized_repo(url: str) -> str:
    value = url.strip().replace("git@github.com:", "https://github.com/")
    return value.removesuffix("/").removesuffix(".git").lower()


def _repo_parts(repository: str) -> tuple[str, str]:
    match = re.search(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?$", repository)
    if not match:
        raise GuardError(f"unsupported repository URL: {repository}")
    return match.group(1), match.group(2)


def _component_ref(component: dict[str, Any]) -> str:
    owner, repo = _repo_parts(component["repository"])
    return f"pkg:github/{owner}/{repo}@{component['commit']}"


def _locked_python_components() -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    records: dict[str, dict[str, Any]] = {}
    owners: dict[str, set[str]] = {}

    def add(owner: str, name: str, version: str, source: str) -> None:
        normalized = re.sub(r"[-_.]+", "-", name).lower()
        bom_ref = f"pkg:pypi/{normalized}@{version}"
        owners.setdefault(owner, set()).add(bom_ref)
        record = records.setdefault(
            bom_ref,
            {
                "type": "library",
                "bom-ref": bom_ref,
                "name": normalized,
                "version": version,
                "purl": bom_ref,
                "_sources": set(),
            },
        )
        record["_sources"].add(source)

    uv_locks = (
        ("veria-ctf-agent", ROOT / "uv.lock", "uv.lock"),
        ("pentestgpt", ROOT / "upstream" / "pentestgpt" / "uv.lock", "upstream/pentestgpt/uv.lock"),
    )
    for owner, path, label in uv_locks:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        for package in data.get("package", []):
            if "registry" not in package.get("source", {}):
                continue
            if package.get("name") and package.get("version"):
                add(owner, package["name"], package["version"], label)

    requirement_re = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;]+)")
    for line in HEXSTRIKE_LOCK.read_text(encoding="utf-8").splitlines():
        match = requirement_re.match(line)
        if match:
            add(
                "hexstrike-ai",
                match.group(1),
                match.group(2),
                "third_party/locks/hexstrike-requirements.lock.txt",
            )

    components: list[dict[str, Any]] = []
    for bom_ref in sorted(records):
        record = records[bom_ref]
        sources = sorted(record.pop("_sources"))
        record["properties"] = [{"name": "ctf-agent:dependency-lock", "value": ",".join(sources)}]
        components.append(record)
    return components, {key: sorted(value) for key, value in owners.items()}


def build_bom(
    lock: dict[str, Any], ci_actions: dict[str, Any], container_images: dict[str, Any] | None = None
) -> dict[str, Any]:
    if container_images is None:
        container_images = _load_json(CONTAINER_IMAGES_PATH)
    components: list[dict[str, Any]] = []
    refs: list[str] = []
    source_refs: dict[str, str] = {}
    for item in lock["components"]:
        owner, repo = _repo_parts(item["repository"])
        bom_ref = _component_ref(item)
        refs.append(bom_ref)
        source_refs[item["id"]] = bom_ref
        components.append(
            {
                "type": item["type"],
                "bom-ref": bom_ref,
                "group": owner,
                "name": repo,
                "version": item["commit"],
                "purl": bom_ref,
                "licenses": [{"license": {"id": item["license"]["spdx"]}}],
                "copyright": item["license"]["copyright"],
                "externalReferences": [
                    {"type": "website", "url": item["homepage"]},
                    {"type": "vcs", "url": f"{item['repository']}#{item['commit']}"},
                ],
                "properties": [
                    {"name": "ctf-agent:git-commit", "value": item["commit"]},
                    {"name": "ctf-agent:integration", "value": item["integration"]},
                    {"name": "ctf-agent:local-path", "value": item["local_path"]},
                ],
            }
        )

    for image in container_images["components"]:
        bom_ref = f"{image['registry']}:{image['tag']}@{image['digest']}"
        refs.append(bom_ref)
        components.append(
            {
                "type": "container",
                "bom-ref": bom_ref,
                "name": image["name"],
                "version": image["tag"],
                "hashes": [{"alg": "SHA-256", "content": image["digest"].removeprefix("sha256:")}],
                "externalReferences": [
                    {"type": "distribution", "url": f"https://hub.docker.com/_/{image['name']}"}
                ],
                "properties": [
                    {"name": "ctf-agent:registry", "value": image["registry"]},
                    {"name": "ctf-agent:used-by", "value": ",".join(image["used_by"])},
                ],
            }
        )

    for action in ci_actions["components"]:
        owner, repo = _repo_parts(action["repository"])
        bom_ref = f"pkg:github/{owner}/{repo}@{action['commit']}"
        refs.append(bom_ref)
        components.append(
            {
                "type": "application",
                "bom-ref": bom_ref,
                "group": owner,
                "name": repo,
                "version": action["commit"],
                "purl": bom_ref,
                "scope": "excluded",
                "licenses": [{"license": {"id": action["license"]}}],
                "externalReferences": [
                    {"type": "vcs", "url": f"{action['repository']}.git#{action['commit']}"}
                ],
                "properties": [{"name": "ctf-agent:scope", "value": action["scope"]}],
            }
        )

    python_components, python_dependencies = _locked_python_components()
    components.extend(python_components)
    identity = json.dumps(
        {
            "lock": lock,
            "ci_actions": ci_actions,
            "container_images": container_images,
            "components": components,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    serial = uuid.uuid5(
        uuid.NAMESPACE_URL, f"ctf-agent-upstreams:{hashlib.sha256(identity.encode()).hexdigest()}"
    )
    product_ref = "urn:ctf-agent:product"
    return {
        "$schema": "https://cyclonedx.org/schema/bom-1.6.schema.json",
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": f"urn:uuid:{serial}",
        "version": 1,
        "metadata": {
            "timestamp": lock["generated_at"],
            "tools": {
                "components": [{"type": "application", "name": "upstream_guard.py", "version": "1"}]
            },
            "component": {
                "type": "application",
                "bom-ref": product_ref,
                "name": lock["product"]["name"],
            },
            "properties": [{"name": "ctf-agent:lockfile", "value": "UPSTREAMS.lock.json"}],
        },
        "components": components,
        "dependencies": [
            {"ref": product_ref, "dependsOn": refs},
            *[
                {"ref": source_refs[component_id], "dependsOn": dependencies}
                for component_id, dependencies in sorted(python_dependencies.items())
            ],
        ],
    }


def _render_bom(lock: dict[str, Any], ci_actions: dict[str, Any]) -> str:
    images = _load_json(CONTAINER_IMAGES_PATH)
    return json.dumps(build_bom(lock, ci_actions, images), indent=2, ensure_ascii=True) + "\n"


def generate_sbom() -> None:
    lock = _load_json(LOCK_PATH)
    ci_actions = _load_json(CI_ACTIONS_PATH)
    _load_json(CONTAINER_IMAGES_PATH)
    SBOM_PATH.parent.mkdir(parents=True, exist_ok=True)
    SBOM_PATH.write_text(_render_bom(lock, ci_actions), encoding="utf-8", newline="\n")
    print(f"wrote {SBOM_PATH.relative_to(ROOT)}")


def _submodule_urls() -> dict[str, str]:
    output = _git("config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$")
    result: dict[str, str] = {}
    for line in output.splitlines():
        key, path = line.split(maxsplit=1)
        url_key = key.removesuffix(".path") + ".url"
        result[path] = _git("config", "-f", ".gitmodules", "--get", url_key)
    return result


def _validate_component(component: dict[str, Any], submodules: dict[str, str], notice: str) -> None:
    required = {
        "id",
        "name",
        "type",
        "integration",
        "repository",
        "homepage",
        "default_branch",
        "commit",
        "local_path",
        "license",
        "environment",
    }
    missing = required - component.keys()
    if missing:
        raise GuardError(f"{component.get('id', '<unknown>')} missing fields: {sorted(missing)}")
    if not SHA_RE.fullmatch(component["commit"]):
        raise GuardError(f"{component['id']} does not use a full 40-character commit SHA")

    license_data = component["license"]
    license_path = ROOT / license_data["path"]
    if not license_path.is_file():
        raise GuardError(f"{component['id']} license file is missing: {license_data['path']}")
    license_text = license_path.read_text(encoding="utf-8", errors="replace")
    if license_data["copyright"] not in license_text:
        raise GuardError(f"{component['id']} copyright statement differs from its license file")
    if component["commit"] not in notice or license_data["copyright"] not in notice:
        raise GuardError(f"{component['id']} is not fully represented in THIRD_PARTY_NOTICES.md")
    if not (ROOT / component["environment"]["definition"]).is_file():
        raise GuardError(f"{component['id']} environment definition is missing")

    if component["integration"] == "fork-base":
        remote = _git("remote", "get-url", "upstream")
        if _normalized_repo(remote) != _normalized_repo(component["repository"]):
            raise GuardError(f"upstream remote mismatch: {remote}")
        _git("cat-file", "-e", f"{component['commit']}^{{commit}}")
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", component["commit"], "HEAD"], cwd=ROOT
        )
        if result.returncode:
            raise GuardError(f"HEAD is not descended from {component['id']} pin")
        return

    if component["integration"] != "git-submodule":
        raise GuardError(f"unsupported integration mode for {component['id']}")
    local_path = component["local_path"]
    if local_path not in submodules:
        raise GuardError(f"{component['id']} missing from .gitmodules")
    if _normalized_repo(submodules[local_path]) != _normalized_repo(component["repository"]):
        raise GuardError(f"{component['id']} submodule URL mismatch")
    checkout = ROOT / local_path
    if _git("rev-parse", "HEAD", cwd=checkout) != component["commit"]:
        raise GuardError(f"{component['id']} checkout differs from lock")
    index_line = _git("ls-files", "--stage", "--", local_path)
    parts = index_line.split()
    if len(parts) < 2 or parts[0] != "160000" or parts[1] != component["commit"]:
        raise GuardError(f"{component['id']} gitlink differs from lock")


def verify() -> None:
    lock = _load_json(LOCK_PATH)
    reuse = _load_json(REUSE_PATH)
    ci_actions = _load_json(CI_ACTIONS_PATH)
    container_images = _load_json(CONTAINER_IMAGES_PATH)
    if lock.get("schema_version") != 1 or reuse.get("schema_version") != 1:
        raise GuardError("unsupported lock or reuse schema")
    components = lock.get("components", [])
    expected = {"veria-ctf-agent", "pentestgpt", "hexstrike-ai"}
    ids = {item.get("id") for item in components}
    if ids != expected:
        raise GuardError(
            f"component inventory differs: expected {sorted(expected)}, got {sorted(ids)}"
        )

    notice = NOTICE_PATH.read_text(encoding="utf-8")
    submodules = _submodule_urls()
    for component in components:
        _validate_component(component, submodules, notice)

    by_id = {item["id"]: item for item in components}
    if container_images.get("schema_version") != 1:
        raise GuardError("unsupported container image schema")
    for image in container_images.get("components", []):
        digest = image.get("digest", "")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise GuardError(f"container image {image.get('name')} does not use a SHA-256 digest")
        unknown = set(image.get("used_by", [])) - by_id.keys()
        if unknown:
            raise GuardError(f"container image has unknown consumers: {sorted(unknown)}")
        expected_from = f"FROM {image['name']}:{image['tag']}@{digest}"
        for component_id in image["used_by"]:
            dockerfile = ROOT / by_id[component_id]["environment"]["definition"]
            if expected_from not in dockerfile.read_text(encoding="utf-8"):
                raise GuardError(f"{component_id} Dockerfile differs from container image lock")
    for entry in reuse.get("entries", []):
        source_id = entry.get("source_component")
        if source_id not in by_id:
            raise GuardError(f"reuse entry has unknown source: {source_id}")
        if entry.get("source_commit") != by_id[source_id]["commit"]:
            raise GuardError(f"reuse entry pin differs for {entry.get('destination')}")
        if entry.get("license") != by_id[source_id]["license"]["spdx"]:
            raise GuardError(f"reuse entry license differs for {entry.get('destination')}")

    if not (ROOT / "uv.lock").is_file():
        raise GuardError("Veria uv.lock is missing")
    if not (ROOT / "upstream" / "pentestgpt" / "uv.lock").is_file():
        raise GuardError("PentestGPT uv.lock is missing")
    if not HEXSTRIKE_LOCK.is_file():
        raise GuardError("HexStrike resolved requirements lock is missing")
    hexstrike_meta = _load_json(HEXSTRIKE_LOCK_META)
    hexstrike = by_id["hexstrike-ai"]
    if hexstrike_meta.get("source_commit") != hexstrike["commit"]:
        raise GuardError("HexStrike dependency lock was generated for a different source commit")
    input_path = ROOT / hexstrike_meta.get("input_path", "")
    if not input_path.is_file():
        raise GuardError("HexStrike dependency-lock input is missing")
    input_digest = hashlib.sha256(input_path.read_bytes()).hexdigest()
    if input_digest != hexstrike_meta.get("input_sha256"):
        raise GuardError("HexStrike requirements changed without regenerating its dependency lock")

    for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
        text = workflow.read_text(encoding="utf-8")
        for action, revision in PINNED_ACTION_RE.findall(text):
            if not SHA_RE.fullmatch(revision):
                raise GuardError(f"{workflow.name} uses floating action {action}@{revision}")

    expected_bom = _render_bom(lock, ci_actions)
    try:
        actual_bom = SBOM_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise GuardError("SBOM is missing; run generate-sbom") from exc
    if actual_bom != expected_bom:
        raise GuardError("SBOM differs from lock; run generate-sbom")

    print(f"verified {len(components)} immutable upstreams, reuse registry, licenses, and SBOM")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("verify", "generate-sbom"))
    args = parser.parse_args()
    try:
        if args.command == "generate-sbom":
            generate_sbom()
        else:
            verify()
    except GuardError as exc:
        print(f"upstream guard: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
