# AGENTS.md

## Communication style (required)

**Always respond in simplified technical English. Make your points up front,
then give detail.**

- Lead every reply, doc, PR description, and commit body with the conclusion:
  1–3 short bullets or sentences that say what was done, found, or decided.
- Then give the detail. Short sentences. One idea per sentence. Plain words.
- In design docs, every section starts with **Key points**, then **Detail**.
- Do not bury the answer under background or reasoning.

This repository's agent instructions live in **[CLAUDE.md](CLAUDE.md)** — the
single source of truth for project overview, branching/PR rules, release
workflow, key files, deployment reality, dev/test commands, and project rules.
Read it first. This file exists so non-Claude agents discover the same guidance;
keep all instructions in CLAUDE.md, not here.

Quick pointers:
- Run `pytest -v` before committing; all must pass. All PRs target `main`.
- Never push directly to `main`; one feature per branch/PR.
- Firmware rollouts advance **one wave per maintenance window** (10% → 50% →
  100%, no canary phase) and pause on failure — enforced by the fail-closed
  gate in `updater/rollout_gate.py` and guarded by
  `tests/test_rollout_invariants.py`. See [docs/gradual-rollout.md](docs/gradual-rollout.md)
  and the target design in [docs/rollout-logic.md](docs/rollout-logic.md).
