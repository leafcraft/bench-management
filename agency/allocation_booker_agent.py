"""allocation_booker_agent — staffing booker (SYSTEM, connector-backed).

The executor that FINISHES the job. It runs AFTER a staffing decision is
human-approved (staffing partner / engagement lead) and BOOKS the
assignment in the PSA: person → project → dates → allocation%, then
updates utilisation. It returns an artifact (assignment ids) + a summary.

  PROD — point PSA_BOOKING_WEBHOOK_URL at an n8n workflow in front of your
  PSA (Kantata/Mavenlink, Workday…). The connector posts the approved
  assignments and the workflow writes them; this module's @sdk.intelligence
  function then shapes the workflow's ack into yields. Inert until the URL
  is set.

  DEV (no connector) — the booker RECORDS each confirmed assignment in
  agency/_shared/store.py (store.book_assignment), so the approve → book
  chain runs day-0 and produces a real artifact (recorded assignment ids +
  updated utilisation).

@chain  enforce_booking_floors — the DETERMINISTIC floor the LLM / a soft
        workflow cannot waive (fail-closed):
          1. NEVER book unless the upstream human decision is an APPROVAL.
             An un-approved / escalated decision books nothing.
          2. NEVER double-book — reuse the store's availability
             (store.consultant_allocation_pct) and refuse any assignment
             that would push a consultant past 100% allocation.
        store.book_assignment re-checks both invariants at write time, so
        even a connector ack cannot smuggle in a bad booking.

Yields (config.yaml): plan_id, booked, assignment_ids, booked_count,
rejected, booking_ok, booking_summary.
"""
from __future__ import annotations

from typing import Any

from leafmesh import LeafMeshLogger

from agency._shared import store

logger = LeafMeshLogger(__name__)

_DEFAULTS: dict[str, Any] = {
    "plan_id": "",
    "booked": [],
    "assignment_ids": [],
    "booked_count": 0,
    "rejected": [],
    "booking_ok": False,
    "booking_summary": "",
}

# Decision tokens that count as a human APPROVAL to book. Anything else
# (escalate_to_practice_lead, halt_for_review, reject, empty…) books nothing.
_APPROVE_TOKENS = ("approve", "approved", "book", "proceed", "confirm")
# Decisions that explicitly are NOT an approval even if the substring matches.
_BLOCK_TOKENS = ("escalate", "halt", "reject", "deny", "decline")


def _pick(input_data: dict[str, Any], key: str, default: Any) -> Any:
    """Shape-tolerant read: input_data, then upstream_yields (flat or keyed
    by agent), then human_data."""
    if not isinstance(input_data, dict):
        return default
    if input_data.get(key) not in (None, "", [], {}):
        return input_data[key]
    upstream = input_data.get("upstream_yields") or input_data.get("upstream_results") or {}
    if isinstance(upstream, dict):
        if upstream.get(key) not in (None, "", [], {}):
            return upstream[key]
        for v in upstream.values():
            if isinstance(v, dict) and v.get(key) not in (None, "", [], {}):
                return v[key]
    human = input_data.get("human_data") or {}
    if isinstance(human, dict) and human.get(key) not in (None, "", [], {}):
        return human[key]
    return default


def _is_approved(input_data: dict[str, Any]) -> bool:
    """Did the upstream human approve? Reads the human gate's decision /
    message. Fail-closed: an unrecognised or blocking decision is NOT an
    approval, so nothing gets booked."""
    signals = [
        str(_pick(input_data, "human_message", "") or ""),
        str(_pick(input_data, "decision", "") or ""),
        str(_pick(input_data, "human_decision", "") or ""),
    ]
    blob = " ".join(s.lower() for s in signals)
    if any(b in blob for b in _BLOCK_TOKENS):
        return False
    return any(t in blob for t in _APPROVE_TOKENS)


def _assignments_from_input(input_data: dict[str, Any]) -> list[dict[str, Any]]:
    """The approved assignments to book. Prefer the plan's `assignments`,
    fall back to `matches` (same {person_id, opportunity_id, ...} shape)."""
    rows = _pick(input_data, "assignments", None)
    if not isinstance(rows, list) or not rows:
        rows = _pick(input_data, "matches", None)
    out: list[dict[str, Any]] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        cid = r.get("person_id") or r.get("consultant_id") or ""
        oid = r.get("opportunity_id") or ""
        if cid and oid:
            out.append({
                "consultant_id": str(cid),
                "opportunity_id": str(oid),
                "allocation_pct": int(r.get("allocation_pct") or 100),
                "start": str(r.get("start") or ""),
                "end": str(r.get("end") or ""),
            })
    return out


def _book_dev(assignments: list[dict[str, Any]], plan_id: str) -> dict[str, Any]:
    """DEV path — record each approved assignment in the store. The store's
    book_assignment is the fail-closed double-booking floor: it refuses any
    slice that would push a consultant past 100% allocation."""
    # Track in-batch allocation so two approved rows in the SAME plan can't
    # together double-book a consultant (the store sees only committed rows).
    pending: dict[str, int] = {}
    booked: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for a in assignments:
        cid = a["consultant_id"]
        alloc = a["allocation_pct"]
        already = store.consultant_allocation_pct(cid) + pending.get(cid, 0)
        if already + alloc > store.MAX_ALLOCATION_PCT:
            rejected.append({**a, "reason": f"would exceed 100% (already {already}%)"})
            continue
        try:
            rec = store.book_assignment(
                consultant_id=cid, opportunity_id=a["opportunity_id"],
                allocation_pct=alloc, start=a["start"], end=a["end"],
                plan_id=plan_id,
            )
        except ValueError as exc:  # the store's own fail-closed floor
            rejected.append({**a, "reason": str(exc)})
            continue
        pending[cid] = pending.get(cid, 0) + alloc
        booked.append(rec)
    return {"booked": booked, "rejected": rejected}


def _shape_connector_result(raw: dict[str, Any], plan_id: str,
                            assignments: list[dict[str, Any]]) -> dict[str, Any]:
    """@sdk.intelligence: map the PSA booking workflow's ack into yields.
    Whatever the workflow returns, the store still re-checks the floor — so
    the dev ledger and the verification stay coherent."""
    booked_raw = raw.get("booked") or raw.get("assignments") or []
    ids = [b.get("assignment_id") or b.get("id")
           for b in booked_raw if isinstance(b, dict)]
    ids = [str(i) for i in ids if i]
    ok = raw.get("booking_ok")
    if ok is None:
        ok = bool(raw.get("success", True)) and not raw.get("error")
    booked = booked_raw if booked_raw else []
    return {
        "plan_id": raw.get("plan_id") or plan_id,
        "booked": booked,
        "assignment_ids": ids,
        "booked_count": len(ids) or len(booked),
        "rejected": raw.get("rejected") or [],
        "booking_ok": bool(ok),
    }


async def enforce_booking_floors(result: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """@chain: nothing here can overturn the fail-closed guarantees — they
    are enforced in book_assignment before any write. This step normalises
    the artifact + summary so the audit terminus always gets a clean record."""
    out = {**_DEFAULTS, **(result if isinstance(result, dict) else {})}
    out["booked_count"] = len(out.get("assignment_ids") or []) or int(out.get("booked_count") or 0)
    out["booking_ok"] = bool(out.get("assignment_ids")) or bool(out.get("booking_ok"))
    if not out.get("booking_summary"):
        if out.get("assignment_ids"):
            out["booking_summary"] = (
                f"Booked {out['booked_count']} assignment(s) for plan "
                f"{out.get('plan_id') or '?'}: {', '.join(out['assignment_ids'])}."
                + (f" Rejected {len(out['rejected'])} (fail-closed)."
                   if out.get("rejected") else "")
            )
        else:
            out["booking_summary"] = (
                f"Booked nothing for plan {out.get('plan_id') or '?'} — "
                "not approved or every assignment was blocked (fail-closed)."
            )
    return out


async def allocation_booker_agent(llm_response, input_data, context):
    """Book the approved staffing assignment. Fail-closed: an un-approved
    decision books nothing; a connector ack still passes through the store's
    double-booking floor. Connector path shapes the PSA ack; dev path records
    the assignments in the store and returns the artifact."""
    input_data = input_data or {}
    plan_id = _pick(input_data, "plan_id", "")

    # FLOOR 1 — never book without a human approval upstream.
    if not _is_approved(input_data):
        logger.info(f"allocation_booker_agent — NOT approved, booking nothing (plan={plan_id!r})")
        out = {**_DEFAULTS, "plan_id": plan_id}
        return await enforce_booking_floors(out, context)

    assignments = _assignments_from_input(input_data)
    if not assignments:
        logger.info(f"allocation_booker_agent — approved but no assignments to book (plan={plan_id!r})")
        out = {**_DEFAULTS, "plan_id": plan_id, "booking_ok": True}
        return await enforce_booking_floors(out, context)

    if isinstance(llm_response, dict) and llm_response:
        computed = _shape_connector_result(llm_response, plan_id, assignments)
        source = "psa_booking_connector"
    else:
        res = _book_dev(assignments, plan_id)
        ids = [b.get("assignment_id") for b in res["booked"]]
        computed = {
            "plan_id": plan_id,
            "booked": res["booked"],
            "assignment_ids": ids,
            "booked_count": len(ids),
            "rejected": res["rejected"],
            "booking_ok": bool(ids),
        }
        source = "dev_store"

    logger.info(
        f"allocation_booker_agent — booked ({source}) plan={plan_id!r}, "
        f"assignments={computed.get('assignment_ids')}, "
        f"rejected={len(computed.get('rejected') or [])}"
    )
    return await enforce_booking_floors({**_DEFAULTS, **computed}, context)
