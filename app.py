from flask import Flask, render_template, request, redirect, url_for, session
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash


app = Flask(__name__)

app.secret_key = "change_this_secret_key"


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


# Create database
with app.app_context():
    db.create_all()



# Home
@app.route("/")
def home():
    return redirect(url_for("login"))



# Register user
@app.route("/register", methods=["GET", "POST"])
def register():

    error = None

    if request.method == "POST":

        username = request.form["username"]
        password = request.form["password"]


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
def login():

    error = None


    if request.method == "POST":

        username = request.form["username"]
        password = request.form["password"]


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



# Dashboard
@app.route("/dashboard")
def dashboard():

    if "user_id" not in session:
        return redirect(
            url_for("login")
        )


    return render_template(
        "dashboard.html",
        username=session["username"]
    )



# Sorting Sessions
@app.route("/sorting-sessions")
def sorting_sessions():

    if "user_id" not in session:
        return redirect(
            url_for("login")
        )


    return render_template(
        "sorting_session.html",
        username=session["username"]
    )



# Egg Records
@app.route("/egg-records")
def egg_records():

    if "user_id" not in session:
        return redirect(
            url_for("login")
        )


    return render_template(
        "egg_records.html",
        username=session["username"]
    )



# Alerts
@app.route("/alerts")
def alerts():

    if "user_id" not in session:
        return redirect(
            url_for("login")
        )


    return render_template(
        "alerts.html",
        username=session["username"]
    )



# Sales
@app.route("/sales")
def sales():

    if "user_id" not in session:
        return redirect(
            url_for("login")
        )


    return render_template(
        "sales.html",
        username=session["username"]
    )



# Reports
@app.route("/reports")
def reports():

    if "user_id" not in session:
        return redirect(
            url_for("login")
        )


    return render_template(
        "reports.html",
        username=session["username"]
    )



# User Management
@app.route("/user-management")
def user_management():

    if "user_id" not in session:
        return redirect(
            url_for("login")
        )


    return render_template(
        "user_management.html",
        username=session["username"]
    )



# Logout
@app.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("login")
    )



if __name__ == "__main__":
    app.run(debug=True)