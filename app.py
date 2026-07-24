import os
from datetime import datetime, time, timedelta, timezone
from functools import wraps
from typing import Callable, Any
from flask import (
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
from sqlalchemy import String, cast, func, or_
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
app.permanent_session_lifetime = timedelta(days=30)


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
                session.permanent = request.form.get("remember-me") == "on"
                write_audit_log(
                    "login",
                    "Operator signed in successfully.",
                    actor=user.username,
                )

                return redirect(
                    url_for("dashboard")
                )

            else:
                error = "Invalid username or password"
                write_audit_log(
                    "login_failed",
                    f"Failed sign-in attempt for username '{username}'.",
                    actor=username or "Unknown",
                )

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
@login_required
def users_data() -> Any:
    users_list = User.query.order_by(User.username.asc()).all()
    return jsonify(
        users=[
            {
                "id": user.id,
                "username": user.username,
                "is_current": user.id == session["user_id"],
            }
            for user in users_list
        ]
    )


@app.post("/api/users")
@login_required
def create_user() -> Any:
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    if not username or not password:
        return jsonify(error="Username and password are required."), 400
    if len(password) < 6:
        return jsonify(error="Password must contain at least 6 characters."), 400
    if User.query.filter_by(username=username).first():
        return jsonify(error="That username is already registered."), 409
    user = User(username=username, password=generate_password_hash(password))
    db.session.add(user)
    db.session.flush()
    write_audit_log(
        "user_created",
        f"Operator account '{username}' was registered.",
        event_key=f"user-created:{user.id}",
        commit=False,
    )
    db.session.commit()
    return jsonify(id=user.id, username=user.username), 201


@app.patch("/api/users/<int:user_id>")
@login_required
def update_user(user_id: int) -> Any:
    user = db.get_or_404(User, user_id)
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    if not username:
        return jsonify(error="Username is required."), 400
    duplicate = User.query.filter(User.username == username, User.id != user_id).first()
    if duplicate:
        return jsonify(error="That username is already registered."), 409
    if password and len(password) < 6:
        return jsonify(error="Password must contain at least 6 characters."), 400
    user.username = username
    if password:
        user.password = generate_password_hash(password)
    if user.id == session["user_id"]:
        session["username"] = username
    write_audit_log(
        "user_updated",
        f"Operator account '{username}' was updated.",
        commit=False,
    )
    db.session.commit()
    return jsonify(id=user.id, username=user.username)


@app.delete("/api/users/<int:user_id>")
@login_required
def delete_user(user_id: int) -> Any:
    if user_id == session["user_id"]:
        return jsonify(error="You cannot delete the account currently signed in."), 409
    user = db.get_or_404(User, user_id)
    if User.query.count() <= 1:
        return jsonify(error="At least one operator account must remain."), 409
    deleted_username = user.username
    db.session.delete(user)
    write_audit_log(
        "user_deleted",
        f"Operator account '{deleted_username}' was deleted.",
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
@login_required
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
