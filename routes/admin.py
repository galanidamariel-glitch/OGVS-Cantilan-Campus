from flask import (
    Blueprint, render_template, session, request, redirect, url_for, flash, abort,
)
from werkzeug.security import generate_password_hash

from database import get_db, DEPARTMENTS
from utils.audit import log_transaction, notify_admins, notify_user
from utils.decorators import role_required

bp = Blueprint("admin", __name__, url_prefix="/admin")
DEPT_NAMES = {department["code"]: department["name"] for department in DEPARTMENTS}
PROGRAMS_BY_DEPARTMENT = {
    department["code"]: department["programs"] for department in DEPARTMENTS
}


def _department_program(department_value, program_value):
    """Return canonical department/program codes for a valid catalog pair."""
    if not isinstance(department_value, str) or not isinstance(program_value, str):
        return None
    department = next(
        (code for code, name in DEPT_NAMES.items()
         if department_value.strip().casefold() in (code.casefold(), name.casefold())),
        None,
    )
    if not department:
        return None
    program = next(
        (item["code"] for item in PROGRAMS_BY_DEPARTMENT[department]
         if program_value.strip().casefold() in (
             item["code"].casefold(), item["name"].casefold())),
        None,
    )
    return (department, program) if program else None


@bp.route("/dashboard")
@role_required("admin")
def dashboard():
    conn = get_db()
    counts = {
        "students": conn.execute("SELECT COUNT(*) FROM students").fetchone()[0],
        "faculty": conn.execute("SELECT COUNT(*) FROM faculty").fetchone()[0],
        "subjects": conn.execute("SELECT COUNT(*) FROM subjects").fetchone()[0],
        "offerings": conn.execute("SELECT COUNT(*) FROM class_offerings").fetchone()[0],
        "submitted": conn.execute(
            "SELECT COUNT(*) FROM grades WHERE status = 'submitted'").fetchone()[0],
        "pending_corrections": conn.execute(
            "SELECT COUNT(*) FROM correction_requests WHERE status = 'pending'").fetchone()[0],
        "pending_faculty": conn.execute(
            "SELECT COUNT(*) FROM faculty WHERE verification_status = 'pending'").fetchone()[0],
        "pending_nominations": conn.execute(
            "SELECT COUNT(*) FROM student_nominations WHERE status = 'pending'"
        ).fetchone()[0],
    }
    conn.close()
    return render_template("admin/dashboard.html", counts=counts)


@bp.route("/student-nominations")
@role_required("admin")
def student_nominations():
    conn = get_db()
    nominations = conn.execute(
        """SELECT n.*, f.full_name AS faculty_name, s.code AS subject_code,
                  s.name AS subject_name, sec.program, sec.year_level, sec.section_name,
                  co.semester, co.school_year
           FROM student_nominations n
           JOIN faculty f ON f.user_id = n.faculty_user_id
           JOIN class_offerings co ON co.id = n.offering_id
           JOIN subjects s ON s.id = co.subject_id
           JOIN sections sec ON sec.id = co.section_id
           ORDER BY CASE n.status WHEN 'pending' THEN 0 ELSE 1 END, n.requested_at DESC"""
    ).fetchall()
    conn.close()
    return render_template("admin/student_nominations.html", nominations=nominations)


@bp.route("/student-nominations/<int:nomination_id>/approve", methods=["POST"])
@role_required("admin")
def approve_student_nomination(nomination_id):
    conn = get_db()
    nomination = conn.execute(
        """SELECT n.*, co.section_id, sec.program, sec.year_level, sec.section_name,
                  sec.department,
                  s.code AS subject_code, s.name AS subject_name
           FROM student_nominations n
           JOIN class_offerings co ON co.id = n.offering_id
           JOIN sections sec ON sec.id = co.section_id
           JOIN subjects s ON s.id = co.subject_id
           WHERE n.id = ? AND n.status = 'pending'""",
        (nomination_id,),
    ).fetchone()
    if not nomination:
        conn.close()
        flash("That student nomination is no longer pending.", "warning")
        return redirect(url_for("admin.student_nominations"))

    student = conn.execute(
        """SELECT st.user_id, st.full_name, u.is_active
           FROM students st JOIN users u ON u.id = st.user_id
           WHERE st.student_id = ?""",
        (nomination["student_id"],),
    ).fetchone()
    if student and not student["is_active"]:
        conn.close()
        flash("This student's account is deactivated. Reactivate it before approval.", "danger")
        return redirect(url_for("admin.student_nominations"))

    if not student:
        pre_enrolled = conn.execute(
            "SELECT full_name, department FROM pre_enrolled_students WHERE student_id = ?",
            (nomination["student_id"],),
        ).fetchone()
        if pre_enrolled and pre_enrolled["full_name"].strip().casefold() != (
                nomination["full_name"].strip().casefold()):
            conn.close()
            flash("The nominee's name conflicts with the Registrar's enrollment record.", "danger")
            return redirect(url_for("admin.student_nominations"))
        if not pre_enrolled:
            conn.execute(
                """INSERT INTO pre_enrolled_students
                   (student_id, full_name, program, year_level, section, department, is_registered)
                   VALUES (?, ?, ?, ?, ?, ?, 0)""",
                (nomination["student_id"], nomination["full_name"], nomination["program"],
                 nomination["year_level"], nomination["section_name"],
                 nomination["department"]),
            )
        elif not pre_enrolled["department"]:
            conn.execute(
                "UPDATE pre_enrolled_students SET department = ? WHERE student_id = ?",
                (nomination["department"], nomination["student_id"]),
            )
    else:
        conn.execute(
            """INSERT OR IGNORE INTO enrollments (student_user_id, offering_id)
               VALUES (?, ?)""",
            (student["user_id"], nomination["offering_id"]),
        )
        notify_user(
            conn, student["user_id"],
            f"Your enrollment in {nomination['subject_code']} - "
            f"{nomination['subject_name']} was approved by the Registrar.",
        )

    cur = conn.execute(
        """UPDATE student_nominations
           SET status = 'approved', reviewed_at = datetime('now', 'localtime'), reviewed_by = ?
           WHERE id = ? AND status = 'pending'""",
        (session["user_id"], nomination_id),
    )
    if cur.rowcount != 1:
        conn.rollback()
        conn.close()
        flash("That nomination was already reviewed. Refresh the page and try again.", "warning")
        return redirect(url_for("admin.student_nominations"))

    faculty_name = conn.execute(
        "SELECT full_name FROM faculty WHERE user_id = ?",
        (nomination["faculty_user_id"],),
    ).fetchone()["full_name"]
    notify_user(
        conn, nomination["faculty_user_id"],
        f"Registrar approved your nomination of {nomination['full_name']} "
        f"({nomination['student_id']}) for {nomination['subject_code']}.",
    )
    log_transaction(
        conn, session["user_id"], "admin", "Approved student nomination",
        f"{nomination['full_name']} ({nomination['student_id']}) for "
        f"{nomination['subject_code']} nominated by {faculty_name}",
    )
    conn.commit()
    conn.close()
    flash("Student nomination approved.", "success")
    return redirect(url_for("admin.student_nominations"))


@bp.route("/student-nominations/<int:nomination_id>/reject", methods=["POST"])
@role_required("admin")
def reject_student_nomination(nomination_id):
    conn = get_db()
    nomination = conn.execute(
        """SELECT n.*, s.code AS subject_code, f.full_name AS faculty_name
           FROM student_nominations n
           JOIN class_offerings co ON co.id = n.offering_id
           JOIN subjects s ON s.id = co.subject_id
           JOIN faculty f ON f.user_id = n.faculty_user_id
           WHERE n.id = ? AND n.status = 'pending'""",
        (nomination_id,),
    ).fetchone()
    if not nomination:
        conn.close()
        flash("That student nomination is no longer pending.", "warning")
        return redirect(url_for("admin.student_nominations"))

    cur = conn.execute(
        """UPDATE student_nominations
           SET status = 'rejected', reviewed_at = datetime('now', 'localtime'), reviewed_by = ?
           WHERE id = ? AND status = 'pending'""",
        (session["user_id"], nomination_id),
    )
    if cur.rowcount != 1:
        conn.rollback()
        conn.close()
        flash("That nomination was already reviewed. Refresh the page and try again.", "warning")
        return redirect(url_for("admin.student_nominations"))

    notify_user(
        conn, nomination["faculty_user_id"],
        f"Registrar did not approve your nomination of {nomination['full_name']} "
        f"({nomination['student_id']}) for {nomination['subject_code']}.",
    )
    log_transaction(
        conn, session["user_id"], "admin", "Rejected student nomination",
        f"{nomination['full_name']} ({nomination['student_id']}) for "
        f"{nomination['subject_code']} nominated by {nomination['faculty_name']}",
    )
    conn.commit()
    conn.close()
    flash("Student nomination rejected.", "success")
    return redirect(url_for("admin.student_nominations"))


# ------------------------------------------- faculty & students (combined) -


@bp.route("/directory")
@role_required("admin")
def directory():
    """One module that manages faculty and students together; every faculty
    member carries a Minor/Major tag (General Comments #2)."""
    q = request.args.get("q", "").strip()
    like = f"%{q}%"
    conn = get_db()
    students = conn.execute(
        """SELECT st.*, u.is_active FROM students st JOIN users u ON u.id = st.user_id
           WHERE (? = '' OR st.student_id LIKE ? OR st.full_name LIKE ?
                  OR st.department LIKE ? OR st.program LIKE ?)
           ORDER BY st.department, st.year_level, st.section, st.full_name""",
        (q, like, like, like, like),
    ).fetchall()
    faculty = conn.execute(
        """SELECT f.*, u.username, u.email, u.is_active,
                  (SELECT COUNT(*) FROM class_offerings co
                    WHERE co.faculty_user_id = f.user_id) AS subject_count
           FROM faculty f JOIN users u ON u.id = f.user_id
           WHERE (? = '' OR f.full_name LIKE ? OR u.username LIKE ?
                  OR f.employee_id LIKE ? OR f.faculty_type LIKE ?)
           ORDER BY f.full_name""",
        (q, like, like, like, like),
    ).fetchall()
    conn.close()
    return render_template("admin/directory.html", students=students, faculty=faculty,
                           q=q, dept_names=DEPT_NAMES)


@bp.route("/students")
@role_required("admin")
def students():
    return redirect(url_for("admin.directory", q=request.args.get("q", "")) + "#students")


@bp.route("/students/new", methods=["GET", "POST"])
@role_required("admin")
def new_student():
    if request.method == "POST":
        student_id = request.form["student_id"].strip()
        full_name = request.form["full_name"].strip()
        department_program = _department_program(
            request.form.get("department", ""),
            request.form.get("program", ""),
        )
        year_level = request.form["year_level"].strip()
        section = request.form["section"].strip()

        conn = get_db()
        if not department_program:
            conn.close()
            flash("Choose a program that belongs to the selected department.", "danger")
            return render_template(
                "admin/student_form.html", mode="new", record=request.form,
                departments=DEPARTMENTS, programs_by_department=PROGRAMS_BY_DEPARTMENT,
            )
        department, program = department_program
        exists = conn.execute(
            "SELECT 1 FROM pre_enrolled_students WHERE student_id = ?", (student_id,)
        ).fetchone()
        if exists:
            flash("A record with this Student ID already exists.", "danger")
            conn.close()
            return render_template(
                "admin/student_form.html", mode="new", record=request.form,
                departments=DEPARTMENTS, programs_by_department=PROGRAMS_BY_DEPARTMENT,
            )
        else:
            conn.execute(
                """INSERT INTO pre_enrolled_students
                   (student_id, full_name, program, year_level, section, department, is_registered)
                   VALUES (?,?,?,?,?,?,0)""",
                (student_id, full_name, program, year_level, section, department),
            )
            log_transaction(conn, session["user_id"], "admin", "Added student record",
                            f"{full_name} ({student_id}) - {program} {year_level}-{section}")
            conn.commit()
            flash("Student pre-enrollment record added. The student can now self-register.",
                  "success")
            conn.close()
            return redirect(url_for("admin.directory") + "#students")
        conn.close()
    return render_template(
        "admin/student_form.html", mode="new", record=None,
        departments=DEPARTMENTS, programs_by_department=PROGRAMS_BY_DEPARTMENT,
    )


@bp.route("/students/<student_id>/edit", methods=["GET", "POST"])
@role_required("admin")
def edit_student(student_id):
    conn = get_db()
    record = conn.execute(
        "SELECT * FROM students WHERE student_id = ?", (student_id,)
    ).fetchone()
    if not record:
        conn.close()
        abort(404)
    if request.method == "POST":
        full_name = request.form["full_name"].strip()
        department_program = _department_program(
            request.form.get("department", ""),
            request.form.get("program", ""),
        )
        year_level = request.form["year_level"].strip()
        section = request.form["section"].strip()
        if not department_program:
            conn.close()
            flash("Choose a program that belongs to the selected department.", "danger")
            return redirect(url_for("admin.edit_student", student_id=student_id))
        department, program = department_program
        conn.execute(
            """UPDATE students SET full_name=?, program=?, year_level=?, section=?, department=?
               WHERE student_id = ?""",
            (full_name, program, year_level, section, department, student_id),
        )
        conn.execute(
            """UPDATE pre_enrolled_students
               SET full_name=?, program=?, year_level=?, section=?, department=?
               WHERE student_id = ?""",
            (full_name, program, year_level, section, department, student_id),
        )
        log_transaction(conn, session["user_id"], "admin", "Edited student record",
                        f"{full_name} ({student_id})")
        conn.commit()
        conn.close()
        flash("Student record updated.", "success")
        return redirect(url_for("admin.directory") + "#students")
    conn.close()
    return render_template(
        "admin/student_form.html", mode="edit", record=record,
        departments=DEPARTMENTS, programs_by_department=PROGRAMS_BY_DEPARTMENT,
    )


@bp.route("/students/<student_id>/toggle", methods=["POST"])
@role_required("admin")
def toggle_student(student_id):
    conn = get_db()
    user = conn.execute(
        """SELECT u.* FROM users u JOIN students s ON s.user_id = u.id
           WHERE s.student_id = ?""",
        (student_id,),
    ).fetchone()
    if user:
        conn.execute(
            "UPDATE users SET is_active = ? WHERE id = ?",
            (0 if user["is_active"] else 1, user["id"]),
        )
        log_transaction(conn, session["user_id"], "admin",
                        "Deactivated student" if user["is_active"] else "Reactivated student",
                        f"Student ID {student_id}")
        conn.commit()
        flash(
            "Student account deactivated." if user["is_active"] else "Student account reactivated.",
            "success",
        )
    else:
        flash("This student has not registered an account yet.", "warning")
    conn.close()
    return redirect(url_for("admin.directory") + "#students")


# ----------------------------------------------------------------- faculty -
@bp.route("/faculty")
@role_required("admin")
def faculty_list():
    return redirect(url_for("admin.directory", q=request.args.get("q", "")) + "#faculty")


@bp.route("/faculty/<int:user_id>")
@role_required("admin")
def faculty_detail(user_id):
    """Selecting a faculty member shows the subjects assigned to them
    (General Comments #3)."""
    conn = get_db()
    faculty = conn.execute(
        """SELECT f.*, u.username, u.email, u.is_active FROM faculty f
           JOIN users u ON u.id = f.user_id WHERE f.user_id = ?""",
        (user_id,),
    ).fetchone()
    if not faculty:
        conn.close()
        abort(404)
    offerings = conn.execute(
        """SELECT co.*, s.code, s.name, s.units, sec.program, sec.year_level,
                  sec.section_name,
                  (SELECT COUNT(*) FROM enrollments e WHERE e.offering_id = co.id) AS enrolled
           FROM class_offerings co
           JOIN subjects s ON s.id = co.subject_id
           JOIN sections sec ON sec.id = co.section_id
           WHERE co.faculty_user_id = ?
           ORDER BY co.school_year DESC, co.semester, s.code""",
        (user_id,),
    ).fetchall()
    conn.close()
    return render_template("admin/faculty_detail.html", faculty=faculty, offerings=offerings)


@bp.route("/offerings/<int:offering_id>/students")
@role_required("admin")
def offering_students(offering_id):
    """Selecting a subject shows its enrolled students (General Comments #3)."""
    conn = get_db()
    offering = conn.execute(
        """SELECT co.*, s.code, s.name, sec.program, sec.year_level, sec.section_name,
                  f.full_name AS instructor
           FROM class_offerings co
           JOIN subjects s ON s.id = co.subject_id
           JOIN sections sec ON sec.id = co.section_id
           LEFT JOIN faculty f ON f.user_id = co.faculty_user_id
           WHERE co.id = ?""",
        (offering_id,),
    ).fetchone()
    if not offering:
        conn.close()
        abort(404)
    students = conn.execute(
        """SELECT st.student_id, st.full_name, st.program, st.year_level, st.section,
                  st.department, g.status AS grade_status
           FROM enrollments e
           JOIN students st ON st.user_id = e.student_user_id
           LEFT JOIN grades g ON g.enrollment_id = e.id
           WHERE e.offering_id = ? ORDER BY st.full_name""",
        (offering_id,),
    ).fetchall()
    conn.close()
    return render_template(
        "admin/offering_students.html", offering=offering, students=students,
        dept_names=DEPT_NAMES,
    )


@bp.route("/faculty/<int:user_id>/type", methods=["POST"])
@role_required("admin")
def set_faculty_type(user_id):
    faculty_type = request.form.get("faculty_type", "")
    if faculty_type not in ("Minor", "Major"):
        abort(400)
    conn = get_db()
    row = conn.execute("SELECT full_name FROM faculty WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        conn.close()
        abort(404)
    conn.execute("UPDATE faculty SET faculty_type = ? WHERE user_id = ?", (faculty_type, user_id))
    log_transaction(conn, session["user_id"], "admin", "Changed faculty classification",
                    f"{row['full_name']} set to {faculty_type}")
    conn.commit()
    conn.close()
    flash(f"{row['full_name']} is now classified as {faculty_type}.", "success")
    return redirect(request.referrer or url_for("admin.directory"))


@bp.route("/faculty/new", methods=["GET", "POST"])
@role_required("admin")
def new_faculty():
    if request.method == "POST":
        username = request.form["username"].strip()
        full_name = request.form["full_name"].strip()
        position = request.form.get("position", "Faculty").strip() or "Faculty"
        email = request.form["email"].strip()
        password = request.form["password"]
        faculty_type = request.form.get("faculty_type", "Minor")
        if faculty_type not in ("Minor", "Major"):
            faculty_type = "Minor"

        conn = get_db()
        exists = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
        if exists:
            flash("Username already taken.", "danger")
            conn.close()
            return render_template("admin/faculty_form.html", mode="new", record=None)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users (username, password_hash, role, email) VALUES (?,?,?,?)",
            (username, generate_password_hash(password), "faculty", email),
        )
        cur.execute(
                """INSERT INTO faculty (user_id, full_name, position, verification_status,
                                        faculty_type)
                    VALUES (?,?,?,'approved',?)""",
                (cur.lastrowid, full_name, position, faculty_type),
        )
        log_transaction(conn, session["user_id"], "admin", "Created faculty account",
                        f"{full_name} ({username}) - {faculty_type}")
        conn.commit()
        conn.close()
        flash("Faculty account created.", "success")
        return redirect(url_for("admin.directory") + "#faculty")
    return render_template("admin/faculty_form.html", mode="new", record=None)


@bp.route("/faculty/<int:user_id>/approve", methods=["POST"])
@role_required("admin")
def approve_faculty(user_id):
    conn = get_db()
    conn.execute(
        "UPDATE users SET is_active = 1 WHERE id = ? AND role = 'faculty'", (user_id,)
    )
    conn.execute(
        "UPDATE faculty SET verification_status = 'approved' WHERE user_id = ?",
        (user_id,),
    )
    name = conn.execute("SELECT full_name FROM faculty WHERE user_id = ?", (user_id,)).fetchone()
    log_transaction(conn, session["user_id"], "admin", "Approved faculty application",
                    name["full_name"] if name else f"user {user_id}")
    conn.commit()
    conn.close()
    flash("Faculty application approved and account activated.", "success")
    return redirect(url_for("admin.directory") + "#faculty")


@bp.route("/faculty/<int:user_id>/toggle", methods=["POST"])
@role_required("admin")
def toggle_faculty(user_id):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user:
        conn.execute(
            "UPDATE users SET is_active = ? WHERE id = ?",
            (0 if user["is_active"] else 1, user_id),
        )
        log_transaction(conn, session["user_id"], "admin",
                        "Deactivated faculty" if user["is_active"] else "Reactivated faculty",
                        user["username"])
        conn.commit()
    conn.close()
    flash("Faculty account status updated.", "success")
    return redirect(url_for("admin.directory") + "#faculty")


# -------------------------------------------------------------- assignment -
@bp.route("/assignments", methods=["GET", "POST"])
@role_required("admin")
def assignments():
    conn = get_db()
    departments = [
        {"code": department["code"], "name": department["name"]}
        for department in DEPARTMENTS
    ]
    programs_by_department = {
        code: programs for code, programs in PROGRAMS_BY_DEPARTMENT.items()
    }
    year_levels = {"1st Year", "2nd Year", "3rd Year", "4th Year", "5th Year", "6th Year"}
    year_levels.update(row["year_level"] for row in conn.execute(
        "SELECT DISTINCT year_level FROM sections UNION SELECT DISTINCT year_level FROM students "
        "UNION SELECT DISTINCT year_level FROM pre_enrolled_students"
    ).fetchall())
    year_levels = sorted(year for year in year_levels if year)

    if request.method == "POST":
        department_program = _department_program(
            request.form.get("department", ""),
            request.form.get("program", ""),
        )
        requested_year = request.form.get("year_level", "").strip()
        requested_section = request.form.get("section_name", "").strip().upper()
        if not department_program:
            conn.close()
            flash("Choose a program that belongs to the selected department.", "danger")
            return redirect(url_for("admin.assignments"))
        department, program = department_program
        year_level = next(
            (value for value in year_levels if value.casefold() == requested_year.casefold()),
            None,
        )
        if not year_level:
            conn.close()
            flash("Choose a year level from the suggestions.", "danger")
            return redirect(url_for("admin.assignments"))
        if len(requested_section) != 1 or not "A" <= requested_section <= "Z":
            conn.close()
            flash("Choose a section/block letter from A to Z.", "danger")
            return redirect(url_for("admin.assignments"))

        # General Comments #10: the subject is typed (code or description) with
        # an auto pop-up list; an unknown code is added to the subject list.
        subject_code = request.form["subject_code"].strip().upper()
        subject_name = request.form["subject_name"].strip()
        subject = conn.execute("SELECT * FROM subjects WHERE code = ?", (subject_code,)).fetchone()
        if not subject:
            subject = conn.execute(
                "SELECT * FROM subjects WHERE lower(name) = lower(?)", (subject_name,)
            ).fetchone()
        if not subject:
            cur = conn.execute(
                "INSERT INTO subjects (code, name, units) VALUES (?,?,3)",
                (subject_code, subject_name),
            )
            subject = conn.execute("SELECT * FROM subjects WHERE id = ?", (cur.lastrowid,)).fetchone()
            flash(f"New subject {subject_code} - {subject_name} added to the subject list.", "info")
        subject_id = subject["id"]
        conn.execute(
            """INSERT OR IGNORE INTO sections (program, year_level, section_name, department)
               VALUES (?, ?, ?, ?)""",
            (program, year_level, requested_section, department),
        )
        section_id = conn.execute(
            """SELECT id FROM sections
               WHERE program = ? AND year_level = ? AND section_name = ?""",
            (program, year_level, requested_section),
        ).fetchone()["id"]
        faculty_user_id = int(request.form["faculty_user_id"])
        semester = request.form["semester"]
        school_year = request.form["school_year"].strip()
        meeting_time = request.form["meeting_time"].strip()
        meeting_days = request.form["meeting_days"].strip().upper()
        conn.execute(
            """INSERT INTO class_offerings
               (subject_id, section_id, faculty_user_id, semester, school_year,
                meeting_time, meeting_days)
               VALUES (?,?,?,?,?,?,?)""",
            (subject_id, section_id, faculty_user_id, semester, school_year,
             meeting_time, meeting_days),
        )
        conn.commit()

        # Auto-enroll every student currently in that section.
        students_in_section = conn.execute(
            """SELECT st.user_id FROM students st
               JOIN sections sec ON sec.program = st.program
                   AND sec.year_level = st.year_level AND sec.section_name = st.section
               WHERE sec.id = ?""",
            (section_id,),
        ).fetchall()
        offering_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for s in students_in_section:
            conn.execute(
                "INSERT OR IGNORE INTO enrollments (student_user_id, offering_id) VALUES (?,?)",
                (s["user_id"], offering_id),
            )
        conn.commit()
        instructor = conn.execute(
            "SELECT full_name FROM faculty WHERE user_id = ?", (faculty_user_id,)
        ).fetchone()
        section_row = conn.execute("SELECT * FROM sections WHERE id = ?", (section_id,)).fetchone()
        section_label = (f"{section_row['program']} {section_row['year_level']}-"
                         f"{section_row['section_name']}")
        notify_user(conn, faculty_user_id,
                    f"You have been assigned {subject['code']} - {subject['name']} for "
                    f"{section_label} ({semester}, {school_year}).")
        log_transaction(
            conn, session["user_id"], "admin", "Assigned subject to instructor",
            f"{subject['code']} - {subject['name']} -> "
            f"{instructor['full_name'] if instructor else faculty_user_id}, {section_label}, "
            f"{len(students_in_section)} student(s) enrolled")
        conn.commit()
        flash("Subject assigned to instructor and students auto-enrolled.", "success")

    subjects = conn.execute("SELECT * FROM subjects ORDER BY code").fetchall()
    subject_list = [{"code": r["code"], "name": r["name"]} for r in subjects]
    faculty = conn.execute(
        """SELECT f.user_id, f.full_name FROM faculty f
           JOIN users u ON u.id = f.user_id WHERE u.is_active = 1 ORDER BY f.full_name"""
    ).fetchall()
    offerings = conn.execute(
        """SELECT co.*, s.code, s.name, sec.program, sec.year_level, sec.section_name,
                  f.full_name AS instructor,
                  (SELECT COUNT(*) FROM enrollments e WHERE e.offering_id = co.id) AS enrolled
           FROM class_offerings co
           JOIN subjects s ON s.id = co.subject_id
           JOIN sections sec ON sec.id = co.section_id
           LEFT JOIN faculty f ON f.user_id = co.faculty_user_id
           ORDER BY co.school_year DESC, co.semester, s.code"""
    ).fetchall()
    conn.close()
    return render_template(
        "admin/assignments.html", subjects=subjects, subject_list=subject_list,
        departments=departments, programs_by_department=programs_by_department,
        year_levels=year_levels, faculty=faculty, offerings=offerings,
    )


# ------------------------------------------------------------ finalization -
@bp.route("/finalization")
@role_required("admin")
def finalization():
    conn = get_db()
    submitted = conn.execute(
        """SELECT g.*, st.full_name AS student_name, st.student_id, s.code, s.name,
                  co.semester, co.school_year, f.full_name AS instructor
           FROM grades g
           JOIN enrollments e ON e.id = g.enrollment_id
           JOIN students st ON st.user_id = e.student_user_id
           JOIN class_offerings co ON co.id = e.offering_id
           JOIN subjects s ON s.id = co.subject_id
           LEFT JOIN faculty f ON f.user_id = co.faculty_user_id
           WHERE g.status = 'submitted'
           ORDER BY co.school_year DESC, co.semester, s.code, st.full_name"""
    ).fetchall()
    corrections = conn.execute(
        """SELECT cr.*, st.full_name AS student_name, st.student_id, s.code, s.name,
                  g.final_grade, g.remarks
           FROM correction_requests cr
           JOIN grades g ON g.id = cr.grade_id
           JOIN enrollments e ON e.id = g.enrollment_id
           JOIN students st ON st.user_id = e.student_user_id
           JOIN class_offerings co ON co.id = e.offering_id
           JOIN subjects s ON s.id = co.subject_id
           WHERE cr.status = 'pending'
           ORDER BY cr.requested_at"""
    ).fetchall()
    conn.close()
    return render_template("admin/finalization.html", submitted=submitted, corrections=corrections)


def _log_grade_action(conn, grade_id, action):
    row = conn.execute(
        """SELECT st.full_name, st.student_id, s.code FROM grades g
           JOIN enrollments e ON e.id = g.enrollment_id
           JOIN students st ON st.user_id = e.student_user_id
           JOIN class_offerings co ON co.id = e.offering_id
           JOIN subjects s ON s.id = co.subject_id WHERE g.id = ?""",
        (grade_id,),
    ).fetchone()
    if row:
        log_transaction(conn, session["user_id"], "admin", action,
                        f"{row['code']} - {row['full_name']} ({row['student_id']})")


@bp.route("/finalization/grades/<int:grade_id>/approve", methods=["POST"])
@role_required("admin")
def approve_grade(grade_id):
    conn = get_db()
    conn.execute(
        "UPDATE grades SET status = 'approved', updated_at = datetime('now') WHERE id = ?",
        (grade_id,),
    )
    _log_grade_action(conn, grade_id, "Approved submitted grade")
    conn.commit()
    conn.close()
    flash("Grade approved and locked for release.", "success")
    return redirect(url_for("admin.finalization"))


@bp.route("/finalization/grades/<int:grade_id>/return", methods=["POST"])
@role_required("admin")
def return_grade(grade_id):
    conn = get_db()
    conn.execute(
        "UPDATE grades SET status = 'draft', updated_at = datetime('now') WHERE id = ?",
        (grade_id,),
    )
    _log_grade_action(conn, grade_id, "Returned grade to instructor")
    conn.commit()
    conn.close()
    flash("Grade returned to the instructor for revision.", "info")
    return redirect(url_for("admin.finalization"))


@bp.route("/finalization/corrections/<int:cr_id>/approve", methods=["POST"])
@role_required("admin")
def approve_correction(cr_id):
    conn = get_db()
    cr = conn.execute("SELECT * FROM correction_requests WHERE id = ?", (cr_id,)).fetchone()
    if cr:
        conn.execute(
            "UPDATE correction_requests SET status='approved', resolved_at=datetime('now') WHERE id=?",
            (cr_id,),
        )
        conn.execute(
            "UPDATE grades SET status='draft', updated_at=datetime('now') WHERE id=?",
            (cr["grade_id"],),
        )
        _log_grade_action(conn, cr["grade_id"], "Approved correction request")
        conn.commit()
    conn.close()
    flash("Correction approved — grade unlocked for the instructor to re-encode.", "success")
    return redirect(url_for("admin.finalization"))


@bp.route("/finalization/corrections/<int:cr_id>/return", methods=["POST"])
@role_required("admin")
def return_correction(cr_id):
    conn = get_db()
    conn.execute(
        "UPDATE correction_requests SET status='returned', resolved_at=datetime('now') WHERE id=?",
        (cr_id,),
    )
    cr = conn.execute("SELECT grade_id FROM correction_requests WHERE id = ?", (cr_id,)).fetchone()
    if cr:
        _log_grade_action(conn, cr["grade_id"], "Denied correction request")
    conn.commit()
    conn.close()
    flash("Correction request denied.", "info")
    return redirect(url_for("admin.finalization"))


# ------------------------------------------------------------------ reports -
@bp.route("/reports")
@role_required("admin")
def reports():
    conn = get_db()
    program = request.args.get("program", "")
    year_level = request.args.get("year_level", "")

    query = """SELECT st.student_id, st.full_name, st.program, st.year_level, st.section,
                      s.code, s.name, co.semester, co.school_year, g.final_grade, g.remarks
               FROM enrollments e
               JOIN students st ON st.user_id = e.student_user_id
               JOIN class_offerings co ON co.id = e.offering_id
               JOIN subjects s ON s.id = co.subject_id
               LEFT JOIN grades g ON g.enrollment_id = e.id AND g.status = 'released'
               WHERE 1=1"""
    params = []
    if program:
        query += " AND st.program = ?"
        params.append(program)
    if year_level:
        query += " AND st.year_level = ?"
        params.append(year_level)
    query += " ORDER BY st.program, st.year_level, st.section, st.full_name, s.code"
    rows = conn.execute(query, params).fetchall()

    programs = [r[0] for r in conn.execute("SELECT DISTINCT program FROM students").fetchall()]
    year_levels = [r[0] for r in conn.execute("SELECT DISTINCT year_level FROM students").fetchall()]
    conn.close()
    return render_template(
        "admin/reports.html", rows=rows, programs=programs, year_levels=year_levels,
        program=program, year_level=year_level,
    )


# ------------------------------------------------- notifications (real time) -
def _admin_notifications(conn, mark_read=False):
    rows = conn.execute(
        "SELECT * FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT 200",
        (session["user_id"],),
    ).fetchall()
    if mark_read:
        conn.execute("UPDATE notifications SET is_read = 1 WHERE user_id = ?", (session["user_id"],))
        conn.commit()
    return rows


@bp.route("/notifications")
@role_required("admin")
def notifications():
    """Faculty requests (applications, grade submissions, correction requests)
    arrive here and refresh automatically (General Comments #6)."""
    conn = get_db()
    rows = _admin_notifications(conn, mark_read=True)
    pending_faculty = conn.execute(
        "SELECT COUNT(*) FROM faculty WHERE verification_status = 'pending'").fetchone()[0]
    conn.close()
    return render_template("admin/notifications.html", rows=rows, pending_faculty=pending_faculty,
                           new_ids=[])


@bp.route("/notifications/feed")
@role_required("admin")
def notifications_feed():
    conn = get_db()
    unread_ids = {r["id"] for r in conn.execute(
        "SELECT id FROM notifications WHERE user_id = ? AND is_read = 0", (session["user_id"],))}
    rows = _admin_notifications(conn, mark_read=True)
    conn.close()
    return render_template("admin/_notification_rows.html", rows=rows, new_ids=unread_ids)


# ------------------------------------------------ transaction history (live) -
def _transaction_rows(conn):
    role = request.args.get("role", "")
    query = "SELECT * FROM transactions"
    params = []
    if role in ("admin", "faculty", "student"):
        query += " WHERE actor_role = ?"
        params.append(role)
    query += " ORDER BY id DESC LIMIT 500"
    return role, conn.execute(query, params).fetchall()


@bp.route("/transactions")
@role_required("admin")
def transactions():
    conn = get_db()
    role, rows = _transaction_rows(conn)
    conn.close()
    return render_template("shared/transactions.html", rows=rows, role_filter=role,
                           feed_url=url_for("admin.transactions_feed", role=role),
                           scope="All users", can_filter=True)


@bp.route("/transactions/feed")
@role_required("admin")
def transactions_feed():
    conn = get_db()
    _, rows = _transaction_rows(conn)
    conn.close()
    return render_template("shared/_transaction_rows.html", rows=rows, show_actor=True)


# ---------------------------------------------------- block / section transfer -
def _section_label(row):
    return f"{row['program']} {row['year_level']}-{row['section_name']}"


@bp.route("/transfers", methods=["GET", "POST"])
@role_required("admin")
def transfers():
    """Track a student moving to another block/section and notify the faculty
    whose class lists change (General Comments #8)."""
    conn = get_db()
    if request.method == "POST":
        student_user_id = int(request.form["student_user_id"])
        to_section_id = int(request.form["to_section_id"])
        reason = request.form.get("reason", "").strip()
        student = conn.execute("SELECT * FROM students WHERE user_id = ?",
                               (student_user_id,)).fetchone()
        target = conn.execute("SELECT * FROM sections WHERE id = ?", (to_section_id,)).fetchone()
        if not student or not target:
            flash("Choose a valid student and destination block.", "danger")
        elif (student["program"], student["year_level"], student["section"]) == (
                target["program"], target["year_level"], target["section_name"]):
            flash("The student is already in that block/section.", "warning")
        else:
            _apply_transfer(conn, student, target, reason)
            flash(f"{student['full_name']} transferred to {_section_label(target)}. "
                  "Affected faculty have been notified.", "success")
        conn.close()
        return redirect(url_for("admin.transfers"))

    students = conn.execute(
        "SELECT * FROM students ORDER BY department, full_name").fetchall()
    sections = conn.execute(
        "SELECT * FROM sections ORDER BY program, year_level, section_name").fetchall()
    history = conn.execute(
        """SELECT t.*, st.full_name, st.student_id FROM section_transfers t
           JOIN students st ON st.user_id = t.student_user_id
           ORDER BY t.id DESC LIMIT 100"""
    ).fetchall()
    requests = conn.execute(
        """SELECT r.*, st.full_name, st.student_id,
                  dst.id AS target_section_id
           FROM section_transfer_requests r
           JOIN students st ON st.user_id = r.student_user_id
           LEFT JOIN sections dst
             ON dst.program = r.from_program AND dst.year_level = r.from_year_level
             AND lower(dst.section_name) = lower(r.requested_section_name)
           ORDER BY CASE r.status WHEN 'pending' THEN 0 ELSE 1 END, r.id DESC
           LIMIT 100"""
    ).fetchall()
    conn.close()
    return render_template("admin/transfers.html", students=students, sections=sections,
                           history=history, requests=requests)


@bp.route("/transfers/requests/<int:request_id>/approve", methods=["POST"])
@role_required("admin")
def approve_transfer_request(request_id):
    conn = get_db()
    transfer_request = conn.execute(
        """SELECT * FROM section_transfer_requests
           WHERE id = ? AND status = 'pending'""",
        (request_id,),
    ).fetchone()
    if not transfer_request:
        conn.close()
        flash("That transfer request is no longer pending.", "warning")
        return redirect(url_for("admin.transfers"))

    student = conn.execute(
        "SELECT * FROM students WHERE user_id = ?",
        (transfer_request["student_user_id"],),
    ).fetchone()
    target = conn.execute(
        """SELECT * FROM sections
           WHERE program = ? AND year_level = ? AND lower(section_name) = lower(?)""",
        (transfer_request["from_program"], transfer_request["from_year_level"],
         transfer_request["requested_section_name"]),
    ).fetchone()
    if student and not target:
        conn.close()
        flash("Add the requested destination section before approving this request.", "warning")
        return redirect(url_for("admin.transfers"))
    valid_request = (
        student and target
        and student["program"] == transfer_request["from_program"]
        and student["year_level"] == transfer_request["from_year_level"]
        and student["section"] == transfer_request["from_section_name"]
        and target["program"] == transfer_request["from_program"]
        and target["year_level"] == transfer_request["from_year_level"]
        and target["section_name"].casefold()
        != transfer_request["from_section_name"].casefold()
    )
    if not valid_request:
        conn.execute(
            """UPDATE section_transfer_requests
               SET status = 'rejected', reviewed_at = datetime('now', 'localtime'),
                   reviewed_by = ?
               WHERE id = ? AND status = 'pending'""",
            (session["user_id"], request_id),
        )
        notify_user(
            conn, transfer_request["student_user_id"],
            "Your block transfer request could not be processed because your current "
            "section has changed. Please submit a new request if needed.",
        )
        log_transaction(
            conn, session["user_id"], "admin", "Closed stale block transfer request",
            f"Request #{request_id} was no longer valid.",
        )
        conn.commit()
        conn.close()
        flash("The request was closed because the student's current section has changed.", "warning")
        return redirect(url_for("admin.transfers"))

    updated = conn.execute(
        """UPDATE section_transfer_requests
           SET status = 'approved', reviewed_at = datetime('now', 'localtime'),
               reviewed_by = ?
           WHERE id = ? AND status = 'pending'""",
        (session["user_id"], request_id),
    ).rowcount
    if not updated:
        conn.close()
        flash("That transfer request is no longer pending.", "warning")
        return redirect(url_for("admin.transfers"))

    _apply_transfer(conn, student, target, transfer_request["reason"])
    conn.close()
    flash(f"{student['full_name']}'s transfer request was approved. "
          "Affected faculty have been notified.", "success")
    return redirect(url_for("admin.transfers"))


@bp.route("/transfers/requests/<int:request_id>/reject", methods=["POST"])
@role_required("admin")
def reject_transfer_request(request_id):
    conn = get_db()
    transfer_request = conn.execute(
        """SELECT r.id, r.student_user_id, st.full_name, st.student_id
           FROM section_transfer_requests r
           JOIN students st ON st.user_id = r.student_user_id
           WHERE r.id = ? AND r.status = 'pending'""",
        (request_id,),
    ).fetchone()
    if not transfer_request:
        conn.close()
        flash("That transfer request is no longer pending.", "warning")
        return redirect(url_for("admin.transfers"))

    updated = conn.execute(
        """UPDATE section_transfer_requests
           SET status = 'rejected', reviewed_at = datetime('now', 'localtime'),
               reviewed_by = ?
           WHERE id = ? AND status = 'pending'""",
        (session["user_id"], request_id),
    ).rowcount
    if updated:
        notify_user(
            conn, transfer_request["student_user_id"],
            "Your block transfer request was not approved. Contact the Registrar if you "
            "would like more information.",
        )
        log_transaction(
            conn, session["user_id"], "admin", "Rejected block transfer request",
            f"{transfer_request['full_name']} ({transfer_request['student_id']})",
        )
        conn.commit()
    conn.close()
    flash("Transfer request rejected.", "info")
    return redirect(url_for("admin.transfers"))


def _apply_transfer(conn, student, target, reason):
    old = conn.execute(
        "SELECT * FROM sections WHERE program=? AND year_level=? AND section_name=?",
        (student["program"], student["year_level"], student["section"]),
    ).fetchone()
    from_label = f"{student['program']} {student['year_level']}-{student['section']}"
    to_label = _section_label(target)
    faculty_ids = set()

    # Leave the old block's classes (keep the enrollment if grades already exist).
    if old:
        for e in conn.execute(
            """SELECT e.id, co.faculty_user_id,
                      (SELECT COUNT(*) FROM grades g WHERE g.enrollment_id = e.id) AS has_grade
               FROM enrollments e JOIN class_offerings co ON co.id = e.offering_id
               WHERE e.student_user_id = ? AND co.section_id = ?""",
            (student["user_id"], old["id"]),
        ).fetchall():
            if e["faculty_user_id"]:
                faculty_ids.add(e["faculty_user_id"])
            if not e["has_grade"]:
                conn.execute("DELETE FROM enrollments WHERE id = ?", (e["id"],))

    # Join the new block's classes.
    for co in conn.execute(
        "SELECT id, faculty_user_id FROM class_offerings WHERE section_id = ?", (target["id"],)
    ).fetchall():
        conn.execute("INSERT OR IGNORE INTO enrollments (student_user_id, offering_id) VALUES (?,?)",
                     (student["user_id"], co["id"]))
        if co["faculty_user_id"]:
            faculty_ids.add(co["faculty_user_id"])

    new_department = target["department"] or student["department"]
    conn.execute(
        "UPDATE students SET program=?, year_level=?, section=?, department=? WHERE user_id=?",
        (target["program"], target["year_level"], target["section_name"], new_department,
         student["user_id"]),
    )
    conn.execute(
        """UPDATE pre_enrolled_students
           SET program=?, year_level=?, section=?, department=? WHERE student_id=?""",
        (target["program"], target["year_level"], target["section_name"], new_department,
         student["student_id"]),
    )

    message = (f"Student transfer: {student['full_name']} ({student['student_id']}) moved from "
               f"{from_label} to {to_label}. Your class list has been updated.")
    for fid in faculty_ids:
        notify_user(conn, fid, message)
    notify_user(conn, student["user_id"],
                f"You have been transferred from {from_label} to {to_label}.")
    conn.execute(
        """INSERT INTO section_transfers
           (student_user_id, from_section, to_section_id, to_section, reason, status,
            faculty_notified, created_by) VALUES (?,?,?,?,?, 'completed', ?, ?)""",
        (student["user_id"], from_label, target["id"], to_label, reason,
         len(faculty_ids), session["user_id"]),
    )
    log_transaction(conn, session["user_id"], "admin", "Transferred student",
                    f"{student['full_name']} ({student['student_id']}): {from_label} -> {to_label}; "
                    f"{len(faculty_ids)} faculty notified")
    conn.commit()
