"""Rank the user's projects for one JD - the step that decides what a tailored
resume leads with.

Three signals, each normalized to 0-1 across the user's own projects, and each
reported back so the choice is explainable and overridable:

  semantic  cosine between the JD's stored embedding and the project's
            kb_chunk embedding. Catches relevance keyword matching misses
            ("observability" vs "OpenTelemetry tracing rollout"). Costs no API
            call: both vectors already exist.
  keyword   weighted overlap between the JD's skills/responsibilities (plus
            the matcher's confirmed skills) and the project's text. Catches
            exact-technology asks the embedding blurs ("Kafka", "Go").
  priority  the user's own score (0-5) and favourite flag. The user knows
            which projects are their best work; the ranker should not bury a
            personal-best project because it shares fewer keywords.

The top of the list is only a default. The resume page shows the ranking and
lets the user pick exactly which projects to use (resume_tailor accepts
project_ids and then does not second-guess the selection).
"""
from __future__ import annotations

import re
import uuid

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

WEIGHT_SEMANTIC = 0.45
WEIGHT_KEYWORD = 0.40
WEIGHT_PRIORITY = 0.15
FAVOURITE_BONUS = 0.4          # added to the priority signal before capping at 1.0
DEFAULT_PICK = 4


class ProjectRank(BaseModel):
    project_id: uuid.UUID
    title: str
    score: float                # 0-1 blended
    semantic: float             # 0-1 normalized
    keyword: float              # 0-1 normalized
    priority_signal: float      # 0-1
    semantic_raw: float | None  # raw cosine, for display
    matched_terms: list[str]    # JD terms found in the project
    priority: int
    is_favourite: bool
    tech_stack: list[str]
    has_resume_bullets: bool
    recommended: bool = False


def jd_terms(requirements: dict, match_result: dict | None) -> dict[str, float]:
    """Weighted JD vocabulary. Confirmed matched skills rank highest - those
    are what the resume can honestly lead with."""
    terms: dict[str, float] = {}

    def add(name, weight):
        key = (name or "").strip().lower()
        if len(key) >= 2:
            terms[key] = max(terms.get(key, 0.0), weight)

    for s in requirements.get("required_skills") or []:
        if isinstance(s, dict):
            add(s.get("name"), 3.0 * float(s.get("weight") or 1.0))
    for s in requirements.get("nice_to_have_skills") or []:
        if isinstance(s, dict):
            add(s.get("name"), 1.5 * float(s.get("weight") or 0.5))
    for s in (match_result or {}).get("matched_skills") or []:
        add(s, 4.0)
    for line in requirements.get("responsibilities") or []:
        for tok in re.findall(r"[A-Za-z][A-Za-z+#.\-]{3,}", str(line)):
            add(tok, 0.4)
    return terms


def keyword_hits(haystack: str, terms: dict[str, float]) -> tuple[float, list[str]]:
    """Word-boundary matches, so 'go' does not hit 'google'."""
    hay = haystack.lower()
    score, hits = 0.0, []
    for term, weight in terms.items():
        if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", hay):
            score += weight
            if weight >= 1.0:
                hits.append(term)
    return score, hits


def _normalize(values: list[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [1.0 if hi > 0 else 0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def blend(rows: list[dict], terms: dict[str, float], pick: int = DEFAULT_PICK) -> list[ProjectRank]:
    """Pure scoring over pre-loaded rows (unit-testable without a DB)."""
    kw_raw, matched = [], []
    for r in rows:
        hay = " ".join([r["title"], r.get("summary") or "", r.get("description") or "",
                        " ".join(r["tech_stack"]), " ".join(r["key_points"]),
                        " ".join(r["resume_bullets"])])
        s, hits = keyword_hits(hay, terms)
        kw_raw.append(s)
        matched.append(hits)
    sem_raw = [r.get("semantic") for r in rows]
    sem_norm = _normalize([s if s is not None else min((x for x in sem_raw if x is not None), default=0.0)
                           for s in sem_raw])
    kw_norm = _normalize(kw_raw)

    ranked = []
    for r, sem, kw, hits, raw in zip(rows, sem_norm, kw_norm, matched, sem_raw):
        pri = min(1.0, r["priority"] / 5 + (FAVOURITE_BONUS if r["is_favourite"] else 0.0))
        score = WEIGHT_SEMANTIC * sem + WEIGHT_KEYWORD * kw + WEIGHT_PRIORITY * pri
        # A project with nothing written about it cannot produce bullets.
        if not (r["key_points"] or r["resume_bullets"] or len(r.get("description") or "") > 80):
            score *= 0.6
        ranked.append(ProjectRank(
            project_id=r["id"], title=r["title"], score=round(score, 4),
            semantic=round(sem, 4), keyword=round(kw, 4), priority_signal=round(pri, 4),
            semantic_raw=round(raw, 4) if raw is not None else None,
            matched_terms=sorted(set(hits))[:12], priority=r["priority"],
            is_favourite=r["is_favourite"], tech_stack=r["tech_stack"],
            has_resume_bullets=bool(r["resume_bullets"]),
        ))
    ranked.sort(key=lambda p: (-p.score, p.title.lower()))
    for p in ranked[:pick]:
        p.recommended = True
    return ranked


def rank_projects_for_jd(session: Session, *, user_id, jd_id, pick: int = DEFAULT_PICK) -> list[ProjectRank]:
    """Every verified project, ranked for this JD."""
    jd = session.execute(
        text("SELECT parsed_requirements FROM job_description WHERE id = :id AND user_id = :u"),
        {"id": jd_id, "u": user_id},
    ).first()
    if not jd:
        raise LookupError(f"job description {jd_id} not found")
    match = session.execute(
        text("SELECT matched_skills FROM match_result WHERE jd_id = :id AND user_id = :u "
             "ORDER BY created_at DESC LIMIT 1"),
        {"id": jd_id, "u": user_id},
    ).first()

    rows = session.execute(
        text("""
            SELECT p.id, p.title, p.summary, p.description, p.tech_stack, p.key_points,
                   p.resume_bullets, p.priority, p.is_favourite,
                   CASE WHEN k.embedding IS NOT NULL AND jd.embedding IS NOT NULL
                        THEN 1 - (k.embedding <=> jd.embedding) END AS semantic
            FROM project p
            JOIN job_description jd ON jd.id = :jd
            LEFT JOIN kb_chunk k ON k.source_table = 'project' AND k.source_id = p.id
            WHERE p.user_id = :u AND p.status = 'verified'
        """),
        {"jd": jd_id, "u": user_id},
    ).mappings().fetchall()
    rows = [{**dict(r), "tech_stack": list(r["tech_stack"] or []), "key_points": list(r["key_points"] or []),
             "resume_bullets": list(r["resume_bullets"] or [])} for r in rows]
    terms = jd_terms(jd[0] or {}, {"matched_skills": list(match[0] or [])} if match else None)
    return blend(rows, terms, pick=pick)
