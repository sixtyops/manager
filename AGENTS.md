# AGENTS.md

This repository's agent instructions live in **[CLAUDE.md](CLAUDE.md)** — the
single source of truth for project overview, branching/PR rules, release
workflow, key files, deployment reality, dev/test commands, and project rules.
Read it first. This file exists so non-Claude agents discover the same guidance;
keep all instructions in CLAUDE.md, not here.

Quick pointers:
- Run `pytest -v` before committing; all must pass. All PRs target `main`.
- Never push directly to `main`; one feature per branch/PR.
- Phased rollouts (firmware/config/RADIUS) must advance **one phase per
  maintenance window** with the canary soaked from fleet-canary completion —
  enforced by the fail-closed gate in `updater/rollout_gate.py` and guarded by
  `tests/test_rollout_invariants.py`. See [docs/gradual-rollout.md](docs/gradual-rollout.md).
