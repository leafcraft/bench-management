"""Global tools for the bench-management pod.

These are the day-0 tool surface for the candidate-screening analyst — the tools
it calls, in the turn, to CLAIM its batch and read the bench. Each reads the dev
store (seed bench) so the pod runs with NO connector wired, and **each announces
itself**: when there is no real source and no seed, it returns `available:false`
and says why — never a synthetic success that looks like a real read.

To go live, wire the PSA/HRIS mcp block in configs/config.yaml (each named
action becomes its own namespaced tool); these dev tools are the fallback
contract.

Identity comes from CODE, not the model: `candidate_screening_agent`'s
@pre_compose sets the current session id here before inference, and the atomic
claim tool keys the backlog on it — the model only chooses a batch size.
"""
from __future__ import annotations

import os

from leafmesh import global_tool, LeafMeshLogger

try:
    from ._shared import store
except ImportError:  # pragma: no cover — direct-run fallback
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _shared import store  # type: ignore

logger = LeafMeshLogger(__name__)

# Set by candidate_screening_agent's @pre_compose (identity from code). All
# parallel instances of one activation share the SAME session id, so a single
# module-level value is correct — the store keys the backlog on it and pops
# atomically, so instances still get disjoint batches.
_CURRENT: dict[str, str] = {"session_id": ""}


def set_current_session(session_id: str) -> None:
    _CURRENT["session_id"] = str(session_id or "")


def _connector_wired() -> bool:
    """True if a real PSA/HRIS connector is configured (URL set)."""
    return bool(os.getenv("PSA_MCP_URL") or os.getenv("PSA_WEBHOOK_URL"))


@global_tool(category="screening")
def claim_screening_batch(batch_size: int = 4, session_id: str = "") -> dict:
    """Atomically claim YOUR disjoint batch of benched consultants to screen.

    Call this FIRST. Concurrent screening copies each get a different batch, so
    no consultant is screened twice. When the backlog is drained, returns
    `available: false` — that means every consultant has been claimed, not that
    the bench is empty.

    Args:
        batch_size: How many consultants to claim this call (default 4).
        session_id: Leave blank — resolved from the turn (identity from code).
    """
    sid = str(session_id or "").strip() or _CURRENT["session_id"]
    if not sid:
        return {"available": False, "reason": "no session context — cannot claim a batch"}
    store.ensure_screening_queue(sid)
    batch = store.claim_screening_batch(sid, batch_size)
    if not batch:
        return {"available": False, "reason": "screening backlog drained — every benched consultant is claimed"}
    logger.info(f"claim_screening_batch session={sid} claimed={len(batch)} remaining={store.screening_remaining(sid)}")
    return {"available": True, "claimed": batch, "remaining": store.screening_remaining(sid)}


@global_tool(category="bench")
def read_open_opportunities() -> dict:
    """Read the open engagements needing staffing, with their required skills and
    headcount, to score claimed consultants against. Reads the seed store when no
    connector is wired; announces `available: false` if there is nothing to read.
    """
    opps = store.open_opportunities()
    if not opps and _connector_wired():
        return {"available": False, "reason": "connector wired but returned no open opportunities"}
    return {"available": True, "open_opportunities": opps}


@global_tool(category="bench")
def read_bench_roster(only_available: bool = True) -> dict:
    """Read the bench roster (benched consultants + level + idle weeks). Use for
    context; the atomic per-batch claim is claim_screening_batch.

    Args:
        only_available: True → only the benched (no active engagement).
    """
    rows = store.bench_roster(only_available=bool(only_available))
    if not rows and _connector_wired():
        return {"available": False, "reason": "connector wired but returned an empty roster"}
    return {"available": True, "roster": rows}
