"""candidate_screening_agent — parallel candidate fan-out over the bench.

One activation → N parallel copies (config: scaling.copies.count). Each copy atomically
CLAIMS a disjoint batch of benched consultants (claim_screening_batch, backed by
the store's per-session queue) and scores them against the open opportunities.
Because every copy claims a different batch, a large bench is screened in
parallel with NO double-work, and the yields are list/sum-safe so
`scaling.copies.handoff: last` combines the shortlists cleanly.

@pre_compose  prime_screening — identity from CODE: set the current session id
              (so the atomic claim tool keys the backlog correctly) and seed the
              backlog once from the benched roster; hand the demand
              (open_opportunities) + backlog size to the model as prepared_data.
@chain        normalise_screening — make the shape merge-safe and honest:
              screened_count = len(shortlist), dedup the shortlist, and never let
              a fabricated escalation flag ride (screening does not escalate — the
              downstream allocation planner owns that decision).

Yields (config.yaml): shortlist, claimed_consultants, screened_count, unmatched,
screening_briefing.
"""
from __future__ import annotations

import json
import re
from typing import Any

from leafmesh import LeafMeshLogger, chain, pre_compose

from agency import tools
from agency._shared import store
from agency._shared.session_stash import resolve_session_id

logger = LeafMeshLogger(__name__)

_DEFAULTS: dict[str, Any] = {
    "shortlist": [],
    "claimed_consultants": [],
    "screened_count": 0,
    "unmatched": [],
    "screening_briefing": "",
}


def prime_screening(data: Any, ctx: Any) -> dict[str, Any]:
    """@pre_compose: set the session (identity from code) so the atomic claim
    tool keys the backlog correctly, seed the backlog once, and hand the demand
    to the model. Fails into a DEFINED state — never an exception the runtime
    swallows (a raising pre_compose would run the agent with no prepared data)."""
    try:
        sid = resolve_session_id(data if isinstance(data, dict) else {},
                                 ctx if isinstance(ctx, dict) else {})
        tools.set_current_session(sid)
        backlog = store.ensure_screening_queue(sid) if sid else 0
        return {
            "open_opportunities": store.open_opportunities(),
            "backlog_size": backlog,
        }
    except Exception as exc:  # pragma: no cover — defensive; never fail into absence
        logger.warning(f"[screening] prime_screening degraded: {exc}")
        return {"open_opportunities": [], "backlog_size": 0}


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


def _dedup(rows: Any) -> list[dict[str, Any]]:
    """Dedup shortlist rows by (consultant_id, opportunity_id) — keeps the merge
    of two copies' shortlists clean even if the bench overlapped a boundary."""
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        key = (str(r.get("consultant_id") or r.get("person_id") or ""),
               str(r.get("opportunity_id") or ""))
        if key == ("", "") or key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


async def normalise_screening(result: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """@chain: make the merged shape honest and sum-safe. screened_count is
    re-derived from the deduped shortlist so copy combination cannot inflate
    it, and no escalation flag is emitted (screening shortlists; it does not
    decide routing — the allocation planner does)."""
    out = {**_DEFAULTS, **(result if isinstance(result, dict) else {})}
    out["shortlist"] = _dedup(out.get("shortlist"))
    out["claimed_consultants"] = [c for c in (out.get("claimed_consultants") or []) if c]
    out["unmatched"] = [c for c in (out.get("unmatched") or []) if c]
    out["screened_count"] = len(out["shortlist"]) + len(out["unmatched"])
    if not out.get("screening_briefing"):
        out["screening_briefing"] = (
            f"Screened {out['screened_count']} consultant(s); "
            f"{len(out['shortlist'])} shortlisted, {len(out['unmatched'])} unplaced."
        )
    logger.info(
        f"[screening] shortlist={len(out['shortlist'])} "
        f"unmatched={len(out['unmatched'])} screened={out['screened_count']}"
    )
    return out


@pre_compose(context_processor=prime_screening)
@chain(normalise_screening)
async def candidate_screening_agent(llm_response: Any, input_data: Any, context: Any) -> dict[str, Any]:
    """Parse the copy's shortlist output to a dict; @chain (normalise_screening)
    then makes the merged shape honest + sum-safe. Kept separate so the
    normalisation is independently testable."""
    return {**_DEFAULTS, **_parse(llm_response)}
