"""SQLite/PostgreSQL connection handling, schema setup, and demo seed data."""
import os
import re
import sqlite3
from pathlib import Path
from werkzeug.security import generate_password_hash

from utils.grading import percentage_to_college

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "instance" / "ogvs.db"
SCHEMA_PATH = BASE_DIR / "schema.sql"

# Five campus departments and their approved programs. The sample rows use one
# representative program per department while the directory exposes all programs.
DEPARTMENTS = [
    {"code": "DCS", "name": "Department of Computer Studies",
     "programs": [
         {"code": "BSCpE", "name": "Bachelor of Science in Computer Engineering"},
         {"code": "BSCS", "name": "Bachelor of Science in Computer Science"},
         {"code": "BSIT", "name": "Bachelor of Science in Information Technology"},
     ],
     "program": "BSIT",
     "subject": ("IT103", "Database Management Systems"),
     "students": [("2023-00123", "Mariel D. Santos"), ("2023-00124", "Joshua R. Pelayo"),
                  ("2023-00125", "Angelica M. Tan"), ("2023-00126", "Kenneth L. Ybanez"),
                  ("2023-00127", "Rhea C. Lumapas")]},
    {"code": "DIT", "name": "Department of Industrial Technology",
     "programs": [
         {"code": "BSAT", "name": "Bachelor of Science in Automotive Technology"},
     ],
     "program": "BSAT",
     "subject": ("AT101", "Automotive Technology"),
     "students": [("2023-00401", "Leo F. Maglasang"), ("2023-00402", "Christian J. Ocampo"),
                  ("2023-00403", "Bryan S. Tumulak"), ("2023-00404", "Nathan E. Quirog"),
                  ("2023-00405", "Gerald H. Pacatang")]},
    {"code": "CCJE", "name": "Department of College of Criminal Justice Education",
     "programs": [
         {"code": "BSCrim", "name": "Bachelor of Science in Criminology"},
     ],
     "program": "BSCrim",
     "subject": ("CRIM101", "Introduction to Criminology"),
     "students": [("2023-00501", "Trisha M. Ramirez"), ("2023-00502", "Vincent A. Solano"),
                  ("2023-00503", "Gwen D. Pancho"), ("2023-00504", "Allan R. Cuizon"),
                  ("2023-00505", "Bea C. Nadela")]},
    {"code": "DBM", "name": "Department of Business Management",
     "programs": [
         {"code": "BSHM", "name": "Bachelor of Science in Hospitality Management"},
     ],
     "program": "BSHM",
     "subject": ("HM101", "Introduction to Hospitality Management"),
     "students": [("2023-00201", "Carla P. Dumaog"), ("2023-00202", "Dennis A. Villaruz"),
                  ("2023-00203", "Maricris T. Alcala"), ("2023-00204", "Jerome B. Cabanlit"),
                  ("2023-00205", "Shaira N. Escalante")]},
    {"code": "DGTT", "name": "Department of General Teacher Training",
     "programs": [
         {"code": "BSEd-English", "name": "Bachelor of Secondary Education major in English"},
     ],
     "program": "BSEd-English",
     "subject": ("EDEN101", "Introduction to English Language Studies"),
     "students": [("2023-00301", "Ana Liza G. Mabini"), ("2023-00302", "Ryan O. Casas"),
                  ("2023-00303", "Pia K. Sarmiento"), ("2023-00304", "Mark Anthony D. Lopez"),
                  ("2023-00305", "Jenny Rose V. Bautista")]},
]
SAMPLE_YEAR_LEVEL = "3rd Year"
SAMPLE_SECTION = "A"
SAMPLE_TERM = ("1st Semester", "2026-2027")
LEGACY_SAMPLE_PROGRAMS = {
    "DIT": "BSIndT",
    "CCJE": "BSA",
    "DBM": "BSBA",
    "DGTT": "BSED",
}


class _PostgresRow(dict):
    def __init__(self, row):
        super().__init__(row)
        self._values = tuple(row.values())

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


class _PostgresCursor:
    def __init__(self, connection, cursor):
        self._connection = connection
        self._cursor = cursor

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def lastrowid(self):
        row = self._connection._raw.execute(
            "SELECT LASTVAL() AS lastrowid"
        ).fetchone()
        return next(iter(row.values()))

    def execute(self, query, parameters=None):
        query = _postgres_query(query)
        if parameters is None:
            self._cursor.execute(query)
        else:
            self._cursor.execute(query, parameters)
        return self

    def fetchone(self):
        row = self._cursor.fetchone()
        return _PostgresRow(row) if row is not None else None

    def fetchall(self):
        return [_PostgresRow(row) for row in self._cursor.fetchall()]

    def __iter__(self):
        for row in self._cursor:
            yield _PostgresRow(row)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _PostgresConnection:
    def __init__(self, raw_connection):
        self._raw = raw_connection

    def execute(self, query, parameters=None):
        return _PostgresCursor(self, self._raw.cursor()).execute(query, parameters)

    def cursor(self):
        return _PostgresCursor(self, self._raw.cursor())

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()


def _postgres_query(query):
    ignore_conflicts = bool(
        re.match(r"\s*INSERT\s+OR\s+IGNORE\s+INTO\b", query, flags=re.IGNORECASE)
    )
    if ignore_conflicts:
        query = re.sub(
            r"^\s*INSERT\s+OR\s+IGNORE\s+INTO\b",
            "INSERT INTO",
            query,
            count=1,
            flags=re.IGNORECASE,
        )
    query = re.sub(
        r"\bdatetime\s*\(\s*'now'\s*,\s*'localtime'\s*,\s*\?\s*\)",
        "(CURRENT_TIMESTAMP + ?::interval)::text",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        r"\bdatetime\s*\(\s*'now'(?:\s*,\s*'localtime')?\s*\)",
        "CURRENT_TIMESTAMP::text",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        r"\blast_insert_rowid\s*\(\s*\)", "LASTVAL()", query, flags=re.IGNORECASE
    )
    query = re.sub(r"\bLIKE\b", "ILIKE", query, flags=re.IGNORECASE)
    query = query.replace("?", "%s")
    if ignore_conflicts:
        query = query.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return query


def _postgres_schema(schema):
    schema = re.sub(
        r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
        "BIGSERIAL PRIMARY KEY",
        schema,
        flags=re.IGNORECASE,
    )
    schema = re.sub(r"\bINTEGER\b", "BIGINT", schema, flags=re.IGNORECASE)
    schema = re.sub(r"\bREAL\b", "DOUBLE PRECISION", schema, flags=re.IGNORECASE)
    schema = re.sub(
        r"\bdatetime\s*\(\s*'now'(?:\s*,\s*'localtime')?\s*\)",
        "CURRENT_TIMESTAMP::text",
        schema,
        flags=re.IGNORECASE,
    )
    return schema


def get_db():
    """Return a SQLite or PostgreSQL connection with dict and positional row access."""
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if database_url:
        import psycopg
        from psycopg.rows import dict_row

        return _PostgresConnection(
            psycopg.connect(database_url, row_factory=dict_row)
        )
    if os.environ.get("VERCEL"):
        raise RuntimeError("DATABASE_URL must be configured when running on Vercel.")

    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Create tables if they don't already exist."""
    conn = get_db()
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema = f.read()
    if isinstance(conn, _PostgresConnection):
        for statement in schema.split(";"):
            statement = statement.strip()
            if statement and "CREATE TABLE" in statement.upper():
                conn.execute(_postgres_schema(statement))
        conn.commit()
        conn.close()
        return
    conn.executescript(schema)
    _migrate_section_transfer_requests(conn)
    student_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(students)")
    }
    if "profile_image" not in student_columns:
        conn.execute("ALTER TABLE students ADD COLUMN profile_image TEXT")
    faculty_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(faculty)")
    }
    if "employee_id" not in faculty_columns:
        conn.execute("ALTER TABLE faculty ADD COLUMN employee_id TEXT")
    if "verification_status" not in faculty_columns:
        conn.execute(
            "ALTER TABLE faculty ADD COLUMN verification_status TEXT NOT NULL DEFAULT 'approved'"
        )
    if "position" not in faculty_columns:
        conn.execute(
            "ALTER TABLE faculty ADD COLUMN position TEXT NOT NULL DEFAULT 'Faculty'"
        )
    if "profile_image" not in faculty_columns:
        conn.execute("ALTER TABLE faculty ADD COLUMN profile_image TEXT")
    offering_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(class_offerings)")
    }
    if "meeting_time" not in offering_columns:
        conn.execute("ALTER TABLE class_offerings ADD COLUMN meeting_time TEXT")
    if "meeting_days" not in offering_columns:
        conn.execute("ALTER TABLE class_offerings ADD COLUMN meeting_days TEXT")
    default_schedules = {
        "IT101": ("1:00-2:30", "MTH"),
        "IT102": ("10:30-12:00", "MTH"),
        "GE105": ("9:00-10:30", "TF"),
    }
    existing_offerings = conn.execute(
        """SELECT co.id, s.code FROM class_offerings co
           JOIN subjects s ON s.id = co.subject_id
           WHERE co.meeting_time IS NULL OR co.meeting_days IS NULL"""
    ).fetchall()
    for offering in existing_offerings:
        schedule = default_schedules.get(offering["code"])
        if schedule:
            conn.execute(
                "UPDATE class_offerings SET meeting_time=?, meeting_days=? WHERE id=?",
                (*schedule, offering["id"]),
            )
    # --- Columns added for the compliance checklist (General Comments) -----
    for column in ("department", "contact_number", "address", "birthdate",
                   "guardian_name", "guardian_contact"):
        if column not in student_columns:
            conn.execute(f"ALTER TABLE students ADD COLUMN {column} TEXT")
    if "faculty_type" not in faculty_columns:
        conn.execute(
            "ALTER TABLE faculty ADD COLUMN faculty_type TEXT NOT NULL DEFAULT 'Minor'"
        )
    section_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(sections)")
    }
    if "department" not in section_columns:
        conn.execute("ALTER TABLE sections ADD COLUMN department TEXT")
    pre_enrolled_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(pre_enrolled_students)")
    }
    if "department" not in pre_enrolled_columns:
        conn.execute("ALTER TABLE pre_enrolled_students ADD COLUMN department TEXT")
    _migrate_percentage_grades(conn)
    conn.commit()
    conn.close()


def _migrate_section_transfer_requests(conn):
    """Preserve existing requests while allowing destinations not yet in sections."""
    columns = {
        row["name"] for row in conn.execute(
            "PRAGMA table_info(section_transfer_requests)"
        )
    }
    if "requested_section_name" in columns:
        return

    conn.execute(
        """CREATE TABLE section_transfer_requests_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_user_id INTEGER NOT NULL REFERENCES users(id),
            from_program TEXT NOT NULL,
            from_year_level TEXT NOT NULL,
            from_section_name TEXT NOT NULL,
            requested_section_name TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'approved', 'rejected')),
            requested_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            reviewed_at TEXT,
            reviewed_by INTEGER REFERENCES users(id)
        )"""
    )
    conn.execute(
        """INSERT INTO section_transfer_requests_new
           (id, student_user_id, from_program, from_year_level, from_section_name,
            requested_section_name, reason, status, requested_at, reviewed_at, reviewed_by)
           SELECT r.id, r.student_user_id, src.program, src.year_level, src.section_name,
                  dst.section_name, r.reason, r.status, r.requested_at, r.reviewed_at,
                  r.reviewed_by
           FROM section_transfer_requests r
           JOIN sections src ON src.id = r.from_section_id
           JOIN sections dst ON dst.id = r.to_section_id"""
    )
    conn.execute("DROP TABLE section_transfer_requests")
    conn.execute(
        "ALTER TABLE section_transfer_requests_new RENAME TO section_transfer_requests"
    )


def _migrate_percentage_grades(conn):
    """Convert legacy 0-100 grades to the college 1.00-5.00 scale once."""
    rows = conn.execute(
        "SELECT id, prelim, midterm, finals, final_grade FROM grades"
    ).fetchall()
    for row in rows:
        values = [row["prelim"], row["midterm"], row["finals"], row["final_grade"]]
        if not any(value is not None and value > 5 for value in values):
            continue
        prelim = percentage_to_college(row["prelim"])
        midterm = percentage_to_college(row["midterm"])
        finals = percentage_to_college(row["finals"])
        final_grade = percentage_to_college(row["final_grade"])
        remarks = "Passed" if final_grade is not None and final_grade <= 3 else "Failed"
        conn.execute(
            "UPDATE grades SET prelim=?, midterm=?, finals=?, final_grade=?, remarks=? WHERE id=?",
            (prelim, midterm, finals, final_grade, remarks, row["id"]),
        )


def _is_seeded(conn):
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0


def seed_data():
    """Populate the database with demo accounts and sample records, once."""
    conn = get_db()
    if _is_seeded(conn):
        conn.close()
        return

    cur = conn.cursor()

    # --- Admin / Registrar ---------------------------------------------
    cur.execute(
        "INSERT INTO users (username, password_hash, role, email) VALUES (?,?,?,?)",
        ("registrar", generate_password_hash("admin123"), "admin",
         "registrar@nemsu.edu.ph"),
    )

    # --- Faculty ----------------------------------------------------------
    cur.execute(
        "INSERT INTO users (username, password_hash, role, email) VALUES (?,?,?,?)",
        ("jdelacruz", generate_password_hash("faculty123"), "faculty",
         "jdelacruz@nemsu.edu.ph"),
    )
    faculty_id = cur.lastrowid
    cur.execute(
        "INSERT INTO faculty (user_id, full_name, faculty_type) VALUES (?,?,?)",
        (faculty_id, "Prof. Juana Dela Cruz", "Major"),
    )

    # --- Subjects & section --------------------------------------------
    cur.execute("INSERT INTO subjects (code, name, units) VALUES (?,?,?)",
                ("IT101", "Introduction to Computing", 3))
    it101 = cur.lastrowid
    cur.execute("INSERT INTO subjects (code, name, units) VALUES (?,?,?)",
                ("IT102", "Web Systems and Technologies", 3))
    it102 = cur.lastrowid
    cur.execute("INSERT INTO subjects (code, name, units) VALUES (?,?,?)",
                ("GE105", "Purposive Communication", 3))
    ge105 = cur.lastrowid

    cur.execute(
        "INSERT INTO sections (program, year_level, section_name) VALUES (?,?,?)",
        ("BSIT", "3rd Year", "A"),
    )
    section_id = cur.lastrowid

    term = ("1st Semester", "2026-2027")
    offering_ids = []
    schedules = {
        it101: ("1:00-2:30", "MTH"),
        it102: ("10:30-12:00", "MTH"),
        ge105: ("9:00-10:30", "TF"),
    }
    for subj_id in (it101, it102, ge105):
        cur.execute(
            """INSERT INTO class_offerings
               (subject_id, section_id, faculty_user_id, semester, school_year,
                meeting_time, meeting_days)
               VALUES (?,?,?,?,?,?,?)""",
            (subj_id, section_id, faculty_id, *term, *schedules[subj_id]),
        )
        offering_ids.append(cur.lastrowid)

    # --- Sample enrolled student -----------------------------------------
    cur.execute(
        "INSERT INTO users (username, password_hash, role, email) VALUES (?,?,?,?)",
        ("2023-00123", generate_password_hash("student123"), "student",
         "mdelacruz.student@nemsu.edu.ph"),
    )
    student_id = cur.lastrowid
    cur.execute(
        "INSERT INTO students (user_id, student_id, full_name, program, year_level, section,"
        " department) VALUES (?,?,?,?,?,?,?)",
        (student_id, "2023-00123", "Mariel D. Santos", "BSIT", "3rd Year", "A",
         "DCS"),
    )
    for off_id, grades in zip(
        offering_ids,
        [(1.75, 1.75, 1.50), (2.50, 4.00, 4.00), (1.25, 1.50, 1.25)],
    ):
        cur.execute(
            "INSERT INTO enrollments (student_user_id, offering_id) VALUES (?,?)",
            (student_id, off_id),
        )
        enrollment_id = cur.lastrowid
        prelim, midterm, finals = grades
        final = round(prelim * 0.3 + midterm * 0.3 + finals * 0.4, 2)
        remarks = "Passed" if final <= 3 else "Failed"
        cur.execute(
            """INSERT INTO grades
               (enrollment_id, prelim, midterm, finals, final_grade, remarks, status)
               VALUES (?,?,?,?,?,?, 'released')""",
            (enrollment_id, prelim, midterm, finals, final, remarks),
        )

    cur.execute(
        "INSERT INTO notifications (user_id, message) VALUES (?,?)",
        (student_id, "Your grades for 1st Semester, 2026-2027 have been released."),
    )

    # --- Registrar's pre-enrollment records (used to validate self-registration) --
    cur.execute(
        "INSERT OR IGNORE INTO pre_enrolled_students "
        "(student_id, full_name, program, year_level, section, department, is_registered) "
        "VALUES (?,?,?,?,?,?,1)",
        ("2023-00123", "Mariel D. Santos", "BSIT", "3rd Year", "A", "DCS"),
    )
    cur.execute(
        "INSERT OR IGNORE INTO pre_enrolled_students "
        "(student_id, full_name, program, year_level, section, department, is_registered) "
        "VALUES (?,?,?,?,?,?,0)",
        ("2023-00456", "Carlo P. Reyes", "BSIT", "3rd Year", "A", "DCS"),
    )

    conn.commit()
    conn.close()


def seed_campus_sample_data():
    """General Comments #9: five departments x five students (25 students).

    Safe to run on every start-up and on existing databases - nothing that
    already exists is duplicated or overwritten.
    """
    conn = get_db()
    cur = conn.cursor()

    # A second sample instructor so the Minor / Major tag has both values.
    reyes = cur.execute("SELECT id FROM users WHERE username = 'mreyes'").fetchone()
    if reyes:
        reyes_id = reyes["id"]
    else:
        cur.execute(
            "INSERT INTO users (username, password_hash, role, email) VALUES (?,?,?,?)",
            ("mreyes", generate_password_hash("faculty123"), "faculty", "mreyes@nemsu.edu.ph"),
        )
        reyes_id = cur.lastrowid
        cur.execute(
            "INSERT INTO faculty (user_id, full_name, employee_id, position, "
            "verification_status, faculty_type) VALUES (?,?,?,?, 'approved', 'Minor')",
            (reyes_id, "Prof. Mario Reyes", "EMP-0002", "Instructor I"),
        )
    delacruz = cur.execute("SELECT id FROM users WHERE username = 'jdelacruz'").fetchone()
    instructors = [delacruz["id"] if delacruz else reyes_id, reyes_id]

    for index, dept in enumerate(DEPARTMENTS):
        cur.execute(
            "INSERT OR IGNORE INTO sections (program, year_level, section_name, department) "
            "VALUES (?,?,?,?)",
            (dept["program"], SAMPLE_YEAR_LEVEL, SAMPLE_SECTION, dept["code"]),
        )
        cur.execute(
            "UPDATE sections SET department = ? WHERE program = ? AND year_level = ? "
            "AND section_name = ?",
            (dept["code"], dept["program"], SAMPLE_YEAR_LEVEL, SAMPLE_SECTION),
        )
        section_id = cur.execute(
            "SELECT id FROM sections WHERE program=? AND year_level=? AND section_name=?",
            (dept["program"], SAMPLE_YEAR_LEVEL, SAMPLE_SECTION),
        ).fetchone()["id"]

        code, name = dept["subject"]
        cur.execute("INSERT OR IGNORE INTO subjects (code, name, units) VALUES (?,?,3)", (code, name))
        subject_id = cur.execute("SELECT id FROM subjects WHERE code = ?", (code,)).fetchone()["id"]
        offering = cur.execute(
            "SELECT id FROM class_offerings WHERE subject_id=? AND section_id=? "
            "AND semester=? AND school_year=?",
            (subject_id, section_id, *SAMPLE_TERM),
        ).fetchone()
        if offering:
            offering_id = offering["id"]
        else:
            cur.execute(
                "INSERT INTO class_offerings (subject_id, section_id, faculty_user_id, "
                "semester, school_year, meeting_time, meeting_days) VALUES (?,?,?,?,?,?,?)",
                (subject_id, section_id, instructors[index % 2], *SAMPLE_TERM,
                 ("8:00-9:30", "10:30-12:00", "1:00-2:30", "2:30-4:00", "4:00-5:30")[index],
                 ("MW", "TF", "MTH", "WF", "TTH")[index]),
            )
            offering_id = cur.lastrowid

        for student_no, full_name in dept["students"]:
            user = cur.execute("SELECT id FROM users WHERE username = ?", (student_no,)).fetchone()
            legacy_program = LEGACY_SAMPLE_PROGRAMS.get(dept["code"], "")
            if user:
                user_id = user["id"]
                cur.execute(
                    """UPDATE students SET program = ?, year_level = ?, section = ?,
                       department = ? WHERE user_id = ?
                       AND (department IS NULL OR department IN ('CITE', 'CBM', 'CTE', 'CET', 'CAC')
                            OR program = ?)""",
                    (dept["program"], SAMPLE_YEAR_LEVEL, SAMPLE_SECTION, dept["code"], user_id,
                     legacy_program),
                )
            else:
                email = full_name.lower().replace(" ", ".").replace("..", ".") + "@gmail.com"
                cur.execute(
                    "INSERT INTO users (username, password_hash, role, email) VALUES (?,?,?,?)",
                    (student_no, generate_password_hash("student123"), "student", email),
                )
                user_id = cur.lastrowid
                cur.execute(
                    "INSERT INTO students (user_id, student_id, full_name, program, year_level,"
                    " section, department) VALUES (?,?,?,?,?,?,?)",
                    (user_id, student_no, full_name, dept["program"], SAMPLE_YEAR_LEVEL,
                     SAMPLE_SECTION, dept["code"]),
                )
            cur.execute(
                """INSERT OR IGNORE INTO pre_enrolled_students
                   (student_id, full_name, program, year_level, section, department, is_registered)
                   VALUES (?, ?, ?, ?, ?, ?, 1)""",
                (student_no, full_name, dept["program"], SAMPLE_YEAR_LEVEL, SAMPLE_SECTION,
                 dept["code"]),
            )
            cur.execute(
                """UPDATE pre_enrolled_students
                   SET full_name = ?, program = ?, year_level = ?, section = ?, department = ?
                   WHERE student_id = ?
                     AND (department IS NULL OR department IN ('CITE', 'CBM', 'CTE', 'CET', 'CAC')
                          OR program = ?)""",
                (full_name, dept["program"], SAMPLE_YEAR_LEVEL, SAMPLE_SECTION, dept["code"],
                 student_no, legacy_program),
            )
            # Enrol in every class offered to this block (incl. the demo ones).
            for off in cur.execute(
                "SELECT id FROM class_offerings WHERE section_id = ?", (section_id,)
            ).fetchall():
                cur.execute(
                    "INSERT OR IGNORE INTO enrollments (student_user_id, offering_id) VALUES (?,?)",
                    (user_id, off["id"]),
                )

    conn.commit()
    conn.close()
