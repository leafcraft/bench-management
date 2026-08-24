"""Dev-mode store for bench management.

Stands in for the firm's PSA / HRIS (Kantata/Mavenlink, Workday, SAP
SuccessFactors, BambooHR…) so the full forecast → match → allocate →
talent-development chain runs day-0, before any connector is wired. It
holds the four feeds the roles reason over:

  * the **bench roster** — who is available, their level, their idle
    weeks (workforce analytics + talent development read this);
  * the **skills inventory** — per-consultant skills with level + last
    used (staffing coordinator matches against this);
  * the **open opportunities / demand** — engagements needing staffing,
    with required skills + headcount (coordinator matches into this);
  * the **utilisation history** — weekly billable / available hours per
    consultant (analytics derives the below-floor flag + bench %).

Production: replace each reader with the matching PSA/HRIS pull behind
the n8n connector (PSA_WEBHOOK_URL); the function signatures are the
contract. Setting the env var alone does nothing until the workforce
analytics shaper routes the connector's raw response through here.

This is a DEV STAND-IN, not a system of record (Rule 7): nothing here
is the firm's authoritative state. Writes (utilisation ingest, skill
upsert) persist to ./data so a multi-call demo is coherent; delete
./data to reseed.
"""
from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_DIR = Path("./data")

# Billable-utilisation floor: under this for WEEKS_BELOW_FLOOR+ recent
# weeks → the consultant needs RM action (the staffing partner). This is
# the deterministic floor the analytics shaper enforces — not an n8n
# workflow's private rule and not a prompt the LLM can soften.
UTIL_FLOOR_PCT = 0.65
WEEKS_BELOW_FLOOR = 3

# Bench-percentage target ceiling — over this and the practice lead must
# hear about it (the visible can_call wire keys on the value this floor
# computes).
BENCH_CEILING_PCT = 0.15

# Idle thresholds (weeks on the bench with no billable engagement).
IDLE_2W = 2
IDLE_4W = 4

# Allocation ceiling — a consultant can never be booked above 100% across
# overlapping assignments. The allocation booker re-checks this against the
# CURRENT confirmed bookings before it writes (fail-closed double-booking
# floor) — it is computed here, not asked of an LLM.
MAX_ALLOCATION_PCT = 100

_LEVELS = {"junior", "mid", "senior", "staff", "partner"}


# ── Seed data ────────────────────────────────────────────────────────
# A realistic ~14-person services bench. idle_weeks > 0 means currently
# benched; on_engagement names the active engagement (or "" if benched).
_SEED_ROSTER = [
    {"consultant_id": "c-lina-2021-007",   "name": "Lina Okafor",   "level": "senior",  "idle_weeks": 2, "on_engagement": ""},
    {"consultant_id": "c-prashant-2024-009","name": "Prashant Rao",  "level": "mid",     "idle_weeks": 5, "on_engagement": ""},
    {"consultant_id": "c-mei-2022-014",    "name": "Mei Tanaka",    "level": "staff",   "idle_weeks": 0, "on_engagement": "acme-data-platform"},
    {"consultant_id": "c-diego-2023-021",  "name": "Diego Alvarez", "level": "mid",     "idle_weeks": 4, "on_engagement": ""},
    {"consultant_id": "c-sara-2020-003",   "name": "Sara Klein",    "level": "senior",  "idle_weeks": 1, "on_engagement": ""},
    {"consultant_id": "c-tom-2024-031",    "name": "Tom Becker",    "level": "junior",  "idle_weeks": 6, "on_engagement": ""},
    {"consultant_id": "c-amara-2021-011",  "name": "Amara Nwosu",   "level": "staff",   "idle_weeks": 0, "on_engagement": "globex-cloud-migration"},
    {"consultant_id": "c-leo-2023-018",    "name": "Leo Marchetti", "level": "mid",     "idle_weeks": 3, "on_engagement": ""},
    {"consultant_id": "c-fatima-2022-006", "name": "Fatima Saleh",  "level": "senior",  "idle_weeks": 0, "on_engagement": "initech-ml-pilot"},
    {"consultant_id": "c-noah-2024-040",   "name": "Noah Park",     "level": "junior",  "idle_weeks": 2, "on_engagement": ""},
    {"consultant_id": "c-yara-2021-009",   "name": "Yara Haddad",   "level": "mid",     "idle_weeks": 0, "on_engagement": "acme-data-platform"},
    {"consultant_id": "c-ivan-2020-002",   "name": "Ivan Petrov",   "level": "partner", "idle_weeks": 0, "on_engagement": "umbrella-strategy"},
    {"consultant_id": "c-grace-2023-025",  "name": "Grace Liu",     "level": "mid",     "idle_weeks": 1, "on_engagement": ""},
    {"consultant_id": "c-omar-2022-013",   "name": "Omar Farouk",   "level": "senior",  "idle_weeks": 0, "on_engagement": "globex-cloud-migration"},
]

# Skills inventory — consultant_id → [{skill, level, last_used}].
_SEED_SKILLS = {
    "c-lina-2021-007":    [{"skill": "data-engineering", "level": "expert", "last_used": "2026-05"},
                           {"skill": "python", "level": "expert", "last_used": "2026-06"},
                           {"skill": "spark", "level": "advanced", "last_used": "2026-04"}],
    "c-prashant-2024-009":[{"skill": "react", "level": "advanced", "last_used": "2026-03"},
                           {"skill": "typescript", "level": "advanced", "last_used": "2026-03"}],
    "c-diego-2023-021":   [{"skill": "python", "level": "intermediate", "last_used": "2026-02"},
                           {"skill": "data-engineering", "level": "intermediate", "last_used": "2026-01"}],
    "c-sara-2020-003":    [{"skill": "cloud-architecture", "level": "expert", "last_used": "2026-05"},
                           {"skill": "kubernetes", "level": "advanced", "last_used": "2026-05"},
                           {"skill": "aws", "level": "expert", "last_used": "2026-06"}],
    "c-tom-2024-031":     [{"skill": "javascript", "level": "intermediate", "last_used": "2026-04"}],
    "c-leo-2023-018":     [{"skill": "ml-engineering", "level": "advanced", "last_used": "2026-04"},
                           {"skill": "python", "level": "advanced", "last_used": "2026-05"}],
    "c-noah-2024-040":    [{"skill": "qa-automation", "level": "intermediate", "last_used": "2026-03"}],
    "c-grace-2023-025":   [{"skill": "data-engineering", "level": "advanced", "last_used": "2026-05"},
                           {"skill": "sql", "level": "advanced", "last_used": "2026-06"}],
}

# Open opportunities / demand — engagements needing staffing.
_SEED_OPPORTUNITIES = [
    {"opportunity_id": "opp-acme-phase2", "client": "Acme", "title": "Data platform phase 2",
     "required_skills": ["data-engineering", "spark"], "headcount": 2, "start": "2026-07", "status": "open"},
    {"opportunity_id": "opp-wayne-cloud", "client": "Wayne", "title": "Cloud migration",
     "required_skills": ["cloud-architecture", "kubernetes"], "headcount": 1, "start": "2026-07", "status": "open"},
    {"opportunity_id": "opp-stark-ml", "client": "Stark", "title": "ML pipeline build",
     "required_skills": ["ml-engineering", "python"], "headcount": 1, "start": "2026-08", "status": "open"},
    {"opportunity_id": "opp-hooli-rt", "client": "Hooli", "title": "Realtime analytics",
     "required_skills": ["data-engineering", "spark"], "headcount": 1, "start": "2026-07", "status": "open"},
]

# Utilisation history — consultant_id → [{week_ending, billable_hours,
# available_hours}], most recent last. Benched consultants show low
# billable for the recent weeks (drives the below-floor flag).
def _hist(weeks: list[tuple[str, float, float]]) -> list[dict[str, Any]]:
    return [{"week_ending": w, "billable_hours": b, "available_hours": a} for w, b, a in weeks]


_SEED_UTILISATION = {
    # Mei — fully utilised on Acme.
    "c-mei-2022-014": _hist([("2026-06-05", 38, 40), ("2026-06-12", 40, 40), ("2026-06-19", 39, 40)]),
    # Lina — recently rolled off, dipping below floor 2 weeks.
    "c-lina-2021-007": _hist([("2026-06-05", 36, 40), ("2026-06-12", 18, 40), ("2026-06-19", 10, 40)]),
    # Prashant — long below floor (5 weeks idle), front-line below-floor case.
    "c-prashant-2024-009": _hist([("2026-06-05", 8, 40), ("2026-06-12", 4, 40), ("2026-06-19", 6, 40)]),
    # Diego — below floor 4 weeks.
    "c-diego-2023-021": _hist([("2026-06-05", 12, 40), ("2026-06-12", 10, 40), ("2026-06-19", 8, 40)]),
    # Amara — fully utilised.
    "c-amara-2021-011": _hist([("2026-06-05", 40, 40), ("2026-06-12", 38, 40), ("2026-06-19", 40, 40)]),
    # Tom — deep bench (junior, 6 weeks).
    "c-tom-2024-031": _hist([("2026-06-05", 2, 40), ("2026-06-12", 0, 40), ("2026-06-19", 4, 40)]),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(k: str) -> Path:
    _DIR.mkdir(parents=True, exist_ok=True)
    return _DIR / f"{k}.json"


def _load(k: str) -> Any:
    p = _path(k)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (ValueError, OSError):
        return None


def _save(k: str, d: Any) -> None:
    p = _path(k)
    t = p.with_suffix(".json.tmp")
    t.write_text(json.dumps(d, indent=2, default=str))
    t.replace(p)


# ── Roster ───────────────────────────────────────────────────────────
def _roster() -> dict[str, dict[str, Any]]:
    s = _load("roster")
    if not s:
        s = {r["consultant_id"]: dict(r) for r in _SEED_ROSTER}
        _save("roster", s)
    return s


def bench_roster(only_available: bool = False) -> list[dict[str, Any]]:
    """The bench roster. only_available → just the benched (no active
    engagement). PROD: PSA/HRIS roster pull."""
    rows = list(_roster().values())
    if only_available:
        rows = [r for r in rows if not r.get("on_engagement")]
    rows.sort(key=lambda r: (-int(r.get("idle_weeks", 0)), r.get("name", "")))
    return rows


def get_consultant(consultant_id: str) -> dict[str, Any]:
    return dict(_roster().get(consultant_id, {}))


# ── Skills inventory ─────────────────────────────────────────────────
def _skills() -> dict[str, list[dict[str, Any]]]:
    s = _load("skills")
    if s is None:
        s = {k: list(v) for k, v in _SEED_SKILLS.items()}
        _save("skills", s)
    return s


def skills_inventory() -> dict[str, list[dict[str, Any]]]:
    """consultant_id → skill records. PROD: PSA/HRIS skills pull."""
    return _skills()


def consultant_skills(consultant_id: str) -> list[dict[str, Any]]:
    return list(_skills().get(consultant_id, []))


def upsert_skill(consultant_id: str, skill: str, level: str = "intermediate",
                 last_used: str = "") -> dict[str, Any]:
    """Insert / update one skill record for one consultant. PROD: PSA write."""
    with _LOCK:
        s = _skills()
        recs = s.setdefault(consultant_id, [])
        for r in recs:
            if r.get("skill") == skill:
                r["level"] = level
                r["last_used"] = last_used or r.get("last_used", "")
                _save("skills", s)
                return dict(r)
        rec = {"skill": skill, "level": level, "last_used": last_used or _now()[:7]}
        recs.append(rec)
        _save("skills", s)
    return dict(rec)


def search_skills(needed_skills: list[str], min_level: str = "intermediate") -> dict[str, Any]:
    """For each needed skill, who has it at min_level+. Returns coverage +
    gaps. PROD: PSA skills search. The level ordering is the deterministic
    skills-match gate the LLM cannot soften."""
    order = ["beginner", "intermediate", "advanced", "expert"]
    floor = order.index(min_level) if min_level in order else 1
    inv = _skills()
    needed = [s for s in (needed_skills or []) if s]
    matches: dict[str, list[str]] = {}
    for skill in needed:
        holders = []
        for cid, recs in inv.items():
            for r in recs:
                lvl = r.get("level", "beginner")
                if r.get("skill") == skill and lvl in order and order.index(lvl) >= floor:
                    holders.append(cid)
                    break
        matches[skill] = holders
    gaps = [s for s in needed if not matches.get(s)]
    coverage_pct = round((len(needed) - len(gaps)) / len(needed), 3) if needed else 1.0
    return {"matches_per_skill": matches, "gaps": gaps, "coverage_pct": coverage_pct,
            "min_level": min_level, "distinct_skills": len(needed)}


# ── Opportunities / demand ───────────────────────────────────────────
def _opportunities() -> dict[str, dict[str, Any]]:
    s = _load("opportunities")
    if not s:
        s = {o["opportunity_id"]: dict(o) for o in _SEED_OPPORTUNITIES}
        _save("opportunities", s)
    return s


def open_opportunities() -> list[dict[str, Any]]:
    """Engagements needing staffing. PROD: PSA pipeline / CRM pull."""
    return [o for o in _opportunities().values() if o.get("status") == "open"]


# ── Utilisation history ──────────────────────────────────────────────
def _utilisation() -> dict[str, list[dict[str, Any]]]:
    s = _load("utilisation")
    if s is None:
        s = {k: list(v) for k, v in _SEED_UTILISATION.items()}
        _save("utilisation", s)
    return s


def utilisation_history(consultant_id: str = "") -> Any:
    if consultant_id:
        return list(_utilisation().get(consultant_id, []))
    return _utilisation()


def ingest_utilisation(consultant_id: str, week_ending: str,
                       billable_hours: float, available_hours: float = 40.0) -> dict[str, Any]:
    """Record one week's utilisation for one consultant. PROD: PSA write."""
    with _LOCK:
        u = _utilisation()
        rows = u.setdefault(consultant_id, [])
        rec = {"week_ending": week_ending, "billable_hours": float(billable_hours),
               "available_hours": float(available_hours)}
        rows = [r for r in rows if r.get("week_ending") != week_ending]
        rows.append(rec)
        rows.sort(key=lambda r: r.get("week_ending", ""))
        u[consultant_id] = rows
        _save("utilisation", u)
    return rec


# ── Derived analytics (the deterministic floors) ─────────────────────
def _util_pct(rows: list[dict[str, Any]]) -> float:
    rows = rows or []
    avail = sum(float(r.get("available_hours", 0)) for r in rows)
    bill = sum(float(r.get("billable_hours", 0)) for r in rows)
    return round(bill / avail, 3) if avail else 0.0


def below_floor_consultants() -> list[dict[str, Any]]:
    """Consultants under UTIL_FLOOR_PCT for the most-recent WEEKS_BELOW_FLOOR
    weeks. This is the enforced utilisation floor — it is computed here, NOT
    asked of an LLM and NOT hidden inside an n8n workflow."""
    out: list[dict[str, Any]] = []
    util = _utilisation()
    for cid, rows in util.items():
        recent = sorted(rows, key=lambda r: r.get("week_ending", ""))[-WEEKS_BELOW_FLOOR:]
        if len(recent) < WEEKS_BELOW_FLOOR:
            continue
        if all(_util_pct([r]) < UTIL_FLOOR_PCT for r in recent):
            cons = get_consultant(cid)
            out.append({"consultant_id": cid, "name": cons.get("name", ""),
                        "level": cons.get("level", ""),
                        "weeks_below_floor": WEEKS_BELOW_FLOOR,
                        "recent_utilisation_pct": _util_pct(recent)})
    out.sort(key=lambda r: r.get("recent_utilisation_pct", 0))
    return out


def bench_pct() -> float:
    """Fraction of the roster currently benched (no active engagement).
    Drives the bench_pct_current > BENCH_CEILING_PCT can_call wire."""
    rows = list(_roster().values())
    if not rows:
        return 0.0
    benched = [r for r in rows if not r.get("on_engagement")]
    return round(len(benched) / len(rows), 3)


def overall_utilisation() -> float:
    """Bench-wide billable utilisation over the tracked history."""
    util = _utilisation()
    allrows = [r for rows in util.values() for r in rows]
    return _util_pct(allrows)


def idle_ledger() -> dict[str, Any]:
    """Idle detection from the roster's idle_weeks. Returns the 2-week and
    4-week idle lists + the average — the deterministic idle floor the
    talent-development chain enforces (the LLM echoes, it does not invent)."""
    rows = list(_roster().values())
    idle = [r for r in rows if int(r.get("idle_weeks", 0)) >= IDLE_2W]
    idle_2w = [{"consultant_id": r["consultant_id"], "name": r.get("name", ""),
                "level": r.get("level", ""), "idle_weeks": int(r.get("idle_weeks", 0))}
               for r in idle if int(r.get("idle_weeks", 0)) >= IDLE_2W]
    idle_4w = [r for r in idle_2w if r["idle_weeks"] >= IDLE_4W]
    benched = [r for r in rows if int(r.get("idle_weeks", 0)) > 0]
    avg = round(sum(int(r.get("idle_weeks", 0)) for r in benched) / len(benched), 2) if benched else 0.0
    return {"idle_2w": idle_2w, "idle_4w": idle_4w, "avg_idle_weeks": avg}


# ── Confirmed staffing assignments (the executor's artifact ledger) ──
# The allocation booker is an ACTUATOR: after a human approves a staffing
# decision it BOOKS the assignment in the PSA (person → project → dates →
# allocation%) and updates utilisation. PROD: the booking goes to the PSA
# behind PSA_BOOKING_WEBHOOK_URL; DEV (no connector) it is recorded here so
# the approval → book chain runs day-0 and produces a real artifact.
#
# Rule 7: this is a DEV STAND-IN, not the firm's system of record. Delete
# ./data to reseed.
def _assignments() -> dict[str, dict[str, Any]]:
    s = _load("assignments")
    if s is None:
        s = {}
        _save("assignments", s)
    return s


def consultant_allocation_pct(consultant_id: str) -> int:
    """Total allocation% a consultant is ALREADY booked at across confirmed,
    overlapping assignments. The double-booking floor (a new booking may not
    push past MAX_ALLOCATION_PCT) keys on this — computed from the ledger,
    NOT trusted from an LLM or a connector ack."""
    if not consultant_id:
        return 0
    total = 0
    for a in _assignments().values():
        if a.get("consultant_id") == consultant_id and a.get("status") == "booked":
            total += int(a.get("allocation_pct", 0))
    return total


def consultant_availability(consultant_id: str) -> dict[str, Any]:
    """Remaining bookable capacity for a consultant. The booker reuses this
    as the fail-closed availability check before it writes."""
    booked = consultant_allocation_pct(consultant_id)
    return {
        "consultant_id": consultant_id,
        "booked_pct": booked,
        "available_pct": max(0, MAX_ALLOCATION_PCT - booked),
        "max_pct": MAX_ALLOCATION_PCT,
    }


def assignment(assignment_id: str) -> dict[str, Any]:
    """Look up one confirmed assignment (idempotency / verification)."""
    return dict(_assignments().get(assignment_id, {}))


def book_assignment(consultant_id: str, opportunity_id: str,
                    allocation_pct: int = 100, start: str = "", end: str = "",
                    plan_id: str = "") -> dict[str, Any]:
    """Record a confirmed staffing assignment and update utilisation.

    The deterministic double-booking floor lives HERE: it re-reads the
    consultant's CURRENT confirmed allocation from the ledger and refuses
    to book when the new slice would push past MAX_ALLOCATION_PCT (or when
    inputs are missing / non-positive). Fail-closed: it raises ValueError
    rather than writing a bad assignment. PROD: the PSA write performs the
    same check; this is the dev stand-in.

    On success: persists the assignment, marks the roster row as engaged,
    and appends a fully-billable utilisation week so the booking shows up in
    the analytics floor. Returns the assignment record (with assignment_id).
    """
    consultant_id = (consultant_id or "").strip()
    opportunity_id = (opportunity_id or "").strip()
    try:
        alloc = int(allocation_pct)
    except (TypeError, ValueError):
        raise ValueError(f"allocation_pct must be an integer, got {allocation_pct!r}")

    if not consultant_id or not opportunity_id:
        raise ValueError("book_assignment requires both consultant_id and opportunity_id")
    if alloc <= 0 or alloc > MAX_ALLOCATION_PCT:
        raise ValueError(f"allocation_pct {alloc} out of range (1..{MAX_ALLOCATION_PCT})")

    with _LOCK:
        already = consultant_allocation_pct(consultant_id)
        if already + alloc > MAX_ALLOCATION_PCT:
            raise ValueError(
                f"double-book: {consultant_id} already booked at {already}% — "
                f"+{alloc}% would exceed {MAX_ALLOCATION_PCT}%"
            )

        assignment_id = f"asg-{secrets.token_hex(5)}"
        rec = {
            "assignment_id": assignment_id,
            "consultant_id": consultant_id,
            "opportunity_id": opportunity_id,
            "allocation_pct": alloc,
            "start": start,
            "end": end,
            "plan_id": plan_id,
            "status": "booked",
            "booked_at_utc": _now(),
        }
        a = _assignments()
        a[assignment_id] = rec
        _save("assignments", a)

        # Update the roster — the consultant is now engaged on this opportunity.
        roster = _roster()
        row = roster.get(consultant_id)
        if row is not None:
            row["on_engagement"] = opportunity_id
            row["idle_weeks"] = 0
            _save("roster", roster)

        # Update utilisation — a booked allocation% is billable capacity, so
        # the analytics below-floor flag clears for this consultant.
        week = _now()[:10]
        u = _utilisation()
        rows = u.setdefault(consultant_id, [])
        billable = round(40.0 * alloc / 100.0, 1)
        rows = [r for r in rows if r.get("week_ending") != week]
        rows.append({"week_ending": week, "billable_hours": billable,
                     "available_hours": 40.0})
        rows.sort(key=lambda r: r.get("week_ending", ""))
        u[consultant_id] = rows
        _save("utilisation", u)

    return dict(rec)


# ── Candidate-screening claim queue (the atomic backlog for instances) ───────
# When candidate_screening_agent fans out into N parallel instances, each copy
# must screen a DISJOINT batch of benched consultants — otherwise they duplicate
# work (the doctrine's "without a claim tool copies duplicate work"). The queue
# is per-session, seeded once from the benched roster, and popped atomically
# under a lock so concurrent instances never claim the same consultant. This is
# ephemeral working state (in-process, per activation), NOT a system of record —
# so it lives in memory, keyed by session_id, and is rebuilt each activation.
_SCREEN_LOCK = threading.Lock()
_SCREEN_QUEUES: dict[str, list[dict[str, Any]]] = {}
_SCREEN_SEEDED: set[str] = set()


def ensure_screening_queue(session_id: str, only_available: bool = True) -> int:
    """Idempotently seed the per-session screening backlog from the benched
    roster (with each consultant's skills attached for scoring). Safe to call
    from every instance's @pre_compose — only the first seeds; returns the
    number of consultants currently waiting to be screened."""
    if not session_id:
        return 0
    with _SCREEN_LOCK:
        if session_id in _SCREEN_SEEDED:
            return len(_SCREEN_QUEUES.get(session_id, []))
        queue = [
            {"consultant_id": r["consultant_id"], "name": r.get("name", ""),
             "level": r.get("level", ""), "idle_weeks": int(r.get("idle_weeks", 0)),
             "skills": [s.get("skill") for s in consultant_skills(r["consultant_id"])]}
            for r in bench_roster(only_available=only_available)
        ]
        _SCREEN_QUEUES[session_id] = queue
        _SCREEN_SEEDED.add(session_id)
        return len(queue)


def claim_screening_batch(session_id: str, batch_size: int = 4) -> list[dict[str, Any]]:
    """Atomically claim (pop) up to `batch_size` benched consultants from the
    session's screening backlog. Concurrent instances serialise on the lock, so
    each pulls a disjoint batch; an empty list means the backlog is drained."""
    if not session_id:
        return []
    try:
        n = max(1, int(batch_size))
    except (TypeError, ValueError):
        n = 1
    with _SCREEN_LOCK:
        queue = _SCREEN_QUEUES.get(session_id) or []
        batch, _SCREEN_QUEUES[session_id] = queue[:n], queue[n:]
        return [dict(c) for c in batch]


def screening_remaining(session_id: str) -> int:
    """How many consultants are still waiting to be screened this session."""
    if not session_id:
        return 0
    with _SCREEN_LOCK:
        return len(_SCREEN_QUEUES.get(session_id) or [])
