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
CLAUDE.md remains the canonical authority. Use the SOP entrypoint below to resume.

Quick pointers:
- Run `pytest -v` before committing; all must pass. All PRs target `main`.
- Never push directly to `main`; one feature per branch/PR.
- Write GitHub text, commit messages, and new code comments in plain words,
  bottom line first. See CLAUDE.md, *Writing and style* and *UI work and skills*,
  for shared skill instructions and the fallback when a skill is unavailable.
- Firmware rollouts advance **one wave per maintenance window** (10% → 50% →
  100%, no canary phase) and pause on failure — enforced by the fail-closed
  gate in `updater/rollout_gate.py` and guarded by
  `tests/test_rollout_invariants.py`. See [docs/gradual-rollout.md](docs/gradual-rollout.md)
  and the target design in [docs/rollout-logic.md](docs/rollout-logic.md).

## Lead session entrypoint

Read [lead-operations-sop.md](docs/lead-operations-sop.md) after CLAUDE.md.
It is the one durable lead procedure, with the fresh-start checklist and
bootstrap prompt. Update that SOP with durable handoff lessons; do not create a
parallel procedure. Discover live owners, queue, locks and budgets before
acting. Prioritize working system functionality under North Star and the
one-right-way contract; defer website work.
