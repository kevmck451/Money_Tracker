from datetime import datetime, timezone
from pathlib import Path
import json

from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, UniqueConstraint, text

app = Flask(__name__)
app.secret_key = "change-this"
DB_PATH = Path(__file__).with_name("money_tracker.db")
CFG_PATH = Path(__file__).with_name("settings.json")

app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# -------------------- Models --------------------
class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)   # show/hide in main UI
    display_order = db.Column(db.Integer, nullable=False, default=9999)  # custom sort


class MonthlyBudget(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey("category.id"), nullable=False)
    month_start = db.Column(db.DateTime, nullable=False)
    base_budget = db.Column(db.Float, nullable=False, default=0.0)
    category = db.relationship("Category", backref=db.backref("monthly_budgets", lazy=True))
    __table_args__ = (UniqueConstraint('category_id', 'month_start', name='uniq_cat_month'),)

class Purchase(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey("category.id"), nullable=False)
    name = db.Column(db.String(160), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    ts = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    category = db.relationship("Category", backref=db.backref("purchases", lazy=True))

# -------------------- Helpers --------------------
def load_cfg():
    with CFG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)

def month_start(dt):
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)

def next_month_start(dt):
    y, m = dt.year, dt.month
    if m == 12:
        return dt.replace(year=y + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return dt.replace(month=m + 1, day=1, hour=0, minute=0, second=0, microsecond=0)

def month_spend_for_category(cat_id, start_dt, end_dt):
    return (
        db.session.query(func.coalesce(func.sum(Purchase.amount), 0.0))
        .filter(Purchase.category_id == cat_id, Purchase.ts >= start_dt, Purchase.ts < end_dt)
        .scalar() or 0.0
    )

def get_or_create_monthly_budget(cat: Category, mstart: datetime, default_base: float) -> MonthlyBudget:
    mb = MonthlyBudget.query.filter_by(category_id=cat.id, month_start=mstart).first()
    if not mb:
        mb = MonthlyBudget(category_id=cat.id, month_start=mstart, base_budget=default_base)
        db.session.add(mb)
        db.session.commit()
    return mb

def cumulative_carry_until(cat: Category, up_to_month_start: datetime) -> float:
    first_purchase_ts = db.session.query(func.min(Purchase.ts)).filter(Purchase.category_id == cat.id).scalar()
    first_budget_ts = db.session.query(func.min(MonthlyBudget.month_start)).filter(MonthlyBudget.category_id == cat.id).scalar()
    if not first_purchase_ts and not first_budget_ts:
        return 0.0
    start = month_start(first_purchase_ts or first_budget_ts)
    carry = 0.0
    cur = start
    while cur < up_to_month_start:
        nxt = next_month_start(cur)
        prev_mb = (MonthlyBudget.query
                   .filter(MonthlyBudget.category_id == cat.id, MonthlyBudget.month_start <= cur)
                   .order_by(MonthlyBudget.month_start.desc())
                   .first())
        if prev_mb:
            mb = get_or_create_monthly_budget(cat, cur, prev_mb.base_budget)
        else:
            default = load_cfg()["categories"].get(cat.name, 0.0)
            mb = get_or_create_monthly_budget(cat, cur, default)
        spent = month_spend_for_category(cat.id, cur, nxt)
        carry += (mb.base_budget - spent)
        cur = nxt
    return carry

def current_month_summary(cat: Category, now_utc: datetime):
    cur_start = month_start(now_utc)
    nxt_start = next_month_start(cur_start)
    prev_mb = (MonthlyBudget.query
               .filter(MonthlyBudget.category_id == cat.id, MonthlyBudget.month_start <= cur_start)
               .order_by(MonthlyBudget.month_start.desc())
               .first())
    if prev_mb and prev_mb.month_start == cur_start:
        base_this_month = prev_mb.base_budget
    else:
        base_seed = prev_mb.base_budget if prev_mb else load_cfg()["categories"].get(cat.name, 0.0)
        base_this_month = get_or_create_monthly_budget(cat, cur_start, base_seed).base_budget
    carry_in = cumulative_carry_until(cat, cur_start)
    effective_budget = max(0.0, base_this_month + carry_in)
    spent = month_spend_for_category(cat.id, cur_start, nxt_start)
    remaining = effective_budget - spent
    pct = 0.0 if effective_budget <= 0 else (spent / effective_budget) * 100.0
    over_by = max(0.0, -remaining)
    return {
        "base": base_this_month,
        "carry_in": carry_in,
        "effective": effective_budget,
        "spent": spent,
        "remaining": remaining,
        "pct": pct,
        "over_by": over_by
    }

def ytd_totals(now_utc: datetime, active_only: bool = False):
    """
    Returns (rows, overall) where:
      - rows = list of {"cat": Category, "spent": float} for Jan 1 -> now
      - overall = float sum across categories
    Set active_only=True to include only currently active categories.
    """
    start_of_year = datetime(now_utc.year, 1, 1, tzinfo=timezone.utc)
    q = Category.query.filter_by(is_active=True) if active_only else Category.query
    cats = sort_categories(q.all())
    rows = []
    overall = 0.0
    for c in cats:
        spent = month_spend_for_category(c.id, start_of_year, now_utc)
        rows.append({"cat": c, "spent": spent})
        overall += spent
    return rows, overall




# Preferred display order: put Home Items under the two “Shopping” cats
PREFERRED_ORDER = [
    "Home Items",
    "Kendall's Shopping",
    "Kevin's Shopping",
    "Eating Out",
    "Groceries",
]

def sort_categories(cats):
    order_index = {name: i for i, name in enumerate(PREFERRED_ORDER)}
    return sorted(cats, key=lambda c: (order_index.get(c.name, 9999), c.name.lower()))

# -------------------- Init --------------------
with app.app_context():
    db.create_all()
    # --- lightweight SQLite migration: add columns if missing ---
    def _table_has_column(table, col):
        rows = db.session.execute(text(f"PRAGMA table_info({table})")).fetchall()
        return any(r[1] == col for r in rows)

    if not _table_has_column("Category", "is_active"):
        db.session.execute(text("ALTER TABLE Category ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 1"))
        db.session.commit()

    if not _table_has_column("Category", "display_order"):
        db.session.execute(text("ALTER TABLE Category ADD COLUMN display_order INTEGER NOT NULL DEFAULT 9999"))
        db.session.commit()

    cfg = load_cfg()
    existing = {c.name for c in Category.query.all()}
    for name in cfg["categories"].keys():
        if name not in existing:
            db.session.add(Category(name=name))
    db.session.commit()
    now = datetime.now(timezone.utc)
    cur_start = month_start(now)
    for c in Category.query.all():
        seed = cfg["categories"].get(c.name, 0.0)
        get_or_create_monthly_budget(c, cur_start, seed)

# -------------------- Routes --------------------
@app.route("/")
def index():
    now = datetime.now(timezone.utc)
    cats = sort_categories(Category.query.filter_by(is_active=True).all())
    rows = [{"cat": c, "sum": current_month_summary(c, now)} for c in cats]
    recent = Purchase.query.order_by(Purchase.ts.desc()).limit(20).all()
    # month progress widget fields (already in your templates)
    cur = month_start(now); nxt = next_month_start(cur)
    total = (nxt - cur).total_seconds()
    elapsed = max(0.0, min(total, (now - cur).total_seconds()))
    mprog = {
        "pct": 0.0 if total <= 0 else (elapsed / total) * 100.0,
        "days_left": max(0, int((nxt - now).total_seconds() // 86400)),
        "start_str": cur.strftime('%-m/%-d/%Y'),
        "end_str": nxt.strftime('%-m/%-d/%Y')


    }
    return render_template("index.html", rows=rows, recent=recent, mprog=mprog)

@app.route("/add", methods=["POST"])
def add():
    cat_id = int(request.form["category_id"])
    name = request.form["name"].strip()
    amount = float(request.form["amount"])
    if not name:
        flash("Name required.", "err")
        return redirect(url_for("index"))
    db.session.add(Purchase(category_id=cat_id, name=name, amount=amount))
    db.session.commit()
    flash("Added.", "ok")
    return redirect(url_for("index"))

@app.route("/delete/<int:pid>", methods=["POST"])
def delete(pid):
    p = Purchase.query.get_or_404(pid)
    db.session.delete(p)
    db.session.commit()
    flash("Purchase deleted.", "ok")
    return redirect(url_for("index"))

@app.route("/edit/<int:pid>", methods=["GET", "POST"])
def edit(pid):
    p = Purchase.query.get_or_404(pid)
    if request.method == "POST":
        p.name = request.form["name"].strip()
        p.amount = float(request.form["amount"])
        p.category_id = int(request.form["category_id"])
        db.session.commit()
        flash("Purchase updated.", "ok")
        return redirect(url_for("index"))
    cats = sort_categories(Category.query.filter_by(is_active=True).all())
    return render_template("edit.html", purchase=p, categories=cats)

@app.route("/admin", methods=["GET", "POST"])
def admin():
    cfg = load_cfg()
    now = datetime.now(timezone.utc)
    cur_start = month_start(now)

    # Unlock with PIN
    if request.method == "POST" and "pin" in request.form and not session.get("is_admin"):
        pin = request.form.get("pin", "")
        if pin != cfg.get("admin_pin", ""):
            flash("Wrong PIN.", "err")
            return redirect(url_for("admin"))
        session["is_admin"] = True
        flash("Admin unlocked.", "ok")
        return redirect(url_for("admin"))

    # Save budgets (requires unlocked)
    if request.method == "POST" and session.get("is_admin"):
        for c in Category.query.all():
            key = f"base_{c.id}"
            if key in request.form:
                try:
                    val = float(request.form[key])
                except ValueError:
                    continue
                mb = get_or_create_monthly_budget(c, cur_start, 0.0)
                mb.base_budget = val
        db.session.commit()
        flash("This month's budgets updated.", "ok")
        return redirect(url_for("admin"))

    items = []
    for c in sort_categories(Category.query.all()):
        mb = get_or_create_monthly_budget(c, cur_start, cfg["categories"].get(c.name, 0.0))
        items.append((c, mb))
    return render_template("admin.html", items=items, is_admin=bool(session.get("is_admin")))

@app.route("/logout")
def logout():
    session.pop("is_admin", None)
    flash("Admin locked.", "ok")
    return redirect(url_for("index"))

@app.route("/totals")
def totals():
    now = datetime.now(timezone.utc)
    active_only = request.args.get("active_only", "0") == "1"
    rows, overall = ytd_totals(now, active_only=active_only)

    labels = [r["cat"].name for r in rows]
    values = [round(r["spent"], 2) for r in rows]

    return render_template(
        "totals.html",
        labels=labels,
        values=values,
        rows=rows,
        overall=overall,
        year=now.year,
        active_only=active_only,   # <-- pass to template
    )




if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8001, debug=True)
