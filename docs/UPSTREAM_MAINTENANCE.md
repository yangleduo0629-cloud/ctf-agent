# Upstream Maintenance

## Source of truth

`UPSTREAMS.lock.json` is the authoritative component inventory. Git submodule
gitlinks, license files, `THIRD_PARTY_NOTICES.md`, `third_party/reuse.json`, and
`sbom/upstreams.cdx.json` must agree with it. Validate all records with:

```bash
git submodule update --init --recursive
python scripts/upstream_guard.py verify
python scripts/regression.py source
```

## Independent environments

Each upstream is built from a separate Dockerfile and receives only its own
runtime dependencies. Base images are fixed by manifest-list digest in
`third_party/container-images.json`:

```bash
python scripts/upstream_env.py smoke veria-ctf-agent
python scripts/upstream_env.py smoke pentestgpt
python scripts/upstream_env.py smoke hexstrike-ai
```

Run `python scripts/upstream_env.py build all` to build without starting the
services. HexStrike's smoke test starts its API in an isolated container and
checks `/health`; the other two execute their CLI help entry points.

## SBOM

Regenerate the deterministic CycloneDX 1.6 source-component SBOM with:

```bash
python scripts/upstream_guard.py generate-sbom
python scripts/upstream_guard.py verify
```

The SBOM also expands each resolved Python package from the dependency locks.
`uv.lock` fixes the Veria Python dependency graph. PentestGPT retains its own
`uv.lock` in its submodule. `third_party/locks/hexstrike-requirements.lock.txt`
is generated from the pinned HexStrike requirements with:

```bash
uv pip compile upstream/hexstrike-ai/requirements.txt \
  --python-version 3.12 --python-platform linux \
  -o third_party/locks/hexstrike-requirements.lock.txt
```

The adjacent `.meta.json` binds that generated lock to both the HexStrike
commit and the SHA-256 of the requirements Git blob. Hashing the blob keeps
the result stable across LF/CRLF checkouts. The provenance guard rejects a
stale resolved lock.

The fork pins `pydantic-ai-slim[bedrock,openai,google]==1.73.0`, the last
release available before the Veria baseline commit. Later 2.x releases remove
the `OpenAIModel` API used by that revision and fail the Veria CLI smoke test.

## Upgrade rule

1. Start from current `main` and create `upstream/<component>/<date>`.
2. Run `python scripts/prepare_upstream_update.py <component> <ref>`.
3. Review the upstream diff, changed license text, dependency locks, and SBOM.
4. Run `python scripts/regression.py source` and the component Docker smoke.
5. Open a pull request. All required policy and environment checks must pass.
   Merge commits are retained to preserve the upstream boundary.

The update script exits on `main` and `master`. The workflow
`.github/workflows/upstream-upgrade.yml` creates only `upstream/*` branches and
opens pull requests; it never pushes the product default branch.

Apply `.github/rulesets/main-protection.json` to the GitHub fork after its
credentials are configured. The ruleset blocks direct default-branch updates,
requires pull requests, resolves review threads, and requires all policy and
environment status checks. Approval count is zero so a single-owner fork can
merge after the automated gates pass.
