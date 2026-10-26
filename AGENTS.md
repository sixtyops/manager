# AGENTS.md

SixtyOps Manager automates firmware updates for Tachyon devices.
Follow this file before work. `CLAUDE.md` is a symlink to it; keep one source.

## Agent rules

Follow this file. These rules match `AGENT_LOOP_REPO_RULES` in
`dev-host/config/sixtyops-manager-loop.env`. They bind loop workers.
Operational how-to documents do not grant approval to exceed these limits.

- Branch from `main` and target `main`. Never push directly to `main`.
  Use one feature per branch and PR. Use a conventional-commit PR title.
- Update `CHANGELOG.md` under `Unreleased` for a user-facing change.
  Keep commits focused and atomic. Use conventional commit messages.
- Run `pytest -v` before committing. All tests must pass. Exclude
  `tests/integration` as shown in Commands.
- Keep `main` stable. `scripts/install.sh` always pulls `main`.
- Fleet rollouts advance one wave per maintenance window: 10%, 50%, 100%.
  Halt the whole job if a device does not return online. Never bypass
  `updater/rollout_gate.py` or weaken `tests/test_rollout_invariants.py`.

### Must not touch

- Live hardware, a dev host, or a production install.
- Secrets or credentials. Never print them, put them on a command line,
  store them in memory, or commit them.
- The live loop clone under `~/repos/`. Do branch work in a git worktree
  off fetched `origin/main`. Do not switch branches or move the live clone.

### Must not do

- Run the integration tests in `tests/integration`, including read-only lanes.
- Create, move, or push a release tag, bump the release version, or run the
  Release workflow. Release tags must be GPG-signed by the release key.
- Merge a PR, cancel a CI run, bypass a denial, or weaken a review gate.
- Send direct Signal messages from lead or worker sessions.

### Stop and label agent:needs-human if

- The issue needs hardware, a release, or a production change.
- Required proof needs a live system or an action prohibited above.
  Record the missing proof. Continue independent safe work if available.

## Commands

Use Python 3.12 or newer. CI uses these commands in `.github/workflows/ci.yml`:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pytest -v
docker build -t firmware-updater:ci .
```

For an offline worker run, exclude the integration directory before `pytest -v`:

```bash
export PYTEST_ADDOPTS="--ignore=tests/integration"
pytest -v
```

The Release workflow uses `pytest -v --timeout=60`. There is no lint job in CI.
Live test commands are in the hardware SOP for the operator or an approved lead.
Run `./dev.sh` for local UI/API development without hardware.

Create branch work from fetched `origin/main`, without checking out the live clone:

```bash
git fetch origin
git worktree add -b <feature-branch> <new-worktree-path> origin/main
```

Use an assigned worktree when the task provides one.

## Lead and worker roles

Read [the lead operations SOP](docs/lead-operations-sop.md) before lead work.
It owns the fresh-start procedure, authority history, delegation, review routing,
and sign-off gates. Update it with lasting handoff lessons.

The owner assigns a lead. Other agents complete one bounded task as followers.
Hold overlapping work if ownership is unclear. The lead delegates implementation,
investigation, testing, and detailed PR reviews. Obtain independent review;
the lead cannot be the designated reviewer for its team's work. Reuse workers.
Preserve locks, budgets, cooldowns, and round limits. If capacity is full, queue work.
Run delegated Paseo workers in `auto` permission mode with safety review on.
Keep at most three active build or fix items. Finish active fixes first.
Prioritize system functionality under North Star and one-right-way; defer website work.
Keep durable lead status, task owners, evidence, and the next action current.

Read fresh `gh` state before reporting or acting on a PR. After a denied or
failed command, read back state before retrying; the command may have changed it.
Do not resolve another reviewer's thread. Engine and driver changes need bench
proof on the exact head from the operator or an approved lead; a skip does not pass.
Start worker completion messages with `DONE from <id> (<title>):`.

## Models

Only the Manager lead session may use Claude models. Workers must not use them.
Use installed and supported models. Verify provider, model, and reasoning IDs.
Use normal mode unless the owner records a current extension.
Large decisions require a four-agent quorum. The owner is the fifth vote.

| Task | Model | Reasoning |
|------|-------|-----------|
| Architecture, planning, and audits | GPT-6 Astra (`gpt-6-astra`) | Follow its recommendations |
| Safety builds | GPT-6.1 Sol (`gpt-6.1-sol`) | medium |
| Reviews; safety reviews | GPT-6.1 Sol (`gpt-6.1-sol`) | medium; high for safety |
| Easier code outside safety paths | GPT-6 Luna (`gpt-6-luna`) | high; xhigh for harder tasks |
| Other complex code | GPT-6.1 Sol (`gpt-6.1-sol`) | Match the task |

The specific safety and review rows take precedence over the general rows.
Give an architect's design to a coding worker for implementation.

## Writing and style

Use Simplified Technical English (STE): result first, short sentences, active
voice, one idea per sentence, and one term per concept. Keep technical text
factual and neutral. In design docs, start each section with **Key points**,
then **Detail**. Keep UI labels, errors, and recovery instructions consistent.

Read `~/.claude/skills/ai-copywriter/SKILL.md` when available. Run its draft
and audit loop for every owner reply and new issue or PR text, review, sign-off
packet, commit message, documentation, UI text, code comment, and docstring.
Then check STE. Do not rewrite old comments without a task need. Marketing
defaults do not require extra questions when the task is clear. If the skill is
missing, report the fallback and apply these rules. Verify each skill's tools.
Do not claim to load a missing or unread skill.

Do not use the owner's personal name in replies, code, comments, docs, commit
messages, or PR text. Use "the owner" for the role. Functional account links
and legal attribution may retain required identifiers.
Write each issue or PR reference as a Markdown link with `#NNN` as its text.

## UI work and skills

Before UI changes, read [UI_STYLE_GUIDE.md](UI_STYLE_GUIDE.md),
[UI principles](docs/ui-principles.md), and the existing flow. Reuse its tokens,
components, and operator terms. Keep "Keep my fleet current" as the primary task.
The design system and [one-right-way](docs/one-right-way.md) take precedence
over generic design or marketing defaults. Distinguish shipped behavior from targets.

Read `.claude/skills/frontend-design/SKILL.md` for UI work, including `static/`
and `updater/templates/`. Search named skills in repo `.claude/skills`, then
`~/.claude/skills` and `~/.codex/skills`. Read relevant instructions before use.
Honor explicitly named skills. Do not load every skill or install a skill,
plugin, or dependency just to satisfy this rule. No dedicated STE skill is bundled.
Use available accessibility or usability skills when useful. Check keyboard
access, visible focus, clear labels, color-independent status, responsive layout,
and reduced motion. Report missing skills or tools. Stop only the affected part;
use repo guidance for work that can proceed. Preserve all safety and proof gates.

## Rollout gotchas

New firmware waits for the Firmware Hold: release date plus
`firmware_canary_hold_days`. Confirmed working firmware clears the hold early
per model family. Hold clearance must not bypass the one-wave-per-window gate.
Manual per-device updates intentionally bypass the hold. Do not add a canary
phase or auto-advance outside the gate. Keep one rollout engine; never copy
its wave loop. See [gradual-rollout.md](docs/gradual-rollout.md).

## Reference docs

- [Repository guide](docs/repository-guide.md): key files, strategy, local development.
- [North Star](docs/north-star.md), [target contract](docs/one-right-way.md),
  [#412](https://github.com/sixtyops/manager/pull/412), and delivery order
  [#413](https://github.com/sixtyops/manager/issues/413).
- [Release SOP](docs/release-sop.md): operator or explicitly approved lead only.
- [Hardware SOP](docs/dev-hardware-validation.md): deployment reality, integration
  test coverage and inputs; operator or explicitly approved lead only.
- [Rollout model](docs/rollout-logic.md), [rearchitecture](docs/rearchitecture.md),
  and [repository hardening](docs/repo-hardening.md).
