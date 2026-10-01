# One right way to keep a Tachyon fleet current

**Key points:** Manager owns update safety. The operator chooses when and how
fast. "Keep my fleet current" is the primary action. This is the target contract,
not a claim about the current engine. Isaac set this direction on 2026-09-30.

## Built-in behavior

**Key points:** Every update uses one engine. Safety gates have no off switch.

**Detail:** Discover devices from one device or a subnet. Match hardware identity,
then learn the AP/SM tree, switch ports, PoE links, and LLDP where supported.
Keep credentials private. Show target firmware and fleet health in one glance.
Use existing UI components. Remove options and screens that do not support this
task. Give a short, tested recovery step when a device fails.

Before each wave, save and show its risk statement: devices and firmware,
worst case, tested limits, rollback evidence, and untested models or firmware.
Run low-risk work on schedule only with proven rollback and passing gates.
Give the operator a Stop action before and during the wave. Stop prevents new
writes; it does not interrupt a flash. Ask for approval only for no rollback,
the first run of a new firmware family, a sole path to other devices, or a policy
exception. Approval covers that plan only. It cannot bypass a failed gate.

Use cumulative 10%, 50%, and 100% waves, one wave per maintenance window.
Before each write, verify a fresh, restorable backup and commit a fresh config
snapshot. Prove recovery with a pre-update reboot. Enforce Firmware Hold,
weather, clock, artifact, and maintenance-window gates. Use single-bank writes.
After each update, verify version, CPE re-association, RSSI, and customer traffic
recovery. Compare AP traffic with its pre-update baseline within a fixed deadline.
Missing or idle traffic needs a tested fallback; it never counts as proof.
Choose the traffic floor and deadline from bench evidence before enabling this gate.

Update children before parents. A management or traffic path waits for all
downstream members to pass, including switch chains and radio backhaul.
Unknown, stale, or conflicting topology blocks an automatic parent update.
Stop the entire window on the first failed safety gate or device check.
Do not resume in that window. A gate block before a write is a hold, not an
outage. Never start another device write until the prior device passes every
recovery check. This limits a window to one update-induced device failure.
"How fast" changes pacing within these limits, not concurrent exposure.
Manual proof uses the same gates and records an explicit Hold exception.

Send one immediate FAIL message with an audible escalation. Send one PASS
summary per window, one daily "Decided today" list with undo for reversible
decisions, and one weekly digest. Use fixed templates and named fields.
Undo never means reversing a flash without recovery checks. Routine success
does not interrupt. Save the risk statement, gate inputs and results, device
results, notifications, and operator actions. Failure delivery must survive restart.

Every engine or driver change needs real Tachyon bench proof on the PR head
before merge. Unit tests do not prove device behavior. Only Isaac names bench
devices. Only humans merge. This work never touches the Treehouse production fleet.

## Gaps, in priority order

**Key points:** Close outage risks before adding convenience. Keep each change small.

**Detail:** This ranking follows the current code and design docs.

| Rank | Gap and user impact | Code or plan to reuse |
|---|---|---|
| 1 | Failures can spread. Snapshot and smoke errors can pass; concurrent writes can fail together. Stop rules must cover every gate and the whole window. | `app.py`: `_update_single_device`, `_run_update_job`; [safety defaults](https://github.com/sixtyops/manager/issues/380); [halt](https://github.com/sixtyops/manager/issues/339) |
| 2 | Recovery is not proven. Backup freshness, rollback evidence, and AP traffic recovery need mandatory checks. Hardware CI can skip missing inputs. | `tachyon.py`; [hardware gate](https://github.com/sixtyops/manager/issues/330); [bench baseline](https://github.com/sixtyops/manager/issues/381) |
| 3 | A parent update can isolate unfinished devices. Existing order covers AP/SM roles and powered APs, not every management and traffic path. | `poller.py`; [topology batches](https://github.com/sixtyops/manager/issues/306) |
| 4 | Operators lack a saved risk decision and reliable attention rules. Completion notices and history do not provide the full contract above. | [completion events](https://github.com/sixtyops/manager/issues/302); [evidence log](https://github.com/sixtyops/manager/issues/387) |
| 5 | Setup and safety options cost operator time. UI docs permit safety overrides; rollout docs permit deferral and concurrent exposure that conflict with this target. | [setup guidance](https://github.com/sixtyops/manager/issues/127); `ui-principles.md`; `rollout-logic.md` |

Execution extends [the existing roadmap](https://github.com/sixtyops/manager/issues/316).
Keep identity, artifact integrity, bridge compatibility, and security work.
Park broad extraction and revenue work until the safe update path works.
Follow-up doc changes must align North Star, rollout, UI, and hardware guidance
with this contract. They must distinguish target behavior from shipped behavior.

The design applies measured promotion and automatic stops from
[Google SRE](https://sre.google/workbook/canarying-releases/) and
[Argo Rollouts](https://argo-rollouts.readthedocs.io/en/stable/features/analysis/).
Bench evidence must establish the recovery limits for physical radio devices.
