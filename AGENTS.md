# AGENTS.md

SixtyOps Manager automates firmware updates for Tachyon devices.
Follow this file before work. `CLAUDE.md` is a symlink to it; keep one source.

## Agent rules

Follow this file. These rules match `AGENT_LOOP_REPO_RULES` in
`dev-host/config/sixtyops-manager-loop.env`. They bind loop workers.
Operational how-to documents do not grant approval to exceed these limits.

- Design Manager as a standalone Tachyon product. Use Manager's data and the
  device APIs it calls; never require an outside system.

- Branch from `main` and target `main`. Never push directly to `main`.
  Use one feature per branch and PR. Use a conventional-commit PR title.
- Add one `changelog.d/<issue-number>.<kind>.md` fragment for each user-facing
  change. Use `added`, `changed`, `fixed`, `removed`, or `security` as the kind.
  Keep commits focused and atomic. Use conventional commit messages.
- Run `pytest -v` before committing. All tests must pass. Exclude
  `tests/integration` as shown in Commands.
- Keep `main` stable. `scripts/install.sh` always pulls `main`.
- Scheduled firmware work uses the serial queue shipped in [#715](https://github.com/sixtyops/manager/pull/715). It runs CPEs, then APs, then switches, one device at a time. The first used windows for a new firmware family or artifact use `firmware_window_ramp_minutes` (30-120); later windows use the normal schedule. Cutoff stops new starts and lets active recovery finish. A failure stops the queue; after resume, check the failed device first. Scheduled updates default to one bank. Never bypass `updater/rollout_gate.py` or weaken `tests/test_rollout_invariants.py`.

### Host access

- Touch the authorized dev host and known lab devices only for owner-assigned
  work, under [docs/dev-hardware-validation.md](docs/dev-hardware-validation.md).
  Production installs stay with the operator.
- Secrets or credentials. Never print them, put them on a command line,
  store them in memory, or commit them.
- The live loop clone under `~/repos/`. Do branch work in a git worktree
  off fetched `origin/main`. Do not switch branches or move the live clone.

### Must not do

- Keep integration tests in `tests/integration` out of ordinary worker runs.
  A worker assigned by the owner to a named dev host run under
  [docs/dev-hardware-validation.md](docs/dev-hardware-validation.md#dev-host-run-procedure)
  may run only the integration tests named in that run brief, against the
  authorized dev host and dedicated lab devices.
- Create, move, or push a release tag, bump the release version, or run the
  Release workflow. Release tags must be GPG-signed by the release key.
- Only the epic owner merges, and only after every review, CI, sign-off, and bench
  gate passes. Workers do not merge. Do not cancel a CI run, bypass a
  denial, or weaken a review gate.
- Send direct Signal messages from owner or worker sessions.

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

## Release workflow

Use changelog fragments for user-facing changes. At release time, run
`python scripts/collect_changelog.py <version> <date>` to move all fragments
into the version section of `CHANGELOG.md` and remove the fragment files.
Run `python scripts/collect_changelog.py --check` to validate fragment names.

## Release notes

- Add one or two STE lines to `changelog.d/<issue-number>.<kind>.md` for each
  user-facing change. Do not edit `CHANGELOG.md` in a pull request.
- Group release fragments by kind when collecting them into the versioned
  `CHANGELOG.md` section.

The Release workflow uses `pytest -v --timeout=60`. There is no lint job in CI.
Live test commands are in the hardware SOP for the operator or an approved owner.
Run `./dev.sh` for local UI/API development without hardware. It creates the schema, runs `scripts/seed_dev_data.py`, then starts the real poller. Seeded devices use RFC 5737 documentation addresses; connection errors and backoff are expected. The seed script does not mark setup complete.

Create branch work from fetched `origin/main` in a Paseo workspace, without
checking out the live clone:

```bash
ws=$(paseo workspace create --isolation worktree \
  --path ~/repos/sixtyops-manager --new-branch <branch> \
  --base origin/main --json | jq -r .workspaceId)
paseo run --workspace "$ws"
```

Use an assigned worktree when the task provides one.

## Owner and worker roles

Read [the owner operations SOP](docs/owner-operations-sop.md) before owner work.
It owns the fresh-start procedure, authority history, delegation, review routing,
and sign-off gates. Update it with lasting lessons.

Each epic has one long-lived feature owner, as defined in the
[feature owner model](https://github.com/isolson/personal-processes/blob/master/docs/feature-owner.md).
The epic issue body is the owner's only memory. Workers complete one sub-issue each as followers.
Hold overlapping work if ownership is unclear. The owner delegates implementation,
investigation, testing, and detailed PR reviews. Obtain independent review;
the owner cannot be the designated reviewer for its team's work. Reuse active
agents for their own task and send review fixes to a new fix worker, as described
in [the owner operations SOP](docs/owner-operations-sop.md#priority-and-delegation).
Preserve locks, budgets, cooldowns, and round limits. If capacity is full, queue work.
Run delegated Paseo workers in `auto` permission mode with safety review on.
Use the file-overlap worker schedule in the
[owner operations SOP](docs/owner-operations-sop.md#priority-and-delegation).
Finish active fixes first.
Prioritize system functionality under North Star and one-right-way; defer website work.
Keep the epic body, task assignees, evidence, and the next action current.

Read fresh `gh` state before reporting or acting on a PR. After a denied or
failed command, read back state before retrying; the command may have changed it.
Do not resolve another reviewer's thread. Engine and driver changes need bench
proof on the exact head from the operator or an approved owner; a skip does not pass.
Start worker completion messages with `DONE from <id> (<title>):`.

### Owner merge authority

The epic owner merges a PR after every review, CI, sign-off, and bench gate passes
(operator decision, 2026-11-01; the repo lead held it until 2026-11-10).
Use an explicit subject and an empty body. A merge does not approve a release, a production install, or a fleet rollout. Read back each merge on `main`. Follow
[the owner operations SOP](docs/owner-operations-sop.md#review-merge-and-development-release-gates).
Workers never merge.

#### Slices and reviews

- Turn big goals into epics. List small slices in dependency order in the epic.
  Give each slice one issue and one PR. Never add `agent:ready` to an epic.
- Size each engine or driver slice before work starts. Split a slice before
  build if it touches more than one rollout core file: `updater/app.py` job
  runner, `updater/scheduler.py`, `updater/rollout_gate.py`, or
  `updater/database.py`. Also split a slice that plans more than about 300
  changed lines of engine code.
- For each engine slice, ask a Luna worker at high reasoning to write a one-page
  design note. The note lists every write boundary, every state, and the
  fail-closed rule for each. Require the design to use only Manager's data and
  the device APIs it calls. Send one narrow hard open question to the advisor
  (see Models) at medium reasoning. Ask the reviewer to check the code against the note.
- Require the first review to list every finding at once and mark each one
  `BLOCKING` or `NON-BLOCKING`. Treat wording, link, and test-name findings as
  `NON-BLOCKING`.
- Limit review to the slice. Ask the reviewer to judge the issue's Done-when
  items and defects the PR introduces. If a gap also exists on `main` and the PR
  does not make it worse, create a new slice issue in the epic and do not block
  the PR for that gap.
- Assign one reviewer to each PR. Keep that reviewer for every review round and
  ask them to check their earlier findings. Send fixes to a new fix worker.
- Hold the base during review. Merge `main` into a PR or rebuild it only to
  resolve a conflict.
- Treat bench proof as its own gate. The designated reviewer approves the code.
  The owner holds exact-head bench proof before the PR enters a merge batch.

## Models

Use GPT workers only. Luna is the default. Use Sol only after Luna fails at a
named step or for a hard design question named by a reviewer. For each
architecture decision, ask one advisor from the other provider for a design and
follow that design: a Claude owner asks Astra, a Codex owner asks Fable. Never
ask both. The owner asks the operator for major product and safety decisions and
brings a recommendation. The owner brings an architecture decision to the operator
only when the owner strongly disagrees with the advisor design. Follow the model
routing in [the owner operations SOP](docs/owner-operations-sop.md#priority-and-delegation).
Fable and Astra are never workers.
Verify installed provider, model, and reasoning IDs before launch.

Give an architect's design to a coding worker for implementation.

## Writing and style

Use Simplified Technical English (STE): result first, short sentences, active
voice, one idea per sentence, and one term per concept. Keep technical text
factual and neutral. In design docs, start each section with **Key points**,
then **Detail**. Keep UI labels, errors, and recovery instructions consistent.
State rules and instructions as actions to take.

Read `~/.claude/skills/ai-copywriter/SKILL.md` when available. Run its draft
and audit loop for every operator reply and new issue or PR text, review, sign-off
packet, commit message, documentation, UI text, code comment, and docstring.
Then check STE. Do not rewrite old comments without a task need. Marketing
defaults do not require extra questions when the task is clear. If the skill is
missing, report the fallback and apply these rules. Verify each skill's tools.
Do not claim to load a missing or unread skill.

Do not use the operator's personal name in replies, code, comments, docs, commit
messages, or PR text. Use "the operator" for the role. Functional account links
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

The fleet rollout uses one ordered serial queue of devices that
need the selected firmware. Run CPEs before APs, then switches, with one device
write and recovery sequence at a time. Keep Firmware Hold. Limit the first used
maintenance windows for each new firmware family or artifact with the configured
minute ramp; later windows use the normal schedule. Persist the cutoff across
restarts. Stop starting devices at cutoff and finish active recovery. Continue
remaining work next window. New and returning devices join the derived queue.
Persist the first failure and stop the whole queue until the operator resolves
it for a later window. Never reopen the failed window. Use single-bank scheduled
updates. Keep one update engine and shared admission checks. Test cutoff,
restart, Firmware Hold, recovery, and stop-on-first-failure in
`tests/test_rollout_invariants.py`.

The serial queue shipped in [#715](https://github.com/sixtyops/manager/pull/715).
Keep scheduled admission closed until required proof passes.
Manual per-device updates intentionally bypass Firmware Hold. See
[gradual-rollout.md](docs/gradual-rollout.md) for current behavior.

## Reference docs

- [Repository guide](docs/repository-guide.md): key files, strategy, local development.
- [North Star](docs/north-star.md), [target contract](docs/one-right-way.md),
  [#412](https://github.com/sixtyops/manager/pull/412), and delivery order
  [#413](https://github.com/sixtyops/manager/issues/413).
- [Release SOP](docs/release-sop.md): operator or explicitly approved owner only.
- [Hardware SOP](docs/dev-hardware-validation.md): deployment reality, integration
  test coverage and inputs; operator or explicitly approved owner only.
- [Rollout model](docs/rollout-logic.md), [rearchitecture](docs/rearchitecture.md),
  and [repository hardening](docs/repo-hardening.md).
