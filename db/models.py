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

    # Auto-apply tracking (added in migration 002)
    auto_applied = Column(Boolean, default=False, nullable=False)
    auto_apply_status = Column(String(50), nullable=True)  # "submitted", "failed", "skipped_captcha", etc.
    auto_apply_log = Column(Text, nullable=True)            # JSON log of what the bot did
    auto_apply_attempted_at = Column(DateTime, nullable=True)

    # Relationships
    job = relationship("Job", back_populates="application")

    __table_args__ = (
        Index("idx_applications_status", "status"),
        Index("idx_applications_auto_applied", "auto_applied"),
    )

    def __repr__(self):
        return f"<Application(job_id={self.job_id}, status='{self.status.value}', auto_applied={self.auto_applied})>"


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
