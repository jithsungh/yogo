"""Builds the tailored resume data dict for one JD.

What this does that a plain "rewrite my bullets" pass does not:

  1. DEDUPES the project list. A GitHub import and a hand-written entry for the
     same repo are two rows in `project` but one project in real life; shipping
     both put "Resume Parser" on the page twice.
  2. SELECTS. Twenty projects is not a resume. Candidates are pre-filtered by
     keyword overlap with the JD, then the model picks and ranks a handful.
  3. BUDGETS LINE LENGTH. Bullets are generated against a measured character
     budget and any that still overflow get one repair pass, because a bullet
     that wraps leaves a two-word orphan line and that is what makes a
     generated resume look generated.
  4. VALIDATES against the source. Every project id, tech item and skill name
     the model returns is intersected with what it was given; anything it
     invented is dropped rather than trusted.

The model's job is strictly to reorder, rephrase, trim and choose. It is never
the source of a fact. Step 4 is what enforces that, not the prompt alone.
"""
from __future__ import annotations

import re
import uuid
from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.llm.gemini_client import generate_json
from app.services.latex_escape import latex_escape, latex_escape_url
from app.services.resume_layout import (
    BULLET_MAX_CHARS,
    BULLET_MIN_CHARS,
    audit_bullets,
    fit_tech_stack,
    normalize_ws,
)

# Bump when the prompt or assembly logic changes in a way that should
# invalidate previously cached resumes. Folded into the resume cache key.
TAILOR_VERSION = "3.2"

MAX_PROJECTS = 4          # how many projects land on the page
PROJECT_CANDIDATES = 8    # how many the model gets to choose among
MAX_EXPERIENCE_BULLETS = 4
MAX_PROJECT_BULLETS = 3


# ═══════════════════════════════════════════════════════════════════════════
# Public entry points
# ═══════════════════════════════════════════════════════════════════════════

def collect_source_data(session: Session, *, user_id: uuid.UUID, jd_id: uuid.UUID) -> dict:
    """Every input the resume is derived from, and nothing else.

    Split out from generation so the caching layer can fingerprint the inputs
    without paying for an LLM call to discover they have not changed.
    """
    jd_row = session.execute(
        text("SELECT company, role_title, parsed_requirements "
             "FROM job_description WHERE id = :id AND user_id = :uid"),
        {"id": jd_id, "uid": user_id},
    ).mappings().first()
    if not jd_row:
        raise ValueError(f"No job description {jd_id} for this user")

    profile = _load_profile(session, user_id)
    if not profile.get("full_name"):
        raise ValueError(
            "No profile on file for this user - a resume needs at least a name. "
            "Fill in the Profile page first."
        )

    return {
        "profile": profile,
        "experiences": _load_experiences(session, user_id),
        "projects": _dedupe_projects(_load_projects(session, user_id)),
        "skills": _load_skills(session, user_id),
        "education": _load_education(session, user_id),
        "certifications": _load_certifications(session, user_id),
        "match_result": _load_match_result(session, jd_id, user_id),
        "jd": {
            "company": jd_row["company"] or "",
            "role_title": jd_row["role_title"] or "",
            "requirements": jd_row["parsed_requirements"] or {},
        },
    }


def build_tailored_resume_data(
    session: Session,
    *,
    user_id: uuid.UUID,
    jd_id: uuid.UUID,
    source: dict | None = None,
    max_projects: int = MAX_PROJECTS,
    project_ids: list | None = None,
) -> tuple[dict, dict]:
    """Return (render_data, report).

    `render_data` goes straight to render_latex(); every string in it is
    already LaTeX-escaped. `report` carries what the UI needs to show about
    how the resume was built - which projects were chosen and why, and any
    bullet that still overflows its line.

    project_ids: the user's own choice of projects, in the order they should
    appear. When given, the model rewrites bullets for exactly those and does
    not select, drop or reorder - choosing projects is the user's call. When
    omitted, project_ranking picks the candidates and the model chooses among
    them.
    """
    from app.services.project_ranking import rank_projects_for_jd

    src = source or collect_source_data(session, user_id=user_id, jd_id=jd_id)
    by_id = {p["id"]: p for p in src["projects"]}
    ranking = {str(r.project_id): r for r in rank_projects_for_jd(session, user_id=user_id, jd_id=jd_id)}

    if project_ids:
        chosen = [str(i) for i in project_ids if str(i) in by_id]
        if not chosen:
            raise ValueError("None of the selected projects exist (or they are unverified drafts).")
        candidates = [by_id[i] for i in chosen]
        fixed = True
        max_projects = len(candidates)
    else:
        ordered = sorted(src["projects"], key=lambda p: -(ranking[p["id"]].score if p["id"] in ranking else 0))
        candidates = ordered[:PROJECT_CANDIDATES]
        fixed = False
    for c in candidates:
        c["prefilter_score"] = round(ranking[c["id"]].score, 3) if c["id"] in ranking else None

    tailored = _tailor_with_gemini(
        experiences=src["experiences"],
        candidates=candidates,
        skills=src["skills"],
        match_result=src["match_result"],
        jd=src["jd"],
        profile=src["profile"],
        max_projects=max_projects,
        fixed_selection=fixed,
    )

    render_data, report = _assemble(src, tailored, max_projects=max_projects)
    report["selection"] = "user" if fixed else "automatic"
    return render_data, report


# ═══════════════════════════════════════════════════════════════════════════
# Project dedup - two rows, one real project
# ═══════════════════════════════════════════════════════════════════════════

def _normalize_repo_url(url: str | None) -> str:
    if not url:
        return ""
    u = url.strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = re.sub(r"\.git$", "", u.rstrip("/"))
    return u


def _title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (title or "").lower())


def _dedupe_projects(projects: list[dict]) -> list[dict]:
    """Group by repo URL (falling back to a normalized title) and merge.

    The repo URL is the reliable key: a GitHub import and the hand-written
    entry for the same project have wildly different titles ("chatbot" vs
    "Adaptive knowledge Chatbot - RAG") but the same repo.
    """
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for p in projects:
        key = (_normalize_repo_url(p.get("github_url"))
               or _normalize_repo_url(p.get("live_url"))
               or _title_key(p["title"]))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(p)
    return [_merge_project_group(groups[k]) for k in order]


def _merge_project_group(group: list[dict]) -> dict:
    """Keep the hand-written framing, pool the evidence.

    A title containing a space was typed by a person ("Smart Resume Parser -
    ML+NLP"); a bare slug came from the repo name. The human title and its
    curated tech stack (frameworks) beat the import's (GitHub's detected
    languages). Bullets are unioned, because the import often has detail the
    hand-written row lacks.
    """
    if len(group) == 1:
        return dict(group[0])

    primary = max(group, key=lambda p: (" " in p["title"], len(p["title"] or "")))
    curated = [p for p in group if " " in p["title"]]
    tech_source = next((p for p in curated if p["tech_stack"]), None) or \
                  next((p for p in group if p["tech_stack"]), primary)

    bullets: list[str] = []
    seen: set[str] = set()
    for p in sorted(group, key=lambda x: x is not primary):
        for b in p["raw_bullets"]:
            fp = re.sub(r"[^a-z0-9]", "", b.lower())[:80]
            if fp and fp not in seen:
                seen.add(fp)
                bullets.append(b)

    merged = dict(primary)
    merged["tech_stack"] = tech_source["tech_stack"]
    merged["raw_bullets"] = bullets
    merged["github_url"] = next((p["github_url"] for p in group if p.get("github_url")), None)
    merged["live_url"] = next((p["live_url"] for p in group if p.get("live_url")), None)
    merged["merged_from"] = [p["title"] for p in group]
    return merged


# ═══════════════════════════════════════════════════════════════════════════
# Relevance pre-filter - cheap, deterministic, keeps the prompt small
# ═══════════════════════════════════════════════════════════════════════════

def _jd_term_weights(requirements: dict, match_result: dict) -> dict[str, float]:
    """Weighted JD vocabulary. Required skills outrank nice-to-haves, and
    skills the matcher already confirmed outrank both - those are the ones the
    resume can honestly lead with."""
    terms: dict[str, float] = {}

    def add(name: str, weight: float) -> None:
        key = (name or "").strip().lower()
        if len(key) >= 2:
            terms[key] = max(terms.get(key, 0.0), weight)

    for s in requirements.get("required_skills") or []:
        if isinstance(s, dict):
            add(s.get("name", ""), 3.0 * float(s.get("weight") or 1.0))
    for s in requirements.get("nice_to_have_skills") or []:
        if isinstance(s, dict):
            add(s.get("name", ""), 1.5 * float(s.get("weight") or 0.5))
    for s in match_result.get("matched_skills") or []:
        add(s, 4.0)
    for line in requirements.get("responsibilities") or []:
        for token in re.findall(r"[A-Za-z][A-Za-z+#.\-]{3,}", str(line)):
            add(token, 0.4)
    return terms


def _score_project(project: dict, jd_terms: dict[str, float]) -> float:
    haystack = " ".join([
        project["title"],
        " ".join(project["tech_stack"] or []),
        " ".join(project["raw_bullets"]),
    ]).lower()
    score = 0.0
    for term, weight in jd_terms.items():
        # Word-boundary match so "go" does not score a hit inside "google".
        if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", haystack):
            score += weight
    # A project with no written evidence cannot produce good bullets.
    if len(project["raw_bullets"]) < 2:
        score -= 1.0
    return score


def _rank_projects(projects: list[dict], jd_terms: dict[str, float]) -> list[dict]:
    scored = [(p, _score_project(p, jd_terms)) for p in projects]
    scored.sort(key=lambda t: (-t[1], t[0]["title"].lower()))
    out = []
    for p, s in scored:
        q = dict(p)
        q["prefilter_score"] = round(s, 2)
        out.append(q)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# The prompt
# ═══════════════════════════════════════════════════════════════════════════

TAILOR_PROMPT = """\
You are a resume editor preparing ONE candidate's resume for ONE specific job.
You do not write resumes from scratch. You choose, reorder, tighten and cut
material that already exists below. Everything you output must be traceable to
the SOURCE MATERIAL section.

================================================================================
TARGET JOB
================================================================================
Company:     {jd_company}
Role:        {jd_role_title}
Seniority:   {jd_seniority}

Required skills:    {required_skills}
Nice to have:       {nice_to_have_skills}
Responsibilities the job lists:
{responsibilities}

================================================================================
MATCH ANALYSIS (already computed - treat as ground truth)
================================================================================
MATCHED  - the candidate genuinely has these and the job wants them.
           Surface them early and often, wherever the source material
           honestly supports it.
           {matched_skills}

MISSING  - the job wants these and the candidate does NOT have them.
           Never imply the candidate has any of these. Do not use these words
           even loosely.
           {missing_skills}

SURPLUS  - the candidate has these but this job did not ask for them.
           Keep them off the page unless a bullet needs one to make sense.
           {surplus_skills}

================================================================================
HARD RULES - breaking any one of these makes the output unusable
================================================================================
1. USE ONLY what is in SOURCE MATERIAL. Never invent a metric, a technology,
   a scale figure, an outcome, a date or a job duty. If a number is not
   written below, it does not exist.
2. You MAY: reorder, merge two related bullets, rephrase for active voice,
   cut a bullet entirely, and choose which projects appear.
3. Every bullet starts with a past-tense action verb (Built, Drove, Designed,
   Implemented, Reduced, Automated). No "Responsible for". No first-person
   pronouns. No filler adjectives - "cutting-edge", "impactful", "robust",
   "leveraged", "state-of-the-art" are banned.
4. Keep the candidate's own concrete detail. "Achieved 99.77% accuracy across
   four classes" is worth more than any rewording of it. Never round, soften
   or drop a real number.
5. Do not mention a MISSING skill. Do not pad with SURPLUS skills.

================================================================================
LINE LENGTH - this resume is typeset, and length is not cosmetic
================================================================================
Each bullet is printed on a single line of fixed width. A bullet of
{max_chars} characters or fewer fills one clean line. A bullet of
{max_chars_plus} or more wraps and leaves two or three orphan words alone on
the next line, which is immediately visible and looks careless.

  * HARD LIMIT:    {max_chars} characters per bullet, counting spaces.
  * AIM FOR:       {min_chars}-{target_chars} characters. Use the room - a
                   60-character bullet wastes a line.
  * Count before you answer. If a bullet is over, cut a qualifier, not a fact.

================================================================================
STYLE - these are the candidate's own bullets. Match this register.
================================================================================
  - Drove OpenTelemetry tracing rollout across 100+ microservices (Go, Java,
    Next.js) and log-trace correlation.
  - Implemented semantic question deduplication, cutting redundant queries by
    generating canonical summaries.
  - Built a resume parser with layout detection, achieving 95+% section
    accuracy across multi-column formats.

Dense, specific, technology named, outcome attached. No throat-clearing.

================================================================================
SOURCE MATERIAL
================================================================================
CANDIDATE SUMMARY ON FILE:
{profile_summary}

SKILLS ON FILE (you may reorder these; you may NOT add to them):
{skill_inventory}

WORK EXPERIENCE - keep every entry, rewrite its bullets:
{experience_source}

PROJECTS - {project_instruction}
{project_source}

================================================================================
WHAT TO RETURN
================================================================================
Return ONLY raw JSON. No markdown fences, no commentary before or after.

{{
  "summary": "2 sentences, {min_chars}-220 characters total. Lead with the
              candidate's strongest MATCHED skill and the role they are
              applying for. Concrete, no adjectives, no 'passionate'.",
  "experiences": [
    {{
      "entry_id": "<uuid copied exactly from source>",
      "bullets": ["...", "..."]
    }}
  ],
  "selected_projects": [
    {{
      "entry_id": "<uuid copied exactly from source>",
      "rank": 1,
      "reason": "<8 words or fewer: why this project fits this job>",
      "tech_stack": ["<subset of that project's own stack, most job-relevant
                      first - reorder and drop only, never add>"],
      "bullets": ["...", "..."]
    }}
  ],
  "skill_priority": ["<skill names copied exactly from SKILLS ON FILE, ordered
                      most relevant to this job first - this reorders the
                      skills section; omitting a name is fine, inventing one
                      is not>"]
}}

Return exactly {max_experience_bullets} bullets or fewer per experience entry,
and exactly {max_project_bullets} or fewer per project. Return exactly
{max_projects} entries in selected_projects, ranked 1 first.

Where a project lists APPROVED RESUME BULLETS, the candidate wrote and approved
those lines for resumes. Prefer them VERBATIM, choosing and ordering the ones
most relevant to this job; rephrase one only if it is over the length limit.
"""


def _format_skill_list(skills: list[dict]) -> str:
    """Group by display label, not raw category, so the model sees the same
    rows the resume will show - cloud and devops share one label."""
    by_label: dict[str, list[str]] = {}
    for s in skills:
        by_label.setdefault(CATEGORY_LABELS.get(s["category"], "Other Skills"), []).append(s["name"])
    return "\n".join(f"  {label}: {', '.join(names)}" for label, names in by_label.items())


def _format_requirement_skills(items: object) -> str:
    if not isinstance(items, list):
        return "(none listed)"
    names = [str(i.get("name", "")).strip() for i in items if isinstance(i, dict)]
    return ", ".join(n for n in names if n) or "(none listed)"


def _tailor_with_gemini(
    *,
    experiences: list[dict],
    candidates: list[dict],
    skills: list[dict],
    match_result: dict,
    jd: dict,
    profile: dict,
    max_projects: int,
    fixed_selection: bool = False,
) -> dict:
    exp_source = "\n\n".join(
        f"[id: {e['id']}] {e['role_title']} — {e['company']}"
        + (f" ({e['date_range']})" if e["date_range"] else "")
        + "\n" + "\n".join(f"    - {b}" for b in e["raw_bullets"])
        for e in experiences
    ) or "(none on file)"

    def _project_block(p: dict) -> str:
        lines = [f"[id: {p['id']}] {p['title']}"]
        if p.get("summary"):
            lines.append(f"    summary: {p['summary']}")
        if p.get("role"):
            lines.append(f"    candidate's role: {p['role']}")
        lines.append(f"    stack: {', '.join(p['tech_stack']) or '(not recorded)'}")
        if p.get("approved_bullets"):
            lines.append("    APPROVED RESUME BULLETS:")
            lines += [f"      * {b}" for b in p["approved_bullets"]]
            lines.append("    other evidence:")
        lines += [f"    - {b}" for b in p["raw_bullets"]]
        return "\n".join(lines)

    proj_source = "\n\n".join(_project_block(p) for p in candidates) or "(none on file)"
    project_instruction = (
        f"the candidate CHOSE these {len(candidates)} projects for this job. Include ALL of "
        f"them in selected_projects, in exactly this order (rank 1 = first listed). Do not "
        f"drop, add or reorder any."
        if fixed_selection else
        f"choose the best {max_projects} for THIS job:"
    )

    reqs = jd.get("requirements") or {}
    responsibilities = "\n".join(f"  - {r}" for r in (reqs.get("responsibilities") or [])[:8]) \
        or "  (none listed)"

    prompt = TAILOR_PROMPT.format(
        jd_company=jd.get("company") or "(not stated)",
        jd_role_title=jd.get("role_title") or "(not stated)",
        jd_seniority=reqs.get("seniority") or "(not stated)",
        required_skills=_format_requirement_skills(reqs.get("required_skills")),
        nice_to_have_skills=_format_requirement_skills(reqs.get("nice_to_have_skills")),
        responsibilities=responsibilities,
        matched_skills=", ".join(match_result.get("matched_skills") or []) or "(none)",
        missing_skills=", ".join(match_result.get("missing_skills") or []) or "(none)",
        surplus_skills=", ".join(match_result.get("surplus_skills") or []) or "(none)",
        max_chars=BULLET_MAX_CHARS,
        max_chars_plus=BULLET_MAX_CHARS + 1,
        min_chars=BULLET_MIN_CHARS,
        target_chars=BULLET_MAX_CHARS - 3,
        profile_summary=profile.get("summary") or "(none on file)",
        skill_inventory=_format_skill_list(skills) or "  (none on file)",
        experience_source=exp_source,
        project_source=proj_source,
        project_instruction=project_instruction,
        max_projects=max_projects,
        max_experience_bullets=MAX_EXPERIENCE_BULLETS,
        max_project_bullets=MAX_PROJECT_BULLETS,
    )

    result = generate_json(prompt)
    if not isinstance(result, dict):
        raise ValueError("Tailoring call did not return a JSON object")

    result = _validate_against_source(result, experiences, candidates, skills)
    if fixed_selection:
        result["selected_projects"] = _enforce_selection(result["selected_projects"], candidates)
    result = _repair_long_bullets(result)
    return result


def _enforce_selection(returned: list[dict], candidates: list[dict]) -> list[dict]:
    """The user's choice is final: every chosen project appears, in their
    order, even if the model dropped or reordered one. A dropped project falls
    back to its approved bullets (or its evidence) rather than disappearing."""
    by_id = {e["entry_id"]: e for e in returned}
    out = []
    for rank, c in enumerate(candidates, 1):
        entry = by_id.get(c["id"]) or {
            "entry_id": c["id"], "reason": "chosen by you",
            "tech_stack": list(c["tech_stack"]),
            "bullets": list(c.get("approved_bullets") or c["raw_bullets"])[:MAX_PROJECT_BULLETS],
        }
        out.append({**entry, "rank": rank})
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Validation - the actual anti-hallucination enforcement
# ═══════════════════════════════════════════════════════════════════════════

def _validate_against_source(
    result: dict,
    experiences: list[dict],
    candidates: list[dict],
    skills: list[dict],
) -> dict:
    """Drop anything the model returned that was not in what it was given.

    The prompt asks for this; this function is what guarantees it. An
    unrecognised entry_id means a fabricated entry, an unrecognised tech item
    means a fabricated stack, an unrecognised skill name means a fabricated
    skill - all are discarded silently rather than rendered.
    """
    exp_ids = {e["id"] for e in experiences}
    cand_by_id = {c["id"]: c for c in candidates}
    skill_names = {s["name"].lower(): s["name"] for s in skills}

    clean_experiences = []
    for entry in result.get("experiences") or []:
        if not isinstance(entry, dict) or entry.get("entry_id") not in exp_ids:
            continue
        clean_experiences.append({
            "entry_id": entry["entry_id"],
            "bullets": _clean_bullets(entry.get("bullets"), MAX_EXPERIENCE_BULLETS),
        })

    clean_projects = []
    for entry in result.get("selected_projects") or []:
        if not isinstance(entry, dict):
            continue
        src = cand_by_id.get(entry.get("entry_id"))
        if not src:
            continue
        allowed = {t.lower(): t for t in (src["tech_stack"] or [])}
        returned = [t for t in (entry.get("tech_stack") or []) if isinstance(t, str)]
        stack = [allowed[t.lower()] for t in returned if t.lower() in allowed]
        clean_projects.append({
            "entry_id": entry["entry_id"],
            "rank": entry.get("rank") or 99,
            "reason": normalize_ws(str(entry.get("reason") or ""))[:80],
            # Fall back to the recorded stack if nothing survived validation.
            "tech_stack": stack or list(src["tech_stack"] or []),
            "bullets": _clean_bullets(entry.get("bullets"), MAX_PROJECT_BULLETS),
        })
    clean_projects.sort(key=lambda e: e["rank"])

    priority, seen_priority = [], set()
    for s in result.get("skill_priority") or []:
        if isinstance(s, str) and s.lower() in skill_names and s.lower() not in seen_priority:
            seen_priority.add(s.lower())
            priority.append(skill_names[s.lower()])

    return {
        "summary": normalize_ws(str(result.get("summary") or "")),
        "experiences": clean_experiences,
        "selected_projects": clean_projects,
        "skill_priority": priority,
    }


def _clean_bullets(value: object, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for b in value:
        if not isinstance(b, str):
            continue
        cleaned = normalize_ws(b).lstrip("-•* ").strip()
        if cleaned:
            out.append(cleaned)
    return out[:limit]


REPAIR_PROMPT = """\
Shorten each numbered line below to {max_chars} characters or fewer, counting
spaces. These are resume bullets that currently overflow their printed line.

RULES:
  - Keep every fact, technology name and number exactly as written.
  - Cut qualifiers, articles and hedging words. Never cut a fact to fit.
  - Keep the leading past-tense verb.
  - Do not drop below {min_chars} characters - a short bullet wastes the line.

LINES:
{lines}

Return ONLY raw JSON, no fences:
{{"rewritten": {{"<line number>": "<shortened bullet>"}}}}
"""


def _repair_long_bullets(result: dict) -> dict:
    """One repair round for bullets that still overflow.

    Truncating here is not an option - it would cut a resume claim mid-word -
    so overlong bullets go back to the model with the single instruction to
    shorten them. Anything still over after this is left alone and reported;
    a slightly long bullet beats a mangled one.
    """
    indexed: list[tuple[tuple, str]] = []
    for i, e in enumerate(result["experiences"]):
        for j, b in enumerate(e["bullets"]):
            if len(b) > BULLET_MAX_CHARS:
                indexed.append((("experiences", i, j), b))
    for i, p in enumerate(result["selected_projects"]):
        for j, b in enumerate(p["bullets"]):
            if len(b) > BULLET_MAX_CHARS:
                indexed.append((("selected_projects", i, j), b))

    if not indexed:
        return result

    lines = "\n".join(f"{n}. ({len(b)} chars) {b}" for n, (_, b) in enumerate(indexed, 1))
    try:
        repaired = generate_json(REPAIR_PROMPT.format(
            max_chars=BULLET_MAX_CHARS, min_chars=BULLET_MIN_CHARS, lines=lines,
        ))
        mapping = repaired.get("rewritten") or {}
    except Exception:
        # A failed cosmetic repair must not fail the whole resume.
        return result

    for n, (path, original) in enumerate(indexed, 1):
        new = mapping.get(str(n)) or mapping.get(n)
        if not isinstance(new, str):
            continue
        new = normalize_ws(new).lstrip("-•* ").strip()
        # Only accept a repair that is both shorter and still substantial.
        if new and len(new) <= BULLET_MAX_CHARS and len(new) >= BULLET_MIN_CHARS * 0.6:
            key, i, j = path
            result[key][i]["bullets"][j] = new
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Skills section
# ═══════════════════════════════════════════════════════════════════════════

CATEGORY_LABELS = {
    "language": "Programming Languages",
    "framework": "Frameworks & Libraries",
    "database": "Databases",
    "cloud": "Cloud & DevOps",
    "devops": "Cloud & DevOps",
    "ml": "AI/ML",
    "tool": "Tools & Protocols",
    "soft_skill": "Other Skills",
}

# Order sections so the technical ones a reviewer scans for come first.
CATEGORY_ORDER = [
    "Programming Languages",
    "Frameworks & Libraries",
    "Databases",
    "Cloud & DevOps",
    "AI/ML",
    "Tools & Protocols",
    "Other Skills",
]

# The KB accumulates the same skill under different names as entries come from
# resume imports, GitHub imports and manual entry. Listing "Go" and "Golang"
# side by side on a resume reads as carelessness.
SKILL_ALIASES = {
    "golang": "Go",
    "js": "JavaScript",
    "ts": "TypeScript",
    "node": "Node.js",
    "nodejs": "Node.js",
    "node.js": "Node.js",
    "postgres": "PostgreSQL",
    "postgresql": "PostgreSQL",
    "k8s": "Kubernetes",
}


def _canonical_skill(name: str) -> tuple[str, str]:
    """Return (dedup key, display name)."""
    key = re.sub(r"[^a-z0-9+#.]", "", name.lower())
    canonical = SKILL_ALIASES.get(name.strip().lower())
    if canonical:
        return re.sub(r"[^a-z0-9+#.]", "", canonical.lower()), canonical
    return key, name


def _build_skills_sections(skills: list[dict], match_result: dict,
                           skill_priority: list[str]) -> list[dict]:
    """Group into labelled rows, ordering within each row by how much this JD
    cares about the skill. A reviewer reads the first two or three names in a
    row and stops, so those slots are the whole value of the section."""
    matched = {s.lower() for s in (match_result.get("matched_skills") or [])}
    priority_rank = {s.lower(): i for i, s in enumerate(skill_priority)}

    rows: dict[str, list[tuple]] = {}
    seen: set[tuple[str, str]] = set()
    for s in skills:
        key, display = _canonical_skill(s["name"])
        label = CATEGORY_LABELS.get(s["category"], "Other Skills")
        if (label, key) in seen:
            continue
        seen.add((label, key))
        lower = s["name"].lower()
        sort_key = (
            priority_rank.get(lower, 10_000),   # model's JD ordering first
            0 if lower in matched else 1,       # then confirmed matches
            display.lower(),
        )
        rows.setdefault(label, []).append((sort_key, display))

    out = []
    for label in CATEGORY_ORDER:
        entries = rows.get(label)
        if not entries:
            continue
        entries.sort(key=lambda t: t[0])
        names = [display for _, display in entries]
        out.append({
            "label": latex_escape(label),
            "items_text": latex_escape(", ".join(names)),
        })
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Assembly - the only place strings get escaped
# ═══════════════════════════════════════════════════════════════════════════

LINK_LABELS = {
    "linkedin": "LinkedIn",
    "github": "GitHub",
    "portfolio": "Portfolio",
    "leetcode": "LeetCode",
    "website": "Website",
    "twitter": "Twitter",
}
LINK_ORDER = ["linkedin", "github", "portfolio", "leetcode", "website", "twitter"]


def _display_url(url: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", (url or "").strip()).rstrip("/")


def _full_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    return u if u.startswith(("http://", "https://", "mailto:")) else f"https://{u}"


def _assemble(src: dict, tailored: dict, *, max_projects: int) -> tuple[dict, dict]:
    profile = src["profile"]
    exp_by_id = {e["id"]: e for e in src["experiences"]}
    proj_by_id = {p["id"]: p for p in src["projects"]}

    # ── Header ──
    contact_items = []
    if profile.get("phone"):
        contact_items.append({"label": "Phone", "text": latex_escape(profile["phone"]), "url": ""})
    if profile.get("email"):
        contact_items.append({
            "label": "Email",
            "text": latex_escape(profile["email"]),
            "url": latex_escape_url(f"mailto:{profile['email']}"),
        })
    if profile.get("location"):
        contact_items.append({"label": "Location", "text": latex_escape(profile["location"]), "url": ""})

    links = profile.get("links") or {}
    link_items = []
    for key in LINK_ORDER + [k for k in links if k not in LINK_ORDER]:
        url = (links.get(key) or "").strip()
        if not url:
            continue
        link_items.append({
            "label": latex_escape(LINK_LABELS.get(key, key.title())),
            "text": latex_escape(_display_url(url)),
            "url": latex_escape_url(_full_url(url)),
        })

    # ── Experience: KB order, model's bullets ──
    bullets_by_exp = {e["entry_id"]: e["bullets"] for e in tailored["experiences"]}
    experiences = []
    all_bullets: list[str] = []
    for e in src["experiences"]:
        bullets = bullets_by_exp.get(e["id"]) or e["raw_bullets"][:MAX_EXPERIENCE_BULLETS]
        all_bullets.extend(bullets)
        experiences.append({
            "role_title": latex_escape(e["role_title"]),
            "company": latex_escape(e["company"]),
            "meta": latex_escape(e["date_range"]),
            "bullets": [latex_escape(b) for b in bullets],
        })

    # ── Projects: model's selection and ranking ──
    projects = []
    chosen = []
    for entry in tailored["selected_projects"][:max_projects]:
        src_proj = proj_by_id.get(entry["entry_id"])
        if not src_proj:
            continue
        bullets = entry["bullets"] or src_proj["raw_bullets"][:MAX_PROJECT_BULLETS]
        all_bullets.extend(bullets)
        url = src_proj.get("github_url") or src_proj.get("live_url") or ""
        projects.append({
            "title": latex_escape(src_proj["title"]),
            "tech": latex_escape(fit_tech_stack(entry["tech_stack"])),
            "link_text": latex_escape(_display_url(url)),
            "link_url": latex_escape_url(_full_url(url)),
            "bullets": [latex_escape(b) for b in bullets],
        })
        chosen.append({"title": src_proj["title"], "reason": entry["reason"],
                       "score": src_proj.get("prefilter_score")})

    summary = tailored["summary"] or profile.get("summary") or ""

    render_data = {
        "profile": {
            "full_name": latex_escape(profile.get("full_name") or ""),
            "contact_items": contact_items,
            "link_items": link_items,
        },
        "summary": latex_escape(summary),
        "skills_sections": _build_skills_sections(
            src["skills"], src["match_result"], tailored["skill_priority"]),
        "experiences": experiences,
        "projects": projects,
        "education": [
            {
                "institution": latex_escape(e["institution"]),
                "degree": latex_escape(e["degree"]),
                "date_range": latex_escape(e["date_range"]),
                "score": latex_escape(e["score"] or ""),
            }
            for e in src["education"]
        ],
        "certifications": [_certification_row(c) for c in src["certifications"]],
    }

    report = {
        "selected_projects": chosen,
        "projects_available": len(src["projects"]),
        "duplicates_merged": sum(len(p.get("merged_from", [])) - 1
                                 for p in src["projects"] if p.get("merged_from")),
        "long_bullets": audit_bullets(all_bullets),
        "summary_chars": len(summary),
        "missing_dates": [e["role_title"] for e in src["experiences"] if not e["date_range"]],
    }
    return render_data, report


def _certification_row(cert: dict) -> dict:
    parts = [f"\\textbf{{{latex_escape(cert['title'])}}}"]
    if cert.get("issuer"):
        parts.append(f"-- {latex_escape(cert['issuer'])}")
    if cert.get("date"):
        parts.append(f"({latex_escape(cert['date'])})")
    url = (cert.get("url") or "").strip()
    short = _display_url(url)
    # A full certificate URL can be 200 characters; only a short one is worth
    # printing verbatim next to the entry.
    label = f"[{short}]" if 0 < len(short) <= 24 else ("[link]" if url else "")
    return {
        "text": " ".join(parts),
        "link_text": latex_escape(label),
        "link_url": latex_escape_url(_full_url(url)),
    }


# ═══════════════════════════════════════════════════════════════════════════
# DB loaders
# ═══════════════════════════════════════════════════════════════════════════

def _fmt_month(d: date | None) -> str:
    return d.strftime("%b %Y") if d else ""


def _date_range(start: date | None, end: date | None, *, year_only: bool = False) -> str:
    """Empty string when nothing is recorded.

    The previous version emitted " – Present" for an entry with no dates at
    all, which printed a bare dash on the resume.
    """
    fmt = (lambda d: d.strftime("%Y")) if year_only else _fmt_month
    s, e = (fmt(start) if start else ""), (fmt(end) if end else "")
    if s and e:
        return f"{s} – {e}"
    if s:
        return f"{s} – Present"
    if e:
        return e
    return ""


def _split_bullets(*blocks: str | None) -> list[str]:
    out, seen = [], set()
    for block in blocks:
        for line in (block or "").split("\n"):
            cleaned = normalize_ws(line).lstrip("-•* ").strip()
            if cleaned and cleaned.lower() not in seen:
                seen.add(cleaned.lower())
                out.append(cleaned)
    return out


def _load_profile(session, user_id) -> dict:
    row = session.execute(
        text("SELECT full_name, email, phone, location, links, summary "
             "FROM profile_basic WHERE user_id = :uid"),
        {"uid": user_id},
    ).mappings().first()
    return dict(row) if row else {}


def _load_experiences(session, user_id) -> list[dict]:
    rows = session.execute(
        text("SELECT id, company, role_title, start_date, end_date, description, tech_stack "
             "FROM work_experience WHERE user_id = :uid "
             "ORDER BY end_date DESC NULLS FIRST, start_date DESC NULLS LAST"),
        {"uid": user_id},
    ).mappings().fetchall()
    return [{
        "id": str(r["id"]),
        "company": r["company"],
        "role_title": r["role_title"],
        "date_range": _date_range(r["start_date"], r["end_date"]),
        "raw_bullets": _split_bullets(r["description"]),
        "tech_stack": list(r["tech_stack"] or []),
    } for r in rows]


def _load_projects(session, user_id) -> list[dict]:
    rows = session.execute(
        text("SELECT id, title, summary, role, description, tech_stack, github_url, live_url, "
             "highlights, key_points, resume_bullets, priority, is_favourite "
             "FROM project WHERE user_id = :uid AND status = 'verified' "
             "ORDER BY is_favourite DESC, priority DESC, updated_at DESC"),
        {"uid": user_id},
    ).mappings().fetchall()
    out = []
    for r in rows:
        key_points = [k for k in (r["key_points"] or []) if k and k.strip()]
        out.append({
            "id": str(r["id"]),
            "title": r["title"],
            "summary": r["summary"],
            "role": r["role"],
            # Key points are the evidence; fall back to splitting prose for
            # projects that have not been curated yet.
            "raw_bullets": key_points or _split_bullets(r["description"], r["highlights"]),
            "approved_bullets": [b for b in (r["resume_bullets"] or []) if b and b.strip()],
            "tech_stack": list(r["tech_stack"] or []),
            "github_url": r["github_url"],
            "live_url": r["live_url"],
            "priority": r["priority"],
            "is_favourite": r["is_favourite"],
        })
    return out


def _load_skills(session, user_id) -> list[dict]:
    rows = session.execute(
        text("SELECT name, category FROM skill WHERE user_id = :uid "
             "ORDER BY proficiency DESC NULLS LAST, name"),
        {"uid": user_id},
    ).mappings().fetchall()
    return [{"name": r["name"], "category": r["category"]} for r in rows]


def _load_education(session, user_id) -> list[dict]:
    rows = session.execute(
        text("SELECT degree, institution, start_date, end_date, score, highlights "
             "FROM education WHERE user_id = :uid ORDER BY end_date DESC NULLS FIRST"),
        {"uid": user_id},
    ).mappings().fetchall()
    return [{
        "degree": r["degree"],
        "institution": r["institution"],
        "date_range": _date_range(r["start_date"], r["end_date"], year_only=True),
        "score": r["score"],
        "highlights": r["highlights"],
    } for r in rows]


def _load_certifications(session, user_id) -> list[dict]:
    rows = session.execute(
        text("SELECT title, issuer, date, url FROM certification "
             "WHERE user_id = :uid ORDER BY date DESC NULLS LAST"),
        {"uid": user_id},
    ).mappings().fetchall()
    return [{
        "title": r["title"],
        "issuer": r["issuer"],
        "date": r["date"].strftime("%Y") if r["date"] else "",
        "url": r["url"],
    } for r in rows]


def _load_match_result(session, jd_id, user_id) -> dict:
    row = session.execute(
        text("SELECT matched_skills, missing_skills, surplus_skills FROM match_result "
             "WHERE jd_id = :jd AND user_id = :uid ORDER BY created_at DESC LIMIT 1"),
        {"jd": jd_id, "uid": user_id},
    ).first()
    if not row:
        return {"matched_skills": [], "missing_skills": [], "surplus_skills": []}
    return {"matched_skills": list(row[0] or []),
            "missing_skills": list(row[1] or []),
            "surplus_skills": list(row[2] or [])}
