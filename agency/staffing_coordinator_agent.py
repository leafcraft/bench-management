"""staffing_coordinator_agent — match people to opportunities, then build
the assignment plan, with the routing floors enforced in code.

@pre_compose  pull_supply_and_demand — pulls the role's OWN context before
              the LLM runs: the available bench, the open opportunities,
              and the skills inventory, all from the store (PROD: PSA/HRIS).
              The coordinator reasons over the real supply/demand, not a
              spoon-fed blob.
@chain        enforce_allocation_floors — DETERMINISTIC floor the LLM cannot
              waive: re-derive double-booking conflicts from the matches,
              re-check whether any single skill gap blocks 2+ open
              opportunities, and FORCE requires_engagement_lead_review /
              requires_practice_lead_review accordingly. The prompt asks for
              these; this guarantees them, so the can_call routing (conflict
              → engagement lead, structural gap → practice lead) cannot be
              softened by a hallucinated flag.

Yields (config.yaml): matches, unmatched_people, unmatched_opportunities,
skills_gaps, staffing_briefing, plan_id, assignments, conflicts,
requires_engagement_lead_review, requires_practice_lead_review.
"""
from __future__ import annotations

import json
import re
import secrets
from collections import Counter
from typing import Any

from leafmesh import LeafMeshLogger, chain, pre_compose

from agency._shared import store

logger = LeafMeshLogger(__name__)

_DEFAULTS: dict[str, Any] = {
    "matches": [],
    "unmatched_people": [],
    "unmatched_opportunities": [],
    "skills_gaps": [],
    "staffing_briefing": "",
    "plan_id": "",
    "assignments": [],
    "conflicts": [],
    "requires_engagement_lead_review": False,
    "requires_practice_lead_review": False,
}


def pull_supply_and_demand(data: Any, ctx: Any) -> dict[str, Any]:
    """@pre_compose: the coordinator pulls its own supply + demand. Lands in
    context["prepared_data"] for the prompt to reason over."""
    available = [
        {"consultant_id": r["consultant_id"], "name": r.get("name", ""),
         "level": r.get("level", ""), "idle_weeks": int(r.get("idle_weeks", 0)),
         "skills": [s.get("skill") for s in store.consultant_skills(r["consultant_id"])]}
        for r in store.bench_roster(only_available=True)
    ]
    opportunities = store.open_opportunities()
    return {
        "available_consultants": available,
        "open_opportunities": opportunities,
        "skills_inventory": store.skills_inventory(),
    }


def _parse(resp: Any) -> dict[str, Any]:
    if isinstance(resp, dict):
        return resp
    if isinstance(resp, str):
        try:
            return json.loads(resp)
        except ValueError:
            m = re.search(r"\{.*\}", resp, re.S)
            if m:
                try:
                    return json.loads(m.group(0))
                except ValueError:
                    pass
    return {}


def _derive_conflicts(matches: list[Any]) -> list[dict[str, Any]]:
    """A person assigned to 2+ opportunities is double-booked. Deterministic."""
    counts: Counter = Counter()
    for m in matches or []:
        if isinstance(m, dict) and m.get("person_id"):
            counts[m["person_id"]] += 1
    return [{"person_id": pid, "count": c} for pid, c in counts.items() if c >= 2]


def _structural_gap(skills_gaps: list[Any], opportunities: list[dict[str, Any]]) -> bool:
    """True when a single skill gap blocks 2+ OPEN opportunities (a hire /
    partner / decline call — practice-level, not a staffing assignment)."""
    for g in skills_gaps or []:
        skill = g.get("skill") if isinstance(g, dict) else None
        if not skill:
            continue
        blocked = sum(1 for o in opportunities
                      if skill in (o.get("required_skills") or []))
        if blocked >= 2:
            return True
    return False


async def enforce_allocation_floors(result: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """@chain: re-derive the routing-critical facts from the data and OVERRIDE
    the LLM's flags. The LLM proposes matches; the routing gates are code."""
    out = {**_DEFAULTS, **(result if isinstance(result, dict) else {})}

    # plan_id: guarantee a real one even if the model left it empty.
    if not out.get("plan_id"):
        out["plan_id"] = f"plan-{secrets.token_hex(5)}"

    # Assignments mirror matches when the model didn't copy them.
    if not out.get("assignments"):
        out["assignments"] = list(out.get("matches") or [])

    # Conflicts: re-derive deterministically (do not trust the LLM's list).
    conflicts = _derive_conflicts(out.get("matches"))
    out["conflicts"] = conflicts

    # Structural skills gap → practice lead. Use the store's CURRENT open
    # opportunities as the source of truth for "blocks 2+".
    opportunities = store.open_opportunities()
    structural = _structural_gap(out.get("skills_gaps"), opportunities)
    out["requires_practice_lead_review"] = bool(structural)

    # Conflict → engagement lead (only when not already a practice-lead call;
    # the can_call edges are mutually exclusive on the practice flag).
    out["requires_engagement_lead_review"] = bool(conflicts)

    if conflicts or structural:
        logger.info(
            f"[coordinator] floor: conflicts={len(conflicts)} "
            f"structural_gap={structural} plan={out['plan_id']}"
        )
    return out


@pre_compose(context_processor=pull_supply_and_demand)
@chain(enforce_allocation_floors)
async def staffing_coordinator_agent(llm_response: Any, input_data: Any, context: Any) -> dict[str, Any]:
    """Parse the LLM's match/plan output to a dict; @chain
    (enforce_allocation_floors) then applies the deterministic routing floor.
    Keep the two stages separate so the floor is independently testable."""
    return {**_DEFAULTS, **_parse(llm_response)}
