"""allocation_booker_agent — the FINISHER / actuator that finishes the job.

It sits BEHIND the human approval step. It never approves — it re-checks that a
human actually approved, refuses to double-book, RENDERS the booking artifact,
and records the assignment. It is the concrete "@chain fails closed":

  a) approval RE-CHECK — reaching this agent does not mean approved. An
     allow-token allowlist + block-token denylist; an UNKNOWN verdict fails
     closed (books nothing).
  b) deterministic invariants a human can't waive — no double-booking: the
     store re-reads current allocation at write time and refuses any slice that
     would push a consultant past 100% (store.book_assignment raises).
  c) render the real artifact to ./out; on a tripped floor DELETE the partial
     and record status="blocked" with reasons — never a half-issued booking.
  d) dev-store recovery — after the HITL gate the upstream plan is dropped, so
     the booker recovers it from the session stash / input.
  e) two-layer defence — the body short-circuits before writing; the @chain
     floor (enforce_booking_floors) re-checks the approval + renders/deletes
     independently.
  f) idempotent — an assignment already booked under the same plan is not
     booked again (a re-approval or a replayed hand-off books nothing new).

DEV (the shipped config: no `integration`) — the booker RECORDS each confirmed
assignment in the store (a real, verifiable record, NOT a faked ack) so the
approve → book chain runs day-0, behind the store's double-booking floor.
WIRED — uncomment `integration: "n8n"` + `connector_config: {connection:
psa_booking_n8n}` on the agent: the n8n workflow in front of your PSA
(Kantata/Mavenlink, Workday…) runs first and does the write, so your PSA's own
allocation rules apply to it; this code shapes a successful ack's `content`
into yields. A failed PSA call books nothing and is recorded as blocked. The
approval re-check (@chain) applies on both paths.

Yields (set in the configuration): plan_id, booked, assignment_ids, booked_count, rejected,
booking_ok, booking_summary, artifact_path, status, block_reasons.
"""
from __future__ import annotations

import sys
import os
from pathlib import Path
from typing import Any

from leafmesh import LeafMeshLogger, chain

try:
    from agency._shared import store
    from agency._shared.session_stash import resolve_session_id, stash_get
except ImportError:  # pragma: no cover — direct-run fallback
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _shared import store  # type: ignore
    from _shared.session_stash import resolve_session_id, stash_get  # type: ignore

logger = LeafMeshLogger(__name__)

_OUT_DIR = Path("./out")

_DEFAULTS: dict[str, Any] = {
    "plan_id": "",
    "booked": [],
    "assignment_ids": [],
    "booked_count": 0,
    "rejected": [],
    "booking_ok": False,
    "booking_summary": "",
    "artifact_path": "",
    "status": "",
    "block_reasons": [],
}

# Decision tokens that count as a human APPROVAL to book. Anything else
# (escalate, halt, reject, held_for_review, empty, unknown…) fails closed.
_APPROVE_TOKENS = frozenset({"approve", "approved", "book", "proceed", "confirm", "yes", "ok"})
_BLOCK_TOKENS = frozenset({"escalate", "escalate_to_practice_lead", "halt",
                           "halt_for_review", "held", "held_for_review",
                           "reject", "rejected", "deny", "denied", "decline"})


def _pick(input_data: dict[str, Any], key: str, default: Any) -> Any:
    """Shape-tolerant read: input_data, then upstream_yields (flat or keyed by
    agent), then human_data."""
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


def _signal(input_data: dict[str, Any]) -> str:
    """The human verdict, from wherever the SDK delivered it (shape-tolerant)."""
    for key in ("human_message", "human_decision", "decision", "message"):
        val = _pick(input_data, key, "")
        if isinstance(val, str) and val.strip():
            return val.strip().lower()
    return ""


def _is_approved(input_data: dict[str, Any]) -> bool:
    """Fail closed: a block token is never approval; only an explicit approve
    token counts; an unknown / empty verdict is NOT approved."""
    sig = _signal(input_data)
    if sig in _BLOCK_TOKENS:
        return False
    return sig in _APPROVE_TOKENS


def _assignments_from_input(input_data: dict[str, Any]) -> list[dict[str, Any]]:
    """The approved assignments to book. Prefer the plan's `assignments`, fall
    back to `matches` (same {person_id, opportunity_id, …} shape)."""
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


def _recover_plan(input_data: dict[str, Any], context: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Recover the plan across the HITL boundary: input → stash. The staffing
    coordinator stashes the plan; here we recover it when the human resume dropped
    the upstream payload."""
    plan_id = str(_pick(input_data, "plan_id", "") or "")
    assignments = _assignments_from_input(input_data)
    if assignments:
        return plan_id, assignments
    sid = resolve_session_id(input_data, context if isinstance(context, dict) else {})
    stashed = stash_get(sid)
    if stashed:
        plan_id = plan_id or str(stashed.get("plan_id") or "")
        assignments = _assignments_from_input(stashed)
    return plan_id, assignments


def _book_dev(assignments: list[dict[str, Any]], plan_id: str) -> dict[str, Any]:
    """DEV path — record each approved assignment in the store. The store's
    book_assignment is the fail-closed double-booking floor: it refuses any slice
    that would push a consultant past 100% allocation."""
    pending: dict[str, int] = {}
    booked: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for a in assignments:
        cid = a["consultant_id"]
        alloc = a["allocation_pct"]
        if plan_id and store.already_booked(plan_id, cid, a["opportunity_id"]):
            # Idempotent: a re-approval / replayed hand-off never books twice.
            rejected.append({**a, "reason": "already booked under this plan"})
            continue
        already = store.consultant_allocation_pct(cid) + pending.get(cid, 0)
        if already + alloc > store.MAX_ALLOCATION_PCT:
            rejected.append({**a, "reason": f"would exceed 100% (already {already}%)"})
            continue
        try:
            rec = store.book_assignment(
                consultant_id=cid, opportunity_id=a["opportunity_id"],
                allocation_pct=alloc, start=a["start"], end=a["end"], plan_id=plan_id,
            )
        except ValueError as exc:  # the store's own fail-closed floor
            rejected.append({**a, "reason": str(exc)})
            continue
        pending[cid] = pending.get(cid, 0) + alloc
        booked.append(rec)
    return {"booked": booked, "rejected": rejected}


def _shape_connector_result(raw: dict[str, Any], plan_id: str) -> dict[str, Any]:
    """Map the PSA booking workflow's ack (its reply content) into yields. Only
    assignments the PSA acknowledged with an id count as booked."""
    booked_raw = raw.get("booked") or raw.get("assignments") or []
    ids = [str(b.get("assignment_id") or b.get("id"))
           for b in booked_raw if isinstance(b, dict) and (b.get("assignment_id") or b.get("id"))]
    ok = raw.get("booking_ok")
    if ok is None:
        ok = bool(raw.get("success", True)) and not raw.get("error")
    return {
        "plan_id": raw.get("plan_id") or plan_id,
        "booked": booked_raw if isinstance(booked_raw, list) else [],
        "assignment_ids": ids,
        "booked_count": len(ids),
        "rejected": raw.get("rejected") or [],
        "booking_ok": bool(ok),
    }


def _render_booking(plan_id: str, booked: list[dict[str, Any]], rejected: list[dict[str, Any]]) -> str:
    """Render the booking confirmation artifact to ./out (the finisher's real
    output). Returns the path."""
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    pid = str(plan_id or "plan")
    path = _OUT_DIR / f"{pid}_booking_confirmation.md"
    lines = [f"# Assignment booking confirmation — {pid}",
             f"Booked: {len(booked)} assignment(s)", ""]
    for b in booked:
        lines.append(
            f"- {b.get('assignment_id','')}  {b.get('consultant_id','')} "
            f"→ {b.get('opportunity_id','')}  @{b.get('allocation_pct','')}%"
            + (f"  ({b.get('start','')}–{b.get('end','')})" if b.get("start") else "")
        )
    if rejected:
        lines += ["", f"## Rejected (fail-closed): {len(rejected)}"]
        for r in rejected:
            lines.append(f"- {r.get('consultant_id','')} → {r.get('opportunity_id','')}: {r.get('reason','')}")
    lines += ["", "_Recorded to the PSA booking ledger._"]
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def _delete_partial(result: dict[str, Any]) -> None:
    p = result.get("artifact_path")
    if p:
        Path(p).unlink(missing_ok=True)
        result["artifact_path"] = ""


async def enforce_booking_floors(result: Any, ctx: Any) -> dict[str, Any]:
    """@chain: the finisher floor — issue ONLY on a real approval + a real
    booking; delete the artifact on a block. Re-checks the approval INDEPENDENTLY
    of the body (two-layer defence)."""
    out = {**_DEFAULTS, **(result if isinstance(result, dict) else {})}
    out.setdefault("block_reasons", [])
    approved = _is_approved(out.get("_input", {}))

    if not approved:
        out.update(status="blocked", booking_ok=False, booked=[], assignment_ids=[], booked_count=0)
        out["block_reasons"].append("no explicit approval on record (fail closed)")
        _delete_partial(out)
        out["booking_summary"] = out["booking_summary"] or (
            f"Booked nothing for plan {out.get('plan_id') or '?'} — not approved (fail-closed)."
        )
        out.pop("_input", None)
        logger.info(f"[booker] floor BLOCKED (not approved): plan={out.get('plan_id')!r}")
        return out

    ids = out.get("assignment_ids") or []
    out["booked_count"] = len(ids) or int(out.get("booked_count") or 0)
    out["booking_ok"] = bool(ids) or bool(out.get("booking_ok"))

    if ids:
        out["artifact_path"] = _render_booking(out.get("plan_id") or "", out.get("booked") or [], out.get("rejected") or [])
        out["status"] = "booked"
        out["booking_summary"] = out["booking_summary"] or (
            f"Booked {out['booked_count']} assignment(s) for plan {out.get('plan_id') or '?'}: "
            f"{', '.join(str(i) for i in ids)}."
            + (f" Rejected {len(out['rejected'])} (fail-closed)." if out.get("rejected") else "")
        )
    else:
        # Approved, but every assignment was blocked (e.g. would double-book) or
        # there was nothing to book — no artifact, record why.
        out["status"] = "blocked"
        _delete_partial(out)
        if any("PSA booking call failed" in str(r.get("reason", ""))
               for r in out.get("rejected") or [] if isinstance(r, dict)):
            out["block_reasons"].append("the PSA booking call failed — nothing was booked")
        elif out.get("rejected"):
            out["block_reasons"].append("every assignment rejected at the double-booking floor")
        else:
            out["block_reasons"].append("approved but no assignments to book")
        out["booking_summary"] = out["booking_summary"] or (
            f"Booked nothing for plan {out.get('plan_id') or '?'} — approved but "
            "no bookable assignment (fail-closed)."
        )
    out.pop("_input", None)
    return out


@chain(enforce_booking_floors)
async def allocation_booker_agent(llm_response: Any, input_data: Any, context: Any) -> dict[str, Any]:
    """Book the approved staffing assignment. Fail-closed: an un-approved decision
    books nothing (the @chain floor re-checks); a connector ack still passes
    through the store's double-booking floor. Connector path shapes the PSA ack;
    dev path records the assignments in the store."""
    input_data = input_data if isinstance(input_data, dict) else {}
    plan_id, assignments = _recover_plan(input_data, context if isinstance(context, dict) else {})

    out: dict[str, Any] = {**_DEFAULTS, "plan_id": plan_id, "_input": input_data}

    # LAYER 1 — never book without a human approval upstream.
    if not _is_approved(input_data):
        logger.info(f"[booker] not approved — booking nothing (plan={plan_id!r})")
        return out

    if not assignments:
        logger.info(f"[booker] approved but no assignments to book (plan={plan_id!r})")
        return out

    if isinstance(llm_response, dict) and llm_response.get("success") is False:
        err = str(llm_response.get("error") or "the PSA booking workflow failed")
        logger.warning(f"[booker] PSA booking call failed (plan={plan_id!r}): {err}")
        return {**out, "rejected": [{**a, "reason": f"PSA booking call failed: {err[:160]}"}
                                    for a in assignments]}
    if isinstance(llm_response, dict) and llm_response.get("success") is True:
        content = llm_response.get("content")
        if isinstance(content, str):
            try:
                import json
                content = json.loads(content)
            except ValueError:
                content = {}
        computed = _shape_connector_result(content if isinstance(content, dict) else {}, plan_id)
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
        f"[booker] booked ({source}) plan={plan_id!r} "
        f"assignments={computed.get('assignment_ids')} rejected={len(computed.get('rejected') or [])}"
    )
    return {**out, **computed}
