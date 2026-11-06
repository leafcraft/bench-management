# bench-management — example scenarios

Four realistic scenarios that exercise the major paths through this mesh.

## How to run

```bash
create-leafmesh --load .   # once: loads the team into the Redis in .env (or use create-leafmesh --start .)
python examples/run_examples.py                 # all scenarios
python examples/run_examples.py 01_             # one scenario (substring match)
```

## Scenario map

- **01_new_project_kickoff** — `new_project_kickoff` — Brightwater Health's data platform phase 2 needs two people from 16 Nov; the coordinator opens the case, screening copies split the bench, the staffing coordinator plans it and a staffing partner approves it in the Inbox.
- **02_weekly_bench_review** — `weekly_bench_review` — Monday 09:00 review: bench at 25% (over the 15% ceiling) reaches the practice lead; four consultants under the 65% billable floor reach the staffing partner.
- **03_promotion_readiness** — `promotion_readiness` — Prashant Rao is nominated for senior; talent_development assembles the readiness assessment and gaps.
- **04_person_rolling_off** — `person_rolling_off` — Sara Klein rolled off last week; talent_development flags her for the Corvane EKS migration and her development plan.

> Every scenario runs on the dev store with no connector wired. To read your
> PSA / HRIS instead, create the vault connection `psa_n8n` (Infrastructure →
> Integrations → n8n) and uncomment `integration` + `connector_config` on
> `workforce_analytics_agent` — in Studio's raw YAML editor.

## Audit trail

Every scenario appends one hash-chained entry to the template's
`logs/<audit>.jsonl` file. Tail it during a run to see the
ledger build up across scenarios.
