# bench-management — Bench-Management Pod

Resource & staffing operations for a professional-services firm, run as a **pod**:
a coordinator that owns each staffing case, a team of specialists that read the
bench and plan the allocation, a **finisher** that books the approved assignment
and produces the real artifact, named humans behind the gates (nothing is booked
on silence), and the Manager as supervisor. **YAML-first**: utilisation, bench and skills data live in
YOUR PSA / HRIS (Kantata/Mavenlink, Workday, SAP SuccessFactors, BambooHR…)
behind n8n / MCP connectors — never in template SQLite as a system of record.

**Runs day-0 with no connectors.** A dev store (`agency/_shared/store.py`) ships a
fictional 24-consultant practice (Halvorsen Partners): six people on the bench
after two engagements ended (25%, over the 15% ceiling), four of them under the
65% billable floor for three weeks, five open engagements needing six seats
(one, an SAP migration, has no one with the skill), and three weeks of
utilisation for everyone — so the full **forecast → screen → plan → approve →
book** flow, and the Monday bench report, run out of the box. The Python here is *floors and controls*, not theater: workforce
analytics computes bench % and the below-floor list; candidate screening claims
disjoint batches atomically so parallel copies never double-screen; the staffing
coordinator's `@chain` re-derives double-booking + structural gaps and **forces**
the routing flags; the finisher re-checks the human approval, refuses to
double-book, and **deletes its artifact on a blocked floor**; talent development
re-asserts the idle ledger; and the ESAT salted-hash ledger keeps respondent
anonymity a *structural control*. The LLM proposes — the floors decide.

## Story

It's Monday. At 08:00 `weekly_bench_sweep_agent` writes the week's bench report
(bench %, who is under the billable floor, who has been idle 4+ weeks, open
demand) to `./out` and the audit log. At 09:00 the weekly review runs. Then
**Anika Desai, a staffing partner**, fires a `new_project_kickoff` for
Brightwater Health's data platform phase 2. The **bench coordinator** opens the
case and hands it to `workforce_analytics_agent`, which serves the 30/60/90-day
utilisation forecast, the current bench percentage, and the skills inventory —
from the seed store day-0, from the firm's PSA/HRIS once its n8n workflow is
wired.
Because `mode == 'forecast'`, the case flows to `candidate_screening_agent`, which
**fans out into parallel copies**: each atomically claims a disjoint batch of
benched consultants (`claim_screening_batch`) and scores them against the open
opportunities, so a large bench is screened in parallel with no double-work
(leafmesh 2.4.484's `copies.mode: auto` with `claim_tool:` could replace this
manual split; that is an owner decision and is not done here). The
combined shortlist flows to `staffing_coordinator_agent` — a **super-agent** that
plans the allocation and builds the assignment plan (`plan-3f9c01ab2d`).

One consultant, **Lina**, is matched to two opportunities, so the
coordinator's `@chain` re-derives the conflict and routes the plan to the
**engagement lead**. If nobody answers within four hours, the card closes and
the ticket records that no reply came — nothing is booked, and nobody is
chased automatically. On approval, `allocation_booker_agent`, the
**finisher**, re-checks that a human actually approved (an unknown verdict
fails closed), refuses any slice that would push a consultant past 100% (so
Lina is booked once and the second slice is listed as rejected), never books
the same plan twice, **renders the booking confirmation to `./out`**, and
records the assignment. Meanwhile the forecast
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
advances a decision** (an unanswered card closes and the ticket records it;
nothing is booked). The finisher is the load-bearing control: reaching it is not
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

`requirements.txt` pins `leafmesh>=2.4.497` + `apscheduler>=3.10` (for the weekly
`wake_up` crons). Do **not** install `leafmesh[all]` on Python 3.12+ — it pulls
`cchardet`, which has no wheel there; add only the specific extras you wire.

### Tier 2 — Host prerequisites + adapters (required to go live)
Addresses and tokens live **only in the vault**: create each connection
(n8n ones on Infrastructure → Integrations → n8n, the MCP one on
Infrastructure → Integrations → MCP: base URL = the workflow's webhook URL /
server URL, token = its auth token), then uncomment the agent's
`integration: "n8n"` + `connector_config: {connection: …}` lines (or the
screener's `mcp:` block) — in `configs/config.yaml` before the first start, in
Studio's YAML editor after it (see *Changing the configuration after the first
start*). A URL or token written in the config is refused at load.

The n8n lines ship commented on purpose: an n8n integration whose vault
record is missing or has no workflow address is switched off at start, so its
day-0 Python would not run. Once wired, the
same Python shapes the workflow's reply; a failed call is a failed run (or, for
the booker, a blocked booking) — never replaced by dev-store numbers.

| Vault connection | Why | Required? |
|---|---|---|
| `psa_n8n` | n8n workflow in front of your PSA/HRIS — serves `forecast` / `ingest` / `report` / `upsert` / `search` for `workforce_analytics_agent` | Required for real data (unwired → dev store) |
| `psa_mcp` | MCP server (URL + token) exposing bench / opportunities / skills as tools for `candidate_screening_agent` — then uncomment its `mcp:` block | Optional (dev store serves screening day-0) |
| `psa_booking_n8n` | n8n workflow that WRITES the human-approved assignment to your PSA (`allocation_booker_agent`); your PSA's own allocation rules then apply to the write | Optional (dev records the booking + renders `./out` day-0) |
| `bench_report_n8n` | n8n workflow in front of your PSA/BI weekly bench report (`weekly_bench_sweep_agent`) | Optional (dev store builds the report day-0) |
| *(Tools → Webhooks: `bench_report_post`)* | posts the finished Monday report to a channel or BI inbox — uncomment `notify` on `weekly_bench_sweep_agent`; the webhook picks an Outbound webhook record | Optional |

The screening tools check the vault for a `psa_n8n` or `psa_mcp` record to know
whether a real PSA is wired; nothing in `.env` switches them.

### Tier 3 — Connectors / escalation (optional)
| Var | Why | Required? |
|---|---|---|
| *(vault connection `escalation_webhook`)* | PagerDuty (or other) target for the Manager's webhook escalation — create vault connection `escalation_webhook` (an "Add custom" record on the Infrastructure page, service type 'Outbound webhook'), then uncomment the webhook target (in `configs/config.yaml` before the first start, in Studio's YAML editor after it) | Optional |
| *(vault connection, any name)* | human members answer in Studio's Inbox; to notify an external tool instead, create a vault connection (an "Add custom" record on the Infrastructure page, service type 'Outbound webhook') and set `webhook_config: {connection: <name>}` on the seat | Optional |
| `ESAT_LEDGER_DB` | local SQLite path for the per-survey ESAT salt ledger | Optional (defaults to `./logs/esat_anonymisation.db`) |

### Try it day-0 (no connectors)

With `main.py` running (API at `http://127.0.0.1:18820/docs`), open a staffing
case against the seeded dev bench:

```bash
curl -X POST http://127.0.0.1:18820/api/mesh/request \
  -H "Content-Type: application/json" \
  -d '{"entry_point":"new_project_kickoff","data":{"cycle":"2026-Q3"}}'
```

The coordinator opens the case → analytics serves the forecast (bench at 25%,
over the 15% ceiling → the practice-lead wire fires; four consultants below the
65% billable floor → the staffing-partner wire) → screening fans out over the bench →
the coordinator plans and routes on the deterministic conflict / structural-gap
floors. Wire the vault connection `psa_n8n` to swap the store for your real
PSA/HRIS; the floors still apply.

## Agents

| Agent | Role (job title) | Responsibilities | Can perform |
|---|---|---|---|
| `bench_coordinator_agent` | Bench coordinator | Owns each staffing case end to end; opens it, orchestrates, keeps status, single point of contact. Never reads the PSA, matches, or books | llm (pure YAML) → analytics on `open_case`; talent dev on `talent`; practice lead on `escalate` |
| `workforce_analytics_agent` | Workforce analyst | Capacity forecast (30/60/90d + bench %), utilisation ledger, skills inventory — mode-dispatched from the dev store day-0 / your PSA/HRIS via n8n when wired. **Computes the bench % + below-floor list (the floor)** | programmatic Python (n8n when wired); Monday 09:00 `wake_up` (timezone-pinned) → screening on `forecast`; bench > 15% → practice lead; below-floor → staffing partner |
| `candidate_screening_agent` | Candidate-screening analyst | Fan out over the bench: each **parallel copy** atomically claims a disjoint batch (`claim_screening_batch`) and shortlists candidates against open opportunities | llm ×2 copies (`scaling.copies: {count: 2, handoff: last}`) + `@pre_compose`/`@chain` module → staffing coordinator on a non-empty shortlist |
| `staffing_coordinator_agent` | Staffing coordinator | Turn the shortlist into an assignment plan. **`@pre_compose` pulls supply/demand; `@chain` re-derives double-booking + structural gaps and forces the routing flags** | llm **super-agent** (dict-tuned) + module → engagement lead on conflict; structural gap → practice lead; clean → staffing partner |
| `talent_development_agent` | Talent development lead | Recommend reskilling bets, draft 6-month retention-grade career paths. **`@pre_compose` pulls the idle ledger; `@chain` re-asserts idle detection + senior-flight-risk routing** | llm (claude-sonnet-4-6) + playbook + module → practice lead on 4-week idle / senior flight risk; talent ops on idle/reskilling |
| `performance_review_agent` | Performance-review writer | Draft Q/annual reviews from feedback + utilisation + skill growth; composite score + rating bands | llm (claude-sonnet-4-6) → practice lead for calibration tails; engagement lead for normal range |
| `esat_survey_agent` | Employee-experience analyst | Launch anonymised pulse surveys; analyse closed ones — engagement index, themes, HRBP flags | llm (gpt-4o-mini) + Python salted-hash ledger → talent ops when the floor or a hot theme trips |
| `weekly_bench_sweep_agent` | Bench-report runner | Build the Monday bench report (bench %, below-floor, idle, open demand, action items) — dev store day-0, your PSA/BI via n8n when wired | programmatic Python; Monday 08:00 `wake_up` (timezone-pinned) + `weekly_bench_report` entry → audit |
| `allocation_booker_agent` | Allocation booker (**finisher**) | The executor after the gate: re-check the human approval (fail closed on unknown), refuse to double-book, never book the same plan twice, **render the booking artifact to `./out`, delete it on a blocked floor**, record the assignment | programmatic Python + `@chain` two-layer floor (n8n PSA write when wired); dev store books + renders day-0 → audit (terminal) |
| `staffing_partner_human` | Staffing partner | Owns assignment decisions on clean plans; gets below-floor alerts | human (4h) → booker on `approved`; any other reply → audit |
| `engagement_lead_human` | Engagement lead | Resolve assignment conflicts / double-bookings; sign normal-range reviews | human (4h) → booker on `approved`; any other reply → audit |
| `talent_ops_human` | Talent ops / HRBP | Reskilling + training pathways; HRBP review of survey signals | human (6h) → audit |
| `practice_lead_human` | Practice lead | Bench % authority, structural-gap calls, retention escalation, calibration; gets the Manager's error escalations | human (8h) → audit |
| `bench_audit_logger_agent` | Compliance recorder | Append-only hash-chained WORM audit of every terminal decision | external (custom intelligence fn registered in main.py); terminal |

### When nobody answers

Nothing is booked on silence. If nobody answers a seat within its time
(staffing partner and engagement lead 4h, talent ops 6h, practice lead 8h), the
request waits no longer: its Inbox card closes and the ticket records that no
reply came. Nobody is chased automatically. Each seat keeps
`fallback_on_timeout` / `fallback_response: held_for_review` in config as the
declared intent; on leafmesh 2.4.486 a timeout on these seats routes nothing.

The **Manager** is the supervisor. It escalates to the practice lead only on
errors: a non-retryable error (sign-in, bad request, bad config) the first time
it happens, three failed agents in one session, or restarts used up. Contract
violations alert people; they do not escalate. It is not a router in the middle
of the team. See `PRD.md` for the full architecture, flow diagram, and per-agent
spec.

## Changing the configuration after the first start

`configs/config.yaml` is only the first-start seed. On the first start (an empty store) the agency imports it as version 1 and from then on keeps its configuration in the store. After that the file is not read again: an edit to it is **not** applied, even after a restart (the boot log says "that edit was NOT applied").

To change a running agency:

- **Studio** — its settings pages, or the raw YAML editor.
- **API** — `PUT /api/yaml/raw` with an `X-API-Key` header and the body `{"yaml_content": "<the whole YAML>", "save_to_disk": true}`, or `PATCH /api/yaml/config` with `{"updates": {...}, "save_to_disk": true}`.

Each save is a new version and applies live on every pod — no restart. (A few settings, such as brokers, make the agency reload itself in place within seconds.) So wherever this README or a comment says "uncomment X in `configs/config.yaml`", do it in the file before the first start, or in Studio's YAML editor after it.

## Changing a running project (Docker)

What reaches a project that is already running (leafmesh 2.4.497). The
image holds the Python code and the first-start `configs/config.yaml`;
`.env` is passed in when the container is created; the store (Redis) keeps
its data on its own volume.

| You change | When it applies |
|---|---|
| An agent in Studio (prompt, model, tools, routes, human seats, schedules, …) | At once, no restart |
| A human seat's `channels` (Slack, email, …) | The agency reloads itself in place within seconds |
| Add or delete an agent in Studio | At once, including routes and schedules |
| A connection in the vault (model keys, MCP, webhooks, connectors, Slack, email) | At once. An agent switched off for a missing connection or address switches back on by itself |
| Anything in `.env` (licence, agency name, store, ports) | After `docker compose up -d` (the container is recreated) |
| Python code (`agency/*.py`: programmatic agents, tools, `@pre_compose`) | After a rebuild: `docker compose up -d --build` |
| `configs/config.yaml` | Never after the first start. It is read only when the store is empty |
| Several app containers on one store | A Studio save reaches every one within about 15 seconds |

- Keep model keys in the vault. A key there wins over the same key in
  `.env` (the log warns), and a change applies at once. From 2.4.497 that
  holds for every value the vault holds (endpoints, organisation / project
  ids, cloud identities); the `.env` value returns when the record goes.
- Code the image does not have: an agent marked pure Python
  (`integration: python`) is switched off until its function is in the image;
  any other programmatic agent stays on, and each call to code it does not
  have fails. Rebuild the image with the code first.
- A new Redis volume is an empty store, so `configs/config.yaml` is imported
  again as version 1.
- **If the agency refuses to start after an upgrade** (a new leafmesh pin,
  then a rebuild: "Configuration has N error(s) in the stored
  configuration"), repair the stored configuration with the agency stopped:

  ```bash
  docker compose run --rm bench-management leafmesh config check      # each problem, with its line
  docker compose run --rm bench-management leafmesh config versions   # kept versions: which pass, which is in use
  docker compose run --rm bench-management leafmesh config rollback   # back to the newest version that passes (saved as a new version)
  ```

  Or `leafmesh config export > config.yaml`, fix it by hand, then
  `leafmesh config import config.yaml` (it is checked before it is saved).

## Where the data store is set

From leafmesh 2.4.482 the store (Redis or Postgres) is set **only in `.env`** — never in `configs/config.yaml`. A `persistence:` block in the YAML is ignored with a warning on leafmesh 2.4.482–2.4.485; from 2.4.486 the agency refuses to start if that block differs from `.env`, and names each `.env` line to add. The project's `.env` sets the store explicitly (`LEAFMESH_STORE`, `LEAFMESH_AGENCY_NAME`, `REDIS_*`).

- Redis (default): `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`, `REDIS_DB`; optional `REDIS_DEFAULT_TTL`, `REDIS_SESSION_TTL`, `REDIS_SSL*`, `REDIS_CLUSTER_NODES`.
- Postgres instead: `LEAFMESH_STORE=postgres` plus `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` (optional `POSTGRES_SSLMODE`).
- Or the whole server in one value: `REDIS_URL=redis://user:pw@host:6379/0` (`rediss://` turns TLS on) or `DATABASE_URL=postgresql://user:pw@host:5432/db`. The single names fill in what the URL leaves out.
- With Docker Compose the app reaches Redis as `redis` (the service name); compose sets that for you.
- Check it in Studio → **Infrastructure → Datastores**: it shows whether the store is reachable (never the address or password).

## This mesh's sign-in (leafmesh 2.4.497)

From leafmesh 2.4.497 each mesh signs in to LeafCraft with its own key, not
a key shared by every install.

- **First start.** The mesh makes a key pair and registers it with
  `LEAFMESH_LICENSE_KEY` and `LEAFMESH_ENV_TOKEN` from `.env` (copy the
  environment token from Studio — a placeholder such as `development` is
  refused). With both valid it works at once. If your organisation turned on
  "require approval", the first start stops and says so: approve it in
  Studio → **Keys → SDK access**, then start again. Organisation admins get an
  email for every new registration.
- **Where the key lives.** In the vault, in the store, encrypted with
  `LEAFMESH_VAULT_KEY`: it survives rebuilds and every copy of the app shares
  it. A new Redis volume, or a new vault key, makes a new key pair that
  registers again — revoke the old one in Studio.
- **Check or change it** (in Docker: `docker compose run --rm bench-management leafmesh auth status`):
  - `leafmesh auth status` — credential id, key fingerprint, registration and
    mode, never a key; `--check` signs in now and reports the tokens.
  - `leafmesh auth register` — register now.
  - `leafmesh auth rotate` — a new key pair: once it is active the mesh
    switches to it and revokes the old one. A revoked or expired key is never
    revived; rotate instead.
- **Lost the environment token?** From 5 Dec 2026 Studio shows the licence
  key and environment token only once, when they are made (then only a hint).
  Rotate the environment token in Studio and put the new one in `.env`.
  Rotating it changes where the agency's data is kept in the store, so do it
  before the agency holds data.
- **An existing project whose `.env` says `LEAFMESH_ENV_TOKEN=development`.**
  2.4.497 needs the real token. The token also names where the agency's data
  sits in the store, so with the real token the agency starts on an empty
  store (vault, sessions, board) — the old data stays under the old name. Set
  the real token before you put data in the agency, or contact us to move it.

## Your vault key — back it up

`create-leafmesh` put a `LEAFMESH_VAULT_KEY` in your `.env` when it made this project. It is the master key that encrypts every connection and credential you store in the vault (in Redis / Postgres).

- **Back it up like a master password.** Lose it and every stored connection has to be entered again.
- **Never change or regenerate it** for an existing project — a new key locks the existing vault. To change it, use `leafmesh vault rotate-key`.
- Every copy of the app (every replica) must get the **same** value. Docker Compose passes it in from `.env` as an environment variable.
- Never commit `.env` (it is already in `.gitignore`).
