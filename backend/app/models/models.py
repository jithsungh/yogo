"""
SQLAlchemy ORM models mirroring db/init/01_schema.sql exactly.

Conventions:
- Enums use create_type=False (they already exist in Postgres from 01_schema.sql)
- UUID PKs with server_default=text("gen_random_uuid()")
- Arrays via ARRAY(Text) or ARRAY(UUID(as_uuid=True))
- JSONB for structured/flexible columns
- Vector(768) for embedding columns (pgvector)
"""
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.orm import DeclarativeBase, relationship
from pgvector.sqlalchemy import Vector


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Enums (all pre-existing in Postgres — create_type=False)
# ---------------------------------------------------------------------------
skill_category_enum = PGEnum(
    "language", "framework", "cloud", "devops", "ml", "database", "tool", "soft_skill",
    name="skill_category", create_type=False,
)

role_category_enum = PGEnum(
    "development", "devops", "sre", "ai_ml", "data", "qa", "other",
    name="role_category", create_type=False,
)

match_verdict_enum = PGEnum(
    "strong", "stretch", "skip",
    name="match_verdict", create_type=False,
)

resume_source_enum = PGEnum(
    "manual", "github_import",
    name="resume_source", create_type=False,
)

qa_category_enum = PGEnum(
    "reusable", "role_specific", "company_specific",
    name="qa_category", create_type=False,
)

qa_style_enum = PGEnum(
    "concise", "detailed_star", "conversational",
    name="qa_style", create_type=False,
)

qa_status_enum = PGEnum(
    "draft", "approved",
    name="qa_status", create_type=False,
)

application_status_enum = PGEnum(
    "saved", "applied", "oa", "interview_1", "interview_2", "interview_3",
    "offer", "rejected", "ghosted", "withdrawn",
    name="application_status", create_type=False,
)

kb_content_type_enum = PGEnum(
    "experience", "project", "skill", "education", "certification", "qa_answer", "basic_info",
    name="kb_content_type", create_type=False,
)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    email = Column(Text, nullable=False, unique=True)
    password_hash = Column(Text, nullable=False)
    full_name = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    # Relationships
    profile = relationship("ProfileBasic", back_populates="user", uselist=False)
    education_entries = relationship("Education", back_populates="user")
    work_experiences = relationship("WorkExperience", back_populates="user")
    projects = relationship("Project", back_populates="user")
    certifications = relationship("Certification", back_populates="user")
    skills = relationship("Skill", back_populates="user")
    job_descriptions = relationship("JobDescription", back_populates="user")
    match_results = relationship("MatchResult", back_populates="user")
    resume_versions = relationship("ResumeVersion", back_populates="user")
    qa_questions = relationship("QAQuestion", back_populates="user")
    applications = relationship("Application", back_populates="user")
    kb_chunks = relationship("KBChunk", back_populates="user")


# ---------------------------------------------------------------------------
# Roles + AKAs (canonical role taxonomy)
# ---------------------------------------------------------------------------
class Role(Base):
    __tablename__ = "role"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    canonical_name = Column(Text, nullable=False, unique=True)
    category = Column(role_category_enum, nullable=False)
    aliases = Column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


# ---------------------------------------------------------------------------
# Profile (Tier 1 — verified facts)
# ---------------------------------------------------------------------------
class ProfileBasic(Base):
    __tablename__ = "profile_basic"

    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    full_name = Column(Text, nullable=False)
    email = Column(Text)
    phone = Column(Text)
    location = Column(Text)
    links = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    summary = Column(Text)
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    user = relationship("User", back_populates="profile")


class Education(Base):
    __tablename__ = "education"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    degree = Column(Text, nullable=False)
    institution = Column(Text, nullable=False)
    start_date = Column(Date)
    end_date = Column(Date)
    score = Column(Text)
    highlights = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    user = relationship("User", back_populates="education_entries")


class WorkExperience(Base):
    __tablename__ = "work_experience"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    company = Column(Text, nullable=False)
    role_title = Column(Text, nullable=False)
    role_id = Column(UUID(as_uuid=True), ForeignKey("role.id"), nullable=True)
    start_date = Column(Date)
    end_date = Column(Date)
    description = Column(Text, nullable=False)
    tech_stack = Column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    user = relationship("User", back_populates="work_experiences")
    role = relationship("Role")


class Project(Base):
    __tablename__ = "project"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title = Column(Text, nullable=False)
    description = Column(Text, nullable=False)
    tech_stack = Column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    github_url = Column(Text)
    live_url = Column(Text)
    highlights = Column(Text)
    source = Column(resume_source_enum, nullable=False, server_default=text("'manual'"))
    status = Column(Text, nullable=False, server_default=text("'verified'"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    user = relationship("User", back_populates="projects")


class Certification(Base):
    __tablename__ = "certification"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title = Column(Text, nullable=False)
    issuer = Column(Text)
    date = Column(Date)
    url = Column(Text)

    user = relationship("User", back_populates="certifications")


class Skill(Base):
    __tablename__ = "skill"
    __table_args__ = (
        UniqueConstraint("user_id", "name"),
        CheckConstraint("proficiency BETWEEN 1 AND 5", name="skill_proficiency_check"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name = Column(Text, nullable=False)
    category = Column(skill_category_enum, nullable=False)
    proficiency = Column(SmallInteger)

    user = relationship("User", back_populates="skills")


# ---------------------------------------------------------------------------
# Job Descriptions + Matching
# ---------------------------------------------------------------------------
class JobDescription(Base):
    __tablename__ = "job_description"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    raw_text = Column(Text, nullable=False)
    source_url = Column(Text)
    company = Column(Text)
    role_title = Column(Text)
    role_id = Column(UUID(as_uuid=True), ForeignKey("role.id"), nullable=True)
    parsed_requirements = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    embedding = Column(Vector(768))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    user = relationship("User", back_populates="job_descriptions")
    role = relationship("Role")
    extracted_skills = relationship("ExtractedSkillMention", back_populates="job_description", cascade="all, delete-orphan")
    match_results = relationship("MatchResult", back_populates="job_description", cascade="all, delete-orphan")
    resume_versions = relationship("ResumeVersion", back_populates="job_description", cascade="all, delete-orphan")


class ExtractedSkillMention(Base):
    __tablename__ = "extracted_skill_mention"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    jd_id = Column(UUID(as_uuid=True), ForeignKey("job_description.id", ondelete="CASCADE"), nullable=False)
    skill_name = Column(Text, nullable=False)
    weight = Column(Float, nullable=False, server_default=text("1.0"))
    is_required = Column(Boolean, nullable=False, server_default=text("true"))

    job_description = relationship("JobDescription", back_populates="extracted_skills")


class MatchResult(Base):
    __tablename__ = "match_result"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    jd_id = Column(UUID(as_uuid=True), ForeignKey("job_description.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    score = Column(Float, nullable=False)
    verdict = Column(match_verdict_enum, nullable=False)
    matched_skills = Column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    missing_skills = Column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    surplus_skills = Column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    job_description = relationship("JobDescription", back_populates="match_results")
    user = relationship("User", back_populates="match_results")


# ---------------------------------------------------------------------------
# Resume Generation
# ---------------------------------------------------------------------------
class ResumeVersion(Base):
    __tablename__ = "resume_version"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    jd_id = Column(UUID(as_uuid=True), ForeignKey("job_description.id", ondelete="CASCADE"), nullable=False)
    latex_source = Column(Text, nullable=False)
    pdf_path = Column(Text)
    notes = Column(Text)
    generated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    user = relationship("User", back_populates="resume_versions")
    job_description = relationship("JobDescription", back_populates="resume_versions")


# ---------------------------------------------------------------------------
# Q&A Answer Bank
# ---------------------------------------------------------------------------
class QAQuestion(Base):
    __tablename__ = "qa_question"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    jd_id = Column(UUID(as_uuid=True), ForeignKey("job_description.id", ondelete="SET NULL"), nullable=True)
    role_id = Column(UUID(as_uuid=True), ForeignKey("role.id"), nullable=True)
    question_text = Column(Text, nullable=False)
    category = Column(qa_category_enum, nullable=False, server_default=text("'reusable'"))
    embedding = Column(Vector(768))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    user = relationship("User", back_populates="qa_questions")
    job_description = relationship("JobDescription")
    role = relationship("Role")
    answers = relationship("QAAnswer", back_populates="question", cascade="all, delete-orphan")


class QAAnswer(Base):
    __tablename__ = "qa_answer"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    question_id = Column(UUID(as_uuid=True), ForeignKey("qa_question.id", ondelete="CASCADE"), nullable=False)
    answer_text = Column(Text, nullable=False)
    style = Column(qa_style_enum, nullable=False)
    status = Column(qa_status_enum, nullable=False, server_default=text("'draft'"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    question = relationship("QAQuestion", back_populates="answers")


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------
class Application(Base):
    __tablename__ = "application"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    jd_id = Column(UUID(as_uuid=True), ForeignKey("job_description.id", ondelete="CASCADE"), nullable=False)
    resume_version_id = Column(UUID(as_uuid=True), ForeignKey("resume_version.id"), nullable=True)
    company = Column(Text, nullable=False)
    role_title = Column(Text, nullable=False)
    status = Column(application_status_enum, nullable=False, server_default=text("'saved'"))
    applied_date = Column(Date)
    next_action_date = Column(Date)
    notes = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    user = relationship("User", back_populates="applications")
    job_description = relationship("JobDescription")
    resume_version = relationship("ResumeVersion")
    reminders = relationship("Reminder", back_populates="application", cascade="all, delete-orphan")


class Reminder(Base):
    __tablename__ = "reminder"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    application_id = Column(UUID(as_uuid=True), ForeignKey("application.id", ondelete="CASCADE"), nullable=False)
    due_date = Column(Date, nullable=False)
    message = Column(Text, nullable=False)
    done = Column(Boolean, nullable=False, server_default=text("false"))

    application = relationship("Application", back_populates="reminders")


# ---------------------------------------------------------------------------
# Unified retrieval index
# ---------------------------------------------------------------------------
class KBChunk(Base):
    __tablename__ = "kb_chunk"
    __table_args__ = (
        UniqueConstraint("source_table", "source_id"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    source_table = Column(Text, nullable=False)
    source_id = Column(UUID(as_uuid=True), nullable=False)
    content_type = Column(kb_content_type_enum, nullable=False)
    role_tags = Column(ARRAY(UUID(as_uuid=True)), nullable=False, server_default=text("'{}'"))
    text_for_embedding = Column(Text, nullable=False)
    embedding = Column(Vector(768), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    user = relationship("User", back_populates="kb_chunks")
