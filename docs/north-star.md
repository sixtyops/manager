# SixtyOps Manager — North Star

> **Status:** v2, decided 2026-08-29. Supersedes v1 (firmware, config, device auth, and monitoring).
> **Rationale:** see [rearchitecture.md](rearchitecture.md) (decisions) and [rollout-logic.md](rollout-logic.md) (rollout rules). Executable plan: epic [#316](https://github.com/sixtyops/manager/issues/316).
> **Tactical detail:** see GitHub issues filtered by phase labels (`phase-1`, `phase-2`, `launch-p0`, `gtm`).

---

## Vision

**Make Tachyon firmware updates boring.** SixtyOps Manager exists so that a small WISP ops team can keep a large Tachyon fleet on current firmware with the discipline of a big one — automated, opinionated rollouts with safety gates that are always on — without paying enterprise prices or surrendering control to a SaaS.

## Mission

**Eliminate the manual, error-prone work of keeping a Tachyon fleet on current firmware.** Replace weekend firmware marathons with one self-hosted system that rolls new firmware across the fleet safely, stops on the first sign of trouble, and gets out of the operator's way.

## What we are

- A **self-hosted firmware automation system** for Tachyon networks. Tachyon only; support for other vendors is no longer committed.
- **Supporting features exist to make firmware updates safe**, not as product pillars: config backup/restore, single- and multi-device config push, signal health, switch-port topology, local users and roles, and Slack/email notifications.
- **Source-available under ELv2** — read it, run it, build on it; resale and managed-service use require a separate agreement.
- **Infrastructure-priced** — billed per AP and per switch under management. SMs (CPEs) are free.
- **Safety-first** — gradual rollouts, maintenance windows, environmental gates, auto-pause on failure.
- **Fleet-scoped** — firmware lifecycle plus the fleet visibility it needs. One install serves one operator's network. Subscriber billing, NOC alerting, device-admin authentication, config enforcement, and direct per-device administration have better-suited homes elsewhere.

## Operating Principles

1. **Safety over speed.** A rollout that pauses on a single failure is a feature, not a bug.
2. **Infrastructure-priced, not subscriber-priced.** Customer growth never punishes the operator's bill.
3. **Self-hosted by default, hosted maybe later.** Operators control their data, their network, their uptime.
4. **Source-available, no resale.** ELv2 — read the code, run the code, don't sell it as a service.
5. **The boring path must just work.** Install, update, push config, get a backup — none of these should require a Slack thread. One exception is accepted: the one-time bridge reinstall from the source-clone install to the pinned image (see [rearchitecture.md](rearchitecture.md), *Database compatibility*).
6. **Earn the next feature.** Don't add vendors, tenants, or pillars until Tachyon firmware automation is rock-solid.
7. **Fleet visibility, not device management.** The dashboard answers "is my fleet on target firmware and healthy?" not "what is device X doing?". Individual device metrics are allowed only where they serve fleet-level decisions — signal reliability, firmware conformity, config drift as a read-only report. Everything else belongs in the device's own interface.
8. **We own the safety defaults.** Operators configure *when* and *how fast*; we decide *how safe*. The defaults below are always on.

### Always-on safety defaults

The rebuilt engine enforces these. Operators cannot turn them off. The precise rules are in [rearchitecture.md](rearchitecture.md), *Rollout rules*.

- **Firmware Hold** — new firmware waits at least 6 days from its release date, per model family, unless one device in that family passes a clean smoke test first. If any family in scope is held, the first wave waits.
- **Weather Guard** — a wave does not start below the minimum temperature.
- **NTP drift block** — a wave does not start when the clock has drifted.
- **Pre-update config snapshot** — committed before upload; a failed snapshot defers the device.
- **Pre-update reboot** — proves the device recovers before it is flashed.
- **Smoke test** — version match, CPE re-association, and RSSI sanity after every update.
- **Halt on failure** — a failure after upload begins stops new devices and pauses the rollout.
- **One wave per maintenance window** — 10% → 50% → 100% of the frozen membership, no canary phase.
- **Switch/PoE ordering** — CPEs, then APs, then switches; a switch waits until every AP it powers has succeeded in an earlier wave.
- **Single-bank updates** — the vendor recommendation; there is no bank-mode setting.

---

## Target Customer

### Phase 1–2 (now → next 12 weeks)

**Mid-size WISP running Tachyon gear:**

- 100–1000 total devices (APs + switches + SMs combined)
- 10–50 tower sites
- 1–3 person ops team
- Self-hosting on a Linux VM (Debian-family)
- Today: managing firmware/configs by hand or shell scripts

### Phase 3 (months 3–9)

Same profile, larger fleets: 1000–5000 devices. Probably requires Postgres migration (SQLite cliff).

### Phase 4 (months 9–18+)

- **Large Tachyon fleets** (5000+ devices) and operators with several regions
- **MSPs / Managed Service Providers** running Tachyon networks on behalf of 5–50 customers (requires multi-tenancy build)
- **Hosted "we run it for you"** for ops teams that don't want to self-host

### Explicit non-targets

- Hyperscale ISPs (>50k devices) — wrong tool, wrong price
- Home networks / single APs — too much product for the use case
- Non-wireless (pure wired enterprise) — different market
- Networks without Tachyon gear — multi-vendor support is no longer committed

---

## Phases & Roadmap

### Phase 0 — Foundation *(today)*

**State:** repo public under ELv2; v1.3.0 shipped as source-available (billing/licensing stripped out — to be re-introduced in Phase 2). The rearchitecture back to the firmware core (epic [#316](https://github.com/sixtyops/manager/issues/316)) runs through Phase 0 and Phase 1. Docs hardening (quickstart, troubleshooting one-pager) has landed; remaining gaps tracked under the `phase-1` / `launch-p0` labels.

**Exit criteria:** clean understanding of gaps; immediate private-repo-migration fallout closed; team aligned on phases.

### Phase 1 — Lean Launch *(weeks 1–3)*

**Goal:** 3–5 design partners running SixtyOps on real networks, providing feedback.

**Themes:** QA gate green, docs operator-readable, install path works for non-engineers.

**Exit criteria:**

- Live-hardware CI lane hard-required on every PR merge
- A non-team-member can install the pinned image (`docker compose pull && up -d`) and onboard their first AP using only `docs/quickstart.md` in <15 minutes
- 3+ design partners on weekly feedback cadence
- Zero P0 bugs from partners in the last week of the phase [^p0]

[^p0]: **P0** = data loss, total outage, security incident, or auth lockout. Target: acknowledge within 4 business hours, fix within 24.

**Design-partner commitments:** Free to use during Phase 1. In exchange we ask for a weekly feedback sync, and prompt P0 reporting. When billing turns on in Phase 2, design partners get founder pricing honored for the life of the customer relationship.

### Phase 2 — First Revenue *(weeks 4–12)*

**Goal:** convert design partners + close net-new customers under per-AP+switch billing.

**Themes:** license-key system, pricing decided, comms to existing users, first paid invoices.

> **Context:** v1.3.0 (2026-04-08) removed all licensing/billing/Stripe code as part of the source-available conversion. Phase 2 re-introduces a thin license-key validation layer on top of that baseline — see issues [#129](https://github.com/sixtyops/manager/issues/129) (bill counter), [#130](https://github.com/sixtyops/manager/issues/130) (license-key validation), [#131](https://github.com/sixtyops/manager/issues/131) (pricing tiers), [#132](https://github.com/sixtyops/manager/issues/132) (trial UX), [#133](https://github.com/sixtyops/manager/issues/133) (rollout comms).

**Exit criteria:**

- License-key validation shipped and verified (offline-friendly, signed) — [#130](https://github.com/sixtyops/manager/issues/130)
- Pricing tiers locked from partner feedback — [#131](https://github.com/sixtyops/manager/issues/131)
- ≥10 paying customers OR MRR target (number set in [#131](https://github.com/sixtyops/manager/issues/131))
- Founder-pricing commitment honored to design partners

### Phase 3 — Harden & Observe *(months 3–9)*

**Goal:** operate at customer scale with confidence; no fire drills.

**Themes:** error reporting on the install itself, load testing, security review, deferred QA, Postgres feasibility. **Fleet visibility** means the operator sees, per rollout, which devices are on target firmware, held, deferred, or failed, and why — from the rollout, job history, and signal health pages. There are no analytics or report pages, and the product sends no usage data home.

**Exit criteria:**

- Operator-side error reporting live (logs or an operator-configured Sentry); <1% job error rate at fleet scale
- Load tested at 1000 devices/instance; bottlenecks documented
- External pen test passed
- Decision made on Postgres migration (do it / defer / never)

### Phase 4 — Expand *(months 9–18+)*

**Goal:** reach larger and managed Tachyon operators without breaking the core product.

**Themes:** multi-tenancy (MSP play), large-fleet scale, config normalization as a strategy on the shared rollout engine, optional hosted offering.

**Exit criteria:**

- Multi-tenancy live with ≥3 MSP customers
- One install runs 5000+ Tachyon devices within the job-error target
- Hosted offering decision: launched / scoped / killed

---

## Success Metrics

| Phase | North-star metric | Supporting metrics |
|-------|------------------|---------------------|
| 1 | Design partners installed and active [^active] | install→first-update time, weekly active operators, P0 bug count |
| 2 | Paying customers | MRR, free→paid conversion, license-key install errors |
| 3 | Operator confidence | job error rate, P0 incident count, mean time to detect, NPS from existing customers |
| 4 | Market reach | tenants per MSP customer, largest fleet per install, hosted vs self-host mix |

[^active]: **Active design partner** = attended a feedback sync in the last 7 days. There is no install heartbeat; the product sends nothing home.

---

## Deferred (Phase 3+ backlog)

| Item | Why deferred |
|------|--------------|
| Structured error reporting | Manual log-tailing fine for 3–5 customers; instrument once volume justifies |
| Load test poller at 50+ devices concurrent | Design partners unlikely to hit limits in Phase 1; instrument and learn |
| SFTP backup restore round-trip integration test | Manual validation suffices pre-launch |
| Code coverage threshold in CI | Hygiene, not launch-blocking |
| Chaos tests (network failures, stuck reboots) | Auto-pause handles most cases today |
| Multi-tenancy QA | Phase 4 — build the feature first |
| Postgres migration | Phase 3 decision — investigate at 1000-device customer |
| External pen test | Phase 3 — after we have paying customers |
| Public roadmap, customer logos, case studies | Phase 4 — need paying customers first |

---

## Working Doc References

- GitHub issues labelled [`phase-1`](https://github.com/sixtyops/manager/issues?q=is%3Aissue+label%3Aphase-1) — current execution scope
- GitHub issues labelled [`launch-p0`](https://github.com/sixtyops/manager/issues?q=is%3Aissue+label%3Alaunch-p0) — must-close before design partners
- GitHub issues labelled [`phase-2`](https://github.com/sixtyops/manager/issues?q=is%3Aissue+label%3Aphase-2) — monetization workstream
- GitHub issues labelled [`gtm`](https://github.com/sixtyops/manager/issues?q=is%3Aissue+label%3Agtm) — go-to-market scope
