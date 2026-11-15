"""Profile image storage for local development and Vercel deployments."""
import os
from pathlib import Path

from flask import current_app


def save_profile_image(uploaded_file, filename):
    if os.environ.get("VERCEL") or os.environ.get("BLOB_READ_WRITE_TOKEN"):
        from vercel.blob.sync import put_blob

        extension = Path(filename).suffix.lower()
        content_type = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }[extension]
        result = put_blob(
            f"profiles/{filename}",
            uploaded_file.read(),
            access="public",
            content_type=content_type,
            allow_overwrite=True,
        )
        return result.url

    upload_dir = Path(current_app.root_path) / "static" / "uploads" / "profiles"
    upload_dir.mkdir(parents=True, exist_ok=True)
    uploaded_file.save(upload_dir / filename)
    return filename
