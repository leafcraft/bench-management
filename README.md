# bench-management — Bench Management

Resource & staffing operations for a professional-services firm, run as a
department of self-reliant agents. **YAML-first**: utilisation, bench and
skills data live in YOUR PSA / HRIS (Kantata/Mavenlink, Workday, SAP
SuccessFactors, BambooHR…) behind n8n connector agents — never in template
SQLite as a system of record.

**Runs day-0 with no connectors.** A dev store (`agency/_shared/store.py`)
ships a realistic ~14-person bench, skills inventory, open opportunities and
utilisation history, so the full forecast → match → allocate → talent flow
runs out of the box. The Python in this template is *floors and controls*,
not theater: workforce analytics computes bench % and the below-floor list;
the staffing coordinator's `@chain` re-derives double-booking + structural
skills-gaps and **forces** the routing flags; talent development's `@chain`
re-asserts the idle ledger and the senior-flight-risk escalation; and the
ESAT salted-hash ledger keeps respondent anonymity a *structural control*.
The LLM proposes — the floors decide where work routes.

## Story

It's Monday. **Priya Nair, a staffing partner**, gets a `new_project_kickoff`
for a new data-platform engagement. The kickoff lands on
`workforce_analytics_agent`, which calls the firm's PSA/HRIS through its n8n
connector and returns the live 30/60/90-day utilisation forecast, the current
bench percentage, and the skills inventory — straight from Workday, not a
local copy. Because `mode == 'forecast'`, the work flows forward to
`staffing_coordinator_agent`, which matches available consultants to the
engagement, scores fit, and builds an assignment plan (`plan-3f9c01ab2d`)
with in-prompt conflict detection.

One consultant, **Lina**, is double-booked across two opportunities, so the
coordinator sets `requires_engagement_lead_review` and the plan routes to the
**engagement lead** to resolve the conflict — a default-deny human gate
(unanswered in 4h → auto-escalates to the practice lead). Meanwhile the
forecast showed the firm's bench is at 17%, above the 15% ceiling, so the
*same* analytics result also fires the visible `bench_pct_current > 0.15` wire
to the **practice lead**. Separately, Lina has been idle two weeks; a
`person_rolling_off` event puts `talent_development_agent` to work drafting
her a six-month career path and reskilling bets, which land with **talent
ops**. Every terminal decision — the lead's conflict ruling, the practice
lead's bench call, the talent-ops sign-off — is appended to a hash-chained
WORM audit so the staffing decision trail is tamper-evident after the fact.

## Why it works in an organization

A services firm lives or dies on **utilisation**: every point of bench above
target is consultants paid to sit idle, and every unfilled engagement is
revenue walking out the door. This agency turns the weekly bench scramble —
spreadsheets, Slack threads, "who's free for the Acme work?" — into a forward
flow with the thresholds made *visible as wires* instead of buried in
someone's head: bench over the ceiling reaches the practice lead
automatically; below-floor consultants reach the staffing partner; idle staff
get a retention-grade career path instead of a generic training list. The
high-judgment calls (conflict resolution, retention escalation, performance
calibration, survey response to a hot theme) stay with named humans behind
default-deny gates, so the mesh accelerates the routine and *surfaces* the
hard decisions rather than making them silently. State stays in the systems
the firm already runs, so nothing here becomes a shadow system of record.

## Configuration (three tiers)

### Tier 1 — Boot (required to start)
| Var | Why |
|---|---|
| `LEAFMESH_LICENSE_KEY` | SDK license |
| `OPENAI_API_KEY` | `gpt-4o-mini` agents (coordinator, ESAT, manager) |
| `ANTHROPIC_API_KEY` | `claude-sonnet-4-6` agents (talent dev, performance review) |
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_PASSWORD` | session + state store (defaults to localhost:6379) |
| `LEAFMESH_ENV_TOKEN` | environment token (`development`) |

### Tier 2 — Host prerequisites + adapters (required to go live)
| Var | Why | Required? |
|---|---|---|
| `PSA_WEBHOOK_URL` / `PSA_WEBHOOK_TOKEN` | n8n workflow in front of your PSA/HRIS — serves the `forecast` / `ingest` / `report` / `upsert` / `search` modes for `workforce_analytics_agent` | Required for real data (empty → connector returns nothing) |
| `BENCH_REPORT_WEBHOOK_URL` / `BENCH_REPORT_WEBHOOK_TOKEN` | n8n workflow in front of your PSA/BI weekly bench report (`weekly_bench_sweep_agent`); enable its cron only after wiring | Required for the weekly sweep |
| `PSA_BOOKING_WEBHOOK_URL` / `PSA_BOOKING_WEBHOOK_TOKEN` | n8n workflow that WRITES the human-approved assignment to your PSA (`allocation_booker_agent`) — inert until set | Optional (dev records the booking in the store day-0; required to write to your real PSA) |
| `mongo_main` knowledge service | RAG grounding for the LLM roles (`staffing_playbook`, `skills_taxonomy`, `career_paths`, `review_rubric`, `esat_taxonomy`) | Optional — agents run without it, less grounded |

### Tier 3 — Connectors / escalation (optional)
| Var | Why | Required? |
|---|---|---|
| `PAGER_WEBHOOK` | PagerDuty Events API target for the manager's webhook escalation | Optional (defaults to the PD enqueue URL) |
| `SLACK_STAFFING_CHANNEL` | `channel_id` for the manager's Slack escalation target | Optional |
| `SLACK_BOT_TOKEN` / `SLACK_SIGNING_SECRET` | **Belong to a `channels: {slack: {...}}` block (ChannelConfig); this template has no such block, so they are NOT auto-consumed by the escalation `channel` target.** Add a channels block to post to Slack as a bot. | Optional |
| `ESAT_LEDGER_DB` | local SQLite path for the per-survey ESAT salt ledger | Optional (defaults to `./logs/esat_anonymisation.db`) |

```bash
cp env .env && pip install -r requirements.txt && python validate_config.py && python main.py
```

### Try it day-0 (no connectors)

With `main.py` running (API at `http://127.0.0.1:18820/docs`), kick off a
staffing run against the seeded dev bench:

```bash
curl -X POST http://127.0.0.1:18820/api/mesh/call \
  -H "Content-Type: application/json" \
  -d '{"entry_point":"new_project_kickoff",
       "input_data":{"mode":"forecast"}}'
```

`workforce_analytics_agent` serves the forecast from the dev store — bench at
~57% (over the 15% ceiling → the practice-lead wire fires) and three
consultants below the 65% billable floor (→ the staffing-partner wire) — then,
because `mode == 'forecast'`, hands off to `staffing_coordinator_agent`, which
pulls the bench + open opportunities (`@pre_compose`), matches, and routes on
the deterministic conflict / structural-gap floors. Wire `PSA_WEBHOOK_URL` to
swap the store for your real PSA/HRIS; the floors still apply.

Or exercise the modules directly without the SDK license:

```bash
python - <<'PY'
import asyncio
from agency._shared import store
from agency.workforce_analytics_agent import workforce_analytics_agent
fc = asyncio.run(workforce_analytics_agent(None, {"mode": "forecast"}, {}))
print("bench_pct_current:", fc["bench_pct_current"])
print("below_floor:", [c["consultant_id"] for c in fc["below_floor_consultants"]])
print("idle_4w:", [p["consultant_id"] for p in store.idle_ledger()["idle_4w"]])
PY
```

## Agents

| Agent | Role (job title) | Responsibilities | Can perform |
|---|---|---|---|
| `workforce_analytics_agent` | Workforce analyst | Capacity forecast (30/60/90d + bench %), weekly utilisation ledger, skills inventory — served from the dev store day-0 / your PSA/HRIS via n8n when wired (mode-dispatched). **Computes the bench % + below-floor list (the floor)** | programmatic n8n connector + shaper module → forecast continues to staffing; bench > 15% → practice lead; below-floor consultants → staffing partner |
| `staffing_coordinator_agent` | Staffing coordinator | Skills-match people to opportunities; build the assignment plan. **`@pre_compose` pulls the bench + opportunities; `@chain` re-derives double-booking + structural gaps and forces the routing flags** | llm (gpt-4o-mini, `skills_taxonomy` RAG) + module → engagement lead on conflict; structural skills gap → practice lead; clean → staffing partner |
| `talent_development_agent` | Talent development lead | Recommend reskilling bets, draft 6-month retention-grade career paths. **`@pre_compose` pulls the idle ledger; `@chain` re-asserts idle detection + senior-flight-risk routing** | llm (claude-sonnet-4-6, `career_paths` RAG) + module → practice lead on 4-week idle or senior flight risk; talent ops on idle/reskilling |
| `performance_review_agent` | Performance review writer | Draft Q/annual reviews from engagement feedback + utilisation + skill growth; composite score + rating bands | llm (claude-sonnet-4-6, `review_rubric` RAG) → practice lead for calibration tails; engagement lead for normal range |
| `esat_survey_agent` | Employee-experience analyst | Launch anonymised pulse surveys; analyse closed surveys — engagement index, themes, HRBP flags | llm (gpt-4o-mini, `esat_taxonomy` RAG) + Python salted-hash ledger → talent ops (HRBP review) when the floor or a hot theme trips |
| `weekly_bench_sweep_agent` | Bench-report runner | Trigger the Monday bench report in the PSA/BI tool, return the link + action items | programmatic n8n connector (cron commented until wired); terminal |
| `allocation_booker_agent` | Allocation booker | **The executor that finishes the job — after human approval, BOOKS the assignment in the PSA (person → project → dates → allocation%) and updates utilisation; returns the assignment id + summary.** Fail-closed: never books an un-approved or double-booking allocation (reuses the store's availability) | programmatic n8n connector (`PSA_BOOKING_WEBHOOK_URL`, inert until wired) + shaper/`@chain` floor module; dev store records it day-0 → audit (terminal) |
| `staffing_partner_human` | Staffing partner | Owns assignment decisions on clean plans | human gate (4h, default-deny) → practice lead on escalation, **else the booker books the assignment** |
| `engagement_lead_human` | Engagement lead | Resolve assignment conflicts / double-bookings | human gate (4h, default-deny) → practice lead on escalation, **else the booker books the assignment** |
| `talent_ops_human` | Talent ops / HRBP | Reskilling + training pathways; HRBP review of survey signals | human gate (6h, default-deny) → practice lead on escalation, else audit |
| `practice_lead_human` | Practice lead | Bench % authority, structural-gap calls, retention escalation, calibration | human gate (8h, default-deny halt) → audit |
| `bench_audit_logger_agent` | Compliance recorder | Append-only hash-chained WORM audit of every terminal decision | external (custom intelligence fn registered in main.py); terminal |

Wire `PSA_WEBHOOK_URL` / `BENCH_REPORT_WEBHOOK_URL` in `.env` to your n8n
workflows to go live against your PSA / HRIS. See `PRD.md` for the full
architecture, flow diagram, and per-agent spec.
