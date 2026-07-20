# Third-Party Notices

The source revisions in this file are controlled by `UPSTREAMS.lock.json`. A
change to an upstream revision must be made through an `upstream/*` pull
request and pass the upstream regression checks.

## Veria CTF Agent

- Source: https://github.com/verialabs/ctf-agent
- Pinned revision: https://github.com/verialabs/ctf-agent/commit/3366d569557c4fda3fd153040632de65e255396d
- Integration: fork base
- License: MIT; the original text is preserved in `LICENSE`.
- Copyright (c) 2026 Veria Labs, Inc.

The inherited `pull_challenges.py` identifies CTFd interaction and HTML
helpers as based on https://github.com/es3n1n/Eruditus. The exact product copy
is the file at the pinned Veria revision above; the Veria source comment is
preserved.

## PentestGPT

- Source: https://github.com/GreyDGL/PentestGPT
- Pinned revision: https://github.com/GreyDGL/PentestGPT/commit/e8b1bb77d1ac00329675cec3b060aba971ec1ac8
- Integration: Git submodule at `upstream/pentestgpt`
- License: MIT; the original text is preserved in `upstream/pentestgpt/LICENSE.md`.
- Copyright (c) 2023 Grey_D

## HexStrike AI

- Source: https://github.com/0x4m4/hexstrike-ai
- Pinned revision: https://github.com/0x4m4/hexstrike-ai/commit/9b8c780f324ce5145a322bfa23c98886f8424ba3
- Integration: Git submodule at `upstream/hexstrike-ai`
- License: MIT; the original text is preserved in `upstream/hexstrike-ai/LICENSE`.
- Copyright (c) 2026 Muhammad Osama (0x4m4) <contact@0x4m4.com>

## CI Action

The workflows use `actions/checkout` at immutable revision
`11bd71901bbe5b1630ceea73d27597364c9af683` (v4.2.2). It is used only in CI and
is not linked into or distributed with the product runtime.
