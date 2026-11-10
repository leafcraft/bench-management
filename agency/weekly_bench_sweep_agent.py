"""weekly_bench_sweep_agent — the Monday bench report (programmatic).

auto_discover matches `weekly_bench_sweep_agent`. It runs every Monday at 08:00
in the firm's timezone (config wake_up), an hour before the 09:00 bench review,
and on demand through the `weekly_bench_report` entry point.

Day 0 (no `integration` in the configuration) the SDK calls it with
`llm_response=None` and it builds the report from the dev store: bench % against
the ceiling, consultants below the billable floor, the idle ledger, open demand,
and one action item per problem. The report is written to
./out/bench_report_<ISO week>.md; a second run in the same week rewrites the same
file (idempotent per week).

Wired (`integration: "n8n"` + `connector_config: {connection:
bench_report_n8n}`) your PSA / BI tool builds the report; a successful reply's
`{report_url, action_items}` is passed through, and a failed call is reported as
a failed run (no dev-store report is substituted).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from leafmesh import LeafMeshLogger

from agency._shared import store

logger = LeafMeshLogger(__name__)

_OUT_DIR = Path("./out")


def _iso_week(now: datetime | None = None) -> str:
    d = now or datetime.now(timezone.utc)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def build_report(now: datetime | None = None) -> dict[str, Any]:
    """The weekly report from the dev store (pure apart from the file write)."""
    week = _iso_week(now)
    bench = store.bench_pct()
    roster = store.bench_roster()
    benched = [r for r in roster if not r.get("on_engagement")]
    below = store.below_floor_consultants()
    idle = store.idle_ledger()
    opps = store.open_opportunities()
    demand = sum(int(o.get("headcount") or 0) for o in opps)

    actions: list[str] = []
    if bench > store.BENCH_CEILING_PCT:
        actions.append(f"Practice lead: bench is {bench:.0%}, over the "
                       f"{store.BENCH_CEILING_PCT:.0%} ceiling — review pipeline and hiring.")
    for b in below:
        actions.append(f"Staffing partner: {b['name']} ({b['consultant_id']}) has been under the "
                       f"{store.UTIL_FLOOR_PCT:.0%} billable floor for {b['weeks_below_floor']}+ weeks "
                       f"(recent {b['recent_utilisation_pct']:.0%}).")
    for p in idle["idle_4w"]:
        actions.append(f"Talent ops: {p['name']} ({p['consultant_id']}) idle {p['idle_weeks']} weeks "
                       f"— career path and reskilling plan due.")
    for o in opps:
        actions.append(f"Staffing: {o['opportunity_id']} ({o.get('client', '')}) needs "
                       f"{o.get('headcount', 0)} — starts {o.get('start', '?')}.")

    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = _OUT_DIR / f"bench_report_{week}.md"
    lines = [
        f"# Weekly bench report — {week}",
        "",
        f"- Headcount: {len(roster)}; on the bench: {len(benched)} ({bench:.0%}; ceiling "
        f"{store.BENCH_CEILING_PCT:.0%})",
        f"- Overall billable utilisation (tracked weeks): {store.overall_utilisation():.0%}",
        f"- Below the {store.UTIL_FLOOR_PCT:.0%} floor for {store.WEEKS_BELOW_FLOOR}+ weeks: {len(below)}",
        f"- Idle 2+ weeks: {len(idle['idle_2w'])}; idle 4+ weeks: {len(idle['idle_4w'])}",
        f"- Open demand: {len(opps)} engagement(s), {demand} seat(s)",
        "",
        "## Action items",
        *[f"- {a}" for a in actions],
        "",
        "_Built from the dev store (agency/_shared/store.py) — not your PSA._",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return {"report_url": str(path), "action_items": actions, "week": week,
            "source": "dev_store"}


async def weekly_bench_sweep_agent(llm_response: Any, input_data: Any, context: Any) -> dict:
    if isinstance(llm_response, dict) and llm_response.get("success") is False:
        err = str(llm_response.get("error") or "the bench-report workflow failed")
        logger.warning(f"[bench-report] workflow failed: {err}")
        return {"report_url": "", "action_items": [], "week": _iso_week(), "source": "bench_report_n8n",
                "success": False, "error": f"bench-report workflow failed: {err[:200]}"}
    if isinstance(llm_response, dict) and llm_response.get("success") is True:
        c = llm_response.get("content")
        if isinstance(c, str):
            try:
                c = json.loads(c)
            except ValueError:
                c = {}
        c = c if isinstance(c, dict) else {}
        return {"report_url": str(c.get("report_url") or ""),
                "action_items": list(c.get("action_items") or []),
                "week": str(c.get("week") or _iso_week()), "source": "bench_report_n8n"}
    out = build_report()
    logger.info(f"[bench-report] {out['week']}: {len(out['action_items'])} action item(s) -> {out['report_url']}")
    return out
