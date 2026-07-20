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


if __name__ == "__main__":
    unittest.main()
