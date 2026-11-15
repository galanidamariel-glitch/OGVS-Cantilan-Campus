from flask import (
    Blueprint, render_template, session, request, redirect, url_for,
    flash, abort,
)
from pathlib import Path
from werkzeug.utils import secure_filename

from database import get_db
from utils.audit import log_transaction, notify_admins, notify_user
from utils.decorators import role_required
from utils.grading import compute_final
from utils.storage import save_profile_image

bp = Blueprint("faculty", __name__, url_prefix="/faculty")
ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}


def _offering_belongs_to_me(conn, offering_id):
    row = conn.execute(
        "SELECT * FROM class_offerings WHERE id = ? AND faculty_user_id = ?",
        (offering_id, session["user_id"]),
    ).fetchone()
    if not row:
        abort(403)
    return row


def _log_grade_save(conn, student_user_id, subject):
    stu = conn.execute("SELECT full_name, student_id FROM students WHERE user_id = ?",
                       (student_user_id,)).fetchone()
    if stu:
        log_transaction(conn, session["user_id"], "faculty", "Saved grade draft",
                        f"{subject['code']} - {stu['full_name']} ({stu['student_id']})")


def _offering_label(conn, offering_id):
    row = conn.execute(
        """SELECT s.code, s.name, sec.program, sec.year_level, sec.section_name
           FROM class_offerings co JOIN subjects s ON s.id = co.subject_id
           JOIN sections sec ON sec.id = co.section_id WHERE co.id = ?""",
        (offering_id,),
    ).fetchone()
    return (f"{row['code']} ({row['program']} {row['year_level']}-{row['section_name']})"
            if row else f"class {offering_id}")


def _my_name(conn):
    row = conn.execute("SELECT full_name FROM faculty WHERE user_id = ?",
                       (session["user_id"],)).fetchone()
    return row["full_name"] if row else session.get("username", "A faculty member")


@bp.route("/dashboard")
@role_required("faculty")
def dashboard():
    conn = get_db()
    faculty = conn.execute(
        """SELECT f.*, u.email FROM faculty f
           JOIN users u ON u.id = f.user_id WHERE f.user_id = ?""",
        (session["user_id"],),
    ).fetchone()
    offerings = conn.execute(
        """SELECT co.*, s.code, s.name, sec.program, sec.year_level, sec.section_name,
                  (SELECT COUNT(*) FROM enrollments e WHERE e.offering_id = co.id) AS enrolled
           FROM class_offerings co
           JOIN subjects s ON s.id = co.subject_id
           JOIN sections sec ON sec.id = co.section_id
           WHERE co.faculty_user_id = ?
           ORDER BY co.school_year DESC, co.semester, s.code""",
        (session["user_id"],),
    ).fetchall()
    pending_corrections = conn.execute(
        """SELECT COUNT(*) FROM correction_requests cr
           JOIN grades g ON g.id = cr.grade_id
           JOIN enrollments e ON e.id = g.enrollment_id
           JOIN class_offerings co ON co.id = e.offering_id
           WHERE co.faculty_user_id = ? AND cr.status = 'pending'""",
        (session["user_id"],),
    ).fetchone()[0]
    unread = conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0",
        (session["user_id"],),
    ).fetchone()[0]
    conn.close()
    return render_template(
        "faculty/dashboard.html", offerings=offerings,
        pending_corrections=pending_corrections, unread=unread, faculty=faculty,
    )


@bp.route("/profile", methods=["POST"])
@role_required("faculty")
def update_profile():
    image = request.files.get("profile_image")
    if not image or not image.filename:
        flash("Choose a profile picture before uploading.", "warning")
        return redirect(url_for("faculty.dashboard"))

    extension = Path(secure_filename(image.filename)).suffix.lower().lstrip(".")
    if extension not in ALLOWED_IMAGE_EXTENSIONS:
        flash("Profile pictures must be JPG, PNG, or WEBP files.", "danger")
        return redirect(url_for("faculty.dashboard"))

    conn = get_db()
    faculty_exists = conn.execute(
        "SELECT 1 FROM faculty WHERE user_id = ?", (session["user_id"],)
    ).fetchone()
    if not faculty_exists:
        conn.close()
        abort(404)

    filename = f"faculty_{session['user_id']}.{extension}"
    profile_image = save_profile_image(image, filename)
    conn.execute(
        "UPDATE faculty SET profile_image = ? WHERE user_id = ?",
        (profile_image, session["user_id"]),
    )
    conn.commit()
    conn.close()
    flash("Profile picture updated.", "success")
    return redirect(url_for("faculty.dashboard"))


@bp.route("/classes")
@role_required("faculty")
def classes():
    conn = get_db()
    offerings = conn.execute(
        """SELECT co.*, s.code, s.name, sec.program, sec.year_level, sec.section_name,
                  (SELECT COUNT(*) FROM enrollments e WHERE e.offering_id = co.id) AS enrolled
           FROM class_offerings co
           JOIN subjects s ON s.id = co.subject_id
           JOIN sections sec ON sec.id = co.section_id
           WHERE co.faculty_user_id = ?
           ORDER BY co.school_year DESC, co.semester, s.code""",
        (session["user_id"],),
    ).fetchall()
    conn.close()
    return render_template("faculty/classes.html", offerings=offerings)


@bp.route("/classes/<int:offering_id>/encode", methods=["GET", "POST"])
@role_required("faculty")
def encode(offering_id):
    conn = get_db()
    offering = _offering_belongs_to_me(conn, offering_id)
    subject = conn.execute("SELECT * FROM subjects WHERE id = ?",
                            (offering["subject_id"],)).fetchone()
    section = conn.execute("SELECT * FROM sections WHERE id = ?",
                            (offering["section_id"],)).fetchone()

    if request.method == "POST":
        student_user_id = int(request.form["student_user_id"])
        enrollment = conn.execute(
            "SELECT * FROM enrollments WHERE student_user_id = ? AND offering_id = ?",
            (student_user_id, offering_id),
        ).fetchone()
        if not enrollment:
            abort(404)

        def parse(field):
            val = request.form.get(field, "").strip()
            return float(val) if val else None

        prelim, midterm, finals = parse("prelim"), parse("midterm"), parse("finals")
        if any(value is not None and not 1 <= value <= 5 for value in (prelim, midterm, finals)):
            flash("Grades must be between 1.00 and 5.00.", "danger")
            return redirect(url_for("faculty.encode", offering_id=offering_id))
        final_grade, remarks = compute_final(prelim, midterm, finals)

        existing = conn.execute(
            "SELECT * FROM grades WHERE enrollment_id = ?", (enrollment["id"],)
        ).fetchone()
        if existing and existing["status"] in ("approved", "released"):
            flash("This grade is locked (approved/released). Submit a correction "
                  "request instead of editing it directly.", "danger")
        elif existing:
            conn.execute(
                """UPDATE grades SET prelim=?, midterm=?, finals=?, final_grade=?,
                   remarks=?, status='draft', updated_at=datetime('now')
                   WHERE id = ?""",
                (prelim, midterm, finals, final_grade, remarks, existing["id"]),
            )
            _log_grade_save(conn, student_user_id, subject)
            conn.commit()
            flash("Grade saved as draft.", "success")
        else:
            conn.execute(
                """INSERT INTO grades
                   (enrollment_id, prelim, midterm, finals, final_grade, remarks, status)
                   VALUES (?,?,?,?,?,?, 'draft')""",
                (enrollment["id"], prelim, midterm, finals, final_grade, remarks),
            )
            _log_grade_save(conn, student_user_id, subject)
            conn.commit()
            flash("Grade saved as draft.", "success")

    students = conn.execute(
        """SELECT st.user_id, st.student_id, st.full_name, g.id AS grade_id,
                  g.prelim, g.midterm, g.finals, g.final_grade, g.remarks, g.status
           FROM enrollments e
           JOIN students st ON st.user_id = e.student_user_id
           LEFT JOIN grades g ON g.enrollment_id = e.id
           WHERE e.offering_id = ?
           ORDER BY st.full_name""",
        (offering_id,),
    ).fetchall()
    conn.close()
    return render_template(
        "faculty/encode.html", offering=offering, subject=subject, section=section,
        students=students,
    )


@bp.route("/classes/<int:offering_id>/submit", methods=["POST"])
@role_required("faculty")
def submit_for_review(offering_id):
    conn = get_db()
    _offering_belongs_to_me(conn, offering_id)
    cur = conn.execute(
        """UPDATE grades SET status = 'submitted', updated_at = datetime('now')
           WHERE status = 'draft' AND enrollment_id IN (
               SELECT id FROM enrollments WHERE offering_id = ?)""",
        (offering_id,),
    )
    if cur.rowcount:
        label = _offering_label(conn, offering_id)
        notify_admins(conn, f"Faculty request: {_my_name(conn)} submitted {cur.rowcount} "
                            f"grade(s) for {label} for review.")
        log_transaction(conn, session["user_id"], "faculty", "Submitted grades for review",
                        f"{label}: {cur.rowcount} grade(s)")
    conn.commit()
    conn.close()
    flash("Draft grades submitted to the Registrar for review.", "success")
    return redirect(url_for("faculty.encode", offering_id=offering_id))


@bp.route("/classes/<int:offering_id>/release", methods=["POST"])
@role_required("faculty")
def release(offering_id):
    conn = get_db()
    _offering_belongs_to_me(conn, offering_id)
    rows = conn.execute(
        """SELECT g.id, e.student_user_id FROM grades g
           JOIN enrollments e ON e.id = g.enrollment_id
           WHERE g.status = 'approved' AND e.offering_id = ?""",
        (offering_id,),
    ).fetchall()
    for r in rows:
        conn.execute(
            "UPDATE grades SET status = 'released', updated_at = datetime('now') WHERE id = ?",
            (r["id"],),
        )
        notify_user(
            conn, r["student_user_id"],
            "A new grade has been released. Check your dashboard.",
        )
    if rows:
        log_transaction(conn, session["user_id"], "faculty", "Released grades",
                        f"{_offering_label(conn, offering_id)}: {len(rows)} grade(s)")
    conn.commit()
    conn.close()
    flash(f"{len(rows)} approved grade(s) released to students.", "success")
    return redirect(url_for("faculty.encode", offering_id=offering_id))


@bp.route("/grades/<int:grade_id>/correction", methods=["POST"])
@role_required("faculty")
def request_correction(grade_id):
    reason = request.form.get("reason", "").strip()
    conn = get_db()
    grade = conn.execute(
        """SELECT g.* FROM grades g
           JOIN enrollments e ON e.id = g.enrollment_id
           JOIN class_offerings co ON co.id = e.offering_id
           WHERE g.id = ? AND co.faculty_user_id = ?""",
        (grade_id, session["user_id"]),
    ).fetchone()
    if not grade:
        abort(404)
    if not reason:
        flash("Please provide a reason for the correction request.", "danger")
    elif grade["status"] != "released":
        flash("Only released grades can have a correction request filed.", "danger")
    else:
        conn.execute(
            "INSERT INTO correction_requests (grade_id, reason) VALUES (?,?)",
            (grade_id, reason),
        )
        info = conn.execute(
            """SELECT st.full_name, st.student_id, s.code FROM grades g
               JOIN enrollments e ON e.id = g.enrollment_id
               JOIN students st ON st.user_id = e.student_user_id
               JOIN class_offerings co ON co.id = e.offering_id
               JOIN subjects s ON s.id = co.subject_id WHERE g.id = ?""",
            (grade_id,),
        ).fetchone()
        notify_admins(conn, f"Faculty request: {_my_name(conn)} asked for a grade correction - "
                            f"{info['code']}, {info['full_name']} ({info['student_id']}). "
                            f"Reason: {reason}")
        log_transaction(conn, session["user_id"], "faculty", "Filed correction request",
                        f"{info['code']} - {info['full_name']} ({info['student_id']})")
        conn.commit()
        flash("Correction request submitted to the Registrar.", "success")
    offering_id = conn.execute(
        "SELECT offering_id FROM enrollments WHERE id = ?", (grade["enrollment_id"],)
    ).fetchone()[0]
    conn.close()
    return redirect(url_for("faculty.encode", offering_id=offering_id))


@bp.route("/notifications")
@role_required("faculty")
def notifications():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM notifications WHERE user_id = ? ORDER BY created_at DESC",
        (session["user_id"],),
    ).fetchall()
    conn.execute(
        "UPDATE notifications SET is_read = 1 WHERE user_id = ?", (session["user_id"],)
    )
    conn.commit()
    conn.close()
    return render_template("faculty/notifications.html", rows=rows)


# ------------------------------------------------------- class list (roster) -
@bp.route("/classes/<int:offering_id>/students")
@role_required("faculty")
def roster(offering_id):
    """Each subject shows its students, with add and edit (General Comments #4)."""
    conn = get_db()
    offering = _offering_belongs_to_me(conn, offering_id)
    subject = conn.execute("SELECT * FROM subjects WHERE id = ?", (offering["subject_id"],)).fetchone()
    section = conn.execute("SELECT * FROM sections WHERE id = ?", (offering["section_id"],)).fetchone()
    students = conn.execute(
        """SELECT st.* FROM enrollments e JOIN students st ON st.user_id = e.student_user_id
           WHERE e.offering_id = ? ORDER BY st.full_name""",
        (offering_id,),
    ).fetchall()
    nominations = conn.execute(
        """SELECT student_id, full_name, status, requested_at
           FROM student_nominations
           WHERE offering_id = ? AND faculty_user_id = ?
           ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, requested_at DESC""",
        (offering_id, session["user_id"]),
    ).fetchall()
    conn.close()
    return render_template("faculty/roster.html", offering=offering, subject=subject,
                           section=section, students=students, nominations=nominations)


@bp.route("/classes/<int:offering_id>/students/add", methods=["POST"])
@role_required("faculty")
def add_student(offering_id):
    """Nominate a student for a class; the registrar makes the final decision."""
    conn = get_db()
    offering = _offering_belongs_to_me(conn, offering_id)
    student_id = request.form.get("student_id", "").strip()
    full_name = request.form.get("full_name", "").strip()
    student = conn.execute(
        """SELECT st.*, u.is_active FROM students st JOIN users u ON u.id = st.user_id
           WHERE st.student_id = ?""",
        (student_id,),
    ).fetchone() if student_id else None
    pre_enrolled = conn.execute(
        "SELECT * FROM pre_enrolled_students WHERE student_id = ?", (student_id,)
    ).fetchone() if student_id else None
    if not student_id:
        flash("Enter the Student ID of the student you want to add.", "danger")
    elif student and not student["is_active"]:
        flash(f"{student['full_name']}'s account is deactivated and cannot be added.", "danger")
    elif student and conn.execute(
            "SELECT 1 FROM enrollments WHERE student_user_id = ? AND offering_id = ?",
            (student["user_id"], offering_id)).fetchone():
        flash(f"{student['full_name']} is already in this class list.", "warning")
    else:
        verified_name = student["full_name"] if student else (
            pre_enrolled["full_name"] if pre_enrolled else full_name
        )
        if not verified_name:
            flash("Enter the student's full name so the Registrar can review the nomination.",
                  "danger")
        elif pre_enrolled and full_name and not student and (
                full_name.casefold() != pre_enrolled["full_name"].strip().casefold()):
            flash("The name does not match the Registrar's existing enrollment record.", "danger")
        else:
            existing = conn.execute(
                """SELECT id, status FROM student_nominations
                   WHERE offering_id = ? AND student_id = ?""",
                (offering_id, student_id),
            ).fetchone()
            if existing and existing["status"] in ("pending", "approved"):
                flash("This student already has a pending or approved nomination for this class.",
                      "warning")
            else:
                conn.execute(
                    """INSERT INTO student_nominations
                       (faculty_user_id, offering_id, student_id, full_name, status,
                        requested_at, reviewed_at, reviewed_by)
                       VALUES (?, ?, ?, ?, 'pending', datetime('now', 'localtime'), NULL, NULL)
                       ON CONFLICT(offering_id, student_id) DO UPDATE SET
                           faculty_user_id = excluded.faculty_user_id,
                           full_name = excluded.full_name,
                           status = 'pending',
                           requested_at = datetime('now', 'localtime'),
                           reviewed_at = NULL,
                           reviewed_by = NULL""",
                    (session["user_id"], offering_id, student_id, verified_name),
                )
                label = _offering_label(conn, offering_id)
                notify_admins(
                    conn,
                    f"Faculty nomination: {_my_name(conn)} nominated {verified_name} "
                    f"({student_id}) for {label}. Registrar approval is required.",
                )
                log_transaction(
                    conn, session["user_id"], "faculty", "Nominated student for class",
                    f"{verified_name} ({student_id}) -> {label}",
                )
                conn.commit()
                flash("Student nomination sent to the Registrar for approval.", "success")
    conn.close()
    return redirect(url_for("faculty.roster", offering_id=offering_id))


@bp.route("/classes/<int:offering_id>/students/<student_id>/edit", methods=["GET", "POST"])
@role_required("faculty")
def edit_student(offering_id, student_id):
    """Edit a student entry that belongs to one of the instructor's classes."""
    conn = get_db()
    offering = _offering_belongs_to_me(conn, offering_id)
    student = conn.execute(
        """SELECT st.* FROM students st
           JOIN enrollments e ON e.student_user_id = st.user_id
           WHERE st.student_id = ? AND e.offering_id = ?""",
        (student_id, offering_id),
    ).fetchone()
    if not student:
        conn.close()
        abort(404)
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        contact = request.form.get("contact_number", "").strip()
        if not full_name:
            flash("The student's name cannot be empty.", "danger")
        else:
            conn.execute("UPDATE students SET full_name = ?, contact_number = ? WHERE user_id = ?",
                         (full_name, contact, student["user_id"]))
            conn.execute("UPDATE pre_enrolled_students SET full_name = ? WHERE student_id = ?",
                         (full_name, student_id))
            log_transaction(conn, session["user_id"], "faculty", "Edited student entry",
                            f"{student['full_name']} -> {full_name} ({student_id}), "
                            f"{_offering_label(conn, offering_id)}")
            conn.commit()
            conn.close()
            flash("Student entry updated.", "success")
            return redirect(url_for("faculty.roster", offering_id=offering_id))
    subject = conn.execute("SELECT * FROM subjects WHERE id = ?", (offering["subject_id"],)).fetchone()
    conn.close()
    return render_template("faculty/student_edit.html", offering=offering, subject=subject,
                           student=student)


# --------------------------------------------------- transaction history (live) -
@bp.route("/transactions")
@role_required("faculty")
def transactions():
    conn = get_db()
    rows = _my_transactions(conn)
    conn.close()
    return render_template("shared/transactions.html", rows=rows, role_filter="",
                           feed_url=url_for("faculty.transactions_feed"),
                           scope="My transactions", can_filter=False)


@bp.route("/transactions/feed")
@role_required("faculty")
def transactions_feed():
    conn = get_db()
    rows = _my_transactions(conn)
    conn.close()
    return render_template("shared/_transaction_rows.html", rows=rows, show_actor=False)


def _my_transactions(conn):
    return conn.execute(
        "SELECT * FROM transactions WHERE actor_user_id = ? ORDER BY id DESC LIMIT 500",
        (session["user_id"],),
    ).fetchall()
