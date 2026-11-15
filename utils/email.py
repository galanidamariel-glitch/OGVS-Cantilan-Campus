"""Resend API delivery for notification emails stored in the outbox."""
import json
import urllib.error
import urllib.request

from flask import current_app

from database import get_db

RESEND_EMAILS_URL = "https://api.resend.com/emails"
MAX_EMAIL_ATTEMPTS = 5
EMAIL_BATCH_SIZE = 20


class EmailDeliveryError(Exception):
    """Raised when Resend rejects an email or cannot be reached."""


def _send_email(api_key, sender, recipient, subject, message):
    payload = json.dumps({
        "from": sender,
        "to": [recipient],
        "subject": subject,
        "text": message,
    }).encode("utf-8")
    request = urllib.request.Request(
        RESEND_EMAILS_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if not 200 <= response.status < 300:
                raise EmailDeliveryError(
                    f"Resend returned HTTP {response.status}."
                )
    except urllib.error.HTTPError as error:
        detail = error.read(500).decode("utf-8", errors="replace")
        raise EmailDeliveryError(
            f"Resend returned HTTP {error.code}: {detail}"
        ) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise EmailDeliveryError(f"Resend request failed: {error}") from error


def send_pending_email_notifications():
    """Send queued emails after the request transaction has committed."""
    api_key = current_app.config.get("RESEND_API_KEY", "")
    sender = current_app.config.get("EMAIL_FROM", "")
    if not api_key or not sender:
        return

    conn = get_db()
    rows = conn.execute(
        """SELECT id, recipient_email, subject, message, attempts
           FROM email_outbox
           WHERE status = 'pending'
             AND (next_attempt_at IS NULL OR next_attempt_at <= datetime('now', 'localtime'))
           ORDER BY id
           LIMIT ?""",
        (EMAIL_BATCH_SIZE,),
    ).fetchall()
    for row in rows:
        try:
            _send_email(
                api_key, sender, row["recipient_email"], row["subject"], row["message"]
            )
        except EmailDeliveryError as error:
            attempts = row["attempts"] + 1
            status = "failed" if attempts >= MAX_EMAIL_ATTEMPTS else "pending"
            retry_delay = min(60 * (2 ** (attempts - 1)), 3600)
            conn.execute(
                """UPDATE email_outbox
                   SET status = ?, attempts = ?, last_error = ?,
                       next_attempt_at = datetime('now', 'localtime', ?)
                   WHERE id = ?""",
                (status, attempts, str(error)[:1000], f"+{retry_delay} seconds", row["id"]),
            )
            current_app.logger.error(
                "Email delivery failed for outbox item %s (attempt %s/%s): %s",
                row["id"], attempts, MAX_EMAIL_ATTEMPTS, error,
            )
        else:
            conn.execute(
                """UPDATE email_outbox
                   SET status = 'sent', attempts = attempts + 1,
                       sent_at = datetime('now', 'localtime'), last_error = NULL
                   WHERE id = ?""",
                (row["id"],),
            )
    conn.commit()
    conn.close()
