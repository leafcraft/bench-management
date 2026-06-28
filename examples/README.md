# bench-management — example scenarios

Four realistic scenarios that exercise the major paths through this mesh.

## How to run

```bash
python examples/run_examples.py                 # all scenarios
python examples/run_examples.py 01_             # one scenario (substring match)
```

## Scenario map

- **01_new_project_kickoff** — `new_project_kickoff` — New 6-month project needs 4 senior consultants — staffing_coordinator finds 7 candidates and builds the assignment plan.
- **02_weekly_bench_review** — `weekly_bench_review` — Monday sweep — workforce_analytics pulls the 30/60/90d projection + utilisation sweep from your PSA/HRIS; talent_development flags >2 week idle.
- **03_promotion_readiness** — `promotion_readiness` — Manager nominates engineer for promotion — talent_development assembles readiness assessment + gaps.
- **04_person_rolling_off** — `person_rolling_off` — Consultant rolling off a 6-month engagement — needs allocation in next 2 weeks.

> Scenarios 01 and 02 enter through `workforce_analytics_agent`,
> which is an n8n connector against your PSA / HRIS — set
> `PSA_WEBHOOK_URL` in `.env` first, or those runs return the
> connector's empty default shape.

## Audit trail

Every scenario appends one hash-chained entry to the template's
`logs/<audit>.jsonl` file. Tail it during a run to see the
ledger build up across scenarios.
