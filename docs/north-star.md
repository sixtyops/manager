# SixtyOps Manager — North Star

> **Status:** v1.
> **Tactical detail:** see GitHub issues filtered by phase labels (`phase-1`, `phase-2`, `launch-p0`, `gtm`).

---

## Vision

**Be the system of record for wireless infrastructure operators.** SixtyOps Manager exists so that a small WISP ops team can run a large network with the discipline of a big one — automated firmware, configuration as code, gradual rollouts with safety gates, and complete visibility — without paying enterprise prices or surrendering control to a SaaS.

## Mission

**Eliminate the manual, error-prone work of keeping a fleet of wireless gear current, configured, and observable.** Replace weekend firmware marathons, hand-edited configs, and tribal-knowledge auth setups with one self-hosted system that does the boring parts safely and gets out of the operator's way.

## What we are

- A **self-hosted** firmware, config, RADIUS, and monitoring system for Tachyon networks (Mikrotik & Cambium on the roadmap).
- **Source-available** (not open-source — ELv2, no resale/managed-service competitors).
- **Infrastructure-priced** — billed per AP and per switch under management. SMs (CPEs) are free.
- **Safety-first** — gradual rollouts, maintenance windows, environmental gates, auto-pause on failure.

## What we are not

- Not a SaaS. Operators run it on their own VM, on their own network, behind their own firewall.
- Not multi-tenant (yet). One install = one operator. MSP/multi-customer use is a Phase 4 conversation.
- Not "another open-source project." Source is available but the business model is paid.
- Not a NOC dashboard or a billing/CRM platform — we manage the network, we don't bill the subscribers.

## Operating Principles

1. **Safety over speed.** A rollout that pauses on a single failure is a feature, not a bug.
2. **Infrastructure-priced, not subscriber-priced.** Customer growth never punishes the operator's bill.
3. **Self-hosted by default, hosted maybe later.** Operators control their data, their network, their uptime.
4. **Source-available, no resale.** ELv2 — read the code, run the code, don't sell it as a service.
5. **The boring path must just work.** Install, update, push config, get a backup — none of these should require a Slack thread.
6. **Earn the next feature.** Don't ship multi-vendor or multi-tenancy until WISP-Tachyon is rock-solid.

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

- **MSPs / Managed Service Providers** running networks on behalf of 5–50 customers (requires multi-tenancy build)
- **Mikrotik-primary networks** (driver exists, untested)
- **Cambium-primary networks** (driver exists, untested)
- **Hosted "we run it for you"** for ops teams that don't want to self-host

### Explicit non-targets

- Hyperscale ISPs (>50k devices) — wrong tool, wrong price
- Home networks / single APs — too much product for the use case
- Non-wireless (pure wired enterprise) — different market

---

## Phases & Roadmap

### Phase 0 — Foundation *(today)*

**State:** repo public under ELv2; feature-complete on engineering; v1.3.0 shipped as open-source (billing/licensing stripped out — to be re-introduced in Phase 2). Docs hardening (quickstart, troubleshooting one-pager) has landed; remaining gaps tracked under the `phase-1` / `launch-p0` labels.

**Exit criteria:** clean understanding of gaps; immediate private-repo-migration fallout closed; team aligned on phases.

### Phase 1 — Lean Launch *(weeks 1–3)*

**Goal:** 3–5 design partners running SixtyOps on real networks, providing feedback.

**Themes:** QA gate green, docs operator-readable, install path works for non-engineers.

**Exit criteria:**

- Live-hardware CI lane hard-required on every PR merge
- A non-team-member can install + onboard their first AP using only `docs/quickstart.md` in <15 minutes
- 3+ design partners on weekly feedback cadence
- Zero P0 bugs from partners in the last week of the phase [^p0]

[^p0]: **P0** = data loss, total outage, security incident, or auth lockout. Target: acknowledge within 4 business hours, fix within 24.

**Design-partner commitments:** Free to use during Phase 1. In exchange we ask for a weekly feedback sync, prompt P0 reporting, and permission to learn from their telemetry. When billing turns on in Phase 2, design partners get founder pricing honored for the life of the customer relationship.

### Phase 2 — First Revenue *(weeks 4–12)*

**Goal:** convert design partners + close net-new customers under per-AP+switch billing.

**Themes:** license-key system, pricing decided, comms to existing users, first paid invoices.

> **Context:** v1.3.0 (2026-04-08) removed all licensing/billing/Stripe code as part of the open-source conversion. Phase 2 re-introduces a thin license-key validation layer on top of that baseline — see issues [#129](https://github.com/sixtyops/manager/issues/129) (bill counter), [#130](https://github.com/sixtyops/manager/issues/130) (license-key validation), [#131](https://github.com/sixtyops/manager/issues/131) (pricing tiers), [#132](https://github.com/sixtyops/manager/issues/132) (trial UX), [#133](https://github.com/sixtyops/manager/issues/133) (rollout comms).

**Exit criteria:**

- License-key validation shipped and verified (offline-friendly, signed) — [#130](https://github.com/sixtyops/manager/issues/130)
- Pricing tiers locked from partner feedback — [#131](https://github.com/sixtyops/manager/issues/131)
- ≥10 paying customers OR MRR target (number set in [#131](https://github.com/sixtyops/manager/issues/131))
- Founder-pricing commitment honored to design partners

### Phase 3 — Harden & Observe *(months 3–9)*

**Goal:** operate at customer scale with confidence; no fire drills.

**Themes:** error reporting, structured observability, load testing, security review, deferred QA, Postgres feasibility.

**Exit criteria:**

- Sentry (or equivalent) live; alert rules set; <1% job error rate at fleet scale
- Load tested at 1000 devices/instance; bottlenecks documented
- External pen test passed
- Decision made on Postgres migration (do it / defer / never)

### Phase 4 — Expand *(months 9–18+)*

**Goal:** address adjacent markets without breaking the core product.

**Themes:** multi-tenancy (MSP play), Mikrotik/Cambium production-ready, optional hosted offering.

**Exit criteria:**

- Multi-tenancy live with ≥3 MSP customers
- Mikrotik OR Cambium has live customers (not just code)
- Hosted offering decision: launched / scoped / killed

---

## Success Metrics

| Phase | North-star metric | Supporting metrics |
|-------|------------------|---------------------|
| 1 | Design partners installed and active [^active] | install→first-update time, weekly active operators, P0 bug count |
| 2 | Paying customers | MRR, free→paid conversion, license-key install errors |
| 3 | Operator confidence | job error rate, P0 incident count, mean time to detect, NPS from existing customers |
| 4 | Market reach | tenants per MSP customer, non-Tachyon device share, hosted vs self-host mix |

[^active]: **Active design partner** = attended a feedback sync in the last 7 days AND the install reported a telemetry heartbeat in the last 7 days.

---

## Deferred (Phase 3+ backlog)

| Item | Why deferred |
|------|--------------|
| Sentry / structured error reporting | Manual log-tailing fine for 3–5 customers; instrument once volume justifies |
| Load test poller at 50+ devices concurrent | Design partners unlikely to hit limits in Phase 1; instrument and learn |
| SFTP backup restore round-trip integration test | Manual validation suffices pre-launch |
| Code coverage threshold in CI | Hygiene, not launch-blocking |
| Chaos tests (network failures, stuck reboots) | Auto-pause handles most cases today |
| Multi-tenancy QA | Phase 4 — build the feature first |
| Postgres migration | Phase 3 decision — investigate at 1000-device customer |
| External pen test | Phase 3 — after we have paying customers |
| Mikrotik/Cambium driver validation | Phase 4 — out of scope for Tachyon-first launch |
| Public roadmap, customer logos, case studies | Phase 4 — need paying customers first |

---

## Working Doc References

- GitHub issues labelled [`phase-1`](https://github.com/sixtyops/manager/issues?q=is%3Aissue+label%3Aphase-1) — current execution scope
- GitHub issues labelled [`launch-p0`](https://github.com/sixtyops/manager/issues?q=is%3Aissue+label%3Alaunch-p0) — must-close before design partners
- GitHub issues labelled [`phase-2`](https://github.com/sixtyops/manager/issues?q=is%3Aissue+label%3Aphase-2) — monetization workstream
- GitHub issues labelled [`gtm`](https://github.com/sixtyops/manager/issues?q=is%3Aissue+label%3Agtm) — go-to-market scope
