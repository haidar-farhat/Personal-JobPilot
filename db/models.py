"""SQLAlchemy models for JobPilot database."""

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Text,
    Boolean,
    DateTime,
    Enum,
    JSON,
    ForeignKey,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class ApplicationStatus(enum.Enum):
    FOUND = "found"
    SCORED = "scored"
    MATERIALS_READY = "materials_ready"
    QUEUED = "queued"
    APPROVED = "approved"
    APPLIED = "applied"
    RESPONSE_RECEIVED = "response_received"
    INTERVIEW = "interview"
    REJECTED = "rejected"
    NO_RESPONSE = "no_response"
    NO_LONGER_AVAILABLE = "no_longer_available"   # posting was taken down / role closed
    SKIPPED = "skipped"


# Auto-apply outcomes that will not change on a retry: the bot never signs in,
# never creates accounts, never solves CAPTCHAs. Shared by the runner (skip on
# re-selection) and the dashboard ("Needs manual apply" surfacing).
# Outcomes that mean the SYSTEM is misbehaving — a wall the bot cannot pass, a
# navigation error, an unhandled crash. Only these count toward the
# consecutive-failure halt.
#
# Deliberately NOT in here: failed_required_fields_unfilled and
# failed_too_many_essays. Those are the bot working correctly — refusing to
# invent an answer — and they are the majority outcome on real forms. Counting
# them halted every cycle after ~6 jobs and left the queue undrained.
SYSTEM_FAULTS = frozenset({
    "failed_captcha", "failed_login_required", "failed_workday_login_required",
    "failed_honeypot", "failed_navigation", "failed_unknown",
    "failed_form_not_found", "failed_not_workday",
    "failed_no_submit_button", "failed_submit_button",
    "failed_workday_unknown_step", "failed_workday_too_many_steps",
})

AUTO_APPLY_PERMANENT_FAILURES = frozenset({
    "failed_login_required", "failed_workday_login_required", "failed_captcha",
    "failed_honeypot", "failed_too_complex", "failed_too_many_essays",
    "failed_not_workday", "failed_no_resume_upload", "failed_resume_field_not_found",
    "failed_required_fields_unfilled",
})


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(500), nullable=False)
    company = Column(String(300), nullable=False)
    location = Column(String(300))
    salary_min = Column(Float, nullable=True)
    salary_max = Column(Float, nullable=True)
    salary_text = Column(String(200), nullable=True)
    description = Column(Text, nullable=True)
    url = Column(String(2000), nullable=False)
    source = Column(String(100), nullable=False)  # indeed, google_jobs, linkedin, greenhouse, lever, etc.
    source_id = Column(String(500), nullable=True)
    date_found = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    date_posted = Column(DateTime, nullable=True)
    is_remote = Column(Boolean, default=False)
    seniority_level = Column(String(100), nullable=True)
    dedup_hash = Column(String(64), nullable=False, unique=True)
    extra_urls = Column(JSON, nullable=True)  # Additional URLs from other sources
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)

    # Hourly comp + employment type (added in migration 005) — Behavioral Technician track
    pay_period = Column(String(20), nullable=True)       # hourly | annual | unknown
    hourly_min = Column(Float, nullable=True)
    hourly_max = Column(Float, nullable=True)
    employment_type = Column(String(20), nullable=True)  # part_time | full_time | contract | per_diem | unknown

    # Relationships
    score = relationship("JobScore", back_populates="job", uselist=False)
    application = relationship("Application", back_populates="job", uselist=False)

    __table_args__ = (
        Index("idx_jobs_dedup_hash", "dedup_hash"),
        Index("idx_jobs_date_found", "date_found"),
        Index("idx_jobs_company", "company"),
        Index("idx_jobs_company_id", "company_id"),
    )

    def __repr__(self):
        return f"<Job(id={self.id}, title='{self.title}', company='{self.company}')>"


class JobScore(Base):
    __tablename__ = "job_scores"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False, unique=True)
    fit_score = Column(Integer, nullable=False)  # 0-100, weighted overall
    key_matches = Column(JSON, nullable=True)  # List of matching skills/experiences
    key_gaps = Column(JSON, nullable=True)  # List of gaps
    ats_keywords = Column(JSON, nullable=True)  # Keywords to include in resume
    seniority_match = Column(Boolean, default=True)
    recommended_action = Column(String(50), nullable=True)  # apply, maybe, skip
    reasoning = Column(Text, nullable=True)
    scored_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Multi-dimensional scoring (added in migration 003)
    archetype = Column(String(50), nullable=True)            # data_analyst | data_scientist | quantitative_analyst | ml_engineer | business_analyst | product_analyst | unknown
    archetype_confidence = Column(Float, nullable=True)      # 0.0-1.0
    dimensions = Column(JSON, nullable=True)                 # {dim: 0-100} per the 10 dimensions in archetypes.yaml
    dimension_weights = Column(JSON, nullable=True)          # {dim: weight} frozen at scoring time
    evaluation_path = Column(String(1000), nullable=True)    # path to the 6-block markdown file

    # AI-forward signal (added in migration 004)
    ai_intensity = Column(Integer, nullable=True)            # 0-100: how central is building-with / using AI tooling day-to-day
    ai_tools = Column(JSON, nullable=True)                   # list of AI tools/tech the JD mentions, e.g. ["LLM APIs","RAG","Copilot"]

    # Relationships
    job = relationship("Job", back_populates="score")

    def __repr__(self):
        return f"<JobScore(job_id={self.job_id}, fit_score={self.fit_score}, archetype={self.archetype})>"


class Application(Base):
    __tablename__ = "applications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False, unique=True)
    status = Column(Enum(ApplicationStatus), default=ApplicationStatus.FOUND)
    resume_path = Column(String(1000), nullable=True)
    cover_letter_path = Column(String(1000), nullable=True)
    date_applied = Column(DateTime, nullable=True)
    response_date = Column(DateTime, nullable=True)
    interview_date = Column(DateTime, nullable=True)
    notes = Column(Text, nullable=True)
    lead_source = Column(String(100), nullable=True)   # "Bianca / Vantage Point", "scanner", ...
    next_action = Column(Text, nullable=True)

    # Auto-apply tracking (added in migration 002)
    auto_applied = Column(Boolean, default=False, nullable=False)
    auto_apply_status = Column(String(50), nullable=True)  # "submitted", "failed", "skipped_captcha", etc.
    auto_apply_log = Column(Text, nullable=True)            # JSON log of what the bot did
    auto_apply_attempted_at = Column(DateTime, nullable=True)

    # Relationships
    job = relationship("Job", back_populates="application")
    events = relationship("ApplicationEvent", back_populates="application",
                          order_by="ApplicationEvent.occurred_at")

    __table_args__ = (
        Index("idx_applications_status", "status"),
        Index("idx_applications_auto_applied", "auto_applied"),
    )

    def __repr__(self):
        return f"<Application(job_id={self.job_id}, status='{self.status.value}', auto_applied={self.auto_applied})>"


class Company(Base):
    """A target company with a tailored profile (Companies tab)."""

    __tablename__ = "companies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(300), nullable=False, unique=True)
    name_normalized = Column(String(300), nullable=False)
    careers_url = Column(String(2000), nullable=True)
    ats_platform = Column(String(50), nullable=True)   # greenhouse|ashby|lever|workday|radancy|custom
    priority = Column(String(20), nullable=True)       # high | medium | low
    status = Column(String(20), default="target")      # target | watch | paused

    # Profile fields — LLM/seed-owned except notes_md (user-owned, never
    # written by the LLM or seed refresh).
    overview_md = Column(Text, nullable=True)
    why_fit_md = Column(Text, nullable=True)
    hiring_bar_md = Column(Text, nullable=True)
    notes_md = Column(Text, nullable=True)

    profile_source = Column(String(20), default="manual")  # seeded | llm | manual
    profile_backup = Column(JSON, nullable=True)   # pre-refresh snapshot (one-step undo)
    draft_status = Column(String(20), nullable=True)  # drafting | failed | None=idle
    suggested = Column(Boolean, default=False, nullable=False)
    last_refreshed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("idx_companies_name_normalized", "name_normalized"),
    )

    def __repr__(self):
        return f"<Company(id={self.id}, name='{self.name}')>"


class ApplicationEvent(Base):
    """Historized status change — one row per transition (advisor timeline)."""

    __tablename__ = "application_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    application_id = Column(Integer, ForeignKey("applications.id"), nullable=False)
    occurred_at = Column(DateTime, nullable=False)
    from_status = Column(String(50), nullable=True)   # null for creation/backfill
    to_status = Column(String(50), nullable=False)
    note = Column(Text, nullable=True)
    # dashboard | extension | auto_applier | review_ui | ranker | tailor | quick_add | backfill
    source = Column(String(30), nullable=False)

    application = relationship("Application", back_populates="events")

    __table_args__ = (
        Index("idx_application_events_app_id", "application_id"),
        Index("idx_application_events_occurred", "occurred_at"),
    )

    def __repr__(self):
        return f"<ApplicationEvent(app_id={self.application_id}, to='{self.to_status}')>"


class ScanLog(Base):
    __tablename__ = "scan_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(100), nullable=False)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    jobs_found = Column(Integer, default=0)
    jobs_new = Column(Integer, default=0)
    errors = Column(Text, nullable=True)
    duration_seconds = Column(Float, nullable=True)

    def __repr__(self):
        return f"<ScanLog(source='{self.source}', jobs_found={self.jobs_found}, jobs_new={self.jobs_new})>"


class LearnedAnswer(Base):
    """A question the user answered by hand, replayed on later applications.

    Written only by POST /api/autofill/learned (the extension captures what the
    human types into a field autofill could not fill); read by /plan. Never
    holds credentials or per-posting values — agents.autofill_mapper's
    is_learnable_field()/is_learnable_value() are the gate, applied on the WRITE
    path so a bad value never reaches disk.

    No migration file: db.database.init_db() calls Base.metadata.create_all,
    which creates brand-new tables (migrations here exist only for ALTERs).
    """

    __tablename__ = "learned_answers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(500), nullable=False)     # normalized question label
    label = Column(String(500), nullable=False)   # verbatim, for the dashboard

    value = Column(Text, nullable=False)          # what to answer next time
    field_type = Column(String(30), nullable=True)      # text|select|radio|...
    options_seen = Column(JSON, nullable=True)          # origin site's options
    section = Column(String(300), nullable=True)

    # A learned answer starts trusted but revocable: 'active' is replayed,
    # 'paused' is kept but ignored, so a wrong answer can be switched off
    # without losing the record of what was answered where.
    status = Column(String(20), nullable=False, default="active")
    times_used = Column(Integer, default=0, nullable=False)
    corrections = Column(Integer, default=0, nullable=False)

    origin_host = Column(String(300), nullable=True)
    origin_company = Column(String(300), nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_used_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("key", name="uq_learned_key"),
        Index("idx_learned_key", "key"),
    )

    def __repr__(self):
        return f"<LearnedAnswer(key='{self.key}', value='{(self.value or '')[:24]}')>"


class LearnedAnswerAlias(Base):
    """Another wording of a question already answered, pointing at the same row.

    Paraphrase is NOT a lexical problem: measured on real pairs,
    "preferred working arrangement" vs "which work setup do you prefer" scores
    53 on token_set_ratio, while "what is your preferred pronoun" — a totally
    different question — scores 85. No fuzzy threshold separates them, so an
    alias is never inferred from string similarity at fill time. It is either
    proposed by the local LLM (and shown to the user as needs-review on first
    use) or created because the user answered that exact wording themselves.
    """

    __tablename__ = "learned_answer_aliases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(500), nullable=False)     # normalize(label) of the rephrasing
    label = Column(String(500), nullable=False)   # verbatim, for the dashboard
    answer_id = Column(Integer, ForeignKey("learned_answers.id", ondelete="CASCADE"),
                       nullable=False)

    status = Column(String(20), nullable=False, default="active")  # active|rejected
    origin = Column(String(20), nullable=False, default="llm")     # llm|manual|exact
    confirmed = Column(Boolean, default=False, nullable=False)     # user saw it and kept it
    times_used = Column(Integer, default=0, nullable=False)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("key", name="uq_alias_key"),
        Index("idx_alias_key", "key"),
    )

    def __repr__(self):
        return f"<LearnedAnswerAlias(key='{self.key}' -> answer {self.answer_id})>"
