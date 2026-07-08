"""Streamlit review dashboard for JobPilot."""

import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st
import yaml
from sqlalchemy import func

# Add parent to path for imports
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from db.database import get_session, init_db
from db.models import Job, JobScore, Application, ApplicationStatus, ScanLog


def load_config():
    config_path = Path(__file__).parent.parent / "config" / "settings.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


def get_stats():
    """Get summary statistics for the dashboard."""
    session = get_session()
    try:
        total_jobs = session.query(Job).count()
        today = datetime.now(timezone.utc).date()

        jobs_today = (
            session.query(Job)
            .filter(func.date(Job.date_found) == today)
            .count()
        )

        pending_review = (
            session.query(Application)
            .filter(Application.status.in_([
                ApplicationStatus.MATERIALS_READY,
                ApplicationStatus.QUEUED,
                ApplicationStatus.SCORED,
            ]))
            .count()
        )

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

        avg_score = session.query(func.avg(JobScore.fit_score)).scalar() or 0

        return {
            "total_jobs": total_jobs,
            "jobs_today": jobs_today,
            "pending_review": pending_review,
            "applied": applied,
            "interviews": interviews,
            "avg_score": round(avg_score, 1),
        }
    finally:
        session.close()


def get_pending_applications():
    """Get applications pending review, with job and score data."""
    session = get_session()
    try:
        results = (
            session.query(Application, Job, JobScore)
            .join(Job, Application.job_id == Job.id)
            .outerjoin(JobScore, JobScore.job_id == Job.id)
            .filter(Application.status.in_([
                ApplicationStatus.MATERIALS_READY,
                ApplicationStatus.QUEUED,
                ApplicationStatus.SCORED,
                ApplicationStatus.FOUND,
            ]))
            .order_by(JobScore.fit_score.desc().nullslast())
            .all()
        )
        # Detach from session so we can use after close
        data = []
        for app, job, score in results:
            data.append({
                "app_id": app.id,
                "job_id": job.id,
                "title": job.title,
                "company": job.company,
                "location": job.location or "",
                "salary_text": job.salary_text or "",
                "source": job.source,
                "url": job.url,
                "description": job.description or "",
                "date_found": job.date_found,
                "fit_score": score.fit_score if score else None,
                "key_matches": score.key_matches if score else [],
                "key_gaps": score.key_gaps if score else [],
                "ats_keywords": score.ats_keywords if score else [],
                "reasoning": score.reasoning if score else "",
                "status": app.status.value,
                "resume_path": app.resume_path,
                "cover_letter_path": app.cover_letter_path,
            })
        return data
    finally:
        session.close()


def get_all_applications():
    """Get all applications for the pipeline view."""
    session = get_session()
    try:
        results = (
            session.query(Application, Job, JobScore)
            .join(Job, Application.job_id == Job.id)
            .outerjoin(JobScore, JobScore.job_id == Job.id)
            .order_by(Application.id.desc())
            .limit(200)
            .all()
        )
        data = []
        for app, job, score in results:
            data.append({
                "app_id": app.id,
                "title": job.title,
                "company": job.company,
                "location": job.location or "",
                "url": job.url,
                "fit_score": score.fit_score if score else None,
                "status": app.status.value,
                "date_found": job.date_found,
                "date_applied": app.date_applied,
            })
        return data
    finally:
        session.close()


def update_application_status(app_id: int, new_status: ApplicationStatus):
    """Update an application's status."""
    session = get_session()
    try:
        app = session.query(Application).get(app_id)
        if app:
            app.status = new_status
            if new_status == ApplicationStatus.APPLIED:
                app.date_applied = datetime.now(timezone.utc)
            session.commit()
    finally:
        session.close()


def get_recent_scan_logs(limit: int = 20):
    """Get recent scan logs."""
    session = get_session()
    try:
        logs = (
            session.query(ScanLog)
            .order_by(ScanLog.timestamp.desc())
            .limit(limit)
            .all()
        )
        return [{
            "source": log.source,
            "timestamp": log.timestamp,
            "jobs_found": log.jobs_found,
            "jobs_new": log.jobs_new,
            "errors": log.errors,
            "duration": log.duration_seconds,
        } for log in logs]
    finally:
        session.close()


# ============================================================
# Streamlit App
# ============================================================

def main():
    st.set_page_config(
        page_title="JobPilot - Review Dashboard",
        page_icon="🎯",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    init_db()

    st.title("🎯 JobPilot - Application Review Dashboard")

    # Sidebar navigation
    page = st.sidebar.radio(
        "Navigation",
        ["📋 Review Queue", "📊 Pipeline", "📈 Stats", "⚙️ Scan Logs"],
    )

    if page == "📋 Review Queue":
        render_review_queue()
    elif page == "📊 Pipeline":
        render_pipeline()
    elif page == "📈 Stats":
        render_stats()
    elif page == "⚙️ Scan Logs":
        render_scan_logs()


def render_review_queue():
    """Render the main review queue page."""
    stats = get_stats()

    # Stats bar
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Jobs Found Today", stats["jobs_today"])
    col2.metric("Pending Review", stats["pending_review"])
    col3.metric("Applied", stats["applied"])
    col4.metric("Interviews", stats["interviews"])
    col5.metric("Avg Fit Score", stats["avg_score"])

    st.divider()

    applications = get_pending_applications()

    if not applications:
        st.info("No applications pending review. The scanner will find new jobs automatically!")
        return

    st.subheader(f"📋 {len(applications)} Applications Pending Review")

    for i, app in enumerate(applications):
        score_color = "🟢" if (app["fit_score"] or 0) >= 70 else "🟡" if (app["fit_score"] or 0) >= 50 else "🔴"
        score_text = f"{app['fit_score']}" if app["fit_score"] is not None else "Not scored"

        with st.expander(
            f"{score_color} **{app['title']}** at **{app['company']}** — Score: {score_text} | {app['location']}",
            expanded=(i == 0),
        ):
            col_left, col_right = st.columns([2, 1])

            with col_left:
                st.markdown(f"**Source:** {app['source']} | **Status:** {app['status']}")
                if app["salary_text"]:
                    st.markdown(f"**Salary:** {app['salary_text']}")
                st.markdown(f"**Found:** {app['date_found'].strftime('%Y-%m-%d %H:%M') if app['date_found'] else 'Unknown'}")

                if app["description"]:
                    with st.container(height=200):
                        st.markdown("**Job Description:**")
                        st.text(app["description"][:2000])

            with col_right:
                if app["key_matches"]:
                    st.markdown("**✅ Key Matches:**")
                    for match in app["key_matches"][:5]:
                        st.markdown(f"- {match}")

                if app["key_gaps"]:
                    st.markdown("**⚠️ Key Gaps:**")
                    for gap in app["key_gaps"][:3]:
                        st.markdown(f"- {gap}")

                if app["ats_keywords"]:
                    st.markdown("**🔑 ATS Keywords:**")
                    st.markdown(", ".join(app["ats_keywords"][:10]))

                if app["reasoning"]:
                    st.markdown(f"**Analysis:** {app['reasoning']}")

            # File links
            file_col1, file_col2 = st.columns(2)
            if app["resume_path"] and Path(app["resume_path"]).exists():
                with file_col1:
                    st.markdown(f"📄 Resume: `{Path(app['resume_path']).name}`")
            if app["cover_letter_path"] and Path(app["cover_letter_path"]).exists():
                with file_col2:
                    st.markdown(f"📝 Cover Letter: `{Path(app['cover_letter_path']).name}`")

            # Action buttons
            btn_col1, btn_col2, btn_col3, btn_col4 = st.columns(4)

            with btn_col1:
                if st.button("✅ Approve & Open Link", key=f"approve_{app['app_id']}"):
                    update_application_status(app["app_id"], ApplicationStatus.APPROVED)
                    webbrowser.open(app["url"])
                    st.success("Approved! Opening application page...")
                    st.rerun()

            with btn_col2:
                if st.button("📝 Mark Applied", key=f"applied_{app['app_id']}"):
                    update_application_status(app["app_id"], ApplicationStatus.APPLIED)
                    st.success("Marked as applied!")
                    st.rerun()

            with btn_col3:
                if st.button("⏭️ Skip", key=f"skip_{app['app_id']}"):
                    update_application_status(app["app_id"], ApplicationStatus.SKIPPED)
                    st.rerun()

            with btn_col4:
                if st.button("🔗 Open Job Link", key=f"link_{app['app_id']}"):
                    webbrowser.open(app["url"])


def render_pipeline():
    """Render the pipeline view showing all applications by status."""
    st.subheader("📊 Application Pipeline")

    applications = get_all_applications()

    if not applications:
        st.info("No applications yet.")
        return

    # Group by status
    status_groups = {}
    for app in applications:
        status = app["status"]
        if status not in status_groups:
            status_groups[status] = []
        status_groups[status].append(app)

    # Display as columns
    status_order = ["found", "scored", "queued", "materials_ready", "approved", "applied",
                    "interview", "response_received", "rejected", "no_response",
                    "no_longer_available", "skipped"]

    status_labels = {
        "found": "🔍 Found",
        "scored": "📊 Scored",
        "queued": "📋 Queued",
        "materials_ready": "📄 Materials Ready",
        "approved": "✅ Approved",
        "applied": "📨 Applied",
        "interview": "🎉 Interview",
        "response_received": "💬 Response",
        "rejected": "❌ Rejected",
        "no_response": "⏳ No Response",
        "no_longer_available": "🚫 No Longer Available",
        "skipped": "⏭️ Skipped",
    }

    for status in status_order:
        apps = status_groups.get(status, [])
        if apps:
            st.markdown(f"### {status_labels.get(status, status)} ({len(apps)})")
            for app in apps[:20]:
                score_text = f"Score: {app['fit_score']}" if app["fit_score"] is not None else ""
                st.markdown(
                    f"- **{app['title']}** at {app['company']} | {app['location']} {score_text}"
                )
            if len(apps) > 20:
                st.markdown(f"  ... and {len(apps) - 20} more")
            st.divider()


def render_stats():
    """Render statistics page."""
    st.subheader("📈 Job Search Statistics")
    stats = get_stats()

    col1, col2, col3 = st.columns(3)
    col1.metric("Total Jobs Found", stats["total_jobs"])
    col2.metric("Total Applied", stats["applied"])
    col3.metric("Interviews", stats["interviews"])

    # Score distribution
    session = get_session()
    try:
        scores = session.query(JobScore.fit_score).all()
        if scores:
            import collections
            score_values = [s[0] for s in scores if s[0] is not None]
            if score_values:
                st.markdown("### Score Distribution")
                bins = {"0-19": 0, "20-39": 0, "40-59": 0, "60-79": 0, "80-100": 0}
                for s in score_values:
                    if s < 20:
                        bins["0-19"] += 1
                    elif s < 40:
                        bins["20-39"] += 1
                    elif s < 60:
                        bins["40-59"] += 1
                    elif s < 80:
                        bins["60-79"] += 1
                    else:
                        bins["80-100"] += 1
                st.bar_chart(bins)

        # Applications by source
        sources = session.query(Job.source, func.count(Job.id)).group_by(Job.source).all()
        if sources:
            st.markdown("### Jobs by Source")
            source_data = {s[0]: s[1] for s in sources}
            st.bar_chart(source_data)
    finally:
        session.close()


def render_scan_logs():
    """Render scan logs page."""
    st.subheader("⚙️ Recent Scan Logs")

    logs = get_recent_scan_logs()

    if not logs:
        st.info("No scan logs yet. Scans will appear here once the scheduler runs.")
        return

    for log in logs:
        status_icon = "✅" if not log["errors"] else "⚠️"
        timestamp = log["timestamp"].strftime("%Y-%m-%d %H:%M:%S") if log["timestamp"] else "Unknown"

        st.markdown(
            f"{status_icon} **{log['source']}** — {timestamp} — "
            f"Found: {log['jobs_found']}, New: {log['jobs_new']} "
            f"({log['duration']:.1f}s)"
        )
        if log["errors"]:
            with st.expander("Show errors"):
                st.code(log["errors"])


if __name__ == "__main__":
    main()
