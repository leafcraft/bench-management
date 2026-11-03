# Bench-Management Pod — PRD

## Story

It's Monday. At 08:00 `weekly_bench_sweep_agent` writes the week's bench
report; at 09:00 the review runs. **Anika Desai, a staffing partner**, fires
`new_project_kickoff` for Brightwater Health's data platform phase 2. The
**bench coordinator** opens the case and hands it to
`workforce_analytics_agent`, which serves the 30/60/90-day utilisation forecast,
current bench %, and skills inventory — from the seed store day-0, from the
PSA/HRIS via n8n once wired. Because `mode == 'forecast'` the case flows
to `candidate_screening_agent`, whose **parallel copies** each atomically
claim a disjoint batch of the bench and shortlist candidates against the open
opportunities. The combined shortlist reaches `staffing_coordinator_agent` — a
**super-agent** that plans the allocation (`plan-3f9c01ab2d`). **Lina** is
double-booked, so the `@chain` re-derives the conflict and routes to the
**engagement lead** (4h; if nobody answers, the card closes and the ticket
records it — nothing is booked and nobody is chased). On approval,
`allocation_booker_agent` — the **finisher** — re-checks the approval (unknown
verdict fails closed), refuses to double-book or to book the same plan twice,
renders the booking confirmation to `./out`, and records it. The same forecast
showed bench over the 15% ceiling → the visible `bench_pct_current > 0.15` wire
reached the **practice lead**; a `person_rolling_off` event had
`talent_development_agent` draft Lina a six-month career path to **talent ops**.
Every terminal decision is appended to a hash-chained WORM audit.

## Why it works in an organization

Utilisation is the firm's P&L. This **pod** turns the weekly bench scramble into
a forward flow with the alert thresholds visible as `can_call` wires instead of
buried in code or someone's head, keeps the high-judgment calls behind named
human gates that never approve on silence, and keeps the system of record in the PSA/HRIS the firm
already runs. One human, one agent, one system per stage: the agent produces, the
human decides, the system records — and silence never advances a decision.

## Flow

```
new_project_kickoff ─┐
case_status         ─┼──▶ bench_coordinator_agent (llm, coordinator)
manual_action       ─┘        open_case ─▶ analytics ; talent ─▶ talent_dev ; escalate ─▶ practice_lead

weekly_bench_report ──▶ weekly_bench_sweep_agent (programmatic; Mon 08:00 wake_up)
                          bench report → ./out ──▶ bench_audit_logger_agent

weekly_bench_review ─┐   ┌─────────────────────────────┐
utilisation_ingest  ─┤   │ workforce_analytics_agent   │ (programmatic; dev store / n8n → PSA/HRIS)
utilisation_report  ─┼──▶│  forecast|ingest|report|     │  + weekly wake_up cron (timezone-pinned)
skill_upsert/search ─┘   │  upsert|search               │
                         └───┬───────────┬───────────┬──┘
       mode==forecast        │           │ bench>15% │ below_floor!=[]
                             ▼           ▼           ▼
              ┌────────────────────┐  practice    staffing
              │ candidate_         │   _lead       _partner
              │ screening (llm ×2  │  (human)      (human)
              │ copies + claim)    │
              └────────┬───────────┘
              shortlist != []
                       ▼
              ┌────────────────────┐
              │ staffing_          │ (llm super-agent)
              │ coordinator        │  @pre_compose supply/demand · @chain routing floor
              └──┬────┬──────┬─────┘
   practice_lead_review  │ conflict/eng_review  │ clean
        ▼               ▼                       ▼
   practice_lead   engagement_lead        staffing_partner
     (human)          (human)                 (human)
                       │ approved                │ approved
                       └───────────┬────────────┘
                                   ▼
                        allocation_booker_agent (FINISHER)
                          re-check approval · no double-book · render ./out · delete-on-block
                                   ▼
                        bench_audit_logger_agent  [terminal WORM]

person_rolling_off ─┐
career_path_request ─┼──▶ talent_development_agent (llm + playbook)
promotion_readiness ─┘        idle_4w or senior-flight-risk ─▶ practice_lead (human)
                              idle_2w or recommendations    ─▶ talent_ops    (human)

draft_performance_review ──▶ performance_review_agent (llm)
        requires_calibration ─▶ practice_lead ; else ─▶ engagement_lead

launch_esat_pulse / analyse_esat_survey ──▶ esat_survey_agent (llm + salt ledger)
        requires_hrbp_review ─▶ talent_ops (human)

Every human reply other than "approved" ──▶ bench_audit_logger_agent [terminal]
Silence routes nothing: the card closes and the ticket records it. No human→human wire.
```

## Actor inventory

| Actor | Job title | Type |
|---|---|---|
| `bench_coordinator_agent` | Bench coordinator | llm (pure YAML) |
| `workforce_analytics_agent` | Workforce analyst | programmatic Python (n8n when wired) |
| `candidate_screening_agent` | Candidate-screening analyst | llm (×2 copies, `scaling.copies.count: 2`) |
| `staffing_coordinator_agent` | Staffing coordinator | llm (super-agent) |
| `talent_development_agent` | Talent development lead | llm |
| `performance_review_agent` | Performance-review writer | llm |
| `esat_survey_agent` | Employee-experience analyst | llm (+ Python salt ledger) |
| `weekly_bench_sweep_agent` | Bench-report runner | programmatic Python (n8n when wired) |
| `allocation_booker_agent` | Allocation booker (finisher) | programmatic + `@chain` floor |
| `staffing_partner_human` | Staffing partner | human |
| `engagement_lead_human` | Engagement lead | human |
| `talent_ops_human` | Talent ops / HRBP | human |
| `practice_lead_human` | Practice lead | human |
| `bench_audit_logger_agent` | Compliance recorder | external (custom) |

## Modern capabilities (fitted to the domain)

- **super_agent** (DICT form) on `staffing_coordinator_agent` — autonomous
  plan/execute/verify across many people × opportunities:
  `super_agent: {cost_ceiling: 100000, wall_clock: 900}`. Never flat
  `super_agent_*` keys.
- **scaling.copies** on `candidate_screening_agent` — `scaling.copies.count: 2` +
  `scaling.copies.handoff: last` fan the bench out to parallel copies; the atomic
  `claim_screening_batch` tool (backed by the store's per-session queue) hands
  each copy a **disjoint** batch, so a large bench is screened in parallel with
  no double-work. Yields are list/sum-safe (`shortlist`, `unmatched`,
  `screened_count` = SUM of disjoint batches) so `last` merges cleanly; the
  `@chain` dedups and re-derives `screened_count`, and screening emits **no**
  escalation boolean (a last-wins boolean could drop a `true`).
- **timezone** — the live weekly `wake_up: "0 9 * * 1"` on
  `workforce_analytics_agent` carries `timezone: "Asia/Kolkata"` (a bare cron
  fires in UTC). `weekly_bench_sweep_agent` runs at `0 8 * * 1` in the same
  timezone, an hour before the review.
- **playbook** — per-intent craft on the staffing coordinator (`allocation`) and
  talent dev (`talent`): only `description` rides in the prompt; `responsibility`
  loads on demand so the prompt stays identity + output contract.
- **mcp** — a commented `mcp:` block on `candidate_screening_agent` shows the
  real PSA/HRIS shape (it picks the vault connection `psa_mcp` — a URL or token
  inline is refused at load); day-0 runs
  on the dev-store tools in `agency/tools.py`.

## Data artifacts

- **AllocationPlan** (`data_structures`): `plan_id`, `cycle`, `received_at_utc`.
- **Shortlist** (screening yields): `shortlist`, `claimed_consultants`,
  `screened_count`, `unmatched`, `screening_briefing`.
- **Assignment plan** (coordinator yields): `matches`, `conflicts`,
  `requires_engagement_lead_review`, `requires_practice_lead_review`, `plan_id`,
  `assignments` — stashed per session so the finisher recovers it across the gate.
- **Booking confirmation** (finisher yields + `./out/{plan_id}_booking_confirmation.md`):
  `assignment_ids`, `booked`, `rejected`, `booking_ok`, `artifact_path`,
  `status` (booked|blocked), `block_reasons`.
- **Career path** (talent dev yields): `skill_bets`, `internal_projects`,
  `certifications`, `retention_risk_score`, `employee_facing_narrative`,
  `confidence`, `requires_practice_lead_review`.
- **Performance review** / **ESAT result** — unchanged (composite score + rating;
  engagement index + themes; per-survey salt only in `ESAT_LEDGER_DB`).
- **Audit entry** (WORM sink): `audit_event_id`, `written_at_utc`,
  `hash_chain_prev`, `hash_chain_current`.

## Per-agent spec

- **bench_coordinator_agent** — pure-YAML llm coordinator. Owns the case; routes
  `open_case`→analytics, `talent`→talent dev, `escalate`→practice lead. Never
  reads the PSA, matches, or books.
- **workforce_analytics_agent** — programmatic Python
  (`agency/workforce_analytics_agent.py`). DEV serves the forecast/ledger/skills
  from the store; wired, it shapes the n8n reply's content, and a failed call is
  a failed run (never dev-store numbers). Its `integration` lines ship
  commented: an unwired n8n integration fails every call and is quarantined
  after five. The **bench % and 65%-billable
  below-floor list (3+ weeks) are computed in the module** and re-asserted from
  the store, so `bench > 0.15` / `below_floor != []` can't be silenced. Weekly
  timezone-pinned `wake_up` (mode=forecast). Routes: forecast→screening;
  bench>0.15→practice lead; below-floor→staffing partner.
- **candidate_screening_agent** — llm ×2 copies +
  `agency/candidate_screening_agent.py`. `@pre_compose` (`prime_screening`) sets
  the session (identity from code) so the atomic claim keys the backlog, seeds
  the backlog once, and hands `open_opportunities` + `backlog_size` to the model;
  `@chain` (`normalise_screening`) dedups the merged shortlist and re-derives
  `screened_count`. Route: non-empty shortlist → staffing coordinator.
- **staffing_coordinator_agent** — llm super-agent +
  `agency/staffing_coordinator_agent.py`. `@pre_compose` pulls supply/demand;
  `@chain` (`enforce_allocation_floors`) re-derives double-booking + structural
  gaps, **forces** the routing flags, and **stashes the plan** (session_stash)
  for the finisher. Three mutually-exclusive routes: structural gap→practice
  lead, conflict→engagement lead, clean→staffing partner. `allocation` playbook.
- **talent_development_agent** — llm + `agency/talent_development_agent.py`.
  `@pre_compose` pulls the idle ledger; `@chain` overwrites the idle lists (the
  LLM can't invent/drop idle staff), clamps scores, caps a weak path's confidence
  at 0.4, and forces the senior-flight-risk escalation. `talent` playbook.
- **performance_review_agent** — llm; composite-score formula + rating bands
  resident in the prompt (needed every turn). Calibration tails→practice lead;
  normal→engagement lead. Drafts only.
- **esat_survey_agent** — llm + salted-hash ledger
  (`agency/esat_survey_agent.py`); the salt is never yielded, logged, or passed
  to the LLM. `requires_hrbp_review`→talent ops.
- **weekly_bench_sweep_agent** — programmatic Python
  (`agency/weekly_bench_sweep_agent.py`). Monday 08:00 (timezone-pinned) and the
  `weekly_bench_report` entry point: builds the bench report (bench % vs
  ceiling, below-floor, idle 4w, open demand, one action item each) to
  `./out/bench_report_<ISO week>.md` — idempotent per week — and records it in
  the audit log. Wired, the PSA/BI workflow builds it instead; an optional
  `notify` posts the finished report to a webhook.
- **allocation_booker_agent** (FINISHER) — programmatic +
  `@chain(enforce_booking_floors)`. Two-layer defence: the body short-circuits
  unless a human approved (allow-token allowlist + block-token denylist, unknown
  fails closed) and recovers the plan from the session stash; the `@chain` floor
  re-checks the approval independently, renders `./out/{plan_id}_booking_
  confirmation.md`, and **deletes it on a blocked floor** (`status=blocked` +
  `block_reasons`). The store's `book_assignment` refuses any slice past 100%
  (double-booking floor), and a plan already booked is never booked again. DEV
  books in the store (a real record, never a faked ack); wired, the PSA
  workflow does the write (its own allocation rules apply) and a failed call
  books nothing. → audit terminus.
- **human gates** — staffing partner (4h), engagement lead (4h), talent ops
  (6h), practice lead (8h). If nobody answers in time, the card closes and the
  ticket records that no reply came; nothing is booked, nobody is chased. All
  keep `fallback_on_timeout: true` + `fallback_response: held_for_review`
  (routed to the audit sink) as the declared intent — on leafmesh 2.4.486 a
  `dual` seat's timeout routes nothing. The two approval seats carry a
  `human_prompt_template` naming the reply words; only `approved` books.
  `dual` only on these boundary agents; they call chain/programmatic agents,
  never another `dual` agent. `operator_ids` unset → shared inbox. No
  human→human wire.
- **bench_audit_logger_agent** — external `framework: custom`; registered in
  `main.py` via `audit_logger.register(leafmesh, "bench_audit_logger_agent")`.
  Terminal.

## Entry points (18)

`new_project_kickoff`, `case_status`, `manual_action`, `weekly_bench_review`,
`weekly_bench_report`,
`person_rolling_off`, `utilisation_ingest`, `utilisation_report`, `skill_upsert`,
`skills_search`, `career_path_request`, `promotion_readiness`,
`draft_performance_review`, `launch_esat_pulse`, `analyse_esat_survey`, plus
direct-call doors for candidate screening and the staffing coordinator, and a
`staffing_decision` HITL-resume door. Every entry is `condition: always`.

## Controls / trust

- **Coordinator + finisher + supervisor** — a coordinator owns each case; the
  finisher is the only actuator (behind the gate, fails closed, deletes on
  block, idempotent per plan); the Manager supervises errors.
- **Human gates** on every high-judgment / irreversible step; nothing is
  booked on silence.
- **Visible alert wires + enforced floors** — routing thresholds are `can_call`
  conditions (legible); the values they fire on (bench %, below-floor, idle
  ledger, double-booking, structural gaps, senior flight risk) are computed in
  deterministic `@chain` / shaper floors (trustworthy). No soft narration route.
- **Atomic claim for fan-out** — parallel screening copies never double-work.
- **Structural anonymity** — the ESAT salt lives only in the local ledger;
  identities exist downstream only as `sha256(salt + id)`.
- **Hash-chained WORM audit** terminates every branch.
- **Manager escalation** — human (practice lead) + a commented webhook target;
  it escalates a non-retryable error at once, 3 failed agents in a session, or
  restarts used up. It does not escalate silence.

## 2.4.131 correctness

Every agent has an explicit `name:`; every llm prompt ends with an OUTPUT
CONTRACT naming exactly its yields; `dual` is only on human boundary agents (no
dual→dual — verified 0 dual warnings at load); the `mcp:` block is commented (no
empty `${VAR:}` url); `max_tool_calls_per_message: 15` on the tool-chaining
agents; every llm agent fn is `async def f(llm_response, input_data, context)`;
`super_agent` is dict form; `timeouts.llm_hard_timeout_s` lands with the 2.4.131
floor. `requirements.txt` is core-only (`leafmesh>=2.4.486` + `apscheduler>=3.10`).

## Config pointer

Roles are in `configs/config.yaml`. Python is confined to floors + controls:
`agency/tools.py` (dev-store tools + atomic claim), `agency/_shared/store.py`
(dev feeds + screening queue + booking ledger), `agency/_shared/session_stash.py`
(plan recovery across the gate), `agency/workforce_analytics_agent.py`,
`agency/candidate_screening_agent.py`, `agency/staffing_coordinator_agent.py`,
`agency/talent_development_agent.py`, `agency/allocation_booker_agent.py`
(finisher), `agency/esat_survey_agent.py` (salt ledger),
`agency/_shared/audit_logger.py` (WORM sink). Run `python validate_config.py`
before `python main.py`.
