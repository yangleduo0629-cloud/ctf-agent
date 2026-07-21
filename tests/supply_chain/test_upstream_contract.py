from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "upstream_guard", ROOT / "scripts" / "upstream_guard.py"
)
assert SPEC and SPEC.loader
upstream_guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upstream_guard)

POLICY_SPEC = importlib.util.spec_from_file_location(
    "check_pr_policy", ROOT / "scripts" / "check_pr_policy.py"
)
assert POLICY_SPEC and POLICY_SPEC.loader
check_pr_policy = importlib.util.module_from_spec(POLICY_SPEC)
POLICY_SPEC.loader.exec_module(check_pr_policy)


class UpstreamContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lock = json.loads((ROOT / "UPSTREAMS.lock.json").read_text(encoding="utf-8"))
        self.actions = json.loads(
            (ROOT / "third_party" / "ci-actions.json").read_text(encoding="utf-8")
        )

    def test_exact_upstream_inventory(self) -> None:
        self.assertEqual(
            {item["id"] for item in self.lock["components"]},
            {"veria-ctf-agent", "pentestgpt", "hexstrike-ai"},
        )
        for item in self.lock["components"]:
            self.assertRegex(item["commit"], r"^[0-9a-f]{40}$")

    def test_sbom_generation_is_deterministic(self) -> None:
        first = upstream_guard.build_bom(self.lock, self.actions)
        second = upstream_guard.build_bom(self.lock, self.actions)
        self.assertEqual(first, second)
        self.assertEqual(first["specVersion"], "1.6")

    def test_every_recorded_component_is_reachable_from_product(self) -> None:
        bom = upstream_guard.build_bom(self.lock, self.actions)
        refs = {component["bom-ref"] for component in bom["components"]}
        graph = {item["ref"]: set(item.get("dependsOn", [])) for item in bom["dependencies"]}
        pending = list(graph["urn:ctf-agent:product"])
        reachable: set[str] = set()
        while pending:
            item = pending.pop()
            if item in reachable:
                continue
            reachable.add(item)
            pending.extend(graph.get(item, ()))
        self.assertEqual(reachable, refs)

    def test_product_dependency_sbom_refresh_does_not_require_upstream_branch(self) -> None:
        self.assertEqual(
            check_pr_policy.protected_changes(
                {"pyproject.toml", "uv.lock", "sbom/upstreams.cdx.json"}
            ),
            [],
        )
        self.assertEqual(
            check_pr_policy.protected_changes(
                {"UPSTREAMS.lock.json", "sbom/upstreams.cdx.json"}
            ),
            ["UPSTREAMS.lock.json"],
        )

    def test_upstream_container_inputs_are_included_in_build_context(self) -> None:
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        required_includes = {
            "!pyproject.toml",
            "!uv.lock",
            "!README.md",
            "!third_party/locks/hexstrike-requirements.lock.txt",
            "!upstream/hexstrike-ai/hexstrike_mcp.py",
            "!upstream/hexstrike-ai/hexstrike_server.py",
            "!upstream/pentestgpt/README.md",
            "!upstream/pentestgpt/pyproject.toml",
            "!upstream/pentestgpt/uv.lock",
            "!upstream/pentestgpt/pentestgpt_legacy/**",
            "!upstream/pentestgpt/unified_agent/**",
        }
        self.assertTrue(required_includes.issubset(dockerignore))


if __name__ == "__main__":
    unittest.main()
