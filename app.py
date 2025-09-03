import os
import csv
import io
from datetime import datetime, timedelta, date
from typing import Optional

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, send_file, jsonify, send_from_directory
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, or_, ForeignKey
from sqlalchemy.orm import relationship
from werkzeug.utils import secure_filename

# ---------- Config ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "inventory.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_EXTS = {"pdf", "png", "jpg", "jpeg", "webp", "txt", "csv", "docx", "xlsx"}

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret")
app.config["UPLOAD_FOLDER"] = UPLOAD_DIR

db = SQLAlchemy(app)

# ---------- Models ----------
class Item(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(256), nullable=False, index=True)
    category = db.Column(db.String(128), index=True)
    vendor = db.Column(db.String(128), index=True)
    catalog_no = db.Column(db.String(128))
    lot_no = db.Column(db.String(128))
    quantity = db.Column(db.Float, default=0)
    units = db.Column(db.String(32))
    location = db.Column(db.String(128))
    threshold = db.Column(db.Float, default=0)
    received_date = db.Column(db.Date)
    expiry_date = db.Column(db.Date)
    notes = db.Column(db.Text)

    transactions = relationship("Transaction", back_populates="item", cascade="all,delete-orphan")
    attachments = relationship("Attachment", back_populates="item", cascade="all,delete-orphan")
    reorder_requests = relationship("ReorderRequest", back_populates="item", cascade="all,delete-orphan")
    labs = relationship("ItemLab", back_populates="item", cascade="all,delete-orphan")

    def is_low(self) -> bool:
        try:
            return (
                self.threshold is not None
                and self.quantity is not None
                and float(self.quantity) <= float(self.threshold)
            )
        except Exception:
            return False

    def is_expiring(self, days: int = 30) -> bool:
        if self.expiry_date:
            return self.expiry_date <= (datetime.utcnow().date() + timedelta(days=days))
        return False


class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, ForeignKey("item.id"), index=True, nullable=False)
    ts = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    delta = db.Column(db.Float, nullable=False)  # + received, - consumed
    reason = db.Column(db.String(128))
    actor = db.Column(db.String(128))
    note = db.Column(db.Text)
    item = relationship("Item", back_populates="transactions")


class Attachment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, ForeignKey("item.id"), index=True, nullable=False)
    filename = db.Column(db.String(512), nullable=False)
    original_name = db.Column(db.String(512), nullable=False)
    kind = db.Column(db.String(64))
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    item = relationship("Item", back_populates="attachments")


class ReorderRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, ForeignKey("item.id"), index=True, nullable=False)
    requested_qty = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(32), default="open")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    notes = db.Column(db.Text)
    item = relationship("Item", back_populates="reorder_requests")


class Lab(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), unique=True, nullable=False, index=True)
    description = db.Column(db.String(256))
    members_hint = db.Column(db.String(256))
    items = relationship("ItemLab", back_populates="lab", cascade="all,delete-orphan")


class ItemLab(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, ForeignKey("item.id"), index=True, nullable=False)
    lab_id = db.Column(db.Integer, ForeignKey("lab.id"), index=True, nullable=False)
    item = relationship("Item", back_populates="labs")
    lab = relationship("Lab", back_populates="items")


# ---------- DB init ----------
with app.app_context():
    db.create_all()


# ---------- Context processors ----------
@app.context_processor
def inject_now():
    return {"now": date.today()}

@app.context_processor
def inject_globals():
    return {"current_year": datetime.utcnow().year}


# ---------- Helpers ----------
def parse_date(s: Optional[str]):
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except Exception:
            continue
    return None

def coerce_float(s: Optional[str]) -> Optional[float]:
    if s is None or str(s).strip() == "":
        return None
    try:
        return float(s)
    except Exception:
        return None

def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in {"pdf","png","jpg","jpeg","webp","txt","csv","docx","xlsx"}


# ---------- Views ----------
@app.route("/")
def dashboard():
    # --- KPIs ---
    total_items = db.session.scalar(db.select(func.count()).select_from(Item)) or 0
    all_items = db.session.execute(db.select(Item)).scalars().all()
    low_count = sum(1 for i in all_items if i.is_low())
    expiring_count = sum(1 for i in all_items if i.is_expiring(30))

    # --- Needs attention (top 6) ---
    low_items = [i for i in all_items if i.is_low()]
    low_items.sort(key=lambda x: (x.quantity or 0))
    low_items = low_items[:6]

    expiring_items = [i for i in all_items if i.is_expiring(30)]
    expiring_items.sort(key=lambda x: (x.expiry_date or date.max))
    expiring_items = expiring_items[:6]

    # --- Reorder preview (latest 6 open) ---
    reorder_preview = db.session.execute(
        db.select(ReorderRequest)
          .where(ReorderRequest.status == "open")
          .order_by(ReorderRequest.created_at.desc())
          .limit(6)
    ).scalars().all()

    # --- Recent activity (transactions) ---
    recent_txns = db.session.execute(
        db.select(Transaction).order_by(Transaction.ts.desc()).limit(8)
    ).scalars().all()

    # --- Recently added/updated items ---
    recent_items = db.session.execute(
        db.select(Item).order_by(Item.id.desc()).limit(8)
    ).scalars().all()

    # --- Usage sparkline (last 14 days of summed deltas) ---
    since = datetime.utcnow().date() - timedelta(days=13)
    rows = db.session.execute(
        db.select(func.date(Transaction.ts), func.sum(Transaction.delta))
          .where(Transaction.ts >= since)
          .group_by(func.date(Transaction.ts))
          .order_by(func.date(Transaction.ts).asc())
    ).all()

    # Fill any missing days with 0
    sums_by_day = {str(d): float(s or 0) for d, s in rows}
    series = []
    for k in range(14):
        d = since + timedelta(days=k)
        series.append(sums_by_day.get(d.isoformat(), 0.0))

    # Build sparkline points (simple polyline)
    def spark_points(values, width=220, height=48, pad=6):
        if not values:
            return ""
        vmin = min(values)
        vmax = max(values)
        span = (vmax - vmin) or 1.0
        n = len(values)
        step_x = (width - 2*pad) / max(1, n-1)
        pts = []
        for idx, v in enumerate(values):
            x = pad + idx * step_x
            # invert y so higher values go up
            y = pad + (height - 2*pad) * (1 - (v - vmin) / span)
            pts.append(f"{x:.1f},{y:.1f}")
        return " ".join(pts)

    spark_points_str = spark_points(series)

    return render_template(
        "dashboard.html",
        title="Dashboard",
        total_items=total_items,
        low_count=low_count,
        expiring_count=expiring_count,
        low_items=low_items,
        expiring_items=expiring_items,
        reorder_preview=reorder_preview,
        recent_txns=recent_txns,
        recent_items=recent_items,
        usage_series=series,
        spark_points=spark_points_str,
    )


@app.context_processor
def url_tools():
    from urllib.parse import urlencode

    def url_for_params(**updates):
        # Start from current query params, then overlay updates (e.g., page/per_page)
        params = dict(request.args.items())
        params.update({k: v for k, v in updates.items() if v is not None})
        # Remove empty values to keep URLs clean
        params = {k: v for k, v in params.items() if str(v).strip() != ""}
        return f"{url_for('items_list')}?{urlencode(params)}"

    return {"url_for_params": url_for_params}


@app.get("/items")
def items_list():
    q = (request.args.get("q") or "").strip()
    category = (request.args.get("category") or "").strip()
    vendor = (request.args.get("vendor") or "").strip()
    lab_filter = (request.args.get("lab") or "").strip()
    status = (request.args.get("status") or "").strip()  # "low" or "expiring"

    # Page size (20/50/100) and page (>=1)
    def _clamp_int(name, default, allowed=None, minv=1):
        try:
            v = int(request.args.get(name, default))
        except Exception:
            v = default
        if allowed:
            return v if v in allowed else default
        return max(minv, v)

    per_page = _clamp_int("per_page", 20, allowed={20, 50, 100})
    page = _clamp_int("page", 1, minv=1)

    # Filter options
    category_options = db.session.execute(
        db.select(Item.category).where(Item.category.isnot(None)).distinct().order_by(Item.category.asc())
    ).scalars().all()
    vendor_options = db.session.execute(
        db.select(Item.vendor).where(Item.vendor.isnot(None)).distinct().order_by(Item.vendor.asc())
    ).scalars().all()
    lab_options = db.session.execute(db.select(Lab.name).order_by(Lab.name.asc())).scalars().all()

    # Base query
    stmt = db.select(Item)

    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(or_(
            Item.name.ilike(pattern),
            Item.catalog_no.ilike(pattern),
            Item.vendor.ilike(pattern),
            Item.category.ilike(pattern),
            Item.location.ilike(pattern),
            Item.notes.ilike(pattern),
        ))
    if category:
        stmt = stmt.where(Item.category == category)
    if vendor:
        stmt = stmt.where(Item.vendor == vendor)

    items = db.session.execute(stmt.order_by(Item.name.asc())).scalars().all()

    # Lab filter (many-to-many) in Python for simplicity
    if lab_filter:
        lab = db.session.execute(db.select(Lab).where(Lab.name == lab_filter)).scalars().first()
        if lab:
            item_ids_in_lab = set(
                db.session.execute(db.select(ItemLab.item_id).where(ItemLab.lab_id == lab.id)).scalars().all()
            )
            items = [i for i in items if i.id in item_ids_in_lab]
        else:
            items = []

    # Status filter (computed flags) in Python
    if status == "low":
        items = [i for i in items if i.is_low()]
    elif status == "expiring":
        items = [i for i in items if i.is_expiring(30)]

    # Pagination (after all filters)
    total = len(items)
    total_pages = (total + per_page - 1) // per_page if total else 1
    page = max(1, min(page, total_pages))  # clamp page if out of range
    start = (page - 1) * per_page
    end = start + per_page
    page_items = items[start:end]

    return render_template(
        "items_list.html",
        items=page_items,
        category_options=category_options,
        vendor_options=vendor_options,
        lab_options=lab_options,
        view_title="All Items",
        dashboard=False,
        title="Items",
        request=request,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
    )

@app.get("/items/new")
def item_new():
    labs = db.session.execute(db.select(Lab).order_by(Lab.name.asc())).scalars().all()
    return render_template("item_form.html",
                           item=None,
                           labs=labs,
                           selected_lab_ids=[],
                           title="New Item")

@app.post("/items/new")
def item_create():
    form = request.form
    item = Item(
        name=form.get("name", "").strip(),
        category=form.get("category", "").strip() or None,
        vendor=form.get("vendor", "").strip() or None,
        catalog_no=form.get("catalog_no", "").strip() or None,
        lot_no=form.get("lot_no", "").strip() or None,
        quantity=coerce_float(form.get("quantity")),
        units=form.get("units", "").strip() or None,
        location=form.get("location", "").strip() or None,
        threshold=coerce_float(form.get("threshold")),
        received_date=parse_date(form.get("received_date")),
        expiry_date=parse_date(form.get("expiry_date")),
        notes=form.get("notes", "").strip() or None,
    )
    if not item.name:
        flash("Name is required.", "error")
        return redirect(url_for("item_new"))

    db.session.add(item)
    db.session.commit()

    for lab_id in request.form.getlist("labs"):
        try:
            db.session.add(ItemLab(item_id=item.id, lab_id=int(lab_id)))
        except Exception:
            pass
    db.session.commit()

    flash("Item created.", "success")
    return redirect(url_for("items_list"))

@app.get("/items/<int:item_id>/edit")
def item_edit(item_id):
    item = db.get_or_404(Item, item_id)
    labs = db.session.execute(db.select(Lab).order_by(Lab.name.asc())).scalars().all()
    attached = db.session.execute(
        db.select(Attachment)
          .where(Attachment.item_id == item.id)
          .order_by(Attachment.uploaded_at.desc())
    ).scalars().all()
    selected_lab_ids = [il.lab_id for il in item.labs]
    return render_template("item_form.html",
                           item=item,
                           labs=labs,
                           attachments=attached,
                           selected_lab_ids=selected_lab_ids,
                           title="Edit Item")

@app.post("/items/<int:item_id>/edit")
def item_update(item_id):
    item = db.get_or_404(Item, item_id)
    form = request.form
    item.name = form.get("name", "").strip()
    item.category = form.get("category", "").strip() or None
    item.vendor = form.get("vendor", "").strip() or None
    item.catalog_no = form.get("catalog_no", "").strip() or None
    item.lot_no = form.get("lot_no", "").strip() or None
    item.quantity = coerce_float(form.get("quantity"))
    item.units = form.get("units", "").strip() or None
    item.location = form.get("location", "").strip() or None
    item.threshold = coerce_float(form.get("threshold"))
    item.received_date = parse_date(form.get("received_date"))
    item.expiry_date = parse_date(form.get("expiry_date"))
    item.notes = form.get("notes", "").strip() or None

    if not item.name:
        flash("Name is required.", "error")
        return redirect(url_for("item_edit", item_id=item_id))

    db.session.execute(db.delete(ItemLab).where(ItemLab.item_id == item.id))
    for lab_id in request.form.getlist("labs"):
        try:
            db.session.add(ItemLab(item_id=item.id, lab_id=int(lab_id)))
        except Exception:
            pass

    db.session.commit()
    flash("Item updated.", "success")
    return redirect(url_for("items_list"))

@app.post("/items/<int:item_id>/delete")
def item_delete(item_id):
    item = db.get_or_404(Item, item_id)
    db.session.delete(item)
    db.session.commit()
    flash("Item deleted.", "success")
    return redirect(url_for("items_list"))

# Consumption / Receiving
@app.post("/items/<int:item_id>/consume")
def item_consume(item_id):
    item = db.get_or_404(Item, item_id)
    amount = coerce_float(request.form.get("amount"))
    actor = (request.form.get("actor") or "").strip() or None
    note = (request.form.get("note") or "").strip() or None
    if not amount or amount <= 0:
        flash("Enter a positive amount to consume.", "error")
        return redirect(url_for("items_list"))
    item.quantity = (item.quantity or 0) - amount
    db.session.add(Transaction(item_id=item.id, delta=-amount, reason="consume", actor=actor, note=note))
    db.session.commit()
    flash(f"Consumed {amount} {item.units or ''} from {item.name}.", "success")
    return redirect(url_for("items_list"))

@app.post("/items/<int:item_id>/receive")
def item_receive(item_id):
    item = db.get_or_404(Item, item_id)
    amount = coerce_float(request.form.get("amount"))
    actor = (request.form.get("actor") or "").strip() or None
    note = (request.form.get("note") or "").strip() or None
    if not amount or amount <= 0:
        flash("Enter a positive amount to receive.", "error")
        return redirect(url_for("items_list"))
    item.quantity = (item.quantity or 0) + amount
    db.session.add(Transaction(item_id=item.id, delta=+amount, reason="receive", actor=actor, note=note))
    db.session.commit()
    flash(f"Received {amount} {item.units or ''} into {item.name}.", "success")
    return redirect(url_for("items_list"))

@app.get("/transactions")
def transactions_list():
    q = db.session.execute(
        db.select(Transaction).order_by(Transaction.ts.desc()).limit(200)
    ).scalars().all()
    return render_template("transactions.html", txns=q, title="Transactions")

# Reorder list
@app.post("/items/<int:item_id>/reorder")
def item_reorder(item_id):
    item = db.get_or_404(Item, item_id)
    qty = coerce_float(request.form.get("requested_qty")) or 1
    note = (request.form.get("note") or "").strip() or None
    db.session.add(ReorderRequest(item_id=item.id, requested_qty=qty, notes=note))
    db.session.commit()
    flash(f"Added to reorder list: {item.name} (qty {qty}).", "success")
    return redirect(url_for("reorder_list"))

@app.get("/reorder")
def reorder_list():
    rows = db.session.execute(
        db.select(ReorderRequest).order_by(ReorderRequest.created_at.desc())
    ).scalars().all()
    return render_template("reorder.html", rows=rows, title="Reorder List")

@app.post("/reorder/<int:req_id>/status")
def reorder_status(req_id):
    req = db.get_or_404(ReorderRequest, req_id)
    new_status = (request.form.get("status") or "").strip()
    if new_status in {"open", "ordered", "received", "canceled"}:
        req.status = new_status
        db.session.commit()
        flash("Reorder status updated.", "success")
    else:
        flash("Invalid status.", "error")
    return redirect(url_for("reorder_list"))

# Attachments
@app.post("/items/<int:item_id>/attach")
def item_attach(item_id):
    item = db.get_or_404(Item, item_id)
    file = request.files.get("file")
    kind = (request.form.get("kind") or "").strip() or None
    if not file or file.filename == "":
        flash("Choose a file to upload.", "error")
        return redirect(url_for("item_edit", item_id=item_id))
    ext_ok = allowed_file(file.filename)
    if not ext_ok:
        flash("Unsupported file type.", "error")
        return redirect(url_for("item_edit", item_id=item_id))
    fname = secure_filename(file.filename)
    stored = f"{item_id}_{int(datetime.utcnow().timestamp())}_{fname}"
    path = os.path.join(app.config["UPLOAD_FOLDER"], stored)
    file.save(path)
    db.session.add(Attachment(item_id=item.id, filename=stored, original_name=fname, kind=kind))
    db.session.commit()
    flash("File attached.", "success")
    return redirect(url_for("item_edit", item_id=item_id))

@app.get("/files/<path:fname>")
def files_serve(fname):
    return send_from_directory(app.config["UPLOAD_FOLDER"], fname, as_attachment=False)

# Analytics (simple)
@app.get("/analytics")
def analytics():
    by_cat = db.session.execute(
        db.select(Item.category, func.count(Item.id)).group_by(Item.category)
    ).all()
    by_vendor = db.session.execute(
        db.select(Item.vendor, func.count(Item.id)).group_by(Item.vendor)
    ).all()
    since = datetime.utcnow() - timedelta(days=90)
    usage = db.session.execute(
        db.select(func.date(Transaction.ts), func.sum(Transaction.delta))
        .where(Transaction.ts >= since)
        .group_by(func.date(Transaction.ts))
        .order_by(func.date(Transaction.ts).asc())
    ).all()
    all_items = db.session.execute(db.select(Item)).scalars().all()
    low = sum(1 for i in all_items if i.is_low())
    expiring_30 = sum(1 for i in all_items if i.is_expiring(30))
    lab_counts = db.session.execute(
        db.select(Lab.name, func.count(ItemLab.id))
        .join(ItemLab, ItemLab.lab_id == Lab.id)
        .group_by(Lab.name).order_by(Lab.name.asc())
    ).all()
    return render_template(
        "analytics.html",
        by_cat=by_cat,
        by_vendor=by_vendor,
        usage=usage,
        low=low,
        expiring_30=expiring_30,
        lab_counts=lab_counts,
        title="Analytics",
    )

# Labs
@app.get("/labs")
def labs_list():
    labs = db.session.execute(db.select(Lab).order_by(Lab.name.asc())).scalars().all()
    return render_template("labs.html", labs=labs, title="Labs")

@app.post("/labs/new")
def lab_new():
    name = (request.form.get("name") or "").strip()
    desc = (request.form.get("description") or "").strip() or None
    hint = (request.form.get("members_hint") or "").strip() or None
    if not name:
        flash("Lab name required.", "error")
    else:
        if db.session.execute(db.select(Lab).where(Lab.name == name)).scalars().first():
            flash("Lab already exists.", "error")
        else:
            db.session.add(Lab(name=name, description=desc, members_hint=hint))
            db.session.commit()
            flash("Lab created.", "success")
    return redirect(url_for("labs_list"))

@app.post("/labs/<int:lab_id>/delete")
def lab_delete(lab_id):
    lab = db.get_or_404(Lab, lab_id)
    db.session.delete(lab)
    db.session.commit()
    flash("Lab deleted.", "success")
    return redirect(url_for("labs_list"))

# Import/Export
@app.get("/import")
def import_view():
    return render_template("import.html", title="Import CSV")

@app.post("/import")
def import_csv():
    f = request.files.get("file")
    if not f:
        flash("Upload a CSV file.", "error")
        return redirect(url_for("import_view"))

    try:
        csv_text = f.read().decode("utf-8", errors="ignore")
        reader = csv.DictReader(io.StringIO(csv_text))
        count = 0
        for row in reader:
            item = Item(
                name=(row.get("name") or "").strip(),
                category=(row.get("category") or None),
                vendor=(row.get("vendor") or None),
                catalog_no=(row.get("catalog_no") or None),
                lot_no=(row.get("lot_no") or None),
                quantity=coerce_float(row.get("quantity")),
                units=(row.get("units") or None),
                location=(row.get("location") or None),
                threshold=coerce_float(row.get("threshold")),
                received_date=parse_date(row.get("received_date")),
                expiry_date=parse_date(row.get("expiry_date")),
                notes=(row.get("notes") or None),
            )
            if item.name:
                db.session.add(item)
                count += 1
        db.session.commit()
        flash(f"Imported {count} items.", "success")
    except Exception as e:
        flash(f"Import failed: {e}", "error")

    return redirect(url_for("items_list"))

@app.get("/export")
def export_csv():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id","name","category","vendor","catalog_no","lot_no",
        "quantity","units","location","threshold",
        "received_date","expiry_date","notes",
    ])
    for it in db.session.execute(db.select(Item).order_by(Item.name.asc())).scalars():
        writer.writerow([
            it.id, it.name or "", it.category or "", it.vendor or "", it.catalog_no or "", it.lot_no or "",
            it.quantity if it.quantity is not None else "",
            it.units or "", it.location or "",
            it.threshold if it.threshold is not None else "",
            it.received_date.isoformat() if it.received_date else "",
            it.expiry_date.isoformat() if it.expiry_date else "",
            it.notes or "",
        ])
    mem = io.BytesIO(output.getvalue().encode("utf-8"))
    mem.seek(0)
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name="inventory_export.csv")

@app.get("/add", endpoint="add_item")
def _legacy_add_item():
    return redirect(url_for("item_new"))

@app.get("/api/items")
def api_items():
    items = db.session.execute(db.select(Item)).scalars().all()
    def serialize(it: Item):
        lab_names = db.session.execute(
            db.select(Lab.name)
            .join(ItemLab, ItemLab.lab_id == Lab.id)
            .where(ItemLab.item_id == it.id)
        ).scalars().all()
        return {
            "id": it.id,
            "name": it.name,
            "category": it.category,
            "vendor": it.vendor,
            "catalog_no": it.catalog_no,
            "lot_no": it.lot_no,
            "quantity": it.quantity,
            "units": it.units,
            "location": it.location,
            "threshold": it.threshold,
            "received_date": it.received_date.isoformat() if it.received_date else None,
            "expiry_date": it.expiry_date.isoformat() if it.expiry_date else None,
            "low": it.is_low(),
            "expiring_30d": it.is_expiring(30),
            "labs": lab_names,
            "notes": it.notes,
        }
    return jsonify([serialize(i) for i in items])

@app.get("/health")
def health():
    return jsonify(ok=True)

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=True)
