"""Windows desktop notifications for JobPilot."""

import logging
import platform

logger = logging.getLogger(__name__)


def _get_notifier():
    """Get the Windows toast notifier, or None if unavailable."""
    if platform.system() != "Windows":
        return None
    try:
        from win10toast import ToastNotifier
        return ToastNotifier()
    except ImportError:
        logger.debug("[notifications] win10toast not available")
        return None


def notify(title: str, message: str, duration: int = 10):
    """Send a Windows desktop toast notification.

    Args:
        title: Notification title.
        message: Notification body text.
        duration: How long to show the notification (seconds).
    """
    notifier = _get_notifier()
    if notifier:
        try:
            notifier.show_toast(
                title,
                message,
                duration=duration,
                threaded=True,
            )
        except Exception as e:
            logger.debug(f"[notifications] Toast failed: {e}")
    else:
        # Fallback: log the notification
        logger.info(f"[NOTIFICATION] {title}: {message}")


def notify_high_score_job(title: str, company: str, score: int):
    """Notify about a high-scoring job match."""
    notify(
        "JobPilot - High Match Found!",
        f"{title} at {company} (Score: {score})",
    )


def notify_materials_ready(count: int):
    """Notify that materials are ready for review."""
    notify(
        "JobPilot - Materials Ready",
        f"{count} application(s) ready for your review",
    )


def notify_daily_summary(found: int, applied: int, interviews: int):
    """Send the daily summary notification."""
    notify(
        "JobPilot - Daily Summary",
        f"Found: {found} | Applied: {applied} | Interviews: {interviews}",
    )
