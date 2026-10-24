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
from sqlalchemy import inspect, text
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
app.config["SUPPLY_REQUEST_TIMEOUT_MINUTES"] = int(os.environ.get("SUPPLY_REQUEST_TIMEOUT_MINUTES", "5"))

CATEGORIES = ["Medicines", "Vaccines", "IV Fluids", "Blood Products", "Surgical Supplies",
              "PPE", "Oxygen", "Emergency Equipment", "Other"]
REQ_STATUS = ["Pending", "Requested", "Partially Fulfilled", "Fulfilled", "Cancelled"]
OPEN_REQ = ["Pending", "Requested", "Partially Fulfilled"]
SR_STATUS = ["PENDING", "ACCEPTED", "REJECTED", "PARTIALLY_FULFILLED", "FULFILLED", "RECEIVED", "TIMEOUT", "CANCELLED"]
ORDER = {"CRITICAL": 0, "LOW": 1, "NORMAL": 2}


def calc_priority(qty, minimum, expiry_date=None):
    if expiry_date and expiry_date < date.today():
        return "CRITICAL"
    if qty == 0:
        return "CRITICAL"
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
    is_active = db.Column(db.Boolean, nullable=False, default=True)
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
    is_supplier_stock = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    hospital = db.relationship("User")

    @property
    def priority(self):
        return calc_priority(self.quantity or 0, self.minimum_quantity or 0, self.expiry_date)

    @property
    def is_expired(self):
        return bool(self.expiry_date and self.expiry_date < date.today())

    @property
    def is_near_expiry(self):
        if not self.expiry_date:
            return False
        today = date.today()
        return today <= self.expiry_date <= today + timedelta(days=30)

    @property
    def priority_reason(self):
        if self.is_expired:
            return f"EXPIRED: Expired on {self.expiry_date}. Unfit for clinical use."
        qty = self.quantity or 0
        mn = self.minimum_quantity or 0
        if qty == 0:
            return "ZERO STOCK: Stockout condition. Emergency replenishment required."
        if mn > 0 and qty <= mn * 0.5:
            return f"CRITICAL: Stock ({qty} {self.unit or 'units'}) is <= 50% of minimum reserve ({mn})."
        if mn > 0 and qty <= mn:
            return f"LOW: Stock ({qty} {self.unit or 'units'}) is below minimum reserve ({mn})."
        if self.is_near_expiry:
            return f"EXPIRING SOON: Valid until {self.expiry_date}. Prioritize dispensing or lateral transfer."
        return f"NORMAL: Stock level is healthy above minimum reserve ({mn} {self.unit or 'units'})."

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
    stock_reserved = db.Column(db.Boolean, default=False, nullable=False)
    request_date = db.Column(db.DateTime, default=datetime.now)
    expires_at = db.Column(db.DateTime)
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
    expires_at = db.Column(db.DateTime)
    responded_at = db.Column(db.DateTime)
    requester = db.relationship("User", foreign_keys=[requester_hospital_id])
    provider = db.relationship("User", foreign_keys=[provider_hospital_id])
    source_inventory = db.relationship("Inventory")


# ---------------------------------------------------------------- helpers
def ensure_timeout_column():
    inspector = inspect(db.engine)
    if "supply_requests" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("supply_requests")}
    if "expires_at" not in columns:
        db.session.execute(text("ALTER TABLE supply_requests ADD COLUMN expires_at DATETIME NULL"))
        db.session.commit()


def ensure_sharing_timeout_column():
    inspector = inspect(db.engine)
    if "inter_hospital_requests" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("inter_hospital_requests")}
    if "expires_at" not in columns:
        db.session.execute(text("ALTER TABLE inter_hospital_requests ADD COLUMN expires_at DATETIME NULL"))
        db.session.commit()


def ensure_user_active_column():
    inspector = inspect(db.engine)
    if "users" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("users")}
    if "is_active" not in columns:
        db.session.execute(text("ALTER TABLE users ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE"))
        db.session.commit()


def ensure_requirements_columns():
    inspector = inspect(db.engine)
    if "requirements" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("requirements")}
    alterations = [
        ("hospital_id", "ALTER TABLE requirements ADD COLUMN hospital_id INT NOT NULL DEFAULT 1"),
        ("item_name", "ALTER TABLE requirements ADD COLUMN item_name VARCHAR(120) NOT NULL DEFAULT ''"),
        ("category", "ALTER TABLE requirements ADD COLUMN category VARCHAR(50) DEFAULT 'Other'"),
        ("required_quantity", "ALTER TABLE requirements ADD COLUMN required_quantity INT NOT NULL DEFAULT 1"),
        ("current_quantity", "ALTER TABLE requirements ADD COLUMN current_quantity INT NOT NULL DEFAULT 0"),
        ("priority", "ALTER TABLE requirements ADD COLUMN priority VARCHAR(10) DEFAULT 'NORMAL'"),
        ("required_by", "ALTER TABLE requirements ADD COLUMN required_by DATE NULL"),
        ("description", "ALTER TABLE requirements ADD COLUMN description TEXT NULL"),
        ("status", "ALTER TABLE requirements ADD COLUMN status VARCHAR(30) DEFAULT 'Pending'"),
        ("created_at", "ALTER TABLE requirements ADD COLUMN created_at DATETIME NULL"),
    ]
    for col, alter_sql in alterations:
        if col not in columns:
            try:
                db.session.execute(text(alter_sql))
                db.session.commit()
            except SQLAlchemyError:
                db.session.rollback()


def ensure_supplier_stock_columns():
    inspector = inspect(db.engine)
    if "inventory" in inspector.get_table_names():
        inv_cols = {c["name"] for c in inspector.get_columns("inventory")}
        if "is_supplier_stock" not in inv_cols:
            try:
                db.session.execute(text("ALTER TABLE inventory ADD COLUMN is_supplier_stock BOOLEAN NOT NULL DEFAULT FALSE"))
                db.session.commit()
            except SQLAlchemyError:
                db.session.rollback()
    if "supply_requests" in inspector.get_table_names():
        sr_cols = {c["name"] for c in inspector.get_columns("supply_requests")}
        if "stock_reserved" not in sr_cols:
            try:
                db.session.execute(text("ALTER TABLE supply_requests ADD COLUMN stock_reserved BOOLEAN NOT NULL DEFAULT FALSE"))
                db.session.commit()
            except SQLAlchemyError:
                db.session.rollback()


def expire_supply_requests():
    now = datetime.now()
    changed = False
    for item in SupplyRequest.query.filter_by(status="PENDING").all():
        if item.expires_at is None:
            start = item.request_date or item.created_at or now
            item.expires_at = start + timedelta(minutes=app.config["SUPPLY_REQUEST_TIMEOUT_MINUTES"])
            changed = True
        if item.expires_at <= now:
            item.status = "TIMEOUT"
            for notice in Alert.query.filter_by(hospital_id=item.hospital_id, alert_type="PENDING_RESPONSE", is_read=False).all():
                if item.item_name in notice.message and item.supplier.org in notice.message:
                    notice.is_read = True
            if item.requirement and item.requirement.status == "Requested":
                other_active = SupplyRequest.query.filter(
                    SupplyRequest.requirement_id == item.requirement_id,
                    SupplyRequest.id != item.id,
                    SupplyRequest.status.in_(["PENDING", "ACCEPTED", "PARTIALLY_FULFILLED"]),
                ).first()
                if not other_active:
                    item.requirement.status = "Pending"
            add_alert(item.hospital_id, None, "REQUEST_TIMEOUT", "WARNING",
                      f"No response received from {item.supplier.org} for {item.item_name}; the request timed out.")
            changed = True
    if changed:
        db.session.commit()


def expire_inter_hospital_requests():
    now = datetime.now()
    changed = False
    for sharing in InterHospitalRequest.query.filter_by(status="PENDING").all():
        if sharing.expires_at is None:
            sharing.expires_at = (sharing.created_at or now) + timedelta(
                minutes=app.config["SUPPLY_REQUEST_TIMEOUT_MINUTES"])
            changed = True
        if sharing.expires_at <= now:
            sharing.status = "TIMEOUT"
            sharing.responded_at = now
            add_alert(sharing.requester_hospital_id, None, "SHARING_TIMEOUT", "WARNING",
                      f"No response received from {sharing.provider.org} for sharing request #{sharing.id} ({sharing.item_name}); the request timed out.")
            add_alert(sharing.provider_hospital_id, sharing.source_inventory_id, "SHARING_TIMEOUT", "INFO",
                      f"Sharing request #{sharing.id} from {sharing.requester.org} for {sharing.item_name} timed out without a response.")
            changed = True
    if changed:
        db.session.commit()


def badge(text, cls=None):
    slug = (cls or str(text)).lower().replace(" ", "-").replace("_", "-")
    return Markup('<span class="badge %s">%s</span>') % (slug, text)


app.jinja_env.globals["badge"] = badge
app.jinja_env.globals["today"] = lambda: date.today().isoformat()

HN = [("Dashboard", "hospital_dashboard"), ("Inventory", "inventory"),
      ("Emergency Requirements", "requirements"), ("Supply Requests", "requests_page"),
      ("Alerts", "alerts"), ("Cold Chain", "cold_chain"), ("Inter-Hospital Sharing", "inter_hospital_sharing"),
      ("Reports", "reports"), ("Profile", "profile")]
NAV = {"hospital": HN,
       "provider": [("Dashboard", "provider_dashboard"), ("Requests", "provider_requests"), ("Profile", "profile")],
       "admin": [("Admin Dashboard", "admin_dashboard"), ("Hospital Dashboard", "hospital_dashboard"),
                 ("Supplier Dashboard", "provider_dashboard"),
                 ("Supplier Requests", "admin_supplier_requests"),
                 ("Inter-Hospital Sharing", "admin_sharing_requests"),
                 ("User Management", "admin_users"), ("Profile", "profile")]}
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
            if g.user.role == "admin" and request.endpoint not in {
                "admin_dashboard", "admin_users", "admin_user_edit", "admin_user_status",
                "admin_supplier_requests", "admin_sharing_requests", "profile",
                "hospital_dashboard", "provider_dashboard", "logout", "static", "service_worker",
                "requirements", "requirement_add", "requirement_edit", "requirement_delete", "requirement_status",
                "requests_page", "supply_request_edit", "supply_request_receive", "supply_request_cancel", "supply_request_delete",
                "reports", "cold_chain", "cold_chain_update", "inventory", "inventory_add", "inventory_edit", "inventory_delete", "alerts", "alerts_read"
            }:
                flash("Admin access is limited to dashboards and user management.", "error")
                return redirect(url_for("admin_dashboard"))
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
    if i.quantity == 0:
        add_alert(i.hospital_id, i.id, "CRITICAL_STOCK", "CRITICAL",
                  f"CRITICAL: {n} is completely out of stock (0 available).")
    elif p == "CRITICAL":
        add_alert(i.hospital_id, i.id, "CRITICAL_STOCK", "CRITICAL",
                  f"CRITICAL: {n} stock ({i.quantity}) is below 50% of the minimum required quantity ({i.minimum_quantity}).")
    elif p == "LOW":
        add_alert(i.hospital_id, i.id, "LOW_STOCK", "WARNING",
                  f"LOW: {n} stock ({i.quantity}) is below the minimum required quantity ({i.minimum_quantity}).")
    if i.expiry_date:
        if i.expiry_date < date.today():
            add_alert(i.hospital_id, i.id, "EXPIRY", "CRITICAL",
                      f"CRITICAL: {n} expired on {i.expiry_date}. Must be quarantined and not dispensed.")
        elif i.expiry_date <= date.today() + timedelta(days=30):
            add_alert(i.hospital_id, i.id, "EXPIRY", "WARNING",
                      f"WARNING: {n} expires soon ({i.expiry_date}).")
    if i.cold_status == "VIOLATION":
        add_alert(i.hospital_id, i.id, "TEMPERATURE", "CRITICAL",
                  f"CRITICAL: {n} temperature is {i.current_temperature:g}°C. "
                  f"Safe range is {i.min_temperature:g}°C–{i.max_temperature:g}°C.")


def refresh_alerts():
    for i in scoped(Inventory).filter_by(is_supplier_stock=False).all():
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
    if not v:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if not s or s.lower() in ("none", "null", "undefined", "0000-00-00"):
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    try:
        return date.fromisoformat(s[:10])
    except (ValueError, TypeError):
        return None


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


def fill_requirement(r, hid, d):
    name = (d.get("item_name") or "").strip()
    if not name:
        raise Bad("Item name is required.")
    cat = (d.get("category") or "Other").strip()
    if cat not in CATEGORIES:
        raise Bad("Choose a valid category.")

    req_raw = d.get("required_quantity")
    try:
        need = int(float(str(req_raw).strip())) if req_raw not in (None, "") else 0
    except (TypeError, ValueError):
        raise Bad("Required quantity must be a valid number.")
    if need <= 0:
        raise Bad("Required quantity must be above zero.")

    cur_raw = d.get("current_quantity")
    if cur_raw in (None, ""):
        m = Inventory.query.filter_by(hospital_id=hid, item_name=name).first()
        cur = int(m.quantity) if (m and m.quantity is not None) else 0
    else:
        try:
            cur = int(float(str(cur_raw).strip()))
        except (TypeError, ValueError):
            raise Bad("Current quantity must be a valid number.")
    if cur < 0:
        raise Bad("Current quantity cannot be negative.")

    status = (d.get("status") or "Pending").strip()
    if status not in REQ_STATUS:
        status = "Pending"
    r.item_name = name[:120]
    r.category = cat[:50]
    r.required_quantity = need
    r.current_quantity = cur
    r.priority = calc_priority(cur, need)
    req_date = to_date(d.get("required_by"))
    if req_date and req_date < date.today():
        raise Bad("Required by date must be today or a future date.")
    r.required_by = req_date
    r.description = (d.get("description") or "").strip()
    r.status = status
    if not getattr(r, "created_at", None):
        r.created_at = datetime.now()
    return r


def make_requirement(hid, d):
    r = Requirement(hospital_id=hid, created_at=datetime.now())
    fill_requirement(r, hid, d)
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
    try:
        t = float(temp)
    except (TypeError, ValueError):
        raise Bad("Temperature reading must be a valid number.")
    if t < -50.0 or t > 50.0:
        raise Bad("Temperature reading out of plausible medical range (-50°C to +50°C).")
    i.current_temperature, i.temp_updated = t, datetime.now()
    check_inventory(i)
    return i


def inv_dict(i):
    return dict(id=i.id, item_name=i.item_name, category=i.category, quantity=i.quantity,
                minimum_quantity=i.minimum_quantity, unit=i.unit, priority=i.priority, pct=i.pct,
                priority_reason=i.priority_reason, is_expired=i.is_expired, is_near_expiry=i.is_near_expiry,
                expiry_date=str(i.expiry_date or ""),
                is_cold_chain=bool(i.is_cold_chain), cold_status=i.cold_status,
                current_temperature=i.current_temperature)


def req_dict(r):
    return dict(id=r.id, item_name=r.item_name, category=r.category, required_quantity=r.required_quantity,
                current_quantity=r.current_quantity, shortfall=r.shortfall, priority=r.priority,
                status=r.status, required_by=str(r.required_by or ""))


def calculate_medicine_demand(hospital_id=None):
    """
    Transparent, rule-based calculation of medicine demand from historical usage data
    without AI or machine learning.
    Uses existing requirements, fulfilled supply requests, and completed inter-hospital transfers.
    """
    q_inv = Inventory.query.filter_by(is_supplier_stock=False)
    if hospital_id:
        q_inv = q_inv.filter_by(hospital_id=hospital_id)
    items = q_inv.order_by(Inventory.item_name).all()

    demand_estimates = []
    for it in items:
        req_q = Requirement.query.filter(Requirement.item_name.ilike(it.item_name))
        if hospital_id:
            req_q = req_q.filter_by(hospital_id=hospital_id)
        reqs = req_q.all()
        active_shortfall = sum(r.shortfall for r in reqs if r.status in OPEN_REQ)

        sr_q = SupplyRequest.query.filter(
            SupplyRequest.item_name.ilike(it.item_name),
            SupplyRequest.status.in_(["FULFILLED", "RECEIVED"])
        )
        if hospital_id:
            sr_q = sr_q.filter_by(hospital_id=hospital_id)
        fulfilled_sr = sr_q.all()
        total_supplied = sum(sr.supplied_quantity or sr.requested_quantity for sr in fulfilled_sr)

        ih_in = InterHospitalRequest.query.filter(
            InterHospitalRequest.item_name.ilike(it.item_name),
            InterHospitalRequest.status == "COMPLETED"
        )
        if hospital_id:
            ih_in = ih_in.filter(InterHospitalRequest.requester_hospital_id == hospital_id)
        transfers_in = ih_in.all()
        total_transferred_in = sum(tr.requested_quantity for tr in transfers_in)

        total_tx = len(fulfilled_sr) + len(transfers_in) + len(reqs)
        deficit_to_minimum = max(0, (it.minimum_quantity or 0) - (it.quantity or 0))

        replenishment_baseline = 0
        if fulfilled_sr or transfers_in:
            replenishment_baseline = round((total_supplied + total_transferred_in) / max(1, len(fulfilled_sr) + len(transfers_in)))

        projected_demand = active_shortfall + deficit_to_minimum + replenishment_baseline
        is_insufficient = total_tx < 2
        note = (
            f"Insufficient historical records (only {total_tx} transaction(s) logged). "
            f"Projection represents current reserve shortfall of {deficit_to_minimum} {it.unit or 'units'}."
            if is_insufficient else
            f"Calculated from {total_tx} historical records: {active_shortfall} shortfall + {deficit_to_minimum} reserve deficit + {replenishment_baseline} avg replenishment."
        )

        demand_estimates.append({
            "item_name": it.item_name,
            "category": it.category,
            "current_stock": it.quantity,
            "minimum_quantity": it.minimum_quantity,
            "unit": it.unit or "units",
            "active_shortfall": active_shortfall,
            "deficit_to_minimum": deficit_to_minimum,
            "replenishment_baseline": replenishment_baseline,
            "projected_demand": projected_demand,
            "total_tx": total_tx,
            "is_insufficient": is_insufficient,
            "note": note,
            "priority": it.priority
        })
    return demand_estimates


def get_near_expiry_recommendations(hid):
    """
    Identifies eligible near-expiry medicines for hospital `hid` that could be
    transferred to other hospitals with active emergency requirements or shortages.
    Strictly excludes expired medicines.
    """
    today = date.today()
    near_expiry_items = Inventory.query.filter(
        Inventory.hospital_id == hid,
        Inventory.is_supplier_stock == False,
        Inventory.quantity > 0,
        Inventory.expiry_date.isnot(None),
        Inventory.expiry_date >= today,
        Inventory.expiry_date <= today + timedelta(days=60)
    ).all()

    recommendations = []
    for it in near_expiry_items:
        other_reqs = Requirement.query.filter(
            Requirement.hospital_id != hid,
            Requirement.item_name.ilike(it.item_name),
            Requirement.status.in_(OPEN_REQ)
        ).all()

        for r in other_reqs:
            recommendations.append({
                "item": it,
                "target_hospital": r.hospital,
                "target_requirement": r,
                "recommended_qty": min(it.quantity, r.shortfall or r.required_quantity),
                "days_left": (it.expiry_date - today).days,
                "reason": f"Expiring in {(it.expiry_date - today).days} days ({it.expiry_date}). {r.hospital.org} has an open requirement for {r.shortfall or r.required_quantity} {it.unit or 'units'}."
            })
    return recommendations


_MIGRATIONS_CHECKED = False

# ---------------------------------------------------------------- hooks
@app.before_request
def load_user():
    global _MIGRATIONS_CHECKED
    if not _MIGRATIONS_CHECKED:
        try:
            ensure_timeout_column()
            ensure_sharing_timeout_column()
            ensure_user_active_column()
            ensure_requirements_columns()
            ensure_supplier_stock_columns()
            _MIGRATIONS_CHECKED = True
        except Exception:
            pass
    g.user = None
    if request.endpoint in ("static", "service_worker"):
        return
    uid = session.get("uid")
    if uid:
        g.user = db.session.get(User, uid)
        if g.user and not g.user.is_active:
            g.user = None
            session.clear()


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
@app.route("/home")
def index():
    return render_template("index.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email, pw, role = request.form.get("email", "").strip().lower(), request.form.get("password", ""), request.form.get("role")
        u = User.query.filter_by(email=email).first()
        if u and u.is_active and u.role == role and check_password_hash(u.password_hash, pw):
            session.clear()
            session["uid"] = u.id
            return redirect(url_for(HOME[u.role]))
        flash("Invalid email or password.", "error")
    else:
        if session.get("uid"):
            session.clear()
            g.user = None
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
    if g.user.role != "admin":
        expire_supply_requests()
        refresh_alerts()
    inv = scoped(Inventory).filter_by(is_supplier_stock=False).all()
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

    expired_items = [i for i in inv if i.is_expired]
    near_expiry_items = [i for i in inv if i.is_near_expiry and not i.is_expired]
    pending_supply_requests = scoped(SupplyRequest).filter_by(status="PENDING").order_by(SupplyRequest.request_date.desc()).all()

    hid = target_hid() if g.user else None
    if hid:
        pending_sharing_sent = InterHospitalRequest.query.filter_by(requester_hospital_id=hid, status="PENDING").all()
        pending_sharing_in = InterHospitalRequest.query.filter_by(provider_hospital_id=hid, status="PENDING").all()
        near_expiry_recommendations = get_near_expiry_recommendations(hid)
        demand_estimates = calculate_medicine_demand(hid)
    else:
        pending_sharing_sent = []
        pending_sharing_in = []
        near_expiry_recommendations = []
        demand_estimates = calculate_medicine_demand()

    cold_alerts = [i for i in cold if i.cold_status in ("VIOLATION", "WARNING")]

    return {
        "stats": stats,
        "charts": charts,
        "inv": inv,
        "reqs": reqs,
        "expired_items": expired_items,
        "near_expiry_items": near_expiry_items,
        "pending_supply_requests": pending_supply_requests,
        "pending_sharing_sent": pending_sharing_sent,
        "pending_sharing_in": pending_sharing_in,
        "near_expiry_recommendations": near_expiry_recommendations,
        "cold_alerts": cold_alerts,
        "demand_estimates": demand_estimates
    }


@app.route("/hospital/dashboard")
@login_required("hospital", "admin")
def hospital_dashboard():
    ctx = dash_data()
    return render_template("hospital_dashboard.html", **ctx, report=False)


@app.route("/hospital/reports")
@login_required("hospital", "admin")
def reports():
    ctx = dash_data()
    return render_template("hospital_dashboard.html", **ctx, report=True)


@app.route("/hospital/inventory")
@login_required("hospital", "admin")
def inventory():
    refresh_alerts()
    q = scoped(Inventory).filter_by(is_supplier_stock=False)
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
        inv = item or Inventory(hospital_id=target_hid(), is_supplier_stock=False)
        fill_inventory(inv, request.form)
        inv.is_supplier_stock = False
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
        except (Bad, ValueError, TypeError) as e:
            db.session.rollback()
            flash(str(e) if isinstance(e, Bad) else "Please check the numbers and dates you entered.", "error")
        except SQLAlchemyError as e:
            db.session.rollback()
            app.logger.exception("Unable to save emergency requirement: %s", e)
            err_msg = getattr(e, "orig", e)
            flash(f"Database error while saving requirement: {err_msg}", "error")
        except Exception as e:
            db.session.rollback()
            app.logger.exception("Unexpected error saving requirement: %s", e)
            flash(f"Unable to save emergency requirement: {e}", "error")
    return render_template("requirement_form.html", d=d, categories=CATEGORIES, statuses=REQ_STATUS, min_date=date.today().isoformat())


@app.route("/hospital/requirements/edit/<int:id>", methods=["GET", "POST"])
@login_required("hospital", "admin")
def requirement_edit(id):
    r = scoped(Requirement).filter_by(id=id).first_or_404()
    d = request.form if request.method == "POST" else {
        "item_name": r.item_name, "category": r.category,
        "required_quantity": str(r.required_quantity), "current_quantity": str(r.current_quantity),
        "required_by": str(r.required_by or ""), "status": r.status, "description": r.description or ""}
    if request.method == "POST":
        try:
            fill_requirement(r, r.hospital_id, d)
            db.session.commit()
            flash("Emergency requirement updated.", "ok")
            return redirect(url_for("requirements"))
        except (Bad, ValueError, TypeError) as e:
            db.session.rollback()
            r = scoped(Requirement).filter_by(id=id).first_or_404()
            flash(str(e) if isinstance(e, Bad) else "Please check the numbers and dates you entered.", "error")
        except SQLAlchemyError as e:
            db.session.rollback()
            r = scoped(Requirement).filter_by(id=id).first_or_404()
            app.logger.exception("Unable to update emergency requirement: %s", e)
            err_msg = getattr(e, "orig", e)
            flash(f"Database error while updating requirement: {err_msg}", "error")
        except Exception as e:
            db.session.rollback()
            r = scoped(Requirement).filter_by(id=id).first_or_404()
            app.logger.exception("Unexpected error updating requirement: %s", e)
            flash(f"Unable to update emergency requirement: {e}", "error")
    return render_template("requirement_form.html", d=d, categories=CATEGORIES, statuses=REQ_STATUS, editing=True, min_date=date.today().isoformat())


@app.route("/hospital/requirements/delete/<int:id>", methods=["POST"])
@login_required("hospital", "admin")
def requirement_delete(id):
    try:
        r = scoped(Requirement).filter_by(id=id).first_or_404()
        SupplyRequest.query.filter_by(requirement_id=r.id).update({SupplyRequest.requirement_id: None}, synchronize_session=False)
        db.session.delete(r)
        db.session.commit()
        flash("Emergency requirement deleted.", "ok")
    except SQLAlchemyError as e:
        db.session.rollback()
        app.logger.exception("Unable to delete requirement: %s", e)
        err_msg = getattr(e, "orig", e)
        flash(f"Database error while deleting requirement: {err_msg}", "error")
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Unexpected error deleting requirement: %s", e)
        flash(f"Unable to delete requirement: {e}", "error")
    return redirect(url_for("requirements"))


@app.route("/hospital/requirements/status/<int:id>", methods=["POST"])
@login_required("hospital", "admin")
def requirement_status(id):
    try:
        r = scoped(Requirement).filter_by(id=id).first_or_404()
        if request.form.get("status") in REQ_STATUS:
            r.status = request.form["status"]
            db.session.commit()
            flash("Status updated.", "ok")
    except SQLAlchemyError as e:
        db.session.rollback()
        app.logger.exception("Unable to update requirement status: %s", e)
        err_msg = getattr(e, "orig", e)
        flash(f"Database error while updating status: {err_msg}", "error")
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Unexpected error updating status: %s", e)
        flash(f"Unable to update status: {e}", "error")
    return redirect(url_for("requirements"))


@app.route("/hospital/requests", methods=["GET", "POST"])
@login_required("hospital", "admin")
def requests_page():
    if request.method == "POST":
        f = request.form
        try:
            hid = target_hid()
            sup_id = int(f.get("supplier_id") or 0)
            sup = User.query.filter_by(id=sup_id, role="provider", is_active=True).first()
            if not sup:
                raise Bad("Choose an active registered supplier.")

            req = Requirement.query.filter_by(id=int(f["requirement_id"]), hospital_id=hid).first() if f.get("requirement_id") else None
            name = (f.get("item_name") or (req.item_name if req else "")).strip()
            if not name:
                raise Bad("Item name is required.")

            try:
                raw_qty = f.get("requested_quantity") or (req.shortfall if req else 0)
                qty = int(raw_qty)
            except (ValueError, TypeError):
                raise Bad("Requested quantity must be a positive integer.")
            if qty <= 0:
                raise Bad("Requested quantity must be greater than zero.")

            # Validate against supplier's available stock
            supplier_stock = Inventory.query.filter_by(hospital_id=sup.id, item_name=name, is_supplier_stock=True).first()
            if supplier_stock and qty > supplier_stock.quantity:
                raise Bad(f"Requested quantity ({qty}) exceeds available stock ({supplier_stock.quantity} {supplier_stock.unit or 'units'}) from {sup.org}.")

            # Prevent duplicate pending submissions for the same item from the same supplier
            dup = SupplyRequest.query.filter_by(hospital_id=hid, supplier_id=sup.id, item_name=name, status="PENDING").first()
            if dup:
                raise Bad(f"A pending supply request (#{dup.id}) for '{name}' with {sup.org} already exists. Please wait for their response or manage your existing request.")

            inv = Inventory.query.filter_by(hospital_id=hid, item_name=name, is_supplier_stock=False).first()
            pr = req.priority if req else (inv.priority if inv else "NORMAL")

            sr = SupplyRequest(hospital_id=hid, supplier_id=sup.id, requirement_id=req.id if req else None,
                               item_name=name, requested_quantity=qty, priority=pr,
                               status="PENDING", stock_reserved=False,
                               request_date=datetime.now(),
                               expires_at=datetime.now() + timedelta(minutes=app.config["SUPPLY_REQUEST_TIMEOUT_MINUTES"]),
                               required_date=to_date(f.get("required_date")))
            db.session.add(sr)
            if req and req.status == "Pending":
                req.status = "Requested"
            db.session.flush()

            add_alert(sup.id, supplier_stock.id if supplier_stock else None, "NEW_SUPPLY_REQUEST", "INFO",
                      f"New supply request #{sr.id} from {g.user.org} for {qty} units of {name}.")
            add_alert(hid, None, "PENDING_RESPONSE", "INFO",
                      f"Awaiting response from {sup.org} for {name} ({qty}).")

            db.session.commit()
            flash(f"Supply request for {qty} units of {name} sent successfully to {sup.org}.", "ok")
        except (Bad, ValueError) as e:
            db.session.rollback()
            flash(str(e) if isinstance(e, Bad) else "Please check the values you entered.", "error")
        except SQLAlchemyError:
            db.session.rollback()
            flash("Unable to send request.", "error")
        return redirect(url_for("requests_page"))

    expire_supply_requests()
    refresh_alerts()

    # Search supplier stock logic
    search_q = request.args.get("q", "").strip()
    search_cat = request.args.get("category", "").strip()
    qty_raw = request.args.get("needed_qty", "").strip()
    try:
        needed_qty = int(qty_raw) if qty_raw else None
        if needed_qty is not None and needed_qty <= 0:
            needed_qty = None
    except (ValueError, TypeError):
        needed_qty = None

    searched = bool(search_q or search_cat or qty_raw)
    supplier_stocks = []
    if searched:
        stock_q = Inventory.query.join(User, Inventory.hospital_id == User.id)\
            .filter(Inventory.is_supplier_stock == True,
                    User.role == "provider",
                    User.is_active == True,
                    Inventory.quantity > 0)
        if search_q:
            stock_q = stock_q.filter(Inventory.item_name.ilike(f"%{search_q}%"))
        if search_cat:
            stock_q = stock_q.filter(Inventory.category == search_cat)
        supplier_stocks = stock_q.order_by(Inventory.item_name.asc(), Inventory.quantity.desc()).all()
        for item in supplier_stocks:
            item.is_sufficient = (item.quantity >= needed_qty) if needed_qty else True
            item.is_partial = (item.quantity < needed_qty) if needed_qty else False

    rows = scoped(SupplyRequest).order_by(SupplyRequest.request_date.desc()).all()
    reqs = [r for r in scoped(Requirement).all() if r.status in OPEN_REQ]
    suppliers = User.query.filter_by(role="provider", is_active=True).order_by(User.organization_name).all()
    return render_template("requests.html", rows=rows, reqs=reqs, suppliers=suppliers,
                           categories=CATEGORIES, supplier_stocks=supplier_stocks,
                           searched=searched, search_q=search_q, search_cat=search_cat,
                           needed_qty=needed_qty, prefill=request.args)


@app.route("/hospital/requests/<int:id>/edit", methods=["GET", "POST"])
@login_required("hospital")
def supply_request_edit(id):
    r = SupplyRequest.query.filter_by(id=id, hospital_id=g.user.id).first_or_404()
    expire_supply_requests()
    if r.status != "PENDING":
        flash("Only pending requests can be edited.", "error")
        return redirect(url_for("requests_page"))
    suppliers = User.query.filter_by(role="provider").all()
    if request.method == "POST":
        try:
            supplier = User.query.filter_by(id=int(request.form.get("supplier_id") or 0), role="provider").first()
            name = (request.form.get("item_name") or "").strip()
            quantity = int(request.form.get("requested_quantity") or 0)
            if not supplier or not name or quantity <= 0:
                raise Bad("Choose a supplier, enter an item, and enter a quantity above zero.")
            if r.requirement_id:
                name = r.item_name
            r.supplier_id, r.item_name, r.requested_quantity = supplier.id, name[:120], quantity
            r.required_date = to_date(request.form.get("required_date"))
            r.expires_at = datetime.now() + timedelta(minutes=app.config["SUPPLY_REQUEST_TIMEOUT_MINUTES"])
            db.session.commit()
            flash("Supply request updated.", "ok")
            return redirect(url_for("requests_page"))
        except (Bad, ValueError) as e:
            db.session.rollback()
            r = SupplyRequest.query.filter_by(id=id, hospital_id=g.user.id).first_or_404()
            flash(str(e) if isinstance(e, Bad) else "Enter a valid quantity and date.", "error")
        except SQLAlchemyError:
            db.session.rollback()
            r = SupplyRequest.query.filter_by(id=id, hospital_id=g.user.id).first_or_404()
            flash("Unable to update supply request.", "error")
    return render_template("supply_request_form.html", r=r, suppliers=suppliers)


@app.route("/hospital/requests/<int:id>/receive", methods=["POST"])
@login_required("hospital")
def supply_request_receive(id):
    r = SupplyRequest.query.filter_by(id=id, hospital_id=g.user.id).with_for_update().first_or_404()
    if r.status != "FULFILLED":
        flash("Only fulfilled requests can be received into inventory.", "error")
        return redirect(url_for("requests_page"))
    quantity = r.supplied_quantity or 0
    if quantity <= 0:
        flash("The supplier has not recorded a positive supplied quantity.", "error")
        return redirect(url_for("requests_page"))
    query = Inventory.query.filter_by(hospital_id=g.user.id, item_name=r.item_name, is_supplier_stock=False)
    if r.requirement and r.requirement.category:
        query = query.filter_by(category=r.requirement.category)
    matches = query.with_for_update().all()
    if len(matches) != 1:
        flash("Create one matching inventory item before receiving this stock." if not matches else
              "Multiple matching inventory items exist; merge or correct them before receiving stock.", "error")
        return redirect(url_for("requests_page"))
    item = matches[0]
    item.quantity += quantity
    check_inventory(item)
    if r.requirement:
        r.requirement.current_quantity += quantity
        r.requirement.priority = calc_priority(r.requirement.current_quantity, r.requirement.required_quantity)
    r.status = "RECEIVED"
    add_alert(g.user.id, item.id, "SUPPLY_RECEIVED", "INFO",
              f"Received {quantity} units of {r.item_name} from {r.supplier.org}; inventory was updated.")
    try:
        db.session.commit()
        flash(f"Received {quantity} units of {r.item_name}; inventory was updated.", "ok")
    except SQLAlchemyError:
        db.session.rollback()
        flash("Unable to record receipt; inventory was not changed.", "error")
    return redirect(url_for("requests_page"))


@app.route("/hospital/requests/<int:id>/cancel", methods=["POST"])
@login_required("hospital")
def supply_request_cancel(id):
    r = SupplyRequest.query.filter_by(id=id, hospital_id=g.user.id).first_or_404()
    expire_supply_requests()
    if r.status != "PENDING":
        flash("Only pending requests can be cancelled.", "error")
    else:
        if getattr(r, "stock_reserved", False):
            s_inv = Inventory.query.filter_by(hospital_id=r.supplier_id, item_name=r.item_name, is_supplier_stock=True).with_for_update().first()
            if s_inv:
                s_inv.quantity += (r.supplied_quantity or r.requested_quantity)
            r.stock_reserved = False
        r.status = "CANCELLED"
        if r.requirement and r.requirement.status == "Requested":
            other_active = SupplyRequest.query.filter(
                SupplyRequest.requirement_id == r.requirement_id,
                SupplyRequest.id != r.id,
                SupplyRequest.status.in_(["PENDING", "ACCEPTED", "PARTIALLY_FULFILLED"]),
            ).first()
            if not other_active:
                r.requirement.status = "Pending"
        db.session.commit()
        flash("Supply request cancelled.", "ok")
    return redirect(url_for("requests_page"))


@app.route("/hospital/requests/<int:id>/delete", methods=["POST"])
@login_required("hospital")
def supply_request_delete(id):
    r = SupplyRequest.query.filter_by(id=id, hospital_id=g.user.id).first_or_404()
    if r.status not in ("REJECTED", "TIMEOUT", "CANCELLED"):
        flash("Only rejected, timed-out, or cancelled requests can be deleted. Active requests and completed deliveries are kept.", "error")
    else:
        db.session.delete(r)
        db.session.commit()
        flash("Rejected request deleted.", "ok")
    return redirect(url_for("requests_page"))


@app.route("/hospital/inter-hospital-sharing", methods=["GET", "POST"])
@login_required("hospital")
def inter_hospital_sharing():
    hid = g.user.id
    expire_inter_hospital_requests()
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
                source = Inventory.query.filter_by(id=source_id, hospital_id=provider_id).with_for_update().first()
                if not provider or provider.id == hid:
                    raise Bad("Choose another hospital as the provider.")
                if not source:
                    raise Bad("Choose an inventory item that still belongs to that hospital.")
                if source.expiry_date and source.expiry_date < date.today():
                    raise Bad("Cannot request or transfer expired medicines.")
                dup_req = InterHospitalRequest.query.filter_by(
                    requester_hospital_id=hid,
                    provider_hospital_id=provider.id,
                    source_inventory_id=source.id,
                    status="PENDING"
                ).first()
                if dup_req:
                    raise Bad(f"A pending sharing request (#{dup_req.id}) for '{source.item_name}' already exists with {provider.org}.")
                if quantity <= 0 or len(reason) < 3 or priority not in ("CRITICAL", "LOW", "NORMAL"):
                    raise Bad("Enter a positive quantity, a reason, and a valid priority.")
                insufficient = source.quantity < quantity
                sharing = InterHospitalRequest(
                    requester_hospital_id=hid, provider_hospital_id=provider.id,
                    source_inventory_id=source.id, item_name=source.item_name,
                    category=source.category, unit=source.unit, batch_number=source.batch_number,
                    requested_quantity=quantity, reason=reason[:2000], priority=priority,
                    status="FAILED" if insufficient else "PENDING",
                    responded_at=datetime.now() if insufficient else None,
                    expires_at=(datetime.now() + timedelta(minutes=app.config["SUPPLY_REQUEST_TIMEOUT_MINUTES"])) if not insufficient else None)
                db.session.add(sharing)
                db.session.flush()
                if insufficient:
                    add_alert(hid, None, "SHARING_INSUFFICIENT", "CRITICAL",
                              f"EMERGENCY: Sharing request #{sharing.id} was automatically rejected. {provider.org} has only {source.quantity} {source.unit or 'units'} of {source.item_name}; {quantity} were requested. No stock was transferred.")
                    add_alert(provider.id, source.id, "SHARING_INSUFFICIENT", "CRITICAL",
                              f"EMERGENCY: Sharing request #{sharing.id} was automatically rejected because only {source.quantity} {source.unit or 'units'} of {source.item_name} are available; {quantity} were requested.")
                    flash("Insufficient stock. The request was automatically rejected and both hospitals were alerted.", "error")
                else:
                    add_alert(provider.id, None, "INTER_HOSPITAL_REQUEST", "INFO",
                              f"Sharing request #{sharing.id} from {g.user.org} for {quantity} {source.unit or 'units'} of {source.item_name}.")
                    flash("Sharing request sent.", "ok")
                db.session.commit()
            elif action == "delete":
                sharing_id = int(request.form.get("request_id") or 0)
                sharing = InterHospitalRequest.query.filter_by(
                    id=sharing_id, requester_hospital_id=hid).with_for_update().first()
                if not sharing:
                    raise Bad("Only your own sent requests can be changed.")
                if sharing.status == "PENDING":
                    sharing.status = "CANCELLED"
                    sharing.responded_at = datetime.now()
                    add_alert(sharing.provider_hospital_id, sharing.source_inventory_id, "INTER_HOSPITAL_CANCELLED", "INFO",
                              f"Hospital {g.user.org} cancelled sharing request #{sharing.id} for {sharing.item_name}.")
                    db.session.commit()
                    flash("Pending sharing request cancelled.", "ok")
                elif sharing.status in ("REJECTED", "FAILED", "CANCELLED", "TIMEOUT"):
                    db.session.delete(sharing)
                    db.session.commit()
                    flash("Sharing request deleted.", "ok")
                else:
                    raise Bad("Completed transfers are kept in history and cannot be deleted.")
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
                    if source and source.expiry_date and source.expiry_date < date.today():
                        sharing.status = "FAILED"
                        sharing.responded_at = datetime.now()
                        db.session.commit()
                        flash(f"Cannot complete transfer: medicine '{sharing.item_name}' has expired. Request marked as FAILED.", "error")
                        return redirect(url_for("inter_hospital_sharing"))
                    if not source or source.quantity < sharing.requested_quantity:
                        available = source.quantity if source else 0
                        sharing.status = "FAILED"
                        sharing.responded_at = datetime.now()
                        # Save the terminal status before writing alerts so an alert error
                        # cannot roll the request back to PENDING.
                        db.session.commit()
                        try:
                            add_alert(sharing.requester_hospital_id, None, "SHARING_INSUFFICIENT", "CRITICAL",
                                      f"EMERGENCY: Sharing request #{sharing.id} was automatically rejected. {g.user.org} has only {available} {sharing.unit or 'units'} of {sharing.item_name}; {sharing.requested_quantity} were requested. No stock was transferred.")
                            add_alert(sharing.provider_hospital_id, source.id if source else None, "SHARING_INSUFFICIENT", "WARNING",
                                      f"Sharing request #{sharing.id} was automatically rejected because only {available} {sharing.unit or 'units'} of {sharing.item_name} are available; {sharing.requested_quantity} were requested.")
                            db.session.commit()
                        except SQLAlchemyError:
                            db.session.rollback()
                        flash("Insufficient stock. The request status is FAILED for both hospitals.", "error")
                        return redirect(url_for("inter_hospital_sharing"))
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

    pending = InterHospitalRequest.query.filter_by(status="PENDING").filter(
        (InterHospitalRequest.requester_hospital_id == hid) |
        (InterHospitalRequest.provider_hospital_id == hid)).with_for_update().all()
    auto_rejected = []
    for sharing in pending:
        source = Inventory.query.filter_by(
            id=sharing.source_inventory_id, hospital_id=sharing.provider_hospital_id).with_for_update().first()
        available = source.quantity if source else 0
        if available < sharing.requested_quantity:
            sharing.status = "FAILED"
            sharing.responded_at = datetime.now()
            auto_rejected.append((sharing, source, available))
    if auto_rejected:
        # Persist every failed request before attempting alert inserts.
        db.session.commit()
        try:
            for sharing, source, available in auto_rejected:
                add_alert(sharing.requester_hospital_id, None, "SHARING_INSUFFICIENT", "CRITICAL",
                          f"EMERGENCY: Sharing request #{sharing.id} was automatically rejected. {sharing.provider.org} has only {available} {sharing.unit or 'units'} of {sharing.item_name}; {sharing.requested_quantity} were requested. No stock was transferred.")
                add_alert(sharing.provider_hospital_id, source.id if source else None, "SHARING_INSUFFICIENT", "CRITICAL",
                          f"EMERGENCY: Sharing request #{sharing.id} was automatically rejected because only {available} {sharing.unit or 'units'} of {sharing.item_name} are available; {sharing.requested_quantity} were requested.")
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
        flash(f"{len(auto_rejected)} pending request(s) are now FAILED because stock was insufficient.", "error")

    hospitals = User.query.filter(User.role == "hospital", User.id != hid).order_by(User.organization_name).all()
    selected_id = request.args.get("provider_id", type=int)
    selected_provider = (User.query.filter_by(id=selected_id, role="hospital").filter(User.id != hid).first()
                         if selected_id else None)
    inventory_options = (Inventory.query.filter_by(hospital_id=selected_provider.id, is_supplier_stock=False)
                         .filter(Inventory.quantity > 0)
                         .filter((Inventory.expiry_date.is_(None)) | (Inventory.expiry_date >= date.today()))
                         .order_by(Inventory.item_name).all()
                         if selected_provider else [])
    sent = InterHospitalRequest.query.filter_by(requester_hospital_id=hid).order_by(
        InterHospitalRequest.created_at.desc()).all()
    incoming = InterHospitalRequest.query.filter_by(provider_hospital_id=hid).order_by(
        InterHospitalRequest.created_at.desc()).all()
    recommendations = get_near_expiry_recommendations(hid)
    return render_template("inter_hospital_sharing.html", hospitals=hospitals,
                           selected_provider=selected_provider, inventory_options=inventory_options,
                           sent=sent, incoming=incoming, recommendations=recommendations,
                           timeout_minutes=app.config["SUPPLY_REQUEST_TIMEOUT_MINUTES"])


@app.route("/hospital/inter-hospital-sharing/status")
@login_required("hospital")
def inter_hospital_sharing_status():
    """Return the latest status for requests visible to the signed-in hospital."""
    expire_inter_hospital_requests()
    hid = g.user.id
    rows = InterHospitalRequest.query.filter(
        (InterHospitalRequest.requester_hospital_id == hid) |
        (InterHospitalRequest.provider_hospital_id == hid)
    ).all()
    return jsonify(requests=[{
        "id": row.id,
        "status": row.status,
        "responded_at": row.responded_at.strftime("%d %b %Y %H:%M") if row.responded_at else None,
        "expires_at": row.expires_at.strftime("%d %b %Y %H:%M") if row.status == "PENDING" and row.expires_at else None,
    } for row in rows])


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
    items = [i for i in scoped(Inventory).filter_by(is_supplier_stock=False).order_by(Inventory.item_name).all() if i.is_cold_chain]
    return render_template("cold_chain.html", items=items)


@app.route("/hospital/cold-chain/update/<int:id>", methods=["POST"])
@login_required("hospital", "admin")
def cold_chain_update(id):
    try:
        temp_val = request.form.get("temperature")
        update_temp(id, temp_val)
        db.session.commit()
        flash("Temperature reading updated successfully.", "ok")
    except (Bad, ValueError) as e:
        db.session.rollback()
        flash(str(e) if isinstance(e, Bad) else "Please enter a valid temperature.", "error")
    except SQLAlchemyError:
        db.session.rollback()
        flash("Unable to update temperature.", "error")
    return redirect(url_for("cold_chain"))


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
    for i in Inventory.query.filter_by(is_supplier_stock=False).all():
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
    if g.user.role != "admin":
        expire_supply_requests()
    q = SupplyRequest.query if g.user.role == "admin" else SupplyRequest.query.filter_by(supplier_id=g.user.id)
    mine = q.order_by(SupplyRequest.request_date.desc()).all()
    n = lambda s: sum(1 for r in mine if r.status == s)
    alerts_ = supply_alerts(mine)
    stats = [("Urgent Alerts", sum(1 for a in alerts_ if a["sev"] == "CRITICAL"), "critical"),
             ("Total Requests", len(mine), ""), ("Critical Requests", sum(1 for r in mine if r.priority == "CRITICAL"), "critical"),
             ("Pending Requests", n("PENDING"), "low"), ("Accepted Requests", n("ACCEPTED"), ""), ("Completed Requests", n("FULFILLED"), "normal")]
    open_reqs = [r for r in Requirement.query.all() if r.status in OPEN_REQ]
    open_reqs.sort(key=lambda r: ORDER.get(r.priority, 3))

    stock_q = Inventory.query.filter_by(is_supplier_stock=True)
    if g.user.role != "admin":
        stock_q = stock_q.filter_by(hospital_id=g.user.id)
    my_stock = stock_q.order_by(Inventory.item_name.asc()).all()

    return render_template("provider_dashboard.html", stats=stats, open_reqs=open_reqs, mine=mine[:8],
                           supply=alerts_[:12], supply_total=len(alerts_), my_stock=my_stock,
                           categories=CATEGORIES, min_expiry_date=(date.today() + timedelta(days=1)).isoformat())


@app.route("/provider/requests")
@login_required("provider", "admin")
def provider_requests():
    expire_supply_requests()
    q = SupplyRequest.query if g.user.role == "admin" else SupplyRequest.query.filter_by(supplier_id=g.user.id)
    st = request.args.get("status", "")
    if st in SR_STATUS:
        q = q.filter_by(status=st)
    return render_template("provider_requests.html", rows=q.order_by(SupplyRequest.request_date.desc()).all(), statuses=SR_STATUS, st=st)


@app.route("/provider/request/<int:id>", methods=["POST"])
@login_required("provider", "admin")
def provider_request(id):
    expire_supply_requests()
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
        if sq is not None and sq > r.requested_quantity:
            raise Bad("Supplied quantity cannot exceed the requested quantity.")

        s_stock = Inventory.query.filter_by(hospital_id=r.supplier_id, item_name=r.item_name, is_supplier_stock=True).with_for_update().first()

        if act == "accept":
            if r.status != "PENDING":
                raise Bad("Only pending requests can be accepted.")
            reserve_qty = sq if sq is not None and sq > 0 else r.requested_quantity
            if s_stock:
                if s_stock.quantity < reserve_qty:
                    raise Bad(f"Cannot accept request: available stock ({s_stock.quantity} {s_stock.unit or 'units'}) is less than the quantity to supply ({reserve_qty}).")
                if not getattr(r, "stock_reserved", False):
                    s_stock.quantity -= reserve_qty
                    r.stock_reserved = True
            r.status = "ACCEPTED"
            if sq is None:
                sq = reserve_qty
        elif act == "reject":
            if r.status != "PENDING":
                raise Bad("Only pending requests can be rejected.")
            if getattr(r, "stock_reserved", False) and s_stock:
                s_stock.quantity += (r.supplied_quantity or r.requested_quantity)
                r.stock_reserved = False
            r.status = "REJECTED"
        elif act == "update" and f.get("status") in ("ACCEPTED", "PARTIALLY_FULFILLED", "FULFILLED"):
            if r.status not in ("ACCEPTED", "PARTIALLY_FULFILLED"):
                raise Bad("This request can no longer be updated.")
            new_status = f["status"]
            if new_status == "FULFILLED" and sq is None:
                sq = r.supplied_quantity or r.requested_quantity
            if not getattr(r, "stock_reserved", False) and s_stock:
                reserve_qty = sq or r.requested_quantity
                if s_stock.quantity < reserve_qty:
                    raise Bad(f"Cannot fulfill request: available stock ({s_stock.quantity}) is less than required ({reserve_qty}).")
                s_stock.quantity -= reserve_qty
                r.stock_reserved = True
            r.status = new_status
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


@app.route("/provider/inventory/add", methods=["POST"])
@login_required("provider")
def provider_inventory_add():
    f = request.form
    try:
        name = (f.get("item_name") or "").strip()
        if not name:
            raise Bad("Item name is required.")
        cat = f.get("category")
        if cat not in CATEGORIES:
            raise Bad("Choose a valid category.")
        try:
            qty = int(f.get("quantity", 0))
        except (ValueError, TypeError):
            raise Bad("Quantity must be a valid number.")
        if qty < 0:
            raise Bad("Quantity cannot be negative.")
        unit = (f.get("unit") or "units").strip()[:30]
        batch = (f.get("batch_number") or "").strip()[:60]
        desc = (f.get("description") or "").strip()
        expiry_date = to_date(f.get("expiry_date"))
        if expiry_date and expiry_date <= date.today():
            raise Bad("Expiry date must be a future date.")
        is_cold = f.get("is_cold_chain") == "on"
        min_temp = max_temp = cur_temp = temp_updated = None
        if is_cold:
            try:
                min_temp = float(f.get("min_temperature", ""))
                max_temp = float(f.get("max_temperature", ""))
                cur_temp = float(f.get("current_temperature", ""))
            except (ValueError, TypeError):
                raise Bad("Enter valid temperature values for cold-chain storage.")
            if min_temp >= max_temp:
                raise Bad("Minimum temperature must be lower than maximum temperature.")
            temp_updated = datetime.now()

        existing = Inventory.query.filter_by(hospital_id=g.user.id, item_name=name, is_supplier_stock=True).first()
        if existing:
            existing.quantity += qty
            existing.category = cat
            existing.unit = unit
            if batch:
                existing.batch_number = batch
            if expiry_date:
                existing.expiry_date = expiry_date
            if desc:
                existing.description = desc
            if is_cold:
                existing.is_cold_chain = True
                existing.min_temperature, existing.max_temperature = min_temp, max_temp
                existing.current_temperature, existing.temp_updated = cur_temp, temp_updated
            flash(f"Updated existing stock for {name}. Total available: {existing.quantity} {existing.unit}.", "ok")
        else:
            inv = Inventory(
                hospital_id=g.user.id,
                item_name=name[:120],
                category=cat,
                quantity=qty,
                minimum_quantity=0,
                unit=unit,
                batch_number=batch or None,
                expiry_date=expiry_date,
                description=desc or None,
                supplier=g.user.org,
                is_supplier_stock=True,
                is_cold_chain=is_cold,
                min_temperature=min_temp,
                max_temperature=max_temp,
                current_temperature=cur_temp,
                temp_updated=temp_updated
            )
            db.session.add(inv)
            flash(f"Added {name} ({qty} {unit}) to your stock listings.", "ok")
        db.session.commit()
    except (Bad, ValueError) as e:
        db.session.rollback()
        flash(str(e) if isinstance(e, Bad) else "Please check your inputs.", "error")
    except SQLAlchemyError:
        db.session.rollback()
        flash("Unable to add stock item.", "error")
    return redirect(url_for("provider_dashboard"))


@app.route("/provider/inventory/update/<int:id>", methods=["POST"])
@login_required("provider")
def provider_inventory_update(id):
    item = Inventory.query.filter_by(id=id, hospital_id=g.user.id, is_supplier_stock=True).first_or_404()
    try:
        qty = int(request.form.get("quantity", 0))
        if qty < 0:
            raise Bad("Quantity cannot be negative.")
        item.quantity = qty
        db.session.commit()
        flash(f"Available stock for {item.item_name} updated to {qty} {item.unit or 'units'}.", "ok")
    except (Bad, ValueError) as e:
        db.session.rollback()
        flash(str(e) if isinstance(e, Bad) else "Enter a valid quantity.", "error")
    except SQLAlchemyError:
        db.session.rollback()
        flash("Unable to update stock quantity.", "error")
    return redirect(url_for("provider_dashboard"))


@app.route("/provider/inventory/edit/<int:id>", methods=["GET", "POST"])
@login_required("provider")
def provider_inventory_edit(id):
    item = Inventory.query.filter_by(id=id, hospital_id=g.user.id, is_supplier_stock=True).first_or_404()
    if request.method == "POST":
        f = request.form
        try:
            name = (f.get("item_name") or "").strip()
            if not name:
                raise Bad("Item name is required.")
            cat = f.get("category")
            if cat not in CATEGORIES:
                raise Bad("Choose a valid category.")
            qty = int(f.get("quantity", 0))
            if qty < 0:
                raise Bad("Quantity cannot be negative.")
            item.item_name = name[:120]
            item.category = cat
            item.quantity = qty
            item.unit = (f.get("unit") or "units").strip()[:30]
            item.batch_number = (f.get("batch_number") or "").strip()[:60]
            item.description = (f.get("description") or "").strip()
            expiry_date = to_date(f.get("expiry_date"))
            if expiry_date and expiry_date <= date.today():
                raise Bad("Expiry date must be in the future.")
            item.expiry_date = expiry_date
            is_cold = f.get("is_cold_chain") == "on"
            item.is_cold_chain = is_cold
            if is_cold:
                lo, hi, cur = (float(f.get(k, "")) for k in ("min_temperature", "max_temperature", "current_temperature"))
                if lo >= hi:
                    raise Bad("Minimum temperature must be lower than maximum temperature.")
                item.min_temperature, item.max_temperature = lo, hi
                item.current_temperature, item.temp_updated = cur, datetime.now()
            else:
                item.min_temperature = item.max_temperature = item.current_temperature = item.temp_updated = None
            db.session.commit()
            flash(f"Updated listing for {item.item_name}.", "ok")
            return redirect(url_for("provider_dashboard"))
        except (Bad, ValueError) as e:
            db.session.rollback()
            flash(str(e) if isinstance(e, Bad) else "Please check your inputs.", "error")
        except SQLAlchemyError:
            db.session.rollback()
            flash("Unable to update stock listing.", "error")
    return render_template("inventory_form.html", item=item, d=form_data(item, None), categories=CATEGORIES,
                           min_expiry_date=(date.today() + timedelta(days=1)).isoformat(), is_provider=True)


@app.route("/provider/inventory/delete/<int:id>", methods=["POST"])
@login_required("provider")
def provider_inventory_delete(id):
    item = Inventory.query.filter_by(id=id, hospital_id=g.user.id, is_supplier_stock=True).first_or_404()
    try:
        active = SupplyRequest.query.filter(
            SupplyRequest.supplier_id == g.user.id,
            SupplyRequest.item_name == item.item_name,
            SupplyRequest.status.in_(["PENDING", "ACCEPTED"])
        ).first()
        if active:
            raise Bad(f"Cannot remove listing for '{item.item_name}' while there are active or pending requests (Request #{active.id}).")
        Alert.query.filter_by(inventory_id=item.id).delete()
        db.session.delete(item)
        db.session.commit()
        flash(f"Stock listing for '{item.item_name}' removed.", "ok")
    except Bad as e:
        flash(str(e), "error")
    except SQLAlchemyError:
        db.session.rollback()
        flash("Unable to remove stock item.", "error")
    return redirect(url_for("provider_dashboard"))


# ---------------------------------------------------------------- admin
def admin_ctx():
    stats = [("Hospitals", User.query.filter_by(role="hospital", is_active=True).count(), ""),
             ("Suppliers", User.query.filter_by(role="provider", is_active=True).count(), ""),
             ("Inactive accounts", User.query.filter_by(is_active=False).count(), "low")]
    return dict(stats=stats)


@app.route("/admin/dashboard")
@login_required("admin")
def admin_dashboard():
    return render_template("admin_dashboard.html", **admin_ctx())


@app.route("/admin/users")
@login_required("admin")
def admin_users():
    return render_template("admin_users.html", users=User.query.order_by(User.role, User.id).all())


@app.route("/admin/supplier-requests")
@login_required("admin")
def admin_supplier_requests():
    rows = SupplyRequest.query.order_by(SupplyRequest.request_date.desc()).all()
    return render_template("admin_supplier_requests.html", rows=rows)


@app.route("/admin/inter-hospital-sharing")
@login_required("admin")
def admin_sharing_requests():
    rows = InterHospitalRequest.query.order_by(InterHospitalRequest.created_at.desc()).all()
    return render_template("admin_sharing_requests.html", rows=rows)


@app.route("/admin/users/<int:id>/edit", methods=["GET", "POST"])
@login_required("admin")
def admin_user_edit(id):
    u = User.query.filter(User.id == id, User.role.in_(("hospital", "provider"))).first_or_404()
    if request.method == "POST":
        f = request.form
        try:
            email = (f.get("email") or "").strip().lower()
            role = f.get("role")
            if not (f.get("name") or "").strip() or not (f.get("organization_name") or "").strip() or "@" not in email:
                raise Bad("Enter an organization, full name, and valid email.")
            if role not in ("hospital", "provider"):
                raise Bad("Choose Hospital or Supplier as the account role.")
            if User.query.filter(User.email == email, User.id != u.id).first():
                raise Bad("That email is already used by another account.")
            if role == "provider" and not (f.get("categories") or "").strip():
                raise Bad("List the supplier categories.")
            password = f.get("password") or ""
            if password and len(password) < 6:
                raise Bad("New password must be at least 6 characters.")
            u.name, u.organization_name = f["name"].strip()[:120], f["organization_name"].strip()[:150]
            u.email, u.role = email, role
            u.phone = (f.get("phone") or "").strip()[:30]
            u.address = (f.get("address") or "").strip()[:255]
            u.categories = (f.get("categories") or "").strip()[:255] or None
            if password:
                u.password_hash = generate_password_hash(password)
            db.session.commit()
            flash("User account updated.", "ok")
            return redirect(url_for("admin_users"))
        except Bad as e:
            db.session.rollback()
            u = db.session.get(User, id)
            flash(str(e), "error")
        except SQLAlchemyError:
            db.session.rollback()
            u = db.session.get(User, id)
            flash("Unable to update user account.", "error")
    return render_template("admin_user_form.html", account=u)


@app.route("/admin/users/<int:id>/status", methods=["POST"])
@login_required("admin")
def admin_user_status(id):
    u = User.query.filter(User.id == id, User.role.in_(("hospital", "provider"))).first_or_404()
    u.is_active = not u.is_active
    db.session.commit()
    flash("User account activated." if u.is_active else "User account deactivated.", "ok")
    return redirect(url_for("admin_users"))


# ---------------------------------------------------------------- API
@app.route("/api/inventory")
@login_required("hospital", "admin")
def api_inventory():
    return jsonify(items=[inv_dict(i) for i in scoped(Inventory).filter_by(is_supplier_stock=False).order_by(Inventory.item_name).all()])


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
        except (Bad, ValueError, TypeError) as e:
            db.session.rollback()
            return jsonify(error=str(e) if isinstance(e, Bad) else "Please check the numbers and dates you entered."), 400
        except SQLAlchemyError as e:
            db.session.rollback()
            app.logger.exception("Unable to save emergency requirement through the API: %s", e)
            err_msg = getattr(e, "orig", e)
            return jsonify(error=f"A database error prevented saving this emergency requirement: {err_msg}"), 400
        except Exception as e:
            db.session.rollback()
            app.logger.exception("Unexpected error saving requirement through the API: %s", e)
            return jsonify(error=f"Unable to save emergency requirement: {e}"), 400
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
    return jsonify(items=[inv_dict(i) for i in scoped(Inventory).filter_by(is_supplier_stock=False).all() if i.is_cold_chain])


@app.route("/api/offline-sync", methods=["POST"])
@login_required("hospital", "admin")
def api_offline_sync():
    raw_ops = (request.get_json(silent=True) or {}).get("ops", [])
    applied, errors = 0, []

    # Deduplicate updates to the same inventory or temperature item
    deduped_ops = []
    latest_state = {}
    for op in raw_ops[:200]:
        t = op.get("type")
        if t in ("inventory_update", "temperature") and op.get("id"):
            latest_state[(t, op["id"])] = op
        else:
            deduped_ops.append(op)
    deduped_ops.extend(latest_state.values())

    for op in deduped_ops:
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
    ensure_timeout_column()
    ensure_sharing_timeout_column()
    ensure_user_active_column()
    ensure_requirements_columns()
    ensure_supplier_stock_columns()

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
    if not Inventory.query.filter_by(hospital_id=h.id, is_supplier_stock=False).first():
        y, soon = date.today() + timedelta(days=365), date.today() + timedelta(days=20)
        rows = [("Paracetamol", "Medicines", 500, 200, "tablets", y, None),
                ("Oxygen Cylinder", "Oxygen", 50, 100, "cylinders", y, None),
                ("Insulin", "Medicines", 20, 100, "vials", soon, None),
                ("Vaccine", "Vaccines", 80, 100, "doses", y, (2, 8, 5)),
                ("Blood Bags", "Blood Products", 20, 100, "bags", y, None)]
        for n, c, q, m, u, e, cold in rows:
            i = Inventory(hospital_id=h.id, item_name=n, category=c, quantity=q, minimum_quantity=m, unit=u,
                          expiry_date=e, supplier="MediSupply Co.", batch_number=f"B-{n[:3].upper()}-01",
                          description=f"{n} for emergency use", is_cold_chain=bool(cold), is_supplier_stock=False)
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

    if not Inventory.query.filter_by(hospital_id=s.id, is_supplier_stock=True).first():
        supplier_items = [
            ("Paracetamol", "Medicines", 300, "tablets", "B-SUP-PARA-01", date.today() + timedelta(days=365)),
            ("Oxygen Cylinder", "Oxygen", 80, "cylinders", "B-SUP-OXY-01", date.today() + timedelta(days=365)),
            ("Insulin", "Medicines", 50, "vials", "B-SUP-INS-01", date.today() + timedelta(days=180)),
            ("Vaccine", "Vaccines", 150, "doses", "B-SUP-VAC-01", date.today() + timedelta(days=200), (2, 8, 4)),
            ("PPE", "PPE", 500, "sets", "B-SUP-PPE-01", date.today() + timedelta(days=730))
        ]
        for itm in supplier_items:
            name, cat, qty, unit, batch, exp = itm[:6]
            cold = itm[6] if len(itm) > 6 else None
            sinv = Inventory(
                hospital_id=s.id, item_name=name, category=cat, quantity=qty, minimum_quantity=0, unit=unit,
                batch_number=batch, expiry_date=exp, description=f"Certified medical grade {name} from {s.org}",
                supplier=s.org, is_supplier_stock=True, is_cold_chain=bool(cold)
            )
            if cold:
                sinv.min_temperature, sinv.max_temperature, sinv.current_temperature, sinv.temp_updated = *cold, datetime.now()
            db.session.add(sinv)
        db.session.commit()

    print("Database ready. Demo users: hospital/supplier/admin @medicrisis.local")


if __name__ == "__main__":
    try:
        with app.app_context():
            db.create_all()
            ensure_timeout_column()
            ensure_sharing_timeout_column()
            ensure_user_active_column()
            ensure_requirements_columns()
            ensure_supplier_stock_columns()
    except Exception as e:
        print("\nERROR: Unable to connect to MySQL. Check that MySQL is running and DATABASE_URL is set (README step 6).\n", e)
    app.run(host="127.0.0.1", port=5000, debug=False)
