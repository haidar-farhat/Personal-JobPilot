"""Desktop notifications for JobPilot — Windows toast or macOS Notification Center."""

import logging
import platform
import subprocess

logger = logging.getLogger(__name__)

_SYSTEM = platform.system()


def _get_notifier():
    """Get the Windows toast notifier, or None if unavailable."""
    if _SYSTEM != "Windows":
        return None
    try:
        from win10toast import ToastNotifier
        return ToastNotifier()
    except ImportError:
        logger.debug("[notifications] win10toast not available")
        return None


def _notify_macos(title: str, message: str) -> bool:
    """Post to macOS Notification Center via osascript. Returns True on success."""
    # osascript string literals escape double quotes with backslash
    t = title.replace("\\", "\\\\").replace('"', '\\"')
    m = message.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{m}" with title "{t}"'
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=True,
            capture_output=True,
            timeout=10,
        )
        return True
    except Exception as e:
        logger.debug(f"[notifications] osascript failed: {e}")
        return False


def notify(title: str, message: str, duration: int = 10):
    """Send a desktop notification (Windows toast / macOS Notification Center).

    Args:
        title: Notification title.
        message: Notification body text.
        duration: How long to show the notification (seconds, Windows only).
    """
    if _SYSTEM == "Darwin":
        if _notify_macos(title, message):
            return
        logger.info(f"[NOTIFICATION] {title}: {message}")
        return

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
