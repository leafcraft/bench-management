# Bench Management — PRD

## Story

It's Monday. **Priya Nair, a staffing partner**, gets a `new_project_kickoff`
for a new data-platform engagement. `workforce_analytics_agent` calls the
firm's PSA/HRIS through its n8n connector and returns the live 30/60/90-day
utilisation forecast, current bench %, and skills inventory — from Workday,
not a local copy. Because `mode == 'forecast'` the work flows to
`staffing_coordinator_agent`, which matches consultants to the engagement and
builds an assignment plan (`plan-3f9c01ab2d`) with in-prompt conflict
detection. **Lina** is double-booked, so `requires_engagement_lead_review`
fires and the plan routes to the **engagement lead** (default-deny gate; 4h →
practice lead). The same forecast showed bench at 17%, above the 15% ceiling,
so the visible `bench_pct_current > 0.15` wire also reaches the **practice
lead**. A `person_rolling_off` event has `talent_development_agent` draft
Lina a six-month career path that lands with **talent ops**. Every terminal
decision is appended to a hash-chained WORM audit.

## Why it works in an organization

Utilisation is the firm's P&L. This agency turns the weekly bench scramble
into a forward-flow DAG with the alert thresholds visible as `can_call` wires
instead of buried in code or someone's head, keeps the high-judgment calls
behind named, default-deny human gates, and keeps the system of record in the
PSA/HRIS the firm already runs. It accelerates the routine and *surfaces* the
hard decisions rather than making them silently.

## Flow

```
new_project_kickoff ─┐
weekly_bench_review ─┤
utilisation_ingest  ─┤   ┌─────────────────────────────┐
manual_action       ─┼──▶│ workforce_analytics_agent   │ (programmatic / n8n → PSA/HRIS)
skill_upsert/search ─┘   │  forecast | ingest | report  │
                         │  | upsert | search           │
                         └───┬───────────┬───────────┬──┘
       mode==forecast        │           │ bench>15% │ below_floor!=[]
                             ▼           ▼           ▼
                  ┌──────────────────┐  practice    staffing
                  │ staffing_        │   _lead       _partner
                  │ coordinator      │  (human)      (human)
                  │ (llm)            │
                  └──┬────┬──────┬───┘
   practice_lead_review  │ conflict/eng_review  │ clean
        ▼               ▼                       ▼
   practice_lead   engagement_lead        staffing_partner
     (human)          (human)                 (human)

person_rolling_off ─┐
career_path_request ─┼──▶ talent_development_agent (llm)
annual_review_path  ─┤        idle_4w or senior-flight-risk ─▶ practice_lead (human)
promotion_readiness ─┘        idle_2w or recommendations    ─▶ talent_ops    (human)

draft_performance_review ──▶ performance_review_agent (llm)
        requires_calibration ─▶ practice_lead ;  else ─▶ engagement_lead

launch_esat_pulse / analyse_esat_survey ──▶ esat_survey_agent (llm + salt ledger)
        requires_hrbp_review ─▶ talent_ops (human)

weekly_bench_review (direct) ──▶ weekly_bench_sweep_agent (programmatic / n8n; cron commented)

ALL human gates ──▶ bench_audit_logger_agent (external custom / hash-chained WORM)  [terminal]
```

## Actor inventory

| Actor | Job title | Type |
|---|---|---|
| `workforce_analytics_agent` | Workforce analyst | programmatic (n8n) |
| `staffing_coordinator_agent` | Staffing coordinator | llm |
| `talent_development_agent` | Talent development lead | llm |
| `performance_review_agent` | Performance review writer | llm |
| `esat_survey_agent` | Employee-experience analyst | llm (+ Python ledger) |
| `weekly_bench_sweep_agent` | Bench-report runner | programmatic (n8n) |
| `staffing_partner_human` | Staffing partner | human |
| `engagement_lead_human` | Engagement lead | human |
| `talent_ops_human` | Talent ops / HRBP | human |
| `practice_lead_human` | Practice lead | human |
| `bench_audit_logger_agent` | Compliance recorder | external (custom) |

## Data artifacts

- **AllocationPlan** (`data_structures`): `plan_id`, `cycle`, `received_at_utc`.
- **Assignment plan** (coordinator yields): `matches`, `conflicts`,
  `requires_engagement_lead_review`, `requires_practice_lead_review`, `plan_id`.
- **Career path** (talent dev yields): `skill_bets`, `internal_projects`,
  `certifications`, `retention_risk_score`, `employee_facing_narrative`,
  `confidence`, `requires_practice_lead_review`.
- **Performance review** (review writer yields): `composite_score`,
  `overall_rating`, `requires_calibration`, `requires_manager_signoff`.
- **ESAT result** (survey yields): `engagement_index`, `avg_scores`,
  `themes`, `response_rate`, `requires_hrbp_review`. Per-survey salt lives
  only in `ESAT_LEDGER_DB`, never in mesh state.
- **Audit entry** (WORM sink): `audit_event_id`, `written_at_utc`,
  `hash_chain_prev`, `hash_chain_current`.

## Per-agent spec

- **workforce_analytics_agent** — programmatic n8n connector with a
  co-located shaper (`agency/workforce_analytics_agent.py`, auto-binds as the
  `@sdk.intelligence` fn). DEV: serves the forecast / ledger / skills from
  `agency/_shared/store.py` so the chain runs day-0. PROD: the n8n workflow
  reads `mode`, queries the PSA/HRIS, and the shaper maps its response. The
  **bench % and the 65%-billable below-floor list (3+ weeks) are computed in
  the module** — the deterministic analytics floor — and re-asserted from the
  store even when a connector under-reports, so the `bench > 0.15` and
  `below_floor != []` wires can't be silenced. Routes: forecast→coordinator;
  bench>0.15→practice lead; below-floor→staffing partner.
- **staffing_coordinator_agent** — llm + `agency/staffing_coordinator_agent.py`.
  **`@pre_compose`** (`pull_supply_and_demand`) pulls the available bench, open
  opportunities and skills inventory so the LLM reasons over real supply/demand
  (lands in `context["prepared_data"]`). **`@chain`** (`enforce_allocation_floors`)
  re-derives double-booking conflicts from the matches and re-checks whether a
  single skill gap blocks 2+ open opportunities, then **forces**
  `requires_engagement_lead_review` / `requires_practice_lead_review` — the LLM
  can't waive the routing. Three mutually-exclusive routes gated on
  `requires_practice_lead_review` (structural gap) → engagement-lead (conflict)
  → staffing-partner (clean).
- **talent_development_agent** — llm + `agency/talent_development_agent.py`.
  **`@pre_compose`** (`pull_idle_ledger`) pulls the deterministic idle ledger
  (2w/4w lists + average) from the bench feed. **`@chain`**
  (`enforce_idle_and_retention_floors`) overwrites the idle lists from that
  ledger (the LLM can't invent or drop idle staff), clamps the scores, caps a
  weak career-path's confidence at 0.4, and **forces** the senior-flight-risk
  escalation when `retention_risk_score >= 0.7` AND level ∈ {senior, staff,
  partner}. Practice-lead route fires on `idle_4w` OR that flag; talent-ops on
  idle/reskilling.
- **performance_review_agent** — llm; deterministic composite-score formula +
  rating bands in the prompt. Calibration tails → practice lead; normal →
  engagement lead. Drafts only — the signed review of record lives in the HRIS.
- **esat_survey_agent** — llm + the salted-hash ledger
  (`agency/esat_survey_agent.py`). The ledger mints the per-survey salt with
  `secrets` and hashes responder ids; the salt is never yielded, logged, or
  passed to the LLM. `requires_hrbp_review` folds in the numeric floors AND a
  high-frequency hot theme → talent ops.
- **weekly_bench_sweep_agent** — n8n connector to the PSA/BI Monday report;
  `wake_up` cron commented out until the connector is wired.
- **human gates** — staffing partner (4h), engagement lead (4h), talent ops
  (6h), practice lead (8h). All `fallback_on_timeout: true`, default-deny;
  partner/lead/ops escalate to the practice lead on timeout, practice lead
  holds. `operator_ids` left unset → shared inbox.
- **bench_audit_logger_agent** — external `framework: custom`; the
  intelligence fn is registered in `main.py` via
  `audit_logger.register(leafmesh, "bench_audit_logger_agent")`. Terminal.

## Entry points

`new_project_kickoff`, `person_rolling_off`, `weekly_bench_review`,
`skills_assessment_update`, `manual_action`, `utilisation_ingest`,
`utilisation_report`, `skill_upsert`, `skills_search`, `career_path_request`,
`annual_review_path`, `promotion_readiness`, plus direct-call entries for the
coordinator, talent dev, weekly sweep, performance review, and the two ESAT
modes (18 total).

## Controls / trust

- **Default-deny human gates** on every high-judgment / irreversible step;
  timeout fallbacks route to a real edge (validated).
- **Visible alert wires + enforced floors** — the routing thresholds are
  `can_call` conditions (legible), and the *values* they fire on are computed
  in deterministic `@chain` / shaper floors (trustworthy): the bench %, the
  below-floor list, the idle ledger, double-booking, structural gaps and
  senior flight risk are code, not a prompt the LLM can soften. Every promised
  route is an enforced edge (no soft narration).
- **Structural anonymity** — the ESAT salt is generated and stored only in
  the local ledger; identities exist downstream only as `sha256(salt + id)`.
- **Hash-chained WORM audit** terminates every branch; insertion/deletion is
  detectable post-hoc.
- **Manager escalation** — three targets: human (practice lead), webhook
  (PagerDuty), channel (Slack). `auto_escalate` on retries/errors/timeout.

## Config pointer

Roles are in `configs/config.yaml`. Python is confined to floors + controls:
`agency/_shared/store.py` (dev bench/skills/opportunities/utilisation feeds),
`agency/workforce_analytics_agent.py` (analytics shaper + bench/below-floor
math), `agency/staffing_coordinator_agent.py` (`@pre_compose` + allocation
`@chain` floor), `agency/talent_development_agent.py` (`@pre_compose` +
idle/retention `@chain` floor), `agency/esat_survey_agent.py` (salt ledger),
and `agency/_shared/audit_logger.py` (WORM sink). Run `python
validate_config.py` before `python main.py`.

## Reflection / decisions

- **YAML-first, code where it must be**: the original capacity-forecaster /
  skills-matcher / idle-detector / reskilling-recommender modules were
  collapsed into three mode-dispatched LLM/connector roles with their
  thresholds as visible `can_call` wires. The *values* those wires fire on are
  computed in code (bench %, below-floor, idle, double-booking, structural
  gaps, senior flight risk) so neither the LLM nor a soft n8n workflow can
  waive routing — a `@pre_compose` to pull real context and a `@chain` to
  enforce the floor, per agent. The dev store makes the whole chain runnable
  day-0; the anonymity control stays Python.
- **No conductor hub**: work flows forward; originators step out; the only
  cycles are the bounded human-gate timeout escalations.
- **Unenforced narration removed**: the coordinator's structural-gap route,
  talent dev's senior-flight-risk route, and ESAT's hot-theme route are now
  enforced edges / flags rather than prompt-only promises.
- **State in the customer's systems**: utilisation/bench/skills live in the
  PSA/HRIS; the template ships no SQLite system of record (the salt ledger is
  a control store, not business state).
