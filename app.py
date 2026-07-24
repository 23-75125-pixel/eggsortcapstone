import os
from datetime import datetime, timezone
from functools import wraps
from typing import Callable, Any
from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    stream_with_context,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
from werkzeug.security import generate_password_hash, check_password_hash
from camera_session import CAMERA_SESSION, CameraSessionError
from egg_standards import SIZE_ORDER, classify_egg_size
from hardware_bridge import ARDUINO_BRIDGE
from detection_service import (
    DetectorUnavailableError,
    InvalidFrameError,
    detect_frame,
)


app = Flask(__name__)

app.secret_key = os.environ.get("SECRET_KEY", "change_this_secret_key")


# SQLite database configuration
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///database.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False


db = SQLAlchemy(app)



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


# Create database
with app.app_context():
    db.create_all()


QUALITY_NAMES = {
    "demage": "Damaged",
    "damage": "Damaged",
    "damaged": "Damaged",
    "dirty": "Dirty",
    "good": "Good",
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


with app.app_context():
    backfill_completed_tray_alerts()


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



# Register user
@app.route("/register", methods=["GET", "POST"])
def register() -> Any:

    error = None

    if request.method == "POST":

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            error = "Username and password are required"
        else:
            existing_user = User.query.filter_by(
                username=username
            ).first()

            if existing_user:
                error = "Username already exists"
            else:
                hashed_password = generate_password_hash(
                    password
                )

                user = User(
                    username=username,
                    password=hashed_password
                )

                db.session.add(user)
                db.session.commit()

                return redirect(url_for("login"))

    return render_template(
        "register.html",
        error=error
    )



# Login
@app.route("/login", methods=["GET", "POST"])
def login() -> Any:

    error = None

    if request.method == "POST":

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            error = "Username and password are required"
        else:
            user = User.query.filter_by(
                username=username
            ).first()

            if user and check_password_hash(
                user.password,
                password
            ):

                session["user_id"] = user.id
                session["username"] = user.username

                return redirect(
                    url_for("dashboard")
                )

            else:
                error = "Invalid username or password"

    return render_template(
        "login.html",
        error=error
    )



def login_required(f: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(f)
    def decorated_function(*args: Any, **kwargs: Any) -> Any:
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function


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
        return jsonify(camera=camera_state, hardware=hardware_state)
    except CameraSessionError as exc:
        return jsonify(error=str(exc)), 503


@app.post("/api/camera/stop")
@login_required
def stop_camera() -> Any:
    hardware_state = ARDUINO_BRIDGE.stop()
    camera_state = CAMERA_SESSION.stop()
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
        return jsonify(ok=True, message="Stopper command sent.")
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 503


@app.get("/api/egg-records")
@login_required
def egg_records_data() -> Any:
    after_id = request.args.get("after_id", default=0, type=int)
    limit = min(request.args.get("limit", default=100, type=int), 500)
    records = (
        EggRecord.query
        .filter(EggRecord.id > after_id)
        .order_by(EggRecord.id.desc())
        .limit(limit)
        .all()
    )
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
@login_required
def user_management() -> Any:
    return render_template(
        "user_management.html",
        username=session["username"]
    )



# Logout
@app.route("/logout")
def logout() -> Any:

    session.clear()

    return redirect(
        url_for("login")
    )



if __name__ == "__main__":
    app.run(
        debug=os.environ.get("FLASK_DEBUG", "0") == "1",
        threaded=True,
    )
