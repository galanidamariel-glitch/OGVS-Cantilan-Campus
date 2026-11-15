"""Copy the existing SQLite database into the configured PostgreSQL database."""
import mimetypes
import os
import re
import sqlite3
import sys
from pathlib import Path

from database import DB_PATH, get_db, init_db

BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "static" / "uploads" / "profiles"


def _quoted_identifier(value):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"Unexpected database identifier: {value!r}")
    return f'"{value}"'


def _transfer_request_rows(source, columns):
    if "requested_section_name" in columns:
        rows = source.execute(
            "SELECT * FROM section_transfer_requests"
        ).fetchall()
        return columns, rows

    rows = source.execute(
        """SELECT r.id, r.student_user_id, src.program AS from_program,
                  src.year_level AS from_year_level,
                  src.section_name AS from_section_name,
                  dst.section_name AS requested_section_name, r.reason, r.status,
                  r.requested_at, r.reviewed_at, r.reviewed_by
           FROM section_transfer_requests r
           JOIN sections src ON src.id = r.from_section_id
           JOIN sections dst ON dst.id = r.to_section_id"""
    ).fetchall()
    migrated_columns = [
        "id", "student_user_id", "from_program", "from_year_level",
        "from_section_name", "requested_section_name", "reason", "status",
        "requested_at", "reviewed_at", "reviewed_by",
    ]
    return migrated_columns, rows


def _blob_profile_image(table, row):
    filename = row.get("profile_image")
    if not filename or filename.startswith(("http://", "https://")):
        return filename

    image_path = PROFILE_DIR / Path(filename).name
    if not image_path.is_file():
        raise FileNotFoundError(f"Profile image not found for migration: {image_path}")

    from vercel.blob.sync import put_blob

    content_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
    result = put_blob(
        f"profiles/migrated/{table}/{row['user_id']}/{image_path.name}",
        image_path.read_bytes(),
        access="public",
        content_type=content_type,
        allow_overwrite=True,
    )
    return result.url


def migrate(source_path=DB_PATH):
    if not os.environ.get("DATABASE_URL", "").strip():
        raise RuntimeError("Set DATABASE_URL to the Neon PostgreSQL connection string.")
    source_path = Path(source_path)
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {source_path}")

    init_db()
    source = sqlite3.connect(source_path)
    source.row_factory = sqlite3.Row
    target = get_db()

    tables = source.execute(
        """SELECT name, sql FROM sqlite_master
           WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
           ORDER BY rowid"""
    ).fetchall()
    migrated_count = 0
    try:
        for table_info in tables:
            table = table_info["name"]
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
                raise ValueError(f"Unexpected table name: {table!r}")
            columns = [
                row["name"] for row in source.execute(
                    f"PRAGMA table_info({_quoted_identifier(table)})"
                )
            ]
            if table == "section_transfer_requests":
                columns, rows = _transfer_request_rows(source, columns)
            else:
                rows = source.execute(
                    f"SELECT * FROM {_quoted_identifier(table)}"
                ).fetchall()

            target_columns = {
                row["column_name"] for row in target.execute(
                    """SELECT column_name FROM information_schema.columns
                       WHERE table_schema = current_schema() AND table_name = ?""",
                    (table,),
                ).fetchall()
            }
            columns = [column for column in columns if column in target_columns]
            if not columns:
                continue

            quoted_table = _quoted_identifier(table)
            quoted_columns = ", ".join(_quoted_identifier(column) for column in columns)
            placeholders = ", ".join("%s" for _ in columns)
            insert = (
                f"INSERT INTO {quoted_table} ({quoted_columns}) "
                f"VALUES ({placeholders}) ON CONFLICT DO NOTHING"
            )
            for row in rows:
                values = dict(row)
                if table in ("students", "faculty") and "profile_image" in values:
                    values["profile_image"] = _blob_profile_image(table, values)
                target.execute(insert, tuple(values[column] for column in columns))
                migrated_count += 1

            if table_info["sql"] and "AUTOINCREMENT" in table_info["sql"].upper():
                target.execute(
                    """SELECT setval(pg_get_serial_sequence(?, 'id'),
                           COALESCE(MAX(id), 1), MAX(id) IS NOT NULL)
                       FROM """ + quoted_table,
                    (table,),
                )
            target.commit()
    finally:
        source.close()
        target.close()

    return migrated_count


if __name__ == "__main__":
    source_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DB_PATH
    print(f"Migrated {migrate(source_path)} rows from {source_path}.")
