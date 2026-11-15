from flask import (
    Blueprint, render_template, session, abort, send_file, request,
    redirect, url_for, flash,
)
import io
from pathlib import Path

from werkzeug.utils import secure_filename

from database import get_db
from utils.audit import log_transaction, notify_admins
from utils.decorators import role_required
from utils.grading import compute_gwa
from utils.pdf import build_grade_slip_pdf
from utils.qr import qr_data_uri
from utils.storage import save_profile_image

bp = Blueprint("student", __name__, url_prefix="/student")
ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}


def _student(conn):
    return conn.execute(
        "SELECT * FROM students WHERE user_id = ?", (session["user_id"],)
    ).fetchone()


def _terms_for_student(conn, user_id):
    """Distinct (semester, school_year) the student has enrollments in,
    most recent first (by school_year desc, semester order)."""
    rows = conn.execute(
        """SELECT DISTINCT co.semester, co.school_year
           FROM enrollments e
           JOIN class_offerings co ON co.id = e.offering_id
           WHERE e.student_user_id = ?
           ORDER BY co.school_year DESC, co.semester""",
        (user_id,),
    ).fetchall()
    return [(r["semester"], r["school_year"]) for r in rows]


def _grades_for_term(conn, user_id, semester, school_year):
    return conn.execute(
        """SELECT co.id AS offering_id, s.code, s.name, s.units, co.meeting_time, co.meeting_days,
              g.prelim, g.midterm, g.finals,
                  g.final_grade, g.remarks, g.status, f.full_name AS instructor
           FROM enrollments e
           JOIN class_offerings co ON co.id = e.offering_id
           JOIN subjects s ON s.id = co.subject_id
           LEFT JOIN grades g ON g.enrollment_id = e.id
           LEFT JOIN faculty f ON f.user_id = co.faculty_user_id
           WHERE e.student_user_id = ? AND co.semester = ? AND co.school_year = ?
           ORDER BY s.code""",
        (user_id, semester, school_year),
    ).fetchall()


@bp.route("/dashboard")
@role_required("student")
def dashboard():
    conn = get_db()
    stu = _student(conn)
    terms = _terms_for_student(conn, session["user_id"])
    current = terms[0] if terms else None
    current_rows = _grades_for_term(conn, session["user_id"], *current) if current else []
    released_only = [r for r in current_rows if r["status"] == "released"]

    unread = conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0",
        (session["user_id"],),
    ).fetchone()[0]

    conn.close()
    return render_template(
        "student/dashboard.html", student=stu, current=current,
        rows=current_rows, released_count=len(released_only),
        total_count=len(current_rows), unread=unread,
        student_qr=qr_data_uri(f"OGVS-STUDENT:{stu['student_id']}"),
    )


@bp.route("/profile", methods=["POST"])
@role_required("student")
def update_profile():
    image = request.files.get("profile_image")
    if not image or not image.filename:
        flash("Choose a profile picture before uploading.", "warning")
        return redirect(url_for("student.dashboard"))

    original_name = secure_filename(image.filename)
    extension = Path(original_name).suffix.lower().lstrip(".")
    if extension not in ALLOWED_IMAGE_EXTENSIONS:
        flash("Profile pictures must be JPG, PNG, or WEBP files.", "danger")
        return redirect(url_for("student.dashboard"))

    conn = get_db()
    student = _student(conn)
    if not student:
        conn.close()
        abort(404)

    filename = f"student_{session['user_id']}.{extension}"
    profile_image = save_profile_image(image, filename)
    conn.execute(
        "UPDATE students SET profile_image = ? WHERE user_id = ?",
        (profile_image, session["user_id"]),
    )
    conn.commit()
    conn.close()
    flash("Profile picture updated.", "success")
    return redirect(url_for("student.dashboard"))


@bp.route("/grades")
@role_required("student")
def grades():
    conn = get_db()
    stu = _student(conn)
    terms = _terms_for_student(conn, session["user_id"])
    sem = request.args.get("semester")
    sy = request.args.get("school_year")
    current = (sem, sy) if sem and sy else (terms[0] if terms else (None, None))
    rows = _grades_for_term(conn, session["user_id"], *current) if current[0] else []
    conn.close()
    return render_template(
        "student/grades.html", student=stu, rows=rows, terms=terms, current=current,
    )


@bp.route("/history")
@role_required("student")
def history():
    conn = get_db()
    stu = _student(conn)
    terms = _terms_for_student(conn, session["user_id"])
    history_data = []
    for semester, school_year in terms:
        rows = _grades_for_term(conn, session["user_id"], semester, school_year)
        gwa_rows = [{"final_grade": r["final_grade"], "units": r["units"]}
                    for r in rows if r["status"] == "released"]
        gwa = compute_gwa(gwa_rows)
        history_data.append({
            "semester": semester, "school_year": school_year,
            "rows": rows, "gwa": gwa,
        })
    conn.close()
    return render_template("student/history.html", student=stu, history=history_data)


@bp.route("/grade-slip/<school_year>/<path:semester>")
@role_required("student")
def grade_slip(school_year, semester):
    conn = get_db()
    stu = _student(conn)
    rows = _grades_for_term(conn, session["user_id"], semester, school_year)
    conn.close()
    if not rows:
        abort(404)
    gwa_rows = [{"final_grade": r["final_grade"], "units": r["units"]}
                for r in rows if r["status"] == "released"]
    gwa = compute_gwa(gwa_rows)
    pdf_bytes = build_grade_slip_pdf(dict(stu), (semester, school_year), rows, gwa)
    filename = f"GradeSlip_{stu['student_id']}_{school_year}_{semester}.pdf".replace(" ", "_")
    return send_file(
        io.BytesIO(pdf_bytes), mimetype="application/pdf",
        as_attachment=True, download_name=filename,
    )


@bp.route("/subjects")
@role_required("student")
def subjects():
    conn = get_db()
    stu = _student(conn)
    terms = _terms_for_student(conn, session["user_id"])
    current = terms[0] if terms else None
    rows = _grades_for_term(conn, session["user_id"], *current) if current else []
    conn.close()
    return render_template(
        "student/subjects.html", student=stu, rows=rows, current=current,
    )


@bp.route("/notifications")
@role_required("student")
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
    return render_template("student/notifications.html", rows=rows)


# ------------------------------------------------------ personal details -
@bp.route("/details", methods=["GET", "POST"])
@role_required("student")
def details():
    """Students add and update their own personal details (General Comments #5)."""
    conn = get_db()
    stu = _student(conn)
    if request.method == "POST":
        fields = {name: request.form.get(name, "").strip() for name in
                  ("contact_number", "birthdate", "address", "guardian_name", "guardian_contact")}
        conn.execute(
            """UPDATE students SET contact_number=?, birthdate=?, address=?,
               guardian_name=?, guardian_contact=? WHERE user_id=?""",
            (fields["contact_number"], fields["birthdate"], fields["address"],
             fields["guardian_name"], fields["guardian_contact"], session["user_id"]),
        )
        log_transaction(conn, session["user_id"], "student", "Updated personal details",
                        f"{stu['full_name']} ({stu['student_id']})")
        conn.commit()
        conn.close()
        flash("Your personal details were saved.", "success")
        return redirect(url_for("student.details"))
    conn.close()
    return render_template("student/details.html", student=stu)


# --------------------------------------- block / section transfer requests -
@bp.route("/transfers", methods=["GET", "POST"])
@role_required("student")
def transfer_requests():
    conn = get_db()
    student = _student(conn)
    if not student:
        conn.close()
        abort(404)

    if request.method == "POST":
        requested_section = request.form.get("requested_section", "").strip()
        reason = request.form.get("reason", "").strip()
        pending = conn.execute(
            """SELECT 1 FROM section_transfer_requests
               WHERE student_user_id = ? AND status = 'pending'""",
            (session["user_id"],),
        ).fetchone()

        if not requested_section:
            flash("Enter the block or section you are requesting.", "warning")
        elif len(requested_section) > 30:
            flash("The requested block or section must be 30 characters or fewer.", "warning")
        elif requested_section.casefold() == student["section"].casefold():
            flash("You are already in that block.", "warning")
        elif not reason:
            flash("Please provide a reason for your transfer request.", "warning")
        elif len(reason) > 500:
            flash("The reason must be 500 characters or fewer.", "warning")
        elif pending:
            flash("You already have a pending transfer request.", "warning")
        else:
            conn.execute(
                """INSERT INTO section_transfer_requests
                   (student_user_id, from_program, from_year_level, from_section_name,
                    requested_section_name, reason)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session["user_id"], student["program"], student["year_level"],
                 student["section"], requested_section, reason),
            )
            from_label = f"{student['program']} {student['year_level']}-{student['section']}"
            to_label = f"{student['program']} {student['year_level']}-{requested_section}"
            notify_admins(
                conn,
                f"Block transfer request: {student['full_name']} ({student['student_id']}) "
                f"from {from_label} to {to_label}.",
            )
            log_transaction(
                conn, session["user_id"], "student", "Requested block/section transfer",
                f"{student['full_name']} ({student['student_id']}): {from_label} -> {to_label}",
            )
            conn.commit()
            conn.close()
            flash("Your transfer request was sent to the Registrar for review.", "success")
            return redirect(url_for("student.transfer_requests"))

        conn.close()
        return redirect(url_for("student.transfer_requests"))

    sections = conn.execute(
        """SELECT section_name FROM sections
           WHERE program = ? AND year_level = ? AND section_name != ?
           ORDER BY section_name""",
        (student["program"], student["year_level"], student["section"]),
    ).fetchall()
    transfer_requests = conn.execute(
        """SELECT r.*
           FROM section_transfer_requests r
           WHERE r.student_user_id = ?
           ORDER BY r.id DESC""",
        (session["user_id"],),
    ).fetchall()
    pending_request = any(row["status"] == "pending" for row in transfer_requests)
    conn.close()
    return render_template(
        "student/transfer_requests.html", student=student,
        sections=sections, transfer_requests=transfer_requests,
        pending_request=pending_request,
    )


# ----------------------------------------------- class access (enrolled only) -
@bp.route("/classes/<int:offering_id>")
@role_required("student")
def class_detail(offering_id):
    """Only students on the class list may open a class (General Comments #1)."""
    conn = get_db()
    stu = _student(conn)
    enrolled = conn.execute(
        "SELECT 1 FROM enrollments WHERE student_user_id = ? AND offering_id = ?",
        (session["user_id"], offering_id),
    ).fetchone()
    if not enrolled:
        conn.close()
        flash("Access denied: you are not enrolled in that class. Only students on the "
              "instructor's class list can open it.", "danger")
        return redirect(url_for("student.subjects"))
    row = conn.execute(
        """SELECT s.code, s.name, s.units, co.meeting_time, co.meeting_days, co.semester,
                  co.school_year, g.prelim, g.midterm, g.finals, g.final_grade, g.remarks,
                  g.status, f.full_name AS instructor
           FROM enrollments e
           JOIN class_offerings co ON co.id = e.offering_id
           JOIN subjects s ON s.id = co.subject_id
           LEFT JOIN grades g ON g.enrollment_id = e.id
           LEFT JOIN faculty f ON f.user_id = co.faculty_user_id
           WHERE e.student_user_id = ? AND e.offering_id = ?""",
        (session["user_id"], offering_id),
    ).fetchone()
    conn.close()
    return render_template("student/class_detail.html", student=stu, r=row)
