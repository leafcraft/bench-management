"""talent_development_agent — idle detection, reskilling, career paths, with
the idle + retention-risk floors enforced in code.

@pre_compose  pull_idle_ledger — the role pulls its OWN idle ledger from the
              store (PROD: PSA/HRIS bench feed): the 2-week and 4-week idle
              lists + the average, computed deterministically from each
              consultant's idle_weeks. Lands in context["prepared_data"] so
              the LLM ECHOES real idle data instead of inventing it.
@chain        enforce_idle_and_retention_floors — DETERMINISTIC floor: the
              idle lists are overwritten from the store (the LLM cannot
              under- or over-report who is idle), and the senior-flight-risk
              flag is forced when retention_risk_score >= 0.7 AND the level
              is senior/staff/partner. Losing a senior without the practice
              lead hearing first is not waivable by a prompt.

Yields (set in the configuration): idle_2w, idle_4w, avg_idle_weeks, recommendations,
plan_id, employee_id, current_level, target_role_6mo, skill_bets,
internal_projects, certifications, retention_risk_score,
employee_facing_narrative, confidence, requires_practice_lead_review.
"""
from __future__ import annotations

import json
import re
from typing import Any

from leafmesh import LeafMeshLogger, chain, pre_compose

from agency._shared import store

logger = LeafMeshLogger(__name__)

_SENIOR_LEVELS = {"senior", "staff", "partner"}
_VALID_LEVELS = {"junior", "mid", "senior", "staff", "partner"}
_RETENTION_FLOOR = 0.7

_DEFAULTS: dict[str, Any] = {
    "idle_2w": [],
    "idle_4w": [],
    "avg_idle_weeks": 0,
    "recommendations": [],
    "plan_id": "",
    "employee_id": "",
    "current_level": "",
    "target_role_6mo": "",
    "skill_bets": [],
    "internal_projects": [],
    "certifications": [],
    "retention_risk_score": 0,
    "employee_facing_narrative": "",
    "confidence": 0,
    "requires_practice_lead_review": False,
}


def _pick(data: Any, key: str, default: Any) -> Any:
    if not isinstance(data, dict):
        return default
    if data.get(key) not in (None, ""):
        return data[key]
    for nest_key in ("upstream_yields", "payload", "idle_staff"):
        nest = data.get(nest_key)
        if isinstance(nest, dict) and nest.get(key) not in (None, ""):
            return nest[key]
    return default


def pull_idle_ledger(data: Any, ctx: Any) -> dict[str, Any]:
    """@pre_compose: pull the deterministic idle ledger from the store."""
    ledger = store.idle_ledger()
    employee_id = str(_pick(data, "employee_id", "") or _pick(data, "consultant_id", ""))
    cons = store.get_consultant(employee_id) if employee_id else {}
    return {
        "idle_ledger": ledger,
        "subject_consultant": cons,
        "open_opportunities": store.open_opportunities(),
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


def _clamp01(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, min(1.0, f)), 2)


async def enforce_idle_and_retention_floors(result: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """@chain: overwrite the idle lists from the store (the floor), clamp the
    risk score, and FORCE the senior-flight-risk escalation flag."""
    out = {**_DEFAULTS, **(result if isinstance(result, dict) else {})}

    # IDLE FLOOR — the store is the authority; the LLM does not get to invent
    # or drop idle staff. Echo the deterministic ledger verbatim.
    ledger = store.idle_ledger()
    out["idle_2w"] = ledger["idle_2w"]
    out["idle_4w"] = ledger["idle_4w"]
    out["avg_idle_weeks"] = ledger["avg_idle_weeks"]

    # Normalise level + clamp scores.
    level = str(out.get("current_level") or "").lower()
    if level not in _VALID_LEVELS:
        level = "mid" if out.get("employee_id") else ""
    out["current_level"] = level
    out["retention_risk_score"] = _clamp01(out.get("retention_risk_score"))
    out["confidence"] = _clamp01(out.get("confidence"))

    # Quality floor — a career path with no narrative or no internal projects
    # is weak; cap confidence at 0.4.
    if out.get("employee_facing_narrative") == "" or not out.get("internal_projects"):
        out["confidence"] = min(out["confidence"], 0.4)

    # RETENTION FLOOR — losing a senior/staff/partner at high risk goes to the
    # practice lead, full stop. Not waivable by the prompt.
    senior_flight = out["retention_risk_score"] >= _RETENTION_FLOOR and level in _SENIOR_LEVELS
    out["requires_practice_lead_review"] = bool(senior_flight)

    if out["idle_4w"] or senior_flight:
        logger.info(
            f"[talent] floor: idle_4w={len(out['idle_4w'])} "
            f"senior_flight={senior_flight} risk={out['retention_risk_score']}"
        )
    return out


@pre_compose(context_processor=pull_idle_ledger)
@chain(enforce_idle_and_retention_floors)
async def talent_development_agent(llm_response: Any, input_data: Any, context: Any) -> dict[str, Any]:
    """Parse the LLM's career-path/reskilling output; @chain
    (enforce_idle_and_retention_floors) then applies the idle + retention
    floors. Keep the two stages separate so the floor is independently
    testable."""
    out = {**_DEFAULTS, **_parse(llm_response)}
    # Identity passthrough — plan_id / employee_id from the input when the
    # model omitted them.
    if not out.get("plan_id"):
        out["plan_id"] = str(_pick(input_data, "plan_id", ""))
    if not out.get("employee_id"):
        out["employee_id"] = str(_pick(input_data, "employee_id", "")
                                 or _pick(input_data, "consultant_id", ""))
    return out
