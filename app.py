import os

from flask import Flask, request, session, url_for

from database import init_db, seed_data, seed_campus_sample_data, get_db
from routes.auth import bp as auth_bp
from routes.student import bp as student_bp
from routes.faculty import bp as faculty_bp
from routes.admin import bp as admin_bp
from utils.email import send_pending_email_notifications


def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "ogvs-dev-secret-key-change-me")
    if os.environ.get("VERCEL") and not os.environ.get("SECRET_KEY"):
        raise RuntimeError("SECRET_KEY must be configured when running on Vercel.")
    app.config["RESEND_API_KEY"] = os.environ.get("RESEND_API_KEY", "").strip()
    app.config["EMAIL_FROM"] = os.environ.get("EMAIL_FROM", "").strip()
    if not app.config["RESEND_API_KEY"] or not app.config["EMAIL_FROM"]:
        app.logger.warning(
            "Email notifications are queued but not sent; configure RESEND_API_KEY "
            "and EMAIL_FROM."
        )

    app.register_blueprint(auth_bp)
    app.register_blueprint(student_bp)
    app.register_blueprint(faculty_bp)
    app.register_blueprint(admin_bp)

    database_initialized = False

    @app.before_request
    def initialize_deployment_database():
        nonlocal database_initialized
        database_url = os.environ.get("DATABASE_URL", "").strip()
        if os.environ.get("VERCEL") and not database_url:
            raise RuntimeError("DATABASE_URL must be configured when running on Vercel.")
        if database_url and not database_initialized:
            init_db()
            database_initialized = True

    @app.after_request
    def deliver_email_notifications(response):
        send_pending_email_notifications()
        return response

    @app.context_processor
    def inject_globals():
        unread = 0
        role = session.get("role")
        page = request.endpoint
        if session.get("user_id"):
            conn = get_db()
            unread = conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0",
                (session["user_id"],),
            ).fetchone()[0]
            conn.close()
        return {
            "current_role": role,
            "current_username": session.get("username"),
            "unread_count": unread,
            "portal_page": page,
            "profile_image_url": _profile_image_url,
        }

    def _profile_image_url(image):
        if image and image.startswith(("http://", "https://")):
            return image
        return url_for("static", filename=f"uploads/profiles/{image}")

    @app.errorhandler(403)
    def forbidden(e):
        return "403 — You don't have access to this page.", 403

    @app.errorhandler(404)
    def not_found(e):
        return "404 — Page not found.", 404

    return app


app = create_app()

if __name__ == "__main__":
    init_db()
    seed_data()
    seed_campus_sample_data()
    app.run(host="0.0.0.0", port=5000, debug=True)
