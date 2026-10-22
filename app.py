import os
from datetime import datetime, date, timedelta
from functools import wraps

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
from flask import (Flask, render_template, request, redirect, url_for, session, flash,
                   jsonify, g, send_from_directory)
from flask_sqlalchemy import SQLAlchemy
from markupsafe import Markup, escape
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
    SQLALCHEMY_DATABASE_URI=os.environ.get("DATABASE_URL", "mysql+pymysql://root:@localhost/medical_crisis"),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
)
db = SQLAlchemy(app)

CATEGORIES = ["Medicines", "Vaccines", "IV Fluids", "Blood Products", "Surgical Supplies",
              "PPE", "Oxygen", "Emergency Equipment", "Other"]
REQ_STATUS = ["Pending", "Requested", "Partially Fulfilled", "Fulfilled", "Cancelled"]
OPEN_REQ = ["Pending", "Requested", "Partially Fulfilled"]
SR_STATUS = ["PENDING", "ACCEPTED", "REJECTED", "PARTIALLY_FULFILLED", "FULFILLED"]
ORDER = {"CRITICAL": 0, "LOW": 1, "NORMAL": 2}


def calc_priority(qty, minimum):
    if qty <= minimum * 0.5:
        return "CRITICAL"
    if qty <= minimum:
        return "LOW"
    return "NORMAL"


class Bad(Exception):
    pass


# ---------------------------------------------------------------- models
class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    organization_name = db.Column(db.String(150))
    phone = db.Column(db.String(30))
    address = db.Column(db.String(255))
    categories = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.now)

    @property
    def org(self):
        return self.organization_name or self.name


class Inventory(db.Model):
    __tablename__ = "inventory"
    id = db.Column(db.Integer, primary_key=True)
    hospital_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    item_name = db.Column(db.String(120), nullable=False)
    category = db.Column(db.String(50), nullable=False)
    description = db.Column(db.Text)
    quantity = db.Column(db.Integer, nullable=False, default=0)
    minimum_quantity = db.Column(db.Integer, nullable=False, default=0)
    unit = db.Column(db.String(30))
    batch_number = db.Column(db.String(60))
    expiry_date = db.Column(db.Date)
    supplier = db.Column(db.String(120))
    is_cold_chain = db.Column(db.Boolean, default=False)
    min_temperature = db.Column(db.Float)
    max_temperature = db.Column(db.Float)
    current_temperature = db.Column(db.Float)
    temp_updated = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    hospital = db.relationship("User")

    @property
    def priority(self):
        return calc_priority(self.quantity or 0, self.minimum_quantity or 0)

    @property
    def pct(self):
        return round((self.quantity or 0) * 100 / self.minimum_quantity) if self.minimum_quantity else 100

    @property
    def cold_status(self):
        if not self.is_cold_chain:
            return None
        t, lo, hi = self.current_temperature, self.min_temperature, self.max_temperature
        if t is None or lo is None or hi is None:
            return "UNKNOWN"
        if t < lo or t > hi:
            return "VIOLATION"
        if t - lo <= 1 or hi - t <= 1:
            return "WARNING"
        return "SAFE"


class Requirement(db.Model):
    __tablename__ = "requirements"
    id = db.Column(db.Integer, primary_key=True)
    hospital_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    item_name = db.Column(db.String(120), nullable=False)
    category = db.Column(db.String(50))
    required_quantity = db.Column(db.Integer, nullable=False)
    current_quantity = db.Column(db.Integer, nullable=False, default=0)
    priority = db.Column(db.String(10))
    required_by = db.Column(db.Date)
    description = db.Column(db.Text)
    status = db.Column(db.String(30), default="Pending")
    created_at = db.Column(db.DateTime, default=datetime.now)
    hospital = db.relationship("User")

    @property
    def shortfall(self):
        return max(self.required_quantity - self.current_quantity, 0)


class SupplyRequest(db.Model):
    __tablename__ = "supply_requests"
    id = db.Column(db.Integer, primary_key=True)
    hospital_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    supplier_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    requirement_id = db.Column(db.Integer, db.ForeignKey("requirements.id"))
    item_name = db.Column(db.String(120), nullable=False)
    requested_quantity = db.Column(db.Integer, nullable=False)
    supplied_quantity = db.Column(db.Integer, default=0)
    priority = db.Column(db.String(10))
    status = db.Column(db.String(30), default="PENDING")
    request_date = db.Column(db.DateTime, default=datetime.now)
    required_date = db.Column(db.Date)
    supplier_response = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    hospital = db.relationship("User", foreign_keys=[hospital_id])
    supplier = db.relationship("User", foreign_keys=[supplier_id])
    requirement = db.relationship("Requirement")


class Alert(db.Model):
    __tablename__ = "alerts"
    id = db.Column(db.Integer, primary_key=True)
    hospital_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    inventory_id = db.Column(db.Integer, db.ForeignKey("inventory.id"))
    alert_type = db.Column(db.String(30), nullable=False)
    severity = db.Column(db.String(15), nullable=False)
    message = db.Column(db.String(400), nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    inventory = db.relationship("Inventory")


class InterHospitalRequest(db.Model):
    __tablename__ = "inter_hospital_requests"
    id = db.Column(db.Integer, primary_key=True)
    requester_hospital_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    provider_hospital_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    source_inventory_id = db.Column(db.Integer, db.ForeignKey("inventory.id", ondelete="SET NULL"))
    item_name = db.Column(db.String(120), nullable=False)
    category = db.Column(db.String(50), nullable=False)
    unit = db.Column(db.String(30))
    batch_number = db.Column(db.String(60))
    requested_quantity = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.Text, nullable=False)
    priority = db.Column(db.String(10), nullable=False, default="NORMAL")
    status = db.Column(db.String(20), nullable=False, default="PENDING")
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    responded_at = db.Column(db.DateTime)
    requester = db.relationship("User", foreign_keys=[requester_hospital_id])
    provider = db.relationship("User", foreign_keys=[provider_hospital_id])
    source_inventory = db.relationship("Inventory")


# ---------------------------------------------------------------- helpers
def badge(text, cls=None):
    slug = (cls or str(text)).lower().replace(" ", "-").replace("_", "-")
    return Markup('<span class="badge %s">%s</span>') % (slug, text)


app.jinja_env.globals["badge"] = badge

HN = [("Dashboard", "hospital_dashboard"), ("Inventory", "inventory"),
      ("Emergency Requirements", "requirements"), ("Supply Requests", "requests_page"),
      ("Alerts", "alerts"), ("Cold Chain", "cold_chain"), ("Inter-Hospital Sharing", "inter_hospital_sharing"),
      ("Reports", "reports"), ("Profile", "profile")]
NAV = {"hospital": HN,
       "provider": [("Dashboard", "provider_dashboard"), ("Requests", "provider_requests"), ("Profile", "profile")],
       "admin": [("Admin Dashboard", "admin_dashboard"), ("Users", "admin_users")] + HN}
HOME = {"hospital": "hospital_dashboard", "provider": "provider_dashboard", "admin": "admin_dashboard"}


def scoped(model):
    q = model.query
    return q if g.user.role == "admin" else q.filter(model.hospital_id == g.user.id)


def target_hid():
    if g.user.role == "hospital":
        return g.user.id
    h = User.query.filter_by(role="hospital").first()
    if not h:
        raise Bad("No hospital account exists yet.")
    return h.id


def login_required(*roles):
    def deco(f):
        @wraps(f)
        def wrapper(*a, **k):
            api = request.path.startswith("/api/")
            if not g.user:
                if api:
                    return jsonify(error="Please log in."), 401
                flash("Please log in to continue.", "error")
                return redirect(url_for("login"))
            if roles and g.user.role not in roles:
                if api:
                    return jsonify(error="You are not authorized to access this page."), 403
                flash("You are not authorized to access this page.", "error")
                return redirect(url_for(HOME[g.user.role]))
            return f(*a, **k)
        return wrapper
    return deco


def add_alert(hid, inv_id, atype, sev, msg):
    if Alert.query.filter_by(hospital_id=hid, inventory_id=inv_id, alert_type=atype,
                             message=msg, is_read=False).first():
        return
    db.session.add(Alert(hospital_id=hid, inventory_id=inv_id, alert_type=atype, severity=sev, message=msg))


def check_inventory(i):
    n, p = i.item_name, i.priority
    if p == "CRITICAL":
        add_alert(i.hospital_id, i.id, "CRITICAL_STOCK", "CRITICAL",
                  f"CRITICAL: {n} stock is below 50% of the minimum required quantity.")
    elif p == "LOW":
        add_alert(i.hospital_id, i.id, "LOW_STOCK", "WARNING",
                  f"LOW: {n} stock ({i.quantity}) is below the minimum required quantity ({i.minimum_quantity}).")
    if i.expiry_date and i.expiry_date <= date.today() + timedelta(days=30):
        word = "has expired" if i.expiry_date < date.today() else "expires soon"
        add_alert(i.hospital_id, i.id, "EXPIRY", "WARNING", f"WARNING: {n} {word} ({i.expiry_date}).")
    if i.cold_status == "VIOLATION":
        add_alert(i.hospital_id, i.id, "TEMPERATURE", "CRITICAL",
                  f"CRITICAL: {n} temperature is {i.current_temperature:g}°C. "
                  f"Safe range is {i.min_temperature:g}°C–{i.max_temperature:g}°C.")


def refresh_alerts():
    for i in scoped(Inventory).all():
        check_inventory(i)
    for r in scoped(SupplyRequest).filter_by(status="PENDING").all():
        add_alert(r.hospital_id, None, "PENDING_RESPONSE", "INFO",
                  f"Awaiting response from {r.supplier.org} for {r.item_name} ({r.requested_quantity}).")
    db.session.commit()


def unread():
    if g.user.role == "provider":
        return 0
    return scoped(Alert).filter_by(is_read=False).count()


def to_date(v):
    return date.fromisoformat(v) if v else None


def fill_inventory(inv, f):
    name = (f.get("item_name") or "").strip()
    if not name:
        raise Bad("Item name is required.")
    if f.get("category") not in CATEGORIES:
        raise Bad("Choose a valid category.")
    qty, mn = int(f.get("quantity", "")), int(f.get("minimum_quantity", ""))
    if qty < 0 or mn <= 0:
        raise Bad("Quantity cannot be negative and minimum must be above zero.")
    inv.item_name, inv.category, inv.quantity, inv.minimum_quantity = name[:120], f["category"], qty, mn
    inv.description = (f.get("description") or "").strip()
    inv.unit = (f.get("unit") or "units").strip()[:30]
    inv.batch_number = (f.get("batch_number") or "").strip()[:60]
    inv.supplier = (f.get("supplier") or "").strip()[:120]
    expiry_date = to_date(f.get("expiry_date"))
    if expiry_date and expiry_date <= date.today():
        raise Bad("Expiry date must be a future date.")
    inv.expiry_date = expiry_date
    inv.is_cold_chain = f.get("is_cold_chain") == "on"
    if inv.is_cold_chain:
        lo, hi, cur = (float(f.get(k, "")) for k in ("min_temperature", "max_temperature", "current_temperature"))
        if lo >= hi:
            raise Bad("Minimum temperature must be lower than maximum temperature.")
        inv.min_temperature, inv.max_temperature = lo, hi
        if inv.current_temperature != cur or not inv.temp_updated:
            inv.temp_updated = datetime.now()
        inv.current_temperature = cur
    else:
        inv.min_temperature = inv.max_temperature = inv.current_temperature = inv.temp_updated = None


INV_FIELDS = ["item_name", "category", "description", "quantity", "minimum_quantity", "unit",
              "batch_number", "expiry_date", "supplier", "min_temperature", "max_temperature", "current_temperature"]


def form_data(item, form):
    if form:
        d = dict(form)
        d["is_cold_chain"] = "on" if form.get("is_cold_chain") else ""
        return d
    if item:
        d = {k: "" if getattr(item, k) is None else str(getattr(item, k)) for k in INV_FIELDS}
        d["is_cold_chain"] = "on" if item.is_cold_chain else ""
        return d
    return {"quantity": "0", "minimum_quantity": "100", "unit": "units"}


def make_requirement(hid, d):
    name = (d.get("item_name") or "").strip()
    if not name:
        raise Bad("Item name is required.")
    cat = d.get("category") or "Other"
    if cat not in CATEGORIES:
        raise Bad("Choose a valid category.")
    need = int(d.get("required_quantity") or 0)
    cur = d.get("current_quantity")
    if cur in (None, ""):
        m = Inventory.query.filter_by(hospital_id=hid, item_name=name).first()
        cur = m.quantity if m else 0
    cur = int(cur)
    if need <= 0 or cur < 0:
        raise Bad("Required quantity must be above zero.")
    status = d.get("status") if d.get("status") in REQ_STATUS else "Pending"
    r = Requirement(hospital_id=hid, item_name=name[:120], category=cat, required_quantity=need,
                    current_quantity=cur, priority=calc_priority(cur, need), required_by=to_date(d.get("required_by")),
                    description=(d.get("description") or "").strip(), status=status)
    db.session.add(r)
    return r


def find_item(iid):
    i = scoped(Inventory).filter_by(id=int(iid)).first()
    if not i:
        raise Bad("Item not found.")
    return i


def update_qty(iid, qty):
    i, q = find_item(iid), int(qty)
    if q < 0:
        raise Bad("Quantity cannot be negative.")
    i.quantity = q
    check_inventory(i)
    return i


def update_temp(iid, temp):
    i = find_item(iid)
    if not i.is_cold_chain:
        raise Bad("Item is not a cold-chain item.")
    i.current_temperature, i.temp_updated = float(temp), datetime.now()
    check_inventory(i)
    return i


def inv_dict(i):
    return dict(id=i.id, item_name=i.item_name, category=i.category, quantity=i.quantity,
                minimum_quantity=i.minimum_quantity, unit=i.unit, priority=i.priority, pct=i.pct,
                is_cold_chain=bool(i.is_cold_chain), cold_status=i.cold_status,
                current_temperature=i.current_temperature)


def req_dict(r):
    return dict(id=r.id, item_name=r.item_name, category=r.category, required_quantity=r.required_quantity,
                current_quantity=r.current_quantity, shortfall=r.shortfall, priority=r.priority,
                status=r.status, required_by=str(r.required_by or ""))


# ---------------------------------------------------------------- hooks
@app.before_request
def load_user():
    g.user = None
    if request.endpoint in ("static", "service_worker"):
        return
    uid = session.get("uid")
    if uid:
        g.user = db.session.get(User, uid)


@app.context_processor
def inject():
    u = g.get("user")
    return dict(user=u, nav=NAV.get(u.role, []) if u else [], unread=unread() if u else 0)


def err_page(msg, code):
    return (f'<link rel="stylesheet" href="/static/css/style.css"><div class="errbox"><h1>{escape(msg)}</h1>'
            f'<p><a class="btn" href="/">Back to home</a></p></div>', code)


@app.errorhandler(OperationalError)
def db_down(e):
    db.session.rollback()
    return err_page("Unable to connect to the MySQL database. Make sure MySQL is running and DATABASE_URL is correct "
                    "(see README, step 6).", 503)


@app.errorhandler(404)
def nf(e):
    return err_page("Page not found.", 404)


@app.errorhandler(500)
def ise(e):
    db.session.rollback()
    return err_page("Something went wrong. Please try again.", 500)


@app.route("/sw.js")
def service_worker():
    r = send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")
    r.headers["Service-Worker-Allowed"] = "/"
    r.headers["Cache-Control"] = "no-cache"
    return r


# ---------------------------------------------------------------- public / auth
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email, pw, role = request.form.get("email", "").strip().lower(), request.form.get("password", ""), request.form.get("role")
        u = User.query.filter_by(email=email).first()
        if u and u.role == role and check_password_hash(u.password_hash, pw):
            session.clear()
            session["uid"] = u.id
            return redirect(url_for(HOME[u.role]))
        flash("Invalid email or password.", "error")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        f = request.form
        role, email = f.get("role"), f.get("email", "").strip().lower()
        try:
            if role not in ("hospital", "provider"):
                raise Bad("Choose Hospital or Supplier.")
            for k, label in (("organization_name", "Organization name"), ("name", "Full name"), ("email", "Email"),
                             ("phone", "Contact number"), ("address", "Address")):
                if not f.get(k, "").strip():
                    raise Bad(f"{label} is required.")
            if "@" not in email:
                raise Bad("Enter a valid email address.")
            if len(f.get("password", "")) < 6:
                raise Bad("Password must be at least 6 characters.")
            if f.get("password") != f.get("confirm_password"):
                raise Bad("Passwords do not match.")
            if role == "provider" and not f.get("categories", "").strip():
                raise Bad("List the medical supply categories you provide.")
            if User.query.filter_by(email=email).first():
                raise Bad("An account with this email already exists.")
            db.session.add(User(name=f["name"].strip(), email=email, role=role,
                                password_hash=generate_password_hash(f["password"]),
                                organization_name=f["organization_name"].strip(), phone=f["phone"].strip(),
                                address=f["address"].strip(), categories=f.get("categories", "").strip()))
            db.session.commit()
            flash("Registration successful. Please log in.", "ok")
            return redirect(url_for("login"))
        except Bad as e:
            flash(str(e), "error")
        except SQLAlchemyError:
            db.session.rollback()
            flash("Unable to register right now.", "error")
    return render_template("register.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/profile", methods=["GET", "POST"])
@login_required()
def profile():
    if request.method == "POST":
        f = request.form
        try:
            g.user.name = f.get("name", "").strip() or g.user.name
            g.user.organization_name = f.get("organization_name", "").strip() or g.user.organization_name
            g.user.phone, g.user.address = f.get("phone", "").strip(), f.get("address", "").strip()
            if f.get("password"):
                if len(f["password"]) < 6 or f["password"] != f.get("confirm_password"):
                    raise Bad("New passwords must match and be at least 6 characters.")
                g.user.password_hash = generate_password_hash(f["password"])
            db.session.commit()
            flash("Profile saved.", "ok")
        except Bad as e:
            db.session.rollback()
            flash(str(e), "error")
        except SQLAlchemyError:
            db.session.rollback()
            flash("Unable to save profile.", "error")
        return redirect(url_for("profile"))
    return render_template("profile.html")


# ---------------------------------------------------------------- hospital
def dash_data():
    refresh_alerts()
    inv = scoped(Inventory).all()
    inv.sort(key=lambda i: (ORDER[i.priority], i.pct))
    reqs = scoped(Requirement).order_by(Requirement.created_at.desc()).all()
    cnt = lambda p: sum(1 for i in inv if i.priority == p)
    cold = [i for i in inv if i.is_cold_chain]
    cs = lambda s: sum(1 for i in cold if i.cold_status == s)
    op = [r for r in reqs if r.status in OPEN_REQ]
    stats = [("Total Items", len(inv), ""), ("Low Stock Items", cnt("LOW"), "low"), ("Critical Items", cnt("CRITICAL"), "critical"),
             ("Pending Requests", scoped(SupplyRequest).filter_by(status="PENDING").count(), ""),
             ("Active Alerts", unread(), "critical"), ("Cold-Chain Items", len(cold), "")]
    charts = [("Inventory status", [("Normal", cnt("NORMAL"), "normal"), ("Low", cnt("LOW"), "low"), ("Critical", cnt("CRITICAL"), "critical")]),
              ("Emergency requirements", [("Pending", sum(1 for r in op if r.priority != "CRITICAL"), "pending"),
                                          ("Fulfilled", sum(1 for r in reqs if r.status == "Fulfilled"), "normal"),
                                          ("Critical", sum(1 for r in op if r.priority == "CRITICAL"), "critical")]),
              ("Cold chain", [("Safe", cs("SAFE"), "normal"), ("Warning", cs("WARNING"), "low"), ("Violation", cs("VIOLATION"), "critical")])]
    return stats, charts, inv, reqs


@app.route("/hospital/dashboard")
@login_required("hospital", "admin")
def hospital_dashboard():
    stats, charts, inv, reqs = dash_data()
    return render_template("hospital_dashboard.html", stats=stats, charts=charts, inv=inv, reqs=reqs, report=False)


@app.route("/hospital/reports")
@login_required("hospital", "admin")
def reports():
    stats, charts, inv, reqs = dash_data()
    return render_template("hospital_dashboard.html", stats=stats, charts=charts, inv=inv, reqs=reqs, report=True)


@app.route("/hospital/inventory")
@login_required("hospital", "admin")
def inventory():
    refresh_alerts()
    q = scoped(Inventory)
    s, cat, pr, sort = (request.args.get(k, "").strip() for k in ("q", "category", "priority", "sort"))
    if s:
        q = q.filter(Inventory.item_name.ilike(f"%{s}%"))
    if cat:
        q = q.filter(Inventory.category == cat)
    q = q.order_by({"qty_asc": Inventory.quantity.asc(), "qty_desc": Inventory.quantity.desc()}.get(sort, Inventory.item_name.asc()))
    items = [i for i in q.all() if not pr or i.priority == pr]
    return render_template("inventory.html", items=items, categories=CATEGORIES, f=dict(q=s, category=cat, priority=pr, sort=sort))


def save_item(item):
    new = item is None
    try:
        inv = item or Inventory(hospital_id=target_hid())
        fill_inventory(inv, request.form)
        if new:
            db.session.add(inv)
        db.session.flush()
        check_inventory(inv)
        db.session.commit()
        flash("Item added." if new else "Item updated.", "ok")
        return redirect(url_for("inventory"))
    except (Bad, ValueError) as e:
        db.session.rollback()
        flash(str(e) if isinstance(e, Bad) else "Please check the numbers and dates you entered.", "error")
    except SQLAlchemyError:
        db.session.rollback()
        flash("Unable to update inventory.", "error")
    return render_template("inventory_form.html", item=item, d=form_data(item, request.form), categories=CATEGORIES,
                           min_expiry_date=(date.today() + timedelta(days=1)).isoformat())


@app.route("/hospital/inventory/add", methods=["GET", "POST"])
@login_required("hospital", "admin")
def inventory_add():
    if request.method == "POST":
        return save_item(None)
    return render_template("inventory_form.html", item=None, d=form_data(None, None), categories=CATEGORIES,
                           min_expiry_date=(date.today() + timedelta(days=1)).isoformat())


@app.route("/hospital/inventory/edit/<int:id>", methods=["GET", "POST"])
@login_required("hospital", "admin")
def inventory_edit(id):
    item = scoped(Inventory).filter_by(id=id).first_or_404()
    if request.method == "POST":
        return save_item(item)
    return render_template("inventory_form.html", item=item, d=form_data(item, None), categories=CATEGORIES)


@app.route("/hospital/inventory/delete/<int:id>", methods=["POST"])
@login_required("hospital", "admin")
def inventory_delete(id):
    item = scoped(Inventory).filter_by(id=id).first_or_404()
    try:
        Alert.query.filter_by(inventory_id=id).delete()
        db.session.delete(item)
        db.session.commit()
        flash("Item deleted.", "ok")
    except SQLAlchemyError:
        db.session.rollback()
        flash("Unable to delete item.", "error")
    return redirect(url_for("inventory"))


@app.route("/hospital/requirements")
@login_required("hospital", "admin")
def requirements():
    reqs = scoped(Requirement).order_by(Requirement.created_at.desc()).all()
    reqs.sort(key=lambda r: (r.status not in OPEN_REQ, ORDER.get(r.priority, 3)))
    return render_template("requirements.html", reqs=reqs, statuses=REQ_STATUS)


@app.route("/hospital/requirements/add", methods=["GET", "POST"])
@login_required("hospital", "admin")
def requirement_add():
    d = request.form
    if request.method == "POST":
        try:
            make_requirement(target_hid(), d)
            db.session.commit()
            flash("Emergency requirement saved.", "ok")
            return redirect(url_for("requirements"))
        except (Bad, ValueError) as e:
            db.session.rollback()
            flash(str(e) if isinstance(e, Bad) else "Please check the numbers and dates you entered.", "error")
        except SQLAlchemyError:
            db.session.rollback()
            flash("Unable to save requirement.", "error")
    return render_template("requirement_form.html", d=d, categories=CATEGORIES, statuses=REQ_STATUS)


@app.route("/hospital/requirements/status/<int:id>", methods=["POST"])
@login_required("hospital", "admin")
def requirement_status(id):
    r = scoped(Requirement).filter_by(id=id).first_or_404()
    if request.form.get("status") in REQ_STATUS:
        r.status = request.form["status"]
        db.session.commit()
        flash("Status updated.", "ok")
    return redirect(url_for("requirements"))


@app.route("/hospital/requests", methods=["GET", "POST"])
@login_required("hospital", "admin")
def requests_page():
    if request.method == "POST":
        f = request.form
        try:
            hid = target_hid()
            sup = User.query.filter_by(id=int(f.get("supplier_id") or 0), role="provider").first()
            if not sup:
                raise Bad("Choose a supplier.")
            req = Requirement.query.filter_by(id=int(f["requirement_id"]), hospital_id=hid).first() if f.get("requirement_id") else None
            name = (f.get("item_name") or (req.item_name if req else "")).strip()
            qty = int(f.get("requested_quantity") or (req.shortfall if req else 0))
            if not name or qty <= 0:
                raise Bad("Enter an item and a quantity above zero.")
            inv = Inventory.query.filter_by(hospital_id=hid, item_name=name).first()
            pr = req.priority if req else (inv.priority if inv else "NORMAL")
            db.session.add(SupplyRequest(hospital_id=hid, supplier_id=sup.id, requirement_id=req.id if req else None,
                                         item_name=name, requested_quantity=qty, priority=pr, required_date=to_date(f.get("required_date"))))
            if req and req.status == "Pending":
                req.status = "Requested"
            db.session.commit()
            flash("Supply request sent.", "ok")
        except (Bad, ValueError) as e:
            db.session.rollback()
            flash(str(e) if isinstance(e, Bad) else "Please check the values you entered.", "error")
        except SQLAlchemyError:
            db.session.rollback()
            flash("Unable to send request.", "error")
        return redirect(url_for("requests_page"))
    refresh_alerts()
    rows = scoped(SupplyRequest).order_by(SupplyRequest.request_date.desc()).all()
    reqs = [r for r in scoped(Requirement).all() if r.status in OPEN_REQ]
    return render_template("requests.html", rows=rows, reqs=reqs, suppliers=User.query.filter_by(role="provider").all())


@app.route("/hospital/inter-hospital-sharing", methods=["GET", "POST"])
@login_required("hospital")
def inter_hospital_sharing():
    hid = g.user.id
    if request.method == "POST":
        action = request.form.get("action", "create")
        try:
            if action == "create":
                provider_id = int(request.form.get("provider_hospital_id") or 0)
                source_id = int(request.form.get("source_inventory_id") or 0)
                quantity = int(request.form.get("requested_quantity") or 0)
                reason = (request.form.get("reason") or "").strip()
                priority = request.form.get("priority", "NORMAL")
                provider = User.query.filter_by(id=provider_id, role="hospital").first()
                source = Inventory.query.filter_by(id=source_id, hospital_id=provider_id).first()
                if not provider or provider.id == hid:
                    raise Bad("Choose another hospital as the provider.")
                if not source or source.quantity <= 0:
                    raise Bad("Choose an available inventory item from that hospital.")
                if quantity <= 0 or len(reason) < 3 or priority not in ("CRITICAL", "LOW", "NORMAL"):
                    raise Bad("Enter a positive quantity, a reason, and a valid priority.")
                sharing = InterHospitalRequest(
                    requester_hospital_id=hid, provider_hospital_id=provider.id,
                    source_inventory_id=source.id, item_name=source.item_name,
                    category=source.category, unit=source.unit, batch_number=source.batch_number,
                    requested_quantity=quantity, reason=reason[:2000], priority=priority)
                db.session.add(sharing)
                db.session.flush()
                add_alert(provider.id, None, "INTER_HOSPITAL_REQUEST", "INFO",
                          f"Sharing request #{sharing.id or 'new'} from {g.user.org} for {quantity} {source.unit or 'units'} of {source.item_name}.")
                db.session.commit()
                flash("Sharing request sent.", "ok")
            elif action in ("accept", "reject"):
                sharing_id = int(request.form.get("request_id") or 0)
                sharing = InterHospitalRequest.query.filter_by(
                    id=sharing_id, provider_hospital_id=hid).with_for_update().first()
                if not sharing or sharing.status != "PENDING":
                    raise Bad("This request is unavailable or has already been handled.")
                if action == "reject":
                    sharing.status = "REJECTED"
                    sharing.responded_at = datetime.now()
                    add_alert(sharing.requester_hospital_id, None, "INTER_HOSPITAL_REJECTED", "WARNING",
                              f"Hospital {g.user.org} rejected sharing request #{sharing.id} for {sharing.item_name}.")
                    db.session.commit()
                    flash("Sharing request rejected.", "ok")
                else:
                    source = Inventory.query.filter_by(
                        id=sharing.source_inventory_id, hospital_id=hid).with_for_update().first()
                    if not source:
                        raise Bad("The provider inventory item no longer exists.")
                    if source.quantity < sharing.requested_quantity:
                        raise Bad("Insufficient stock. No inventory was transferred.")
                    matches = Inventory.query.filter_by(
                        hospital_id=sharing.requester_hospital_id,
                        item_name=sharing.item_name, category=sharing.category,
                        unit=sharing.unit, batch_number=sharing.batch_number).with_for_update().all()
                    if len(matches) > 1:
                        raise Bad("The receiving hospital has duplicate matching inventory rows; transfer was not made.")
                    source.quantity -= sharing.requested_quantity
                    if matches:
                        destination = matches[0]
                        destination.quantity += sharing.requested_quantity
                    else:
                        destination = Inventory(
                            hospital_id=sharing.requester_hospital_id, item_name=source.item_name,
                            category=source.category, description=source.description,
                            quantity=sharing.requested_quantity, minimum_quantity=source.minimum_quantity,
                            unit=source.unit, batch_number=source.batch_number, expiry_date=source.expiry_date,
                            supplier=source.supplier, is_cold_chain=source.is_cold_chain,
                            min_temperature=source.min_temperature, max_temperature=source.max_temperature)
                        db.session.add(destination)
                        db.session.flush()
                    check_inventory(source)
                    check_inventory(destination)
                    sharing.status = "COMPLETED"
                    sharing.responded_at = datetime.now()
                    add_alert(sharing.requester_hospital_id, destination.id, "INTER_HOSPITAL_COMPLETED", "INFO",
                              f"Sharing request #{sharing.id} was completed. {sharing.requested_quantity} {sharing.unit or 'units'} of {sharing.item_name} received from {g.user.org}.")
                    add_alert(sharing.provider_hospital_id, source.id, "INTER_HOSPITAL_COMPLETED", "INFO",
                              f"Sharing request #{sharing.id} was completed. {sharing.requested_quantity} {sharing.unit or 'units'} of {sharing.item_name} sent to {sharing.requester.org}.")
                    db.session.commit()
                    flash("Transfer completed and both inventory records updated.", "ok")
            else:
                raise Bad("Unknown sharing request action.")
        except (Bad, ValueError, TypeError) as e:
            db.session.rollback()
            flash(str(e) if isinstance(e, Bad) else "Please check the values entered.", "error")
        except SQLAlchemyError:
            db.session.rollback()
            flash("Unable to process the sharing request. No transfer was committed.", "error")
        return redirect(url_for("inter_hospital_sharing"))

    hospitals = User.query.filter(User.role == "hospital", User.id != hid).order_by(User.organization_name).all()
    selected_id = request.args.get("provider_id", type=int)
    selected_provider = (User.query.filter_by(id=selected_id, role="hospital").filter(User.id != hid).first()
                         if selected_id else None)
    inventory_options = (Inventory.query.filter_by(hospital_id=selected_provider.id)
                         .filter(Inventory.quantity > 0).order_by(Inventory.item_name).all()
                         if selected_provider else [])
    sent = InterHospitalRequest.query.filter_by(requester_hospital_id=hid).order_by(
        InterHospitalRequest.created_at.desc()).all()
    incoming = InterHospitalRequest.query.filter_by(provider_hospital_id=hid).order_by(
        InterHospitalRequest.created_at.desc()).all()
    return render_template("inter_hospital_sharing.html", hospitals=hospitals,
                           selected_provider=selected_provider, inventory_options=inventory_options,
                           sent=sent, incoming=incoming)


@app.route("/hospital/alerts")
@login_required("hospital", "admin")
def alerts():
    refresh_alerts()
    rows = scoped(Alert).order_by(Alert.created_at.desc(), Alert.id.desc()).limit(200).all()
    return render_template("alerts.html", rows=rows)


@app.route("/hospital/alerts/read", methods=["POST"])
@login_required("hospital", "admin")
def alerts_read():
    q = scoped(Alert).filter_by(is_read=False)
    if request.form.get("id"):
        q = q.filter(Alert.id == int(request.form["id"]))
    for a in q.all():
        a.is_read = True
    db.session.commit()
    return redirect(url_for("alerts"))


@app.route("/hospital/cold-chain")
@login_required("hospital", "admin")
def cold_chain():
    refresh_alerts()
    items = [i for i in scoped(Inventory).order_by(Inventory.item_name).all() if i.is_cold_chain]
    return render_template("cold_chain.html", items=items)


# ---------------------------------------------------------------- provider
def supply_alerts(mine):
    """Urgent supplier-facing alerts: what to supply, for whom, how much."""
    out, today = [], date.today()
    tokens = [t.strip().lower() for t in (g.user.categories or "").split(",") if t.strip()]

    def match(cat, name):
        c, n = (cat or "").lower(), (name or "").lower()
        return any(t in c or (c and c in t) or t in n for t in tokens)

    def due(d):
        if not d:
            return ""
        k = (d - today).days
        return "Overdue" if k < 0 else "Due today" if k == 0 else f"Due in {k}d"

    def add(sev, kind, item, hosp, qty, detail, d, cat, action=None):
        out.append(dict(sev=sev, kind=kind, item=item, hospital=hosp, qty=qty, detail=detail, due=due(d),
                        when=d or date.max, match=match(cat, item), action=action))

    for r in mine:
        if r.status == "PENDING":
            add("CRITICAL" if r.priority == "CRITICAL" else "WARNING", "Awaiting your response", r.item_name,
                r.hospital.org, r.requested_quantity, "Accept, reject or tell them what quantity you can send.",
                r.required_date, r.requirement.category if r.requirement else "", "Respond now")
    covered = {r.requirement_id for r in mine if r.requirement_id and r.status != "REJECTED"}
    open_reqs = [r for r in Requirement.query.all() if r.status in OPEN_REQ]
    for r in open_reqs:
        if r.priority in ("CRITICAL", "LOW") and r.id not in covered:
            add(r.priority == "CRITICAL" and "CRITICAL" or "WARNING", "Open requirement", r.item_name, r.hospital.org,
                r.shortfall, f"{r.current_quantity} of {r.required_quantity} on hand. {r.description or ''}".strip(),
                r.required_by, r.category)
    keys = {(r.hospital_id, r.item_name.lower()) for r in open_reqs}
    for i in Inventory.query.all():
        if i.priority == "CRITICAL" and (i.hospital_id, i.item_name.lower()) not in keys:
            add("CRITICAL", "Critical stock", i.item_name, i.hospital.org, i.minimum_quantity - i.quantity,
                f"Stock is {i.quantity} of {i.minimum_quantity} {i.unit or 'units'} ({i.pct}% of minimum).", None, i.category)
        if i.cold_status == "VIOLATION":
            add("CRITICAL", "Cold-chain failure", i.item_name, i.hospital.org, None,
                f"Temperature {i.current_temperature:g}°C is outside {i.min_temperature:g}-{i.max_temperature:g}°C. "
                "Replacement stock may be needed.", None, i.category)
    out.sort(key=lambda a: (ORDER.get(a["sev"], 2) if a["sev"] in ORDER else {"WARNING": 1}.get(a["sev"], 2),
                            not a["match"], a["when"]))
    return out


@app.route("/provider/dashboard")
@login_required("provider", "admin")
def provider_dashboard():
    q = SupplyRequest.query if g.user.role == "admin" else SupplyRequest.query.filter_by(supplier_id=g.user.id)
    mine = q.order_by(SupplyRequest.request_date.desc()).all()
    n = lambda s: sum(1 for r in mine if r.status == s)
    alerts_ = supply_alerts(mine)
    stats = [("Urgent Alerts", sum(1 for a in alerts_ if a["sev"] == "CRITICAL"), "critical"),
             ("Total Requests", len(mine), ""), ("Critical Requests", sum(1 for r in mine if r.priority == "CRITICAL"), "critical"),
             ("Pending Requests", n("PENDING"), "low"), ("Accepted Requests", n("ACCEPTED"), ""), ("Completed Requests", n("FULFILLED"), "normal")]
    open_reqs = [r for r in Requirement.query.all() if r.status in OPEN_REQ]
    open_reqs.sort(key=lambda r: ORDER.get(r.priority, 3))
    return render_template("provider_dashboard.html", stats=stats, open_reqs=open_reqs, mine=mine[:8], supply=alerts_[:12], supply_total=len(alerts_))


@app.route("/provider/requests")
@login_required("provider", "admin")
def provider_requests():
    q = SupplyRequest.query if g.user.role == "admin" else SupplyRequest.query.filter_by(supplier_id=g.user.id)
    st = request.args.get("status", "")
    if st in SR_STATUS:
        q = q.filter_by(status=st)
    return render_template("provider_requests.html", rows=q.order_by(SupplyRequest.request_date.desc()).all(), statuses=SR_STATUS, st=st)


@app.route("/provider/request/<int:id>", methods=["POST"])
@login_required("provider", "admin")
def provider_request(id):
    r = db.get_or_404(SupplyRequest, id)
    if g.user.role == "provider" and r.supplier_id != g.user.id:
        flash("You are not authorized to access this page.", "error")
        return redirect(url_for("provider_dashboard"))
    f, act = request.form, request.form.get("action")
    try:
        sq = f.get("supplied_quantity", "").strip()
        sq = int(sq) if sq else None
        if sq is not None and sq < 0:
            raise Bad("Quantity cannot be negative.")
        if act == "accept":
            r.status = "ACCEPTED"
        elif act == "reject":
            r.status = "REJECTED"
        elif act == "update" and f.get("status") in ("ACCEPTED", "PARTIALLY_FULFILLED", "FULFILLED"):
            r.status = f["status"]
            if r.status == "FULFILLED" and sq is None:
                sq = r.requested_quantity
        else:
            raise Bad("Unknown action.")
        if sq is not None:
            r.supplied_quantity = sq
        if f.get("supplier_response", "").strip():
            r.supplier_response = f["supplier_response"].strip()[:500]
        if r.requirement:
            m = {"ACCEPTED": "Requested", "PARTIALLY_FULFILLED": "Partially Fulfilled", "FULFILLED": "Fulfilled"}
            if r.status in m:
                r.requirement.status = m[r.status]
            elif r.requirement.status == "Requested":
                r.requirement.status = "Pending"
        add_alert(r.hospital_id, None, "SUPPLIER_RESPONSE", "INFO",
                  f"{r.supplier.org} set the request for {r.item_name} to {r.status.replace('_', ' ')}.")
        db.session.commit()
        flash("Request updated.", "ok")
    except (Bad, ValueError) as e:
        db.session.rollback()
        flash(str(e) if isinstance(e, Bad) else "Enter a valid quantity.", "error")
    except SQLAlchemyError:
        db.session.rollback()
        flash("Unable to update request.", "error")
    return redirect(url_for("provider_requests"))


# ---------------------------------------------------------------- admin
def admin_ctx():
    inv = Inventory.query.all()
    c = lambda p: sum(1 for i in inv if i.priority == p)
    stats = [("Total Hospitals", User.query.filter_by(role="hospital").count(), ""),
             ("Total Suppliers", User.query.filter_by(role="provider").count(), ""),
             ("Inventory Items", len(inv), ""), ("Critical Items", c("CRITICAL"), "critical"), ("Low-Stock Items", c("LOW"), "low"),
             ("Active Alerts", Alert.query.filter_by(is_read=False).count(), "critical"),
             ("Pending Requests", SupplyRequest.query.filter_by(status="PENDING").count(), "")]
    return dict(stats=stats, users=User.query.order_by(User.role, User.id).all())


@app.route("/admin/dashboard")
@login_required("admin")
def admin_dashboard():
    return render_template("admin_dashboard.html", **admin_ctx())


@app.route("/admin/users")
@login_required("admin")
def admin_users():
    return render_template("admin_dashboard.html", **admin_ctx())


@app.route("/admin/users/delete/<int:id>", methods=["POST"])
@login_required("admin")
def admin_delete_user(id):
    u = db.get_or_404(User, id)
    if u.id == g.user.id:
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("admin_users"))
    try:
        SupplyRequest.query.filter((SupplyRequest.hospital_id == id) | (SupplyRequest.supplier_id == id)).delete(synchronize_session=False)
        InterHospitalRequest.query.filter((InterHospitalRequest.requester_hospital_id == id) | (InterHospitalRequest.provider_hospital_id == id)).delete(synchronize_session=False)
        Alert.query.filter_by(hospital_id=id).delete()
        Inventory.query.filter_by(hospital_id=id).delete()
        Requirement.query.filter_by(hospital_id=id).delete()
        db.session.delete(u)
        db.session.commit()
        flash("User and related records deleted.", "ok")
    except SQLAlchemyError:
        db.session.rollback()
        flash("Unable to delete user.", "error")
    return redirect(url_for("admin_users"))


# ---------------------------------------------------------------- API
@app.route("/api/inventory")
@login_required("hospital", "admin")
def api_inventory():
    return jsonify(items=[inv_dict(i) for i in scoped(Inventory).order_by(Inventory.item_name).all()])


@app.route("/api/inventory/update", methods=["POST"])
@login_required("hospital", "admin")
def api_inventory_update():
    d = request.get_json(silent=True) or {}
    try:
        i = update_qty(d.get("id"), d.get("quantity"))
        db.session.commit()
        return jsonify(ok=True, item=inv_dict(i))
    except (Bad, ValueError, TypeError, SQLAlchemyError):
        db.session.rollback()
        return jsonify(error="Unable to update inventory."), 400


@app.route("/api/requirements", methods=["GET", "POST"])
@login_required("hospital", "admin")
def api_requirements():
    if request.method == "POST":
        try:
            r = make_requirement(target_hid(), request.get_json(silent=True) or {})
            db.session.commit()
            return jsonify(ok=True, requirement=req_dict(r)), 201
        except (Bad, ValueError, TypeError, SQLAlchemyError) as e:
            db.session.rollback()
            return jsonify(error=str(e) if isinstance(e, Bad) else "Unable to save requirement."), 400
    return jsonify(requirements=[req_dict(r) for r in scoped(Requirement).all()])


@app.route("/api/alerts")
@login_required("hospital", "admin")
def api_alerts():
    refresh_alerts()
    rows = scoped(Alert).order_by(Alert.created_at.desc()).limit(100).all()
    return jsonify(unread=sum(1 for a in rows if not a.is_read),
                   alerts=[dict(id=a.id, type=a.alert_type, severity=a.severity, message=a.message,
                                is_read=bool(a.is_read), created_at=str(a.created_at)) for a in rows])


@app.route("/api/cold-chain", methods=["GET", "POST"])
@login_required("hospital", "admin")
def api_cold_chain():
    if request.method == "POST":
        d = request.get_json(silent=True) or {}
        try:
            i = update_temp(d.get("id"), d.get("temperature"))
            db.session.commit()
            return jsonify(ok=True, item=inv_dict(i))
        except (Bad, ValueError, TypeError, SQLAlchemyError):
            db.session.rollback()
            return jsonify(error="Unable to update temperature."), 400
    return jsonify(items=[inv_dict(i) for i in scoped(Inventory).all() if i.is_cold_chain])


@app.route("/api/offline-sync", methods=["POST"])
@login_required("hospital", "admin")
def api_offline_sync():
    ops = (request.get_json(silent=True) or {}).get("ops", [])
    applied, errors = 0, []
    for op in ops[:200]:
        try:
            t = op.get("type")
            if t == "inventory_update":
                update_qty(op["id"], op["quantity"])
            elif t == "temperature":
                update_temp(op["id"], op["temperature"])
            elif t == "requirement":
                make_requirement(target_hid(), op["data"])
            else:
                raise Bad("Unknown change type.")
            db.session.commit()
            applied += 1
        except (Bad, ValueError, KeyError, TypeError, AttributeError) as e:
            db.session.rollback()
            errors.append(str(e) if isinstance(e, Bad) else "Invalid change skipped.")
        except SQLAlchemyError:
            db.session.rollback()
            errors.append("Database error.")
    return jsonify(applied=applied, errors=errors)


# ---------------------------------------------------------------- seed
@app.cli.command("seed")
def seed():
    db.create_all()

    def mk(email, pw, role, org, name, phone, addr, cats=None):
        u = User.query.filter_by(email=email).first()
        if not u:
            u = User(email=email, password_hash=generate_password_hash(pw), role=role, organization_name=org,
                     name=name, phone=phone, address=addr, categories=cats)
            db.session.add(u)
        return u

    h = mk("hospital@medicrisis.local", "hospital123", "hospital", "City General Hospital", "Dr. Asha Menon", "0484-555-0101", "12 Lake Road, Kochi")
    s = mk("supplier@medicrisis.local", "supplier123", "provider", "MediSupply Co.", "Ravi Nair", "0484-555-0202", "45 Port Avenue, Kochi", "Medicines, Vaccines, Oxygen, Blood Products")
    mk("admin@medicrisis.local", "admin123", "admin", "MediCrisis Admin", "System Admin", "0000000000", "HQ")
    db.session.commit()
    if not Inventory.query.filter_by(hospital_id=h.id).first():
        y, soon = date.today() + timedelta(days=365), date.today() + timedelta(days=20)
        rows = [("Paracetamol", "Medicines", 500, 200, "tablets", y, None),
                ("Oxygen Cylinder", "Oxygen", 50, 100, "cylinders", y, None),
                ("Insulin", "Medicines", 20, 100, "vials", soon, None),
                ("Vaccine", "Vaccines", 80, 100, "doses", y, (2, 8, 5)),
                ("Blood Bags", "Blood Products", 20, 100, "bags", y, None)]
        for n, c, q, m, u, e, cold in rows:
            i = Inventory(hospital_id=h.id, item_name=n, category=c, quantity=q, minimum_quantity=m, unit=u,
                          expiry_date=e, supplier="MediSupply Co.", batch_number=f"B-{n[:3].upper()}-01",
                          description=f"{n} for emergency use", is_cold_chain=bool(cold))
            if cold:
                i.min_temperature, i.max_temperature, i.current_temperature, i.temp_updated = *cold, datetime.now()
            db.session.add(i)
            db.session.flush()
            check_inventory(i)
        r = Requirement(hospital_id=h.id, item_name="Oxygen Cylinder", category="Oxygen", required_quantity=200,
                        current_quantity=50, priority=calc_priority(50, 200), status="Requested",
                        required_by=date.today() + timedelta(days=3), description="Emergency ward surge")
        db.session.add(r)
        db.session.flush()
        db.session.add(SupplyRequest(hospital_id=h.id, supplier_id=s.id, requirement_id=r.id, item_name="Oxygen Cylinder",
                                     requested_quantity=150, priority=r.priority, required_date=r.required_by))
        db.session.commit()
    print("Database ready. Demo users: hospital/supplier/admin @medicrisis.local")


if __name__ == "__main__":
    try:
        with app.app_context():
            db.create_all()
    except Exception as e:
        print("\nERROR: Unable to connect to MySQL. Check that MySQL is running and DATABASE_URL is set (README step 6).\n", e)
    app.run(host="127.0.0.1", port=5000, debug=False)