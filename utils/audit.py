"""Transaction history + notification helpers shared by all route modules."""


def _actor_name(conn, user_id, role):
    if role == "faculty":
        row = conn.execute("SELECT full_name FROM faculty WHERE user_id = ?", (user_id,)).fetchone()
        if row:
            return row["full_name"]
    if role == "student":
        row = conn.execute("SELECT full_name FROM students WHERE user_id = ?", (user_id,)).fetchone()
        if row:
            return row["full_name"]
    row = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    if role == "admin":
        return "Registrar"
    return row["username"] if row else "System"


def log_transaction(conn, user_id, role, action, details=""):
    """Append one row to the transaction history. Caller commits."""
    conn.execute(
        "INSERT INTO transactions (actor_user_id, actor_name, actor_role, action, details) "
        "VALUES (?,?,?,?,?)",
        (user_id, _actor_name(conn, user_id, role), role, action, details),
    )


def notify_user(conn, user_id, message):
    cursor = conn.execute(
        "INSERT INTO notifications (user_id, message) VALUES (?,?)",
        (user_id, message),
    )
    user = conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
    if user and user["email"] and user["email"].strip():
        conn.execute(
            """INSERT INTO email_outbox
               (notification_id, recipient_email, subject, message)
               VALUES (?, ?, 'OGVS notification', ?)""",
            (cursor.lastrowid, user["email"].strip(), message),
        )


def notify_admins(conn, message):
    """Send a notification to every admin/registrar account."""
    for row in conn.execute("SELECT id FROM users WHERE role = 'admin' AND is_active = 1").fetchall():
        notify_user(conn, row["id"], message)
