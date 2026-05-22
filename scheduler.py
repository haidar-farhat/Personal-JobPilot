"""JobPilot Scheduler — Main entry point that orchestrates all pipeline tasks."""

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from rich.console import Console
from rich.logging import RichHandler

from db.database import init_db
from utils.ollama_client import check_ollama_health
from utils.notifications import notify, notify_high_score_job, notify_materials_ready, notify_daily_summary

console = Console()

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[RichHandler(console=console, rich_tracebacks=True)],
)
logger = logging.getLogger("jobpilot")


def load_config() -> dict:
    config_path = Path(__file__).parent / "config" / "settings.yaml"
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ============================================================
# Task functions
# ============================================================

def task_scan_career_pages():
    """Run ATS-API scanners (Greenhouse + Lever + Ashby).

    Replaces the old Indeed/Google Jobs scrapers — ATS APIs are zero-ToS-risk
    and more reliable than HTML scraping (career-ops scan.mjs pattern).
    """
    try:
        config = load_config()

        from agents.scanner.career_pages import GreenhouseScanner, LeverScanner
        from agents.scanner.ashby import AshbyScanner

        gh_result = GreenhouseScanner(config).run()
        lever_result = LeverScanner(config).run()
        ashby_result = AshbyScanner(config).run()

        total_new = gh_result["jobs_new"] + lever_result["jobs_new"] + ashby_result["jobs_new"]
        if total_new > 0:
            logger.info(f"[career_pages] {total_new} new jobs (gh={gh_result['jobs_new']}, lever={lever_result['jobs_new']}, ashby={ashby_result['jobs_new']})")
    except Exception as e:
        logger.error(f"[career_pages] Scan failed: {e}")


def task_rank_jobs():
    """Score unscored jobs using Gemma 4."""
    try:
        if not check_ollama_health():
            logger.warning("[ranker] Ollama not available — skipping ranking")
            return

        config = load_config()
        from agents.ranker import rank_new_jobs
        result = rank_new_jobs(config)

        # Notify about high-score jobs
        if result["queued"] > 0:
            from db.database import get_session
            from db.models import Job, JobScore, ApplicationStatus, Application
            session = get_session()
            try:
                threshold = config.get("notifications", {}).get("high_score_threshold", 80)
                high_scores = (
                    session.query(Job, JobScore)
                    .join(JobScore, Job.id == JobScore.job_id)
                    .join(Application, Job.id == Application.job_id)
                    .filter(JobScore.fit_score >= threshold)
                    .filter(Application.status == ApplicationStatus.QUEUED)
                    .all()
                )
                for job, score in high_scores:
                    notify_high_score_job(job.title, job.company, score.fit_score)
            finally:
                session.close()

    except Exception as e:
        logger.error(f"[ranker] Ranking failed: {e}")


def task_tailor_resumes():
    """Generate tailored resumes and cover letters for queued jobs."""
    try:
        if not check_ollama_health():
            logger.warning("[tailor] Ollama not available — skipping tailoring")
            return

        config = load_config()
        from agents.tailor import tailor_queued_jobs
        result = tailor_queued_jobs(config)

        if result["tailored"] > 0:
            notify_materials_ready(result["tailored"])

    except Exception as e:
        logger.error(f"[tailor] Tailoring failed: {e}")


def task_auto_apply():
    """Auto-submit applications for high-fit jobs with materials ready.

    Reads guardrails from config/applicant_profile.yaml:
      - guardrails.enabled
      - guardrails.min_score        (default 75)
      - guardrails.daily_cap        (default 10)
      - guardrails.allowed_ats_platforms  (default: greenhouse, ashby, lever)
      - guardrails.dry_run          (default False)

    Halts on CAPTCHA, missing files, or N consecutive failures. Per-application
    timeout prevents hanging on a stuck form.
    """
    try:
        from agents.auto_applier.runner import run_auto_apply
        result = run_auto_apply(load_config())
        if result.get("submitted", 0) > 0:
            logger.info(
                f"[auto_apply] submitted {result['submitted']} application(s) "
                f"({result.get('failed_other', 0)} failed, "
                f"{result.get('failed_captcha', 0)} captcha)"
            )
    except Exception as e:
        logger.error(f"[auto_apply] cycle failed: {e}")


def task_daily_summary():
    """Generate and send daily summary."""
    try:
        from db.database import get_session
        from db.models import Job, Application, ApplicationStatus
        from sqlalchemy import func

        session = get_session()
        try:
            today = datetime.now(timezone.utc).date()

            found = session.query(Job).filter(func.date(Job.date_found) == today).count()
            applied = (
                session.query(Application)
                .filter(Application.status == ApplicationStatus.APPLIED)
                .count()
            )
            interviews = (
                session.query(Application)
                .filter(Application.status == ApplicationStatus.INTERVIEW)
                .count()
            )

            notify_daily_summary(found, applied, interviews)
        finally:
            session.close()

    except Exception as e:
        logger.error(f"[summary] Daily summary failed: {e}")


# ============================================================
# Startup
# ============================================================

def run_initial_scan():
    """Run all scanners once on startup."""
    console.print("\n[bold cyan]Running initial scan...[/bold cyan]\n")

    task_scan_career_pages()
    task_rank_jobs()
    task_tailor_resumes()
    task_auto_apply()

    console.print("\n[bold green]Initial scan complete![/bold green]\n")


def main():
    # Force UTF-8 stdout on Windows so rich's unicode glyphs (✓, ⚡, ●) don't crash
    # the scheduler when launched as a background task / under cp1252 cmd shells.
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    config = load_config()
    schedule = config.get("schedule", {})
    intervals = schedule.get("intervals", {})
    active_start = schedule.get("active_hours", {}).get("start", 7)
    active_end = schedule.get("active_hours", {}).get("end", 22)

    # Initialize database
    console.print("[bold]Initializing JobPilot...[/bold]")
    init_db()

    # Check Ollama
    if check_ollama_health():
        console.print("[green]✓ Ollama is running and model is available[/green]")
    else:
        console.print("[yellow]⚠ Ollama not available — ranking/tailoring will be skipped until it's running[/yellow]")

    # Run initial scan
    run_initial_scan()

    # Set up scheduler
    scheduler = BlockingScheduler()

    # ATS API scan (Greenhouse + Lever + Ashby) — replaces Indeed + Google Jobs scrapers
    scheduler.add_job(
        task_scan_career_pages,
        IntervalTrigger(minutes=intervals.get("career_pages_scan_minutes", 30)),
        id="career_pages_scan",
        name="Career Pages Scanner (Greenhouse + Lever + Ashby)",
        max_instances=1,
    )

    # Ranking (run after each scan - every 15 min)
    scheduler.add_job(
        task_rank_jobs,
        IntervalTrigger(minutes=15),
        id="rank_jobs",
        name="Job Ranker",
        max_instances=1,
    )

    # Tailoring (run periodically for queued jobs)
    scheduler.add_job(
        task_tailor_resumes,
        IntervalTrigger(minutes=20),
        id="tailor_resumes",
        name="Resume Tailor",
        max_instances=1,
    )

    # Auto-apply — submit forms for high-fit jobs with materials ready
    # (runs after tailor so it picks up freshly-generated materials)
    scheduler.add_job(
        task_auto_apply,
        IntervalTrigger(minutes=25),
        id="auto_apply",
        name="Auto-Applier (Greenhouse/Ashby/Lever)",
        max_instances=1,
    )

    # Daily summary
    summary_hour = config.get("notifications", {}).get("daily_summary_hour", 8)
    scheduler.add_job(
        task_daily_summary,
        CronTrigger(hour=summary_hour, minute=0),
        id="daily_summary",
        name="Daily Summary",
    )

    # Print schedule
    console.print("\n[bold]Scheduled tasks:[/bold]")
    for job in scheduler.get_jobs():
        console.print(f"  • {job.name}: {job.trigger}")

    console.print(f"\n[bold green]JobPilot is running! Press Ctrl+C to stop.[/bold green]")
    console.print(f"[dim]Dashboard: run 'python -m server.dashboard' in another terminal → http://localhost:7777[/dim]\n")

    try:
        scheduler.start()
    except KeyboardInterrupt:
        console.print("\n[bold]Shutting down JobPilot...[/bold]")
        scheduler.shutdown()
        console.print("[green]Goodbye![/green]")


if __name__ == "__main__":
    main()
