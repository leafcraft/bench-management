# bench-management — Bench-Management Pod

Resource & staffing operations for a professional-services firm, run as a **pod**:
a coordinator that owns each staffing case, a team of specialists that read the
bench and plan the allocation, a **finisher** that books the approved assignment
and produces the real artifact, named humans behind default-deny gates, and the
Manager as supervisor. **YAML-first**: utilisation, bench and skills data live in
YOUR PSA / HRIS (Kantata/Mavenlink, Workday, SAP SuccessFactors, BambooHR…)
behind n8n / MCP connectors — never in template SQLite as a system of record.

**Runs day-0 with no connectors.** A dev store (`agency/_shared/store.py`) ships a
realistic ~14-person bench, skills inventory, open opportunities and utilisation
history, so the full **forecast → screen → plan → approve → book** flow runs out
of the box. The Python here is *floors and controls*, not theater: workforce
analytics computes bench % and the below-floor list; candidate screening claims
disjoint batches atomically so parallel copies never double-screen; the staffing
coordinator's `@chain` re-derives double-booking + structural gaps and **forces**
the routing flags; the finisher re-checks the human approval, refuses to
double-book, and **deletes its artifact on a blocked floor**; talent development
re-asserts the idle ledger; and the ESAT salted-hash ledger keeps respondent
anonymity a *structural control*. The LLM proposes — the floors decide.

## Story

It's Monday. **Priya Nair, a staffing partner**, fires a `new_project_kickoff`
for a new data-platform engagement. The **bench coordinator** opens the case and
hands it to `workforce_analytics_agent`, which serves the live 30/60/90-day
utilisation forecast, the current bench percentage, and the skills inventory —
from the firm's PSA/HRIS via its n8n connector day-1, from the seed store day-0.
Because `mode == 'forecast'`, the case flows to `candidate_screening_agent`, which
**fans out into parallel copies**: each atomically claims a disjoint batch of
benched consultants (`claim_screening_batch`) and scores them against the open
opportunities, so a large bench is screened in parallel with no double-work. The
combined shortlist flows to `staffing_coordinator_agent` — a **super-agent** that
plans the allocation and builds the assignment plan (`plan-3f9c01ab2d`).

One consultant, **Lina**, is double-booked across two opportunities, so the
coordinator's `@chain` re-derives the conflict and routes the plan to the
**engagement lead** — a default-deny gate (unanswered → the Manager escalates to
the practice lead). On approval, `allocation_booker_agent`, the **finisher**,
re-checks that a human actually approved (an unknown verdict fails closed),
refuses any slice that would push a consultant past 100%, **renders the booking
confirmation to `./out`**, and records the assignment. Meanwhile the forecast
showed the bench above the 15% ceiling, so the *same* analytics result fired the
visible `bench_pct_current > 0.15` wire to the **practice lead**; and a
`person_rolling_off` event put `talent_development_agent` to work on Lina's
six-month career path. Every terminal decision is appended to a hash-chained WORM
audit — the staffing decision trail is tamper-evident after the fact.

## Why it works in an organization

A services firm lives or dies on **utilisation**: every point of bench above
target is consultants paid to sit idle, every unfilled engagement is revenue
walking out the door. This pod turns the weekly bench scramble — spreadsheets,
Slack threads, "who's free for the Acme work?" — into a forward flow with the
thresholds made *visible as wires* instead of buried in someone's head: bench over
the ceiling reaches the practice lead automatically; below-floor consultants reach
the staffing partner; idle staff get a retention-grade career path instead of a
generic training list. The **one-human / one-agent / one-system per stage**
discipline holds throughout: the agent produces, the human decides, the system
records — no stage advances on an agent's own authority, and **silence never
advances a decision** (every gate default-denies to `held_for_review` and lands in
the audit sink). The finisher is the load-bearing control: reaching it is not
approval, and a partial booking is deleted rather than half-issued. State stays in
the systems the firm already runs, so nothing here becomes a shadow system of
record.

## Prerequisites (three tiers)

### Tier 1 — Boot (required to start)
| Var | Why |
|---|---|
| `LEAFMESH_LICENSE_KEY` | SDK license (get one at leafcraft.ai) |
| `OPENAI_API_KEY` | `gpt-4o-mini` agents (coordinator, screening, staffing, ESAT, manager) |
| `ANTHROPIC_API_KEY` | `claude-sonnet-4-6` agents (talent development, performance review) |
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_PASSWORD` | session + state store (defaults to localhost:6379) |
| `LEAFMESH_ENV_TOKEN` | environment token (`development`) |

Install is **core-only** (all LLM providers included) and runs the pod day-0
against the seed store:

```bash
cp env .env && pip install -r requirements.txt && python validate_config.py && python main.py
```

`requirements.txt` pins `leafmesh>=2.4.131` + `apscheduler>=3.10` (for the weekly
`wake_up` cron). Do **not** install `leafmesh[all]` on Python 3.12+ — it pulls
`cchardet`, which has no wheel there; add only the specific extras you wire.

### Tier 2 — Host prerequisites + adapters (required to go live)
| Var | Why | Required? |
|---|---|---|
| `PSA_WEBHOOK_URL` / `PSA_WEBHOOK_TOKEN` | n8n workflow in front of your PSA/HRIS — serves `forecast` / `ingest` / `report` / `upsert` / `search` for `workforce_analytics_agent` | Required for real data (empty → dev store) |
| `PSA_MCP_URL` / `PSA_MCP_TOKEN` | MCP server exposing bench / opportunities / skills as tools for `candidate_screening_agent` (uncomment its `mcp:` block after setting the URL — an empty-URL http mcp is rejected at load) | Optional (dev store serves screening day-0) |
| `PSA_BOOKING_WEBHOOK_URL` / `PSA_BOOKING_WEBHOOK_TOKEN` | n8n workflow that WRITES the human-approved assignment to your PSA (`allocation_booker_agent`) — inert until set | Optional (dev records the booking + renders `./out` day-0) |
| `BENCH_REPORT_WEBHOOK_URL` / `BENCH_REPORT_WEBHOOK_TOKEN` | n8n workflow in front of your PSA/BI weekly bench report (`weekly_bench_sweep_agent`); enable its cron only after wiring | Optional |

### Tier 3 — Connectors / escalation (optional)
| Var | Why | Required? |
|---|---|---|
| `PAGER_WEBHOOK` | PagerDuty (or other) target for the Manager's webhook escalation — inert while empty | Optional |
| `HITL_OUTBOUND_URL` | where human-gate notifications are POSTed (if you switch the humans to `human_interface: webhook`) | Optional |
| `ESAT_LEDGER_DB` | local SQLite path for the per-survey ESAT salt ledger | Optional (defaults to `./logs/esat_anonymisation.db`) |

### Try it day-0 (no connectors)

With `main.py` running (API at `http://127.0.0.1:18820/docs`), open a staffing
case against the seeded dev bench:

```bash
curl -X POST http://127.0.0.1:18820/api/mesh/request \
  -H "Content-Type: application/json" \
  -d '{"entry_point":"new_project_kickoff","data":{"cycle":"2026-Q3"}}'
```

The coordinator opens the case → analytics serves the forecast (bench over the
15% ceiling → the practice-lead wire fires; three consultants below the 65%
billable floor → the staffing-partner wire) → screening fans out over the bench →
the coordinator plans and routes on the deterministic conflict / structural-gap
floors. Wire `PSA_WEBHOOK_URL` to swap the store for your real PSA/HRIS; the
floors still apply.

## Agents

| Agent | Role (job title) | Responsibilities | Can perform |
|---|---|---|---|
| `bench_coordinator_agent` | Bench coordinator | Owns each staffing case end to end; opens it, orchestrates, keeps status, single point of contact. Never reads the PSA, matches, or books | llm (pure YAML) → analytics on `open_case`; talent dev on `talent`; practice lead on `escalate` |
| `workforce_analytics_agent` | Workforce analyst | Capacity forecast (30/60/90d + bench %), utilisation ledger, skills inventory — mode-dispatched from the dev store day-0 / your PSA/HRIS via n8n. **Computes the bench % + below-floor list (the floor)** | programmatic n8n + shaper; weekly `wake_up` cron (timezone-pinned) → screening on `forecast`; bench > 15% → practice lead; below-floor → staffing partner |
| `candidate_screening_agent` | Candidate-screening analyst | Fan out over the bench: each **parallel instance** atomically claims a disjoint batch (`claim_screening_batch`) and shortlists candidates against open opportunities | llm ×2 instances (`instances_handoff: last`) + `@pre_compose`/`@chain` module → staffing coordinator on a non-empty shortlist |
| `staffing_coordinator_agent` | Staffing coordinator | Turn the shortlist into an assignment plan. **`@pre_compose` pulls supply/demand; `@chain` re-derives double-booking + structural gaps and forces the routing flags** | llm **super-agent** (dict-tuned) + module → engagement lead on conflict; structural gap → practice lead; clean → staffing partner |
| `talent_development_agent` | Talent development lead | Recommend reskilling bets, draft 6-month retention-grade career paths. **`@pre_compose` pulls the idle ledger; `@chain` re-asserts idle detection + senior-flight-risk routing** | llm (claude-sonnet-4-6) + playbook + module → practice lead on 4-week idle / senior flight risk; talent ops on idle/reskilling |
| `performance_review_agent` | Performance-review writer | Draft Q/annual reviews from feedback + utilisation + skill growth; composite score + rating bands | llm (claude-sonnet-4-6) → practice lead for calibration tails; engagement lead for normal range |
| `esat_survey_agent` | Employee-experience analyst | Launch anonymised pulse surveys; analyse closed ones — engagement index, themes, HRBP flags | llm (gpt-4o-mini) + Python salted-hash ledger → talent ops when the floor or a hot theme trips |
| `weekly_bench_sweep_agent` | Bench-report runner | Trigger the Monday bench report in your PSA/BI tool, return the link + action items | programmatic n8n connector (cron commented until wired); terminal |
| `allocation_booker_agent` | Allocation booker (**finisher**) | The executor after the gate: re-check the human approval (fail closed on unknown), refuse to double-book, **render the booking artifact to `./out`, delete it on a blocked floor**, record the assignment | programmatic n8n + `@chain` two-layer floor; dev store books + renders day-0 → audit (terminal) |
| `staffing_partner_human` | Staffing partner | Owns assignment decisions on clean plans | human gate (4h, default-deny) → booker on `approved`; else recorded to audit |
| `engagement_lead_human` | Engagement lead | Resolve assignment conflicts / double-bookings | human gate (4h, default-deny) → booker on `approved`; else recorded to audit |
| `talent_ops_human` | Talent ops / HRBP | Reskilling + training pathways; HRBP review of survey signals | human gate (6h, default-deny) → audit |
| `practice_lead_human` | Practice lead | Bench % authority, structural-gap calls, retention escalation, calibration | human gate (8h, default-deny) → audit |
| `bench_audit_logger_agent` | Compliance recorder | Append-only hash-chained WORM audit of every terminal decision | external (custom intelligence fn registered in main.py); terminal |

The **Manager** is the supervisor — it watches every hand-off and escalates
stalls, contract violations, and timed-out gates to the practice lead
(`manager.escalation`); it is not a router in the middle of the team. See `PRD.md`
for the full architecture, flow diagram, and per-agent spec.
