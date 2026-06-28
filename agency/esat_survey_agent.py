"""esat_survey_agent — salted-hash anonymisation ledger (micro-module).

WHY THIS MUST STAY PYTHON (Rule 2 — ANONYMITY CONTROLS never merge
or move into a prompt): respondent anonymity is enforced
STRUCTURALLY, not behaviourally. A YAML prompt can only *ask* the
LLM not to reveal who said what; this module *guarantees* it:

  · the per-survey salt is generated with `secrets` and persisted
    ONLY in the local ledger — it is never yielded, never logged,
    and never enters mesh state or the LLM context;
  · responder identities exist downstream only as
    sha256(salt + responder_id) — without the salt, a full
    transcript of mesh traffic cannot be joined back to a person,
    even by us.

Cryptographic salt generation and one-way hashing cannot be
expressed in YAML. Everything else the old 224-line module did
(engagement-index math, HRBP threshold flags, response-rate
aggregation, question bank) now lives in the YAML prompt as
explicit instructions.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LEDGER = Path(os.environ.get("ESAT_LEDGER_DB", "./logs/esat_anonymisation.db"))


def _conn() -> sqlite3.Connection:
    _LEDGER.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_LEDGER)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS survey_salts ("
        " survey_id TEXT PRIMARY KEY,"
        " salt TEXT NOT NULL,"
        " launched_at_utc TEXT NOT NULL)"
    )
    return conn


def register_survey(cohort: str) -> tuple[str, str]:
    """Mint a survey identity + per-survey salt. The salt is persisted
    in the ledger and NEVER returned to the caller of the agent."""
    now = datetime.now(timezone.utc)
    survey_id = f"esat-{cohort}-{int(now.timestamp())}"
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO survey_salts VALUES (?, ?, ?)",
            (survey_id, secrets.token_hex(16), now.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    return survey_id, now.isoformat()


def anonymise_responder(survey_id: str, responder_id: str) -> str:
    """Salted SHA-256 of a responder_id — the ONLY form an identity may
    take past this point. Used by the response-ingestion path before
    anything is stored or analysed."""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT salt FROM survey_salts WHERE survey_id = ?",
            (survey_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise KeyError(f"unknown survey {survey_id!r} — launch via mode=pulse first")
    return hashlib.sha256(f"{row[0]}:{responder_id}".encode()).hexdigest()


async def esat_survey_agent(llm_response, input_data, context):
    """Post-processor: on pulse launch, mint the survey identity + salt
    (the LLM is instructed to leave "survey_id" / "launched_at_utc"
    empty — the salt never passes through it). Analysis math lives in
    the YAML prompt; yields enforcement supplies the output shape."""
    if isinstance(llm_response, str):
        try:
            llm_response = json.loads(llm_response)
        except (ValueError, TypeError):
            llm_response = {}
    if not isinstance(llm_response, dict):
        llm_response = {}

    out: dict[str, Any] = dict(llm_response)
    mode = str(input_data.get("mode") or out.get("action") or "").lower()
    if mode == "pulse" and out.get("action") != "skipped":
        cohort = str(input_data.get("cohort") or out.get("cohort") or "bench")
        out["survey_id"], out["launched_at_utc"] = register_survey(cohort)
    return out
