"""workforce_analytics_agent — capacity forecast, utilisation ledger and
skills inventory (SYSTEM, connector-backed + deterministic floors).

This is the analytics ROLE, mode-dispatched (forecast | ingest | report |
upsert | search). It is `agent_type: programmatic, integration: n8n` in
config.yaml: in PRODUCTION the n8n workflow in front of your PSA/HRIS
(PSA_WEBHOOK_URL) runs and the SDK passes its raw response here as the
first arg — this module shapes it into the declared yields
(@sdk.intelligence role). In DEV (no connector) it serves the bench feeds
from agency/_shared/store.py so the full forecast → match → allocate chain
runs day-0.

WHY THIS IS PYTHON, NOT YAML / NOT THE WORKFLOW (the depth bar):
the bench-ceiling, below-floor (65% billable for 3+ weeks) and idle
thresholds are DETERMINISTIC FLOORS. The can_call wires in config.yaml
fire on `bench_pct_current > 0.15` and `below_floor_consultants != []`
— but those VALUES have to come from somewhere trustworthy. Computing
them here (store.bench_pct / store.below_floor_consultants) means the
routing cannot be waived by a soft n8n workflow or an LLM prompt: the
floor is code, the wire is config, neither can quietly drop a below-floor
consultant or under-report the bench.

Yields (config.yaml): mode, the forecast block, the ledger block, the
skills block. The SDK enforces the shape; this fills the values.
"""
from __future__ import annotations

from typing import Any

from leafmesh import LeafMeshLogger

from agency._shared import store

logger = LeafMeshLogger(__name__)

# Full-shape: every declared yield with a safe default.
_DEFAULTS: dict[str, Any] = {
    "mode": "forecast",
    # capacity forecast
    "utilisation_30d_pct": 0.0,
    "utilisation_60d_pct": 0.0,
    "utilisation_90d_pct": 0.0,
    "bench_pct_current": 0.0,
    # utilisation ledger
    "consultants_tracked": 0,
    "horizon_weeks": 0,
    "overall_utilisation": 0.0,
    "below_floor_consultants": [],
    "utilisation_briefing": "",
    # skills inventory
    "total_skill_records": 0,
    "distinct_skills": 0,
    "min_level": "intermediate",
    "matches_per_skill": {},
    "coverage_pct": 0.0,
    "gaps": [],
    "skills_inventory_briefing": "",
    # forecast carries the demand the coordinator matches into
    "open_opportunities": [],
    "available_consultants": [],
}


def _pick(input_data: dict[str, Any], key: str, default: Any) -> Any:
    """Shape-tolerant read: input_data, then upstream_yields (flat or keyed),
    then payload."""
    if not isinstance(input_data, dict):
        return default
    if input_data.get(key) not in (None, ""):
        return input_data[key]
    for nest_key in ("upstream_yields", "payload"):
        nest = input_data.get(nest_key)
        if isinstance(nest, dict):
            if nest.get(key) not in (None, ""):
                return nest[key]
            for v in nest.values():
                if isinstance(v, dict) and v.get(key) not in (None, ""):
                    return v[key]
    return default


def _mode(input_data: dict[str, Any], raw: Any) -> str:
    m = _pick(input_data, "mode", "")
    if not m and isinstance(raw, dict):
        m = raw.get("mode", "")
    return str(m or "forecast").lower()


# ── Deterministic domain math (the enforced floors) ──────────────────
def _forecast() -> dict[str, Any]:
    """Capacity forecast + the bench-ceiling input. The 30/60/90d figures
    project current overall utilisation forward (dev heuristic); the bench %
    and below-floor list are the REAL deterministic floors."""
    overall = store.overall_utilisation()
    bench = store.bench_pct()
    below = store.below_floor_consultants()
    available = [
        {"consultant_id": r["consultant_id"], "name": r.get("name", ""),
         "level": r.get("level", ""), "idle_weeks": int(r.get("idle_weeks", 0)),
         "skills": [s.get("skill") for s in store.consultant_skills(r["consultant_id"])]}
        for r in store.bench_roster(only_available=True)
    ]
    opps = store.open_opportunities()
    briefing = (
        f"Bench at {bench:.0%} ({'OVER' if bench > store.BENCH_CEILING_PCT else 'within'} "
        f"the {store.BENCH_CEILING_PCT:.0%} ceiling); overall utilisation {overall:.0%}; "
        f"{len(below)} consultant(s) below the {store.UTIL_FLOOR_PCT:.0%} floor for "
        f"{store.WEEKS_BELOW_FLOOR}+ weeks; {len(available)} available against "
        f"{len(opps)} open opportunit(y/ies)."
    )
    return {
        "mode": "forecast",
        "utilisation_30d_pct": overall,
        "utilisation_60d_pct": round(min(1.0, overall + 0.03), 3),
        "utilisation_90d_pct": round(min(1.0, overall + 0.05), 3),
        "bench_pct_current": bench,
        "below_floor_consultants": below,
        "overall_utilisation": overall,
        "open_opportunities": opps,
        "available_consultants": available,
        "utilisation_briefing": briefing,
    }


def _report() -> dict[str, Any]:
    util = store.utilisation_history()
    below = store.below_floor_consultants()
    horizon = max((len(rows) for rows in util.values()), default=0)
    overall = store.overall_utilisation()
    return {
        "mode": "report",
        "consultants_tracked": len(util),
        "horizon_weeks": horizon,
        "overall_utilisation": overall,
        "below_floor_consultants": below,
        "bench_pct_current": store.bench_pct(),
        "utilisation_briefing": (
            f"{len(util)} consultants tracked over {horizon} weeks; overall "
            f"utilisation {overall:.0%}; {len(below)} below the "
            f"{store.UTIL_FLOOR_PCT:.0%} floor."
        ),
    }


def _ingest(input_data: dict[str, Any]) -> dict[str, Any]:
    cid = str(_pick(input_data, "consultant_id", ""))
    week = str(_pick(input_data, "week_ending", ""))
    bill = float(_pick(input_data, "billable_hours", 0) or 0)
    avail = float(_pick(input_data, "available_hours", 40) or 40)
    if cid and week:
        store.ingest_utilisation(cid, week, bill, avail)
    rep = _report()
    rep["mode"] = "ingest"
    return rep


def _upsert(input_data: dict[str, Any]) -> dict[str, Any]:
    cid = str(_pick(input_data, "consultant_id", ""))
    skill = str(_pick(input_data, "skill", ""))
    level = str(_pick(input_data, "level", "intermediate"))
    last = str(_pick(input_data, "last_used", ""))
    if cid and skill:
        store.upsert_skill(cid, skill, level, last)
    inv = store.skills_inventory()
    return {
        "mode": "upsert",
        "total_skill_records": sum(len(v) for v in inv.values()),
        "distinct_skills": len({s["skill"] for v in inv.values() for s in v}),
        "skills_inventory_briefing": f"Upserted {skill or '(none)'} for {cid or '(none)'}.",
    }


def _search(input_data: dict[str, Any]) -> dict[str, Any]:
    needed = _pick(input_data, "needed_skills", []) or []
    if isinstance(needed, str):
        needed = [needed]
    min_level = str(_pick(input_data, "min_level", "intermediate"))
    res = store.search_skills(list(needed), min_level)
    inv = store.skills_inventory()
    gaps = res["gaps"]
    return {
        "mode": "search",
        "total_skill_records": sum(len(v) for v in inv.values()),
        "distinct_skills": res["distinct_skills"],
        "min_level": min_level,
        "matches_per_skill": res["matches_per_skill"],
        "coverage_pct": res["coverage_pct"],
        "gaps": gaps,
        "skills_inventory_briefing": (
            f"Coverage {res['coverage_pct']:.0%} over {len(needed)} needed skill(s); "
            f"{len(gaps)} gap(s)" + (f": {', '.join(gaps)}." if gaps else ".")
        ),
    }


def _shape_connector(raw: dict[str, Any], mode: str) -> dict[str, Any]:
    """@sdk.intelligence: map the n8n/PSA workflow's raw response into the
    declared yields, preserving keys it already provides. The deterministic
    floor still applies — if the workflow under-reports below_floor or bench,
    fall back to the locally-computed value so the wire cannot be silenced."""
    out = {k: raw[k] for k in _DEFAULTS if k in raw}
    out["mode"] = mode
    # Re-assert the floors from the store unless the workflow returned them.
    if mode in ("forecast", "report"):
        if "bench_pct_current" not in raw:
            out["bench_pct_current"] = store.bench_pct()
        if "below_floor_consultants" not in raw:
            out["below_floor_consultants"] = store.below_floor_consultants()
    return out


async def workforce_analytics_agent(llm_response, input_data, context):
    """Mode-dispatched analytics. Connector path shapes the PSA response;
    dev path computes the real bench feeds from the store."""
    input_data = input_data or {}
    mode = _mode(input_data, llm_response)

    # When the n8n connector is wired the SDK passes its raw response as the
    # first arg — a non-empty dict means "shape this".
    if isinstance(llm_response, dict) and llm_response:
        computed = _shape_connector(llm_response, mode)
        source = "psa_connector"
    elif mode == "ingest":
        computed = _ingest(input_data); source = "dev_store"
    elif mode == "report":
        computed = _report(); source = "dev_store"
    elif mode == "upsert":
        computed = _upsert(input_data); source = "dev_store"
    elif mode == "search":
        computed = _search(input_data); source = "dev_store"
    else:
        computed = _forecast(); source = "dev_store"

    logger.info(
        f"workforce_analytics_agent — mode={computed.get('mode')} ({source}); "
        f"bench={computed.get('bench_pct_current')}, "
        f"below_floor={len(computed.get('below_floor_consultants') or [])}"
    )
    return {**_DEFAULTS, **computed}
