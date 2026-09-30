"""Knowledge-base entities: create / update / out models.

Conventions used by every entity:
  * <X>Create   everything needed to create one; defaults for the optional parts.
  * <X>Update   every field optional. Only fields the client actually sends are
                applied (model_dump(exclude_unset=True)), so a PATCH that sets
                `priority` cannot blank out `description`.
  * <X>Out      what reads return, including id and timestamps.

Normalization happens here, once, instead of in every form: strings are
stripped, list items are stripped and de-duplicated (case-insensitively),
bare URLs get a scheme, and date ranges are checked.
"""
from __future__ import annotations

import datetime as dt
import re
import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SkillCategory = Literal["language", "framework", "cloud", "devops", "ml", "database", "tool", "soft_skill"]
ProjectStatus = Literal["verified", "draft"]
ProjectSource = Literal["manual", "github_import"]


# ── shared normalizers ────────────────────────────────────────────────────────

def clean_str(v):
    if v is None:
        return None
    v = re.sub(r"[ \t]+", " ", str(v)).strip()
    return v or None


def clean_list(v) -> list[str]:
    """Strip, drop empties and list markers, de-duplicate case-insensitively."""
    if v is None:
        return []
    if isinstance(v, str):
        v = v.split("\n")
    out, seen = [], set()
    for item in v:
        s = re.sub(r"\s+", " ", str(item or "")).strip().lstrip("-•*·").strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def clean_url(v):
    v = clean_str(v)
    if not v:
        return None
    return v if re.match(r"^(https?://|mailto:)", v, re.I) else f"https://{v}"


def _check_range(start, end):
    if start and end and end < start:
        raise ValueError("end date is before start date")


class _Base(BaseModel):
    model_config = ConfigDict(from_attributes=True, str_strip_whitespace=True)


# ── Profile ───────────────────────────────────────────────────────────────────

class ProfileIn(_Base):
    full_name: str = Field(min_length=1)
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    summary: str | None = None
    links: dict[str, str] = Field(default_factory=dict)

    _s = field_validator("email", "phone", "location", "summary", mode="before")(clean_str)

    @field_validator("links", mode="before")
    @classmethod
    def _links(cls, v):
        return {str(k).strip().lower(): clean_url(u) for k, u in (v or {}).items() if clean_url(u)}


class ProfileOut(ProfileIn):
    user_id: uuid.UUID
    updated_at: datetime | None = None


# ── Education ─────────────────────────────────────────────────────────────────

class EducationCreate(_Base):
    degree: str = Field(min_length=1)
    institution: str = Field(min_length=1)
    start_date: date | None = None
    end_date: date | None = None
    score: str | None = None
    highlights: str | None = None

    _s = field_validator("score", "highlights", mode="before")(clean_str)

    @model_validator(mode="after")
    def _range(self):
        _check_range(self.start_date, self.end_date)
        return self


class EducationUpdate(_Base):
    degree: str | None = Field(default=None, min_length=1)
    institution: str | None = Field(default=None, min_length=1)
    start_date: date | None = None
    end_date: date | None = None
    score: str | None = None
    highlights: str | None = None

    _s = field_validator("score", "highlights", mode="before")(clean_str)


class EducationOut(EducationCreate):
    id: uuid.UUID
    created_at: datetime | None = None


# ── Work experience ───────────────────────────────────────────────────────────

class ExperienceCreate(_Base):
    company: str = Field(min_length=1)
    role_title: str = Field(min_length=1)
    start_date: date | None = None
    end_date: date | None = None          # None + start_date = current role
    description: str = ""                 # one achievement per line
    tech_stack: list[str] = Field(default_factory=list)

    _l = field_validator("tech_stack", mode="before")(clean_list)

    @model_validator(mode="after")
    def _range(self):
        _check_range(self.start_date, self.end_date)
        return self


class ExperienceUpdate(_Base):
    company: str | None = Field(default=None, min_length=1)
    role_title: str | None = Field(default=None, min_length=1)
    start_date: date | None = None
    end_date: date | None = None
    description: str | None = None
    tech_stack: list[str] | None = None

    @field_validator("tech_stack", mode="before")
    @classmethod
    def _l(cls, v):
        return None if v is None else clean_list(v)


class ExperienceOut(ExperienceCreate):
    id: uuid.UUID
    role_id: uuid.UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ── Project ───────────────────────────────────────────────────────────────────

class ProjectCreate(_Base):
    title: str = Field(min_length=1)
    summary: str | None = None            # one-line pitch
    role: str | None = None               # the candidate's part in it
    description: str = ""                 # long form
    tech_stack: list[str] = Field(default_factory=list)
    github_url: str | None = None
    live_url: str | None = None
    key_points: list[str] = Field(default_factory=list)       # detailed evidence
    resume_bullets: list[str] = Field(default_factory=list)   # short, resume-ready
    start_date: date | None = None
    end_date: date | None = None
    priority: int = Field(default=0, ge=0, le=5)
    is_favourite: bool = False
    status: ProjectStatus = "verified"
    source: ProjectSource = "manual"

    _s = field_validator("summary", "role", mode="before")(clean_str)
    _l = field_validator("tech_stack", "key_points", "resume_bullets", mode="before")(clean_list)
    _u = field_validator("github_url", "live_url", mode="before")(clean_url)

    @field_validator("description", mode="before")
    @classmethod
    def _desc(cls, v):
        return (v or "").strip()

    @model_validator(mode="after")
    def _range(self):
        _check_range(self.start_date, self.end_date)
        return self


class ProjectUpdate(_Base):
    title: str | None = Field(default=None, min_length=1)
    summary: str | None = None
    role: str | None = None
    description: str | None = None
    tech_stack: list[str] | None = None
    github_url: str | None = None
    live_url: str | None = None
    key_points: list[str] | None = None
    resume_bullets: list[str] | None = None
    start_date: date | None = None
    end_date: date | None = None
    priority: int | None = Field(default=None, ge=0, le=5)
    is_favourite: bool | None = None
    status: ProjectStatus | None = None

    _s = field_validator("summary", "role", mode="before")(clean_str)
    _u = field_validator("github_url", "live_url", mode="before")(clean_url)

    @field_validator("tech_stack", "key_points", "resume_bullets", mode="before")
    @classmethod
    def _l(cls, v):
        return None if v is None else clean_list(v)


class ProjectOut(ProjectCreate):
    id: uuid.UUID
    repo_key: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ── Skill ─────────────────────────────────────────────────────────────────────

class SkillCreate(_Base):
    name: str = Field(min_length=1)
    category: SkillCategory
    proficiency: int | None = Field(default=None, ge=1, le=5)


class SkillUpdate(_Base):
    name: str | None = Field(default=None, min_length=1)
    category: SkillCategory | None = None
    proficiency: int | None = Field(default=None, ge=1, le=5)


class SkillOut(SkillCreate):
    id: uuid.UUID


# ── Certification ─────────────────────────────────────────────────────────────

class CertificationCreate(_Base):
    title: str = Field(min_length=1)
    issuer: str | None = None
    date: dt.date | None = None   # dt.date: a field named `date` shadows the type here
    url: str | None = None

    _s = field_validator("issuer", mode="before")(clean_str)
    _u = field_validator("url", mode="before")(clean_url)


class CertificationUpdate(_Base):
    title: str | None = Field(default=None, min_length=1)
    issuer: str | None = None
    date: dt.date | None = None
    url: str | None = None

    _s = field_validator("issuer", mode="before")(clean_str)
    _u = field_validator("url", mode="before")(clean_url)


class CertificationOut(CertificationCreate):
    id: uuid.UUID


class SaveResult(_Base):
    """What every create/update returns: the saved row, and whether its
    kb_chunk was re-embedded (False when the embedded text did not change)."""
    item: dict
    reembedded: bool
