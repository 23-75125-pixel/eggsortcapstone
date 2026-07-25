import os
import hashlib
import re
import secrets
from datetime import datetime, time, timedelta, timezone
from functools import wraps
from typing import Callable, Any
from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.flask_client import OAuth
from flask import (
    abort,
    Flask,
    Response,
    has_request_context,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    stream_with_context,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import String, cast, func, inspect, or_, text
from werkzeug.security import check_password_hash, generate_password_hash
from camera_session import CAMERA_SESSION, CameraSessionError
from egg_standards import SIZE_ORDER, classify_egg_size
from hardware_bridge import ARDUINO_BRIDGE
from detection_service import (
    DetectorUnavailableError,
    InvalidFrameError,
    detect_frame,
)


app = Flask(__name__)

app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(days=30)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["GOOGLE_CLIENT_ID"] = os.environ.get("GOOGLE_CLIENT_ID", "")
app.config["GOOGLE_CLIENT_SECRET"] = os.environ.get("GOOGLE_CLIENT_SECRET", "")


# SQLite database configuration
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///database.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False


db = SQLAlchemy(app)
oauth = OAuth(app)
oauth.register(
    name="google",
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_id=app.config["GOOGLE_CLIENT_ID"],
    client_secret=app.config["GOOGLE_CLIENT_SECRET"],
    client_kwargs={"scope": "openid email profile"},
)


INITIAL_ADMIN_EMAIL = "capstonecutie1@gmail.com"
VALID_ROLES = {"admin", "staff"}
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{3,30}$")
INVITE_LIFETIME = timedelta(hours=24)


def user_display_label(user: "User") -> str:
    if (
        user.display_name
        and user.display_name.casefold() != (user.email or "").casefold()
    ):
        return user.display_name
    if user.email:
        return user.email.split("@", 1)[0]
    return user.username


def invite_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def invitation_is_valid(user: "User") -> bool:
    expires_at = user.invite_expires_at
    if not user.invite_token_hash or expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at > datetime.now(timezone.utc)


def create_staff_invitation(user: "User") -> str:
    token = secrets.token_urlsafe(32)
    user.invite_token_hash = invite_token_hash(token)
    user.invite_expires_at = datetime.now(timezone.utc) + INVITE_LIFETIME
    return token


def sign_in_user(user: "User", remember: bool = False) -> None:
    session.clear()
    session["user_id"] = user.id
    session["username"] = user_display_label(user)
    session["email"] = user.email or ""
    session["role"] = user.role
    session.permanent = remember



# User Model
class User(db.Model):

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    username = db.Column(
        db.String(50),
        unique=True,
        nullable=False
    )

    password = db.Column(
        db.String(200),
        nullable=False
    )

    email = db.Column(
        db.String(254),
        unique=True,
        nullable=True,
    )

    google_sub = db.Column(
        db.String(255),
        unique=True,
        nullable=True,
    )

    display_name = db.Column(
        db.String(120),
        nullable=True,
    )

    role = db.Column(
        db.String(20),
        nullable=False,
        default="staff",
    )

    is_active = db.Column(
        db.Boolean,
        nullable=False,
        default=True,
    )

    password_set = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
    )

    invite_token_hash = db.Column(
        db.String(64),
        nullable=True,
    )

    invite_expires_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
    )


class EggRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    weight_grams = db.Column(db.Integer, nullable=False)
    size = db.Column(db.String(30), nullable=False)
    quality = db.Column(db.String(30), nullable=False)
    confidence = db.Column(db.Float, nullable=False, default=0.0)
    session_ref = db.Column(db.String(40), nullable=False)
    sorted_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict[str, Any]:
        sorted_at = self.sorted_at
        if sorted_at.tzinfo is None:
            sorted_at = sorted_at.replace(tzinfo=timezone.utc)
        return {
            "id": self.id,
            "egg_id": f"EGG-{self.id:06d}",
            "weight_grams": self.weight_grams,
            "size": self.size,
            "quality": self.quality,
            "confidence": round(self.confidence, 4),
            "session_ref": self.session_ref,
            "sorted_at": sorted_at.isoformat(),
        }


class TrayAlert(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tray_number = db.Column(db.Integer, unique=True, nullable=False)
    egg_count = db.Column(db.Integer, nullable=False)
    session_ref = db.Column(db.String(40), nullable=False)
    is_read = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict[str, Any]:
        created_at = self.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        return {
            "id": self.id,
            "tray_number": self.tray_number,
            "egg_count": self.egg_count,
            "session_ref": self.session_ref,
            "is_read": self.is_read,
            "created_at": created_at.isoformat(),
            "title": f"Tray {self.tray_number} completed",
            "message": (
                f"Tray {self.tray_number} reached 30 sorted eggs "
                f"({self.egg_count} total eggs)."
            ),
        }


class Sale(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    buyer_name = db.Column(db.String(120), nullable=False)
    size = db.Column(db.String(30), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    total_amount = db.Column(db.Float, nullable=False)
    payment_method = db.Column(db.String(30), nullable=False)
    status = db.Column(db.String(30), nullable=False, default="Completed")
    sold_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict[str, Any]:
        sold_at = self.sold_at
        if sold_at.tzinfo is None:
            sold_at = sold_at.replace(tzinfo=timezone.utc)
        return {
            "id": self.id,
            "invoice_id": f"INV-{self.id:06d}",
            "buyer_name": self.buyer_name,
            "size": self.size,
            "quantity": self.quantity,
            "total_amount": round(self.total_amount, 2),
            "payment_method": self.payment_method,
            "status": self.status,
            "sold_at": sold_at.isoformat(),
        }


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_type = db.Column(db.String(40), nullable=False)
    actor = db.Column(db.String(80), nullable=False, default="System")
    description = db.Column(db.String(300), nullable=False)
    event_key = db.Column(db.String(100), unique=True, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict[str, Any]:
        created_at = self.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        return {
            "id": self.id,
            "event_type": self.event_type,
            "actor": self.actor,
            "description": self.description,
            "created_at": created_at.isoformat(),
        }


# Create and safely extend the database without deleting existing records.
with app.app_context():
    db.create_all()
    user_columns = {
        column["name"]
        for column in inspect(db.engine).get_columns("user")
    }
    migration_statements = {
        "email": "ALTER TABLE user ADD COLUMN email VARCHAR(254)",
        "google_sub": "ALTER TABLE user ADD COLUMN google_sub VARCHAR(255)",
        "display_name": "ALTER TABLE user ADD COLUMN display_name VARCHAR(120)",
        "role": (
            "ALTER TABLE user ADD COLUMN role VARCHAR(20) "
            "NOT NULL DEFAULT 'staff'"
        ),
        "is_active": (
            "ALTER TABLE user ADD COLUMN is_active BOOLEAN "
            "NOT NULL DEFAULT 1"
        ),
        "password_set": (
            "ALTER TABLE user ADD COLUMN password_set BOOLEAN "
            "NOT NULL DEFAULT 0"
        ),
        "invite_token_hash": (
            "ALTER TABLE user ADD COLUMN invite_token_hash VARCHAR(64)"
        ),
        "invite_expires_at": (
            "ALTER TABLE user ADD COLUMN invite_expires_at DATETIME"
        ),
    }
    for column_name, statement in migration_statements.items():
        if column_name not in user_columns:
            db.session.execute(text(statement))
    db.session.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_user_email "
            "ON user (email)"
        )
    )
    db.session.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_user_google_sub "
            "ON user (google_sub)"
        )
    )
    db.session.commit()

    initial_admin = User.query.filter(
        func.lower(User.email) == INITIAL_ADMIN_EMAIL
    ).first()
    if initial_admin is None:
        initial_admin = User.query.filter(
            func.lower(User.username) == "admin"
        ).first()
    if initial_admin is None:
        initial_admin = User.query.filter(
            func.lower(User.username) == INITIAL_ADMIN_EMAIL
        ).first()
    if initial_admin is None and User.query.count() == 1:
        initial_admin = User.query.first()
    if initial_admin is None:
        initial_admin = User(
            username=INITIAL_ADMIN_EMAIL,
            password=generate_password_hash(secrets.token_urlsafe(32)),
            email=INITIAL_ADMIN_EMAIL,
            role="admin",
            is_active=True,
        )
        db.session.add(initial_admin)
    else:
        initial_admin.email = INITIAL_ADMIN_EMAIL
        initial_admin.role = "admin"
        initial_admin.is_active = True
    db.session.commit()


QUALITY_NAMES = {
    "demage": "Damaged",
    "damage": "Damaged",
    "damaged": "Damaged",
    "dirty": "Dirty",
    "good": "Good",
}

SALE_SIZES = ["Small", "Medium", "Large", "Extra Large", "Jumbo"]


def parse_date_boundary(value: str | None, end: bool = False) -> datetime | None:
    if not value:
        return None
    parsed_date = datetime.strptime(value, "%Y-%m-%d").date()
    boundary = time.max if end else time.min
    return datetime.combine(parsed_date, boundary, tzinfo=timezone.utc)


def sellable_stock_counts() -> dict[str, int]:
    available_rows = (
        db.session.query(EggRecord.size, func.count(EggRecord.id))
        .filter(EggRecord.quality == "Good")
        .group_by(EggRecord.size)
        .all()
    )
    sold_rows = (
        db.session.query(Sale.size, func.coalesce(func.sum(Sale.quantity), 0))
        .filter(Sale.status != "Cancelled")
        .group_by(Sale.size)
        .all()
    )
    available = {size: 0 for size in SALE_SIZES}
    available.update({size: count for size, count in available_rows})
    sold = {size: count for size, count in sold_rows}
    return {
        size: max(0, int(available.get(size, 0)) - int(sold.get(size, 0)))
        for size in SALE_SIZES
    }


def create_tray_alert_if_needed(
    total_sorted: int,
    session_ref: str,
) -> TrayAlert | None:
    if total_sorted <= 0 or total_sorted % 30 != 0:
        return None
    tray_number = total_sorted // 30
    existing = TrayAlert.query.filter_by(tray_number=tray_number).first()
    if existing is not None:
        return None
    alert = TrayAlert(
        tray_number=tray_number,
        egg_count=total_sorted,
        session_ref=session_ref,
    )
    db.session.add(alert)
    return alert


def write_audit_log(
    event_type: str,
    description: str,
    *,
    actor: str | None = None,
    event_key: str | None = None,
    created_at: datetime | None = None,
    commit: bool = True,
) -> AuditLog | None:
    if event_key and AuditLog.query.filter_by(event_key=event_key).first():
        return None
    resolved_actor = actor
    if resolved_actor is None and has_request_context():
        resolved_actor = session.get("username")
    log = AuditLog(
        event_type=event_type,
        actor=resolved_actor or "System",
        description=description,
        event_key=event_key,
        created_at=created_at or datetime.now(timezone.utc),
    )
    db.session.add(log)
    if commit:
        db.session.commit()
    return log


def backfill_completed_tray_alerts() -> None:
    total_sorted = EggRecord.query.count()
    created = False
    for tray_number in range(1, (total_sorted // 30) + 1):
        boundary_record = (
            EggRecord.query
            .order_by(EggRecord.id.asc())
            .offset((tray_number * 30) - 1)
            .first()
        )
        session_ref = (
            boundary_record.session_ref
            if boundary_record is not None
            else "HISTORICAL"
        )
        alert = create_tray_alert_if_needed(tray_number * 30, session_ref)
        created = created or alert is not None
    if created:
        db.session.commit()


def backfill_sorting_audit_logs() -> None:
    created = False
    for record in EggRecord.query.order_by(EggRecord.id.asc()).all():
        log = write_audit_log(
            "egg_sorted",
            (
                f"{record.to_dict()['egg_id']} sorted at {record.weight_grams} g "
                f"as {record.size}, quality {record.quality}."
            ),
            event_key=f"egg-sorted:{record.id}",
            created_at=record.sorted_at,
            commit=False,
        )
        created = created or log is not None
    if created:
        db.session.commit()


with app.app_context():
    backfill_completed_tray_alerts()
    backfill_sorting_audit_logs()


def persist_arduino_event(event: dict[str, Any]) -> None:
    if event.get("type") != "egg_complete":
        return
    weight = event.get("weight_grams")
    if weight is None:
        return

    quality_result = CAMERA_SESSION.quality_snapshot(window_seconds=4.0)
    raw_quality = str(quality_result["label"]).lower()
    quality = QUALITY_NAMES.get(raw_quality, raw_quality.title() or "Unknown")
    session_ref = (
        CAMERA_SESSION.status().get("session_ref")
        or "NO-ACTIVE-SESSION"
    )

    size = classify_egg_size(int(weight))
    with app.app_context():
        record = EggRecord(
            weight_grams=int(weight),
            size=size,
            quality=quality,
            confidence=float(quality_result["confidence"]),
            session_ref=session_ref,
        )
        db.session.add(record)
        db.session.flush()
        total_sorted = EggRecord.query.count()
        create_tray_alert_if_needed(total_sorted, session_ref)
        write_audit_log(
            "egg_sorted",
            (
                f"EGG-{record.id:06d} sorted at {record.weight_grams} g "
                f"as {record.size}, quality {record.quality}."
            ),
            event_key=f"egg-sorted:{record.id}",
            commit=False,
        )
        db.session.commit()
    try:
        ARDUINO_BRIDGE.sort_egg(size)
    except RuntimeError:
        # The completed record remains valid if the servo disconnects.
        pass


ARDUINO_BRIDGE.set_event_handler(persist_arduino_event)



# Home
@app.route("/")
def home() -> Any:
    return redirect(url_for("login"))



# Public self-registration is intentionally disabled. Admins allowlist staff
# Google accounts from User Management.
@app.route("/register", methods=["GET", "POST"])
def register() -> Any:
    session["login_error"] = (
        "Public registration is disabled. Staff accounts require an "
        "invitation from the administrator."
    )
    return redirect(url_for("login"))


# Staff use invite-only passwords; administrators use Google.
@app.route("/login", methods=["GET", "POST"])
def login() -> Any:
    if "user_id" in session:
        return redirect(url_for("dashboard"))

    error = session.pop("login_error", None)
    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter(
            or_(
                func.lower(User.username) == identifier,
                func.lower(User.email) == identifier,
            )
        ).first()
        if (
            user is None
            or not user.is_active
            or not user.password_set
            or not check_password_hash(user.password, password)
        ):
            error = "Invalid username/email or password."
            write_audit_log(
                "login_failed",
                "Failed staff sign-in attempt.",
                actor=identifier or "Unknown",
            )
        else:
            sign_in_user(
                user,
                remember=request.form.get("remember-me") == "on",
            )
            write_audit_log(
                "login",
                f"{user.role.title()} signed in successfully with a password.",
                actor=user.email or user.username,
            )
            return redirect(url_for("dashboard"))

    return render_template(
        "login.html",
        error=error,
        notice=session.pop("login_notice", None),
        google_ready=bool(
            app.config["GOOGLE_CLIENT_ID"]
            and app.config["GOOGLE_CLIENT_SECRET"]
        ),
    )


@app.route("/accept-invite/<token>", methods=["GET", "POST"])
def accept_invite(token: str) -> Any:
    user = User.query.filter_by(
        invite_token_hash=invite_token_hash(token),
        role="staff",
        is_active=True,
    ).first()
    valid_invite = user is not None and invitation_is_valid(user)
    error = None

    if request.method == "POST":
        if not valid_invite or user is None:
            error = "This invitation is invalid, expired, or already used."
        else:
            password = request.form.get("password", "")
            password_confirmation = request.form.get(
                "password_confirmation",
                "",
            )
            if len(password) < 8:
                error = "Password must contain at least 8 characters."
            elif len(password) > 128:
                error = "Password must not exceed 128 characters."
            elif password != password_confirmation:
                error = "Passwords do not match."
            else:
                user.password = generate_password_hash(password)
                user.password_set = True
                user.invite_token_hash = None
                user.invite_expires_at = None
                write_audit_log(
                    "staff_activated",
                    f"Staff account '{user.username}' accepted its invitation.",
                    actor=user.email or user.username,
                    commit=False,
                )
                db.session.commit()
                session["login_notice"] = (
                    "Account activated. You can now sign in as staff."
                )
                return redirect(url_for("login"))

    return render_template(
        "accept_invite.html",
        error=error,
        valid_invite=valid_invite,
        invited_user=user,
    )


@app.get("/auth/google")
def google_login() -> Any:
    if not (
        app.config["GOOGLE_CLIENT_ID"]
        and app.config["GOOGLE_CLIENT_SECRET"]
    ):
        session["login_error"] = (
            "Google sign-in is not configured yet. Add the Google OAuth "
            "client ID and secret, then restart EggSort+."
        )
        return redirect(url_for("login"))
    redirect_uri = url_for("google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@app.get("/auth/google/callback")
def google_callback() -> Any:
    try:
        token = oauth.google.authorize_access_token()
        userinfo = token.get("userinfo")
        if not userinfo:
            userinfo = oauth.google.userinfo(token=token).json()
    except OAuthError:
        session["login_error"] = (
            "Google sign-in was cancelled or could not be verified."
        )
        return redirect(url_for("login"))

    email = str(userinfo.get("email", "")).strip().lower()
    google_sub = str(userinfo.get("sub", "")).strip()
    if not email or not google_sub or userinfo.get("email_verified") is not True:
        session["login_error"] = (
            "Google did not provide a verified email address."
        )
        return redirect(url_for("login"))

    user = User.query.filter_by(google_sub=google_sub).first()
    if user is None:
        user = User.query.filter(func.lower(User.email) == email).first()

    if user is None:
        write_audit_log(
            "login_denied",
            f"Google account '{email}' is not authorized.",
            actor=email,
        )
        session["login_error"] = (
            "This Google account is not authorized. Ask the administrator "
            "to add your email in User Management."
        )
        return redirect(url_for("login"))

    if user.role != "admin":
        session["login_error"] = (
            "Staff members sign in with their username/email and password."
        )
        return redirect(url_for("login"))

    if not user.is_active:
        write_audit_log(
            "login_denied",
            f"Disabled Google account '{email}' attempted to sign in.",
            actor=email,
        )
        session["login_error"] = "This account has been disabled."
        return redirect(url_for("login"))

    user.google_sub = google_sub
    user.email = email
    user.display_name = (
        str(userinfo.get("name", "")).strip()[:120]
        or email.split("@", 1)[0]
    )
    db.session.commit()

    sign_in_user(user, remember=True)
    session["google_verified_for_password_setup"] = True
    write_audit_log(
        "login",
        "Operator signed in successfully with Google.",
        actor=user.email or user.username,
    )
    if not user.password_set:
        return redirect(url_for("setup_admin_password"))
    session.pop("google_verified_for_password_setup", None)
    return redirect(url_for("dashboard"))



def login_required(f: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(f)
    def decorated_function(*args: Any, **kwargs: Any) -> Any:
        if "user_id" not in session:
            return redirect(url_for("login"))
        user = db.session.get(User, session["user_id"])
        if user is None or not user.is_active:
            session.clear()
            return redirect(url_for("login"))
        session["username"] = user_display_label(user)
        session["email"] = user.email or ""
        session["role"] = user.role
        if (
            user.role == "admin"
            and not user.password_set
            and request.endpoint != "setup_admin_password"
        ):
            if session.get("google_verified_for_password_setup"):
                return redirect(url_for("setup_admin_password"))
            session.clear()
            session["login_error"] = (
                "Continue with Google to finish the one-time admin setup."
            )
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(f)
    @login_required
    def decorated_function(*args: Any, **kwargs: Any) -> Any:
        if session.get("role") != "admin":
            if request.path.startswith("/api/"):
                return jsonify(error="Administrator access is required."), 403
            abort(403)
        return f(*args, **kwargs)
    return decorated_function


@app.context_processor
def inject_current_user() -> dict[str, str | None]:
    return {
        "current_user_role": session.get("role"),
        "current_user_email": session.get("email"),
    }


@app.route("/setup-admin-password", methods=["GET", "POST"])
@admin_required
def setup_admin_password() -> Any:
    user = db.session.get(User, session["user_id"])
    if user is None:
        session.clear()
        return redirect(url_for("login"))
    if user.password_set:
        session.pop("google_verified_for_password_setup", None)
        return redirect(url_for("dashboard"))
    if not session.get("google_verified_for_password_setup"):
        session.clear()
        session["login_error"] = (
            "Sign in with Google first to create the administrator password."
        )
        return redirect(url_for("login"))

    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        password_confirmation = request.form.get(
            "password_confirmation",
            "",
        )
        if len(password) < 8:
            error = "Password must contain at least 8 characters."
        elif len(password) > 128:
            error = "Password must not exceed 128 characters."
        elif password != password_confirmation:
            error = "Passwords do not match."
        else:
            user.password = generate_password_hash(password)
            user.password_set = True
            session.pop("google_verified_for_password_setup", None)
            write_audit_log(
                "admin_password_created",
                "Administrator created a password after Google verification.",
                actor=user.email or user.username,
                commit=False,
            )
            db.session.commit()
            return redirect(url_for("dashboard"))

    return render_template(
        "setup_admin_password.html",
        error=error,
        email=user.email,
    )


# Dashboard
@app.route("/dashboard")
@login_required
def dashboard() -> Any:
    return render_template(
        "dashboard.html",
        username=session["username"]
    )



# Sorting Sessions
@app.route("/sorting-sessions")
@login_required
def sorting_sessions() -> Any:
    return render_template(
        "sorting_session.html",
        username=session["username"]
    )


@app.post("/api/detect")
@login_required
def detect() -> Any:
    frame = request.files.get("frame")
    if frame is None:
        return jsonify(error="A camera frame is required."), 400

    if frame.mimetype not in {"image/jpeg", "image/png"}:
        return jsonify(error="Only JPEG and PNG camera frames are supported."), 415

    try:
        return jsonify(detect_frame(frame.read()))
    except InvalidFrameError as exc:
        return jsonify(error=str(exc)), 400
    except DetectorUnavailableError as exc:
        return jsonify(error=str(exc)), 503


@app.post("/api/camera/start")
@login_required
def start_camera() -> Any:
    try:
        camera_state = CAMERA_SESSION.start()
        hardware_state = ARDUINO_BRIDGE.start()
        write_audit_log(
            "camera_started",
            f"Sorting camera session {camera_state.get('session_ref')} started.",
        )
        return jsonify(camera=camera_state, hardware=hardware_state)
    except CameraSessionError as exc:
        return jsonify(error=str(exc)), 503


@app.post("/api/camera/stop")
@login_required
def stop_camera() -> Any:
    hardware_state = ARDUINO_BRIDGE.stop()
    camera_state = CAMERA_SESSION.stop()
    write_audit_log(
        "camera_stopped",
        "Sorting camera and hardware session stopped manually.",
    )
    return jsonify(camera=camera_state, hardware=hardware_state)


@app.get("/api/camera/status")
@login_required
def camera_status() -> Any:
    return jsonify(CAMERA_SESSION.status())


@app.get("/api/camera/feed")
@login_required
def camera_feed() -> Any:
    if not CAMERA_SESSION.status()["running"]:
        return jsonify(error="No camera session is running."), 409

    def generate_frames() -> Any:
        sequence = 0
        while True:
            next_sequence, jpeg, running = CAMERA_SESSION.wait_for_frame(
                sequence
            )
            if jpeg is not None and next_sequence != sequence:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Cache-Control: no-store\r\n\r\n"
                    + jpeg
                    + b"\r\n"
                )
            sequence = next_sequence
            if not running:
                break

    return Response(
        stream_with_context(generate_frames()),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.get("/api/hardware/status")
@login_required
def hardware_status() -> Any:
    return jsonify(ARDUINO_BRIDGE.status())


@app.post("/api/hardware/stopper/start")
@login_required
def trigger_stopper() -> Any:
    try:
        ARDUINO_BRIDGE.trigger_stopper()
        write_audit_log(
            "stopper_advanced",
            "Operator manually advanced the egg stopper.",
        )
        return jsonify(ok=True, message="Stopper command sent.")
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 503


@app.get("/api/egg-records")
@login_required
def egg_records_data() -> Any:
    after_id = request.args.get("after_id", default=0, type=int)
    limit = min(request.args.get("limit", default=100, type=int), 500)
    query = EggRecord.query.filter(EggRecord.id > after_id)
    search = request.args.get("q", "").strip()
    size = request.args.get("size", "").strip()
    quality = request.args.get("quality", "").strip()
    try:
        start_date = parse_date_boundary(request.args.get("start_date"))
        end_date = parse_date_boundary(request.args.get("end_date"), end=True)
    except ValueError:
        return jsonify(error="Dates must use YYYY-MM-DD format."), 400

    if search:
        numeric = "".join(character for character in search if character.isdigit())
        conditions = [
            EggRecord.session_ref.ilike(f"%{search}%"),
            cast(EggRecord.weight_grams, String).ilike(f"%{search}%"),
        ]
        if numeric:
            conditions.append(EggRecord.id == int(numeric))
        query = query.filter(or_(*conditions))
    if size and size != "All Sizes":
        query = query.filter(EggRecord.size == size)
    if quality and quality != "All Qualities":
        query = query.filter(EggRecord.quality == quality)
    if start_date is not None:
        query = query.filter(EggRecord.sorted_at >= start_date)
    if end_date is not None:
        query = query.filter(EggRecord.sorted_at <= end_date)

    records = query.order_by(EggRecord.id.desc()).limit(limit).all()
    return jsonify(
        records=[record.to_dict() for record in records],
        latest_id=max((record.id for record in records), default=after_id),
    )


@app.get("/api/dashboard/stats")
@login_required
def dashboard_stats() -> Any:
    total_sorted = EggRecord.query.count()
    size_rows = (
        db.session.query(EggRecord.size, func.count(EggRecord.id))
        .group_by(EggRecord.size)
        .all()
    )
    quality_rows = (
        db.session.query(EggRecord.quality, func.count(EggRecord.id))
        .group_by(EggRecord.quality)
        .all()
    )
    size_counts = {size: 0 for size in SIZE_ORDER}
    size_counts.update({size: count for size, count in size_rows})
    quality_counts = {quality: count for quality, count in quality_rows}
    good_count = quality_counts.get("Good", 0)
    camera_state = CAMERA_SESSION.status()
    latest_record = EggRecord.query.order_by(EggRecord.id.desc()).first()
    today = datetime.now(timezone.utc).date()
    trend_days = [today - timedelta(days=offset) for offset in range(6, -1, -1)]
    trend_counts = {day.isoformat(): 0 for day in trend_days}
    trend_start = datetime.combine(trend_days[0], time.min, tzinfo=timezone.utc)
    recent_records = EggRecord.query.filter(EggRecord.sorted_at >= trend_start).all()
    for record in recent_records:
        sorted_at = record.sorted_at
        if sorted_at.tzinfo is None:
            sorted_at = sorted_at.replace(tzinfo=timezone.utc)
        day_key = sorted_at.date().isoformat()
        if day_key in trend_counts:
            trend_counts[day_key] += 1
    recent_audits = (
        AuditLog.query
        .order_by(AuditLog.id.desc())
        .limit(12)
        .all()
    )

    return jsonify(
        total_sorted=total_sorted,
        trays_completed=total_sorted // 30,
        quality_rate=round(
            (good_count / total_sorted * 100) if total_sorted else 0,
            1,
        ),
        camera_eggs_visible=camera_state.get("total", 0),
        camera_running=camera_state.get("running", False),
        size_counts=size_counts,
        quality_counts=quality_counts,
        latest_record=latest_record.to_dict() if latest_record else None,
        hardware=ARDUINO_BRIDGE.status(),
        unread_alerts=TrayAlert.query.filter_by(is_read=False).count(),
        daily_trend=[
            {
                "date": day.isoformat(),
                "label": day.strftime("%a"),
                "count": trend_counts[day.isoformat()],
            }
            for day in trend_days
        ],
        audit_logs=[log.to_dict() for log in recent_audits],
        total_revenue=round(
            float(
                db.session.query(func.coalesce(func.sum(Sale.total_amount), 0))
                .filter(Sale.status == "Completed")
                .scalar()
            ),
            2,
        ),
    )


@app.get("/api/alerts")
@login_required
def alerts_data() -> Any:
    unread_only = request.args.get("filter") == "unread"
    query = TrayAlert.query
    if unread_only:
        query = query.filter_by(is_read=False)
    alerts_list = query.order_by(TrayAlert.id.desc()).limit(200).all()
    return jsonify(
        alerts=[alert.to_dict() for alert in alerts_list],
        unread_count=TrayAlert.query.filter_by(is_read=False).count(),
    )


@app.post("/api/alerts/read-all")
@login_required
def mark_all_alerts_read() -> Any:
    TrayAlert.query.filter_by(is_read=False).update(
        {"is_read": True},
        synchronize_session=False,
    )
    db.session.commit()
    return jsonify(ok=True, unread_count=0)


@app.get("/api/sales")
@login_required
def sales_data() -> Any:
    search = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    query = Sale.query
    if search:
        numeric = "".join(character for character in search if character.isdigit())
        conditions = [
            Sale.buyer_name.ilike(f"%{search}%"),
            Sale.size.ilike(f"%{search}%"),
        ]
        if numeric:
            conditions.append(Sale.id == int(numeric))
        query = query.filter(or_(*conditions))
    if status and status != "All Statuses":
        query = query.filter(Sale.status == status)
    sales_list = query.order_by(Sale.id.desc()).limit(500).all()
    return jsonify(
        sales=[sale.to_dict() for sale in sales_list],
        stocks=sellable_stock_counts(),
        total_deals=Sale.query.count(),
        total_revenue=round(
            float(
                db.session.query(func.coalesce(func.sum(Sale.total_amount), 0))
                .filter(Sale.status == "Completed")
                .scalar()
            ),
            2,
        ),
    )


@app.post("/api/sales")
@login_required
def create_sale() -> Any:
    payload = request.get_json(silent=True) or {}
    buyer_name = str(payload.get("buyer_name", "")).strip()
    size = str(payload.get("size", "")).strip()
    payment_method = str(payload.get("payment_method", "")).strip()
    try:
        quantity = int(payload.get("quantity", 0))
        total_amount = round(float(payload.get("total_amount", 0)), 2)
    except (TypeError, ValueError):
        return jsonify(error="Quantity and total amount must be numbers."), 400

    if not buyer_name:
        return jsonify(error="Buyer name is required."), 400
    if size not in SALE_SIZES:
        return jsonify(error="Select a valid egg size."), 400
    if quantity <= 0 or total_amount < 0:
        return jsonify(error="Quantity must be positive and amount cannot be negative."), 400
    if payment_method not in {"Cash", "GCash", "Bank Transfer"}:
        return jsonify(error="Select a valid payment method."), 400
    available = sellable_stock_counts().get(size, 0)
    if quantity > available:
        return jsonify(
            error=f"Only {available} sellable {size} eggs are available."
        ), 409

    sale = Sale(
        buyer_name=buyer_name,
        size=size,
        quantity=quantity,
        total_amount=total_amount,
        payment_method=payment_method,
        status="Completed",
    )
    db.session.add(sale)
    db.session.flush()
    write_audit_log(
        "sale_created",
        (
            f"{sale.to_dict()['invoice_id']} recorded for {quantity} "
            f"{size} eggs sold to {buyer_name}."
        ),
        event_key=f"sale-created:{sale.id}",
        commit=False,
    )
    db.session.commit()
    return jsonify(sale=sale.to_dict()), 201


@app.get("/api/reports")
@login_required
def reports_data() -> Any:
    sampling = request.args.get("sampling", "daily").lower()
    if sampling not in {"daily", "weekly", "monthly"}:
        return jsonify(error="Invalid sampling period."), 400
    try:
        start_date = parse_date_boundary(request.args.get("start_date"))
        end_date = parse_date_boundary(request.args.get("end_date"), end=True)
    except ValueError:
        return jsonify(error="Dates must use YYYY-MM-DD format."), 400
    if start_date and end_date and start_date > end_date:
        return jsonify(error="Start date cannot be after end date."), 400

    query = EggRecord.query
    if start_date:
        query = query.filter(EggRecord.sorted_at >= start_date)
    if end_date:
        query = query.filter(EggRecord.sorted_at <= end_date)
    records = query.order_by(EggRecord.sorted_at.asc()).all()

    groups: dict[str, dict[str, Any]] = {}
    for record in records:
        sorted_at = record.sorted_at
        if sorted_at.tzinfo is None:
            sorted_at = sorted_at.replace(tzinfo=timezone.utc)
        if sampling == "daily":
            key = sorted_at.strftime("%Y-%m-%d")
        elif sampling == "weekly":
            iso_year, iso_week, _ = sorted_at.isocalendar()
            key = f"{iso_year}-W{iso_week:02d}"
        else:
            key = sorted_at.strftime("%Y-%m")
        row = groups.setdefault(
            key,
            {"period": key, "total": 0, "good": 0, "damaged": 0, "dirty": 0},
        )
        row["total"] += 1
        quality_key = record.quality.lower()
        if quality_key in row:
            row[quality_key] += 1

    total = len(records)
    good = sum(1 for record in records if record.quality == "Good")
    damaged = sum(1 for record in records if record.quality == "Damaged")
    return jsonify(
        rows=list(groups.values()),
        summary={
            "total": total,
            "good": good,
            "damaged": damaged,
            "quality_rate": round((good / total * 100) if total else 0, 1),
            "revenue": round(
                float(
                    db.session.query(func.coalesce(func.sum(Sale.total_amount), 0))
                    .filter(Sale.status == "Completed")
                    .scalar()
                ),
                2,
            ),
        },
    )


@app.get("/api/users")
@admin_required
def users_data() -> Any:
    users_list = User.query.order_by(User.role.asc(), User.email.asc()).all()
    return jsonify(
        users=[
            {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "display_name": user.display_name,
                "role": user.role,
                "is_active": user.is_active,
                "password_set": user.password_set,
                "invite_pending": invitation_is_valid(user),
                "google_connected": bool(user.google_sub),
                "is_current": user.id == session["user_id"],
            }
            for user in users_list
        ]
    )


@app.post("/api/users")
@admin_required
def create_user() -> Any:
    payload = request.get_json(silent=True) or {}
    email = str(payload.get("email", "")).strip().lower()
    username = str(payload.get("username", "")).strip().lower()
    display_name = str(payload.get("display_name", "")).strip()
    if not EMAIL_PATTERN.fullmatch(email):
        return jsonify(error="Enter a valid email address."), 400
    if not USERNAME_PATTERN.fullmatch(username):
        return jsonify(
            error=(
                "Username must be 3-30 characters using letters, numbers, "
                "dots, underscores, or hyphens."
            )
        ), 400
    if not 2 <= len(display_name) <= 120:
        return jsonify(error="Name must contain 2-120 characters."), 400
    existing_user = User.query.filter(
        or_(
            func.lower(User.email) == email,
            func.lower(User.username) == username,
        )
    ).first()
    if existing_user:
        return jsonify(error="That email or username is already registered."), 409
    user = User(
        username=username,
        password=generate_password_hash(secrets.token_urlsafe(32)),
        email=email,
        display_name=display_name,
        role="staff",
        is_active=True,
        password_set=False,
    )
    token = create_staff_invitation(user)
    db.session.add(user)
    db.session.flush()
    write_audit_log(
        "staff_invited",
        f"Staff account '{username}' was invited.",
        event_key=f"user-created:{user.id}",
        commit=False,
    )
    db.session.commit()
    return jsonify(
        id=user.id,
        email=user.email,
        username=user.username,
        role=user.role,
        invite_url=url_for("accept_invite", token=token, _external=True),
        expires_in_hours=24,
    ), 201


@app.patch("/api/users/<int:user_id>")
@admin_required
def update_user(user_id: int) -> Any:
    user = db.get_or_404(User, user_id)
    if user.role == "admin":
        return jsonify(
            error="The Google administrator profile is managed by sign-in."
        ), 409
    payload = request.get_json(silent=True) or {}
    email = str(payload.get("email", "")).strip().lower()
    username = str(payload.get("username", "")).strip().lower()
    display_name = str(payload.get("display_name", "")).strip()
    if not EMAIL_PATTERN.fullmatch(email):
        return jsonify(error="Enter a valid email address."), 400
    if not USERNAME_PATTERN.fullmatch(username):
        return jsonify(
            error=(
                "Username must be 3-30 characters using letters, numbers, "
                "dots, underscores, or hyphens."
            )
        ), 400
    if not 2 <= len(display_name) <= 120:
        return jsonify(error="Name must contain 2-120 characters."), 400
    duplicate = User.query.filter(
        or_(
            func.lower(User.email) == email,
            func.lower(User.username) == username,
        ),
        User.id != user_id,
    ).first()
    if duplicate:
        return jsonify(error="That email or username is already registered."), 409
    user.email = email
    user.username = username
    user.display_name = display_name
    write_audit_log(
        "user_updated",
        f"Staff account '{username}' was updated.",
        commit=False,
    )
    db.session.commit()
    return jsonify(
        id=user.id,
        email=user.email,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
    )


@app.post("/api/users/<int:user_id>/invite")
@admin_required
def regenerate_user_invite(user_id: int) -> Any:
    user = db.get_or_404(User, user_id)
    if user.role != "staff":
        return jsonify(error="Only staff accounts use invitations."), 409
    token = create_staff_invitation(user)
    write_audit_log(
        "staff_reinvited",
        f"A new invitation was created for staff account '{user.username}'.",
        commit=False,
    )
    db.session.commit()
    return jsonify(
        invite_url=url_for("accept_invite", token=token, _external=True),
        expires_in_hours=24,
    )


@app.delete("/api/users/<int:user_id>")
@admin_required
def delete_user(user_id: int) -> Any:
    if user_id == session["user_id"]:
        return jsonify(error="You cannot delete the account currently signed in."), 409
    user = db.get_or_404(User, user_id)
    if user.role == "admin" and User.query.filter_by(role="admin").count() <= 1:
        return jsonify(error="At least one administrator must remain."), 409
    deleted_identity = user.email or user.username
    db.session.delete(user)
    write_audit_log(
        "user_deleted",
        f"Account '{deleted_identity}' was removed.",
        commit=False,
    )
    db.session.commit()
    return jsonify(ok=True)



# Egg Records
@app.route("/egg-records")
@login_required
def egg_records() -> Any:
    return render_template(
        "egg_records.html",
        username=session["username"]
    )



# Alerts
@app.route("/alerts")
@login_required
def alerts() -> Any:
    return render_template(
        "alerts.html",
        username=session["username"]
    )



# Sales
@app.route("/sales")
@login_required
def sales() -> Any:
    return render_template(
        "sales.html",
        username=session["username"]
    )



# Reports
@app.route("/reports")
@login_required
def reports() -> Any:
    return render_template(
        "reports.html",
        username=session["username"]
    )



# User Management
@app.route("/user-management")
@admin_required
def user_management() -> Any:
    return render_template(
        "user_management.html",
        username=session["username"]
    )



# Logout
@app.route("/logout")
def logout() -> Any:
    if "user_id" in session:
        write_audit_log(
            "logout",
            "Operator signed out.",
        )
    session.clear()

    return redirect(
        url_for("login")
    )



if __name__ == "__main__":
    app.run(
        debug=os.environ.get("FLASK_DEBUG", "0") == "1",
        threaded=True,
    )
