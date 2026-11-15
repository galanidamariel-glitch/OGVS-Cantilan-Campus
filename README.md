# OGVS — Online Grade Viewing System
### NEMSU Cantilan Campus

"Every grade, one login away."

A role-based grade viewing and encoding system for **Students**, **Faculty**, and the
**Admin / Registrar's Office**, built with Flask and SQLite.

## 1. Requirements

- Python 3.10+
- pip

## 2. Setup (VS Code)

```bash
# 1. Open this folder in VS Code
# 2. Create a virtual environment
python -m venv venv

# 3. Activate it
venv\Scripts\activate        # Windows
source venv/bin/activate     # macOS / Linux

# 4. Install dependencies
pip install -r requirements.txt

# 5. Run the app (creates and seeds ogvs.db on first run)
python app.py

```
cd ".\OGVS_Flask_System_restyled\ogvs"
pip install -r requirements.txt
python app.py


Open **http://127.0.0.1:5000** in your browser.

### Email notifications (Resend)

OGVS queues an email for each in-app notification when the recipient has an
email address on file. Configure a Resend API key and a verified sender address
in the environment before starting the app. In PowerShell:

```powershell
$env:RESEND_API_KEY = "your-resend-api-key"
$env:EMAIL_FROM = "OGVS Notifications <notifications@your-verified-domain>"
python app.py
```

Keep the API key out of source control. Emails are retried after temporary
delivery failures; missing credentials leave messages queued until configured.

## Vercel deployment (Neon + Blob)

Set the Vercel project root directory to `OGVS_Flask_System_restyled/ogvs`.
Create a Neon PostgreSQL database and connect a **public** Vercel Blob store.
Configure these Vercel environment variables:

- `DATABASE_URL` — Neon connection string (use its pooled connection string).
- `SECRET_KEY` — a long random value used to sign Flask sessions.
- `RESEND_API_KEY` and `EMAIL_FROM` — Resend API key and verified sender.
- `BLOB_READ_WRITE_TOKEN` — connect a Blob store with a read/write token.

The app creates the PostgreSQL schema on the first Vercel request, but it does
not seed production accounts. Before deploying, run the migration once from this
project directory with `DATABASE_URL` and `BLOB_READ_WRITE_TOKEN` set. This
copies the existing SQLite records and uploads profile images; without it,
Neon will be empty and users will not be able to sign in:

```powershell
$env:DATABASE_URL = "<Neon pooled connection string>"
$env:BLOB_READ_WRITE_TOKEN = "<Vercel Blob read/write token>"
python migrate_sqlite_to_postgres.py
```

Generate a session secret with `python -c "import secrets; print(secrets.token_hex(32))"`.
Do not commit any of these environment values. The local SQLite database is
excluded from the Vercel deployment.

## 3. Seeded demo accounts

The database is auto-created and seeded the first time you run the app
(see `database.py: seed_data()`).

| Role     | Username     | Password    | Notes                                   |
|----------|--------------|-------------|------------------------------------------|
| Admin    | `registrar`  | `admin123`  | Registrar / Admin account                |
| Faculty  | `jdelacruz`  | `faculty123`| Assigned to IT101 & IT102                |
| Student  | `2023-00123` | `student123`| Already enrolled with sample grades      |

A **new** student can also self-register through *Register* — registration
only succeeds if the Student ID matches a record the registrar already
pre-loaded (simulating validation against enrollment records). Try
Student ID `2023-00456` (pre-loaded, not yet registered) to test this.

## Faculty student nominations

From a class's **Class List**, faculty can nominate a student by Student ID.
The Registrar reviews nominations under **Student Nominations** and makes the
final approve/reject decision. Approved students with existing accounts are
added to the class roster immediately. If an approved nominee has not
registered yet, approval creates the campus enrollment record; the student is
added to that class automatically after completing registration.

Students can request a block/section transfer from **Block Transfer** in their
sidebar. Requests are limited to other sections in the same program and year
level and must be approved or rejected by the Registrar under **Block Transfers**.
Students can enter a desired section even if it is not set up yet; the Registrar
must add that section under **Assignments** before approving the request.

## Departments and programs

The admin assignment and student-record forms use department-specific program
suggestions:

| Department | Program(s) |
|---|---|
| Department of Computer Studies (DCS) | BSCpE — Bachelor of Science in Computer Engineering; BSCS — Bachelor of Science in Computer Science; BSIT — Bachelor of Science in Information Technology |
| Department of Industrial Technology (DIT) | BSAT — Bachelor of Science in Automotive Technology |
| Department of College of Criminal Justice Education (CCJE) | BSCrim — Bachelor of Science in Criminology |
| Department of Business Management (DBM) | BSHM — Bachelor of Science in Hospitality Management |
| Department of General Teacher Training (DGTT) | BSEd-English — Bachelor of Secondary Education major in English |

On **Assignments**, choose a department to filter its program suggestions,
then choose a Year Level and Section/Block letter A-Z. Typing in the fields
shows matching suggestions. The selected department and program are saved with
the section and student enrollment records.

## 4. Project structure

```
ogvs/
├── app.py                 # App factory + entry point
├── database.py             # SQLite connection, schema init, seed data
├── schema.sql               # Full DB schema
├── routes/
│   ├── auth.py              # Register / login / logout
│   ├── student.py           # Dashboard, grades, history, slip, notifications
│   ├── faculty.py           # Classes, grade encoding, release, corrections
│   └── admin.py             # Manage students/faculty, assignments, finalization, reports
├── utils/
│   ├── grading.py           # Final grade formula, GWA, remarks
│   ├── decorators.py         # login_required / role_required guards
│   └── pdf.py                 # Grade slip PDF export (reportlab)
├── templates/                # Jinja2 templates, NEMSU blue/gold theme
└── static/
    ├── css/style.css
    └── img/logo.jpg
```

## 5. Grading formula (adjust to your campus's actual formula)

```
Final Grade = (Prelim × 30%) + (Midterm × 30%) + (Finals × 40%)
Passed  : Final Grade ≤ 3.00
Failed  : Final Grade > 3.00
Incomplete : any period not yet encoded
```
Grades use the college scale: 1.00 is highest, 3.00 is passing, and 5.00 is failing.
Edit `utils/grading.py` to match NEMSU Cantilan's official computation.

## 6. Grade workflow

```
Faculty encodes → Submitted for review → Registrar approves
   → Faculty releases → Visible to student
```
A released grade can only be changed through a **correction request**
(Faculty → reason recorded → Registrar approves/returns).

## 7. Design notes

Color palette reflects NEMSU's official blue identity ("pacific blue" /
sky-blue seal, per the university hymn and seal description), paired with
gold for accreditation-style accent and contrast. The application uses the NEMSU
seal in `static/img/logo.jpg`.

## 8. Compliance checklist - General Comments (where each item lives)

| # | Comment | Where |
|---|---------|-------|
| 1 | Faculty nominate students; Registrar approves class admission | Faculty > My Classes > **Class List** (`/faculty/classes/<id>/students`). Registrar reviews requests under **Student Nominations**; students can only open classes they are enrolled in (`/student/classes/<id>`). |
| 2 | Faculty + students in one admin module, Minor/Major | Admin > **Faculty & Students** (`/admin/directory`); change the tag from the dropdown in the Classification column. |
| 3 | Faculty > subjects > enrolled students | Click a faculty name, then a subject code. |
| 4 | Instructor: students per subject, add + edit | Class List page (Add Student form, Edit button per row). |
| 5 | Student adds personal details | Student > **My Details**. |
| 6 | Real-time admin notifications of faculty requests | Admin > **Notifications** (refreshes every 4 s, sidebar badge every 5 s). |
| 7 | Real-time, printable transaction history | Admin / Faculty > **Transaction History** (live, Print button). |
| 8 | Track block/section transfers, notify faculty | Admin > **Block Transfers**. |
| 9 | Five departments x five students | Department/program catalog and sample cohorts are defined by `DEPARTMENTS` in `database.py`; sample records are seeded automatically on start-up. |
| 10 | Subject code / description pop-up list | Admin > Assignments (type in either field). |

Sample accounts added: `mreyes` / `faculty123` (Minor); `jdelacruz` is Major.
All sample students use password `student123` and log in with their Student ID.
