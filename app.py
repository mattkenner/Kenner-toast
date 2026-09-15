import json
import os
from datetime import datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo

import psycopg
from flask import Flask, Response, jsonify, render_template_string, request

app = Flask(__name__)
DATABASE_URL = os.environ.get("DATABASE_URL")
DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "owner")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD")
LOCAL_TZ = ZoneInfo(os.environ.get("DASHBOARD_TIMEZONE", "America/New_York"))

LOCATIONS = ["Ritual", "Dogwood", "Madison Maison"]


def requires_auth(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not DASHBOARD_PASSWORD:
            return Response("Dashboard password is not configured.", status=503)
        auth = request.authorization
        if not auth or auth.username != DASHBOARD_USER or auth.password != DASHBOARD_PASSWORD:
            return Response(
                "Authentication required",
                401,
                {"WWW-Authenticate": 'Basic realm="Kenner Group Owner Dashboard"'},
            )
        return fn(*args, **kwargs)

    return wrapped


def money(value):
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def employee_label(value):
    if not isinstance(value, dict):
        return None
    first = value.get("firstName") or ""
    last = value.get("lastName") or ""
    name = f"{first} {last}".strip()
    return name or value.get("guid") or value.get("entityType")


def payment_label(payment):
    typ = payment.get("type") or payment.get("paymentMethod") or payment.get("cardType") or "Other"
    if isinstance(typ, dict):
        return typ.get("name") or typ.get("guid") or "Other"
    return str(typ)


def order_metrics(order, location_name):
    gross = net = tax = tips = discounts = 0.0
    checks_count = 0
    payments = {}
    server_sales = {}
    hourly_sales = {}
    ritual_dayparts = {"Dinner (5-10 PM)": 0.0, "Nightlife (10 PM-4 AM)": 0.0, "Other": 0.0}

    for check in order.get("checks") or []:
        if check.get("voided"):
            continue
        checks_count += 1

        total = money(check.get("totalAmount") or check.get("amount"))
        check_tax = money(check.get("taxAmount"))
        tip_total = sum(money(p.get("tipAmount")) for p in (check.get("payments") or []) if not p.get("voided"))
        discount_total = sum(
            abs(money(d.get("discountAmount") or d.get("amount"))) for d in (check.get("appliedDiscounts") or [])
        )

        gross += total
        tax += check_tax
        tips += tip_total
        discounts += discount_total

        check_net = max(0.0, total - check_tax - tip_total)
        if not check_net:
            paid = sum(money(p.get("amount")) for p in (check.get("payments") or []) if not p.get("voided"))
            check_net = max(0.0, paid - check_tax - tip_total)
        net += check_net

        for p in check.get("payments") or []:
            if p.get("voided"):
                continue
            label = payment_label(p)
            payments[label] = payments.get(label, 0.0) + money(p.get("amount"))

        server = employee_label(check.get("server") or check.get("employee") or {})
        if server:
            server_sales[server] = server_sales.get(server, 0.0) + check_net

        dt = parse_dt(check.get("closedDate") or check.get("openedDate") or order.get("openedDate"))
        if dt:
            local_dt = dt.astimezone(LOCAL_TZ)
            hour_label = local_dt.strftime("%I %p").lstrip("0")
            hourly_sales[hour_label] = hourly_sales.get(hour_label, 0.0) + check_net

            if location_name == "Ritual":
                h = local_dt.hour
                if 17 <= h < 22:
                    ritual_dayparts["Dinner (5-10 PM)"] += check_net
                elif h >= 22 or h < 4:
                    ritual_dayparts["Nightlife (10 PM-4 AM)"] += check_net
                else:
                    ritual_dayparts["Other"] += check_net

    return {
        "gross": gross,
        "net": net,
        "tax": tax,
        "tips": tips,
        "discounts": discounts,
        "checks": checks_count,
        "payments": payments,
        "server_sales": server_sales,
        "hourly_sales": hourly_sales,
        "ritual_dayparts": ritual_dayparts,
    }


def labor_metrics(entry):
    in_dt = parse_dt(entry.get("inDate") or entry.get("clockIn"))
    out_dt = parse_dt(entry.get("outDate") or entry.get("clockOut"))
    hours = 0.0
    if in_dt and out_dt and out_dt > in_dt:
        hours = (out_dt - in_dt).total_seconds() / 3600.0

    regular = money(entry.get("regularHours"))
    overtime = money(entry.get("overtimeHours"))
    if regular or overtime:
        hours = regular + overtime

    rate = money(entry.get("hourlyWage") or entry.get("wage") or entry.get("payRate"))
    labor_cost = money(entry.get("totalPay"))
    if not labor_cost and rate and hours:
        labor_cost = rate * hours

    return hours, overtime, labor_cost


def load_dashboard(days=7, location="All"):
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    location_sql = "" if location == "All" else " AND location_name = %s"

    order_params = [cutoff] + ([] if location == "All" else [location])
    labor_params = [cutoff] + ([] if location == "All" else [location])

    with psycopg.connect(DATABASE_URL) as conn:
        orders = conn.execute(
            f"""
            SELECT location_name, raw
            FROM toast_orders_raw
            WHERE COALESCE(closed_date, opened_date, modified_date, synced_at) >= %s
            {location_sql}
            """,
            order_params,
        ).fetchall()

        labor = conn.execute(
            f"""
            SELECT location_name, raw
            FROM toast_time_entries_raw
            WHERE COALESCE(modified_date, synced_at) >= %s
            {location_sql}
            """,
            labor_params,
        ).fetchall()

        last_sync = conn.execute(
            "SELECT finished_at, status, detail FROM toast_sync_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()

        first_order = conn.execute("SELECT MIN(COALESCE(closed_date, opened_date, synced_at)) FROM toast_orders_raw").fetchone()[0]

    totals = {
        "gross": 0.0,
        "net": 0.0,
        "tax": 0.0,
        "tips": 0.0,
        "discounts": 0.0,
        "checks": 0,
        "orders": 0,
        "labor_hours": 0.0,
        "ot_hours": 0.0,
        "labor_cost": 0.0,
    }
    by_location = {}
    hourly = {}
    payments = {}
    servers = {}
    ritual_dayparts = {"Dinner (5-10 PM)": 0.0, "Nightlife (10 PM-4 AM)": 0.0, "Other": 0.0}

    for loc, raw in orders:
        if isinstance(raw, str):
            raw = json.loads(raw)
        m = order_metrics(raw or {}, loc)
        loc_row = by_location.setdefault(
            loc,
            {"net": 0.0, "checks": 0, "orders": 0, "labor_cost": 0.0, "labor_hours": 0.0},
        )
        loc_row["net"] += m["net"]
        loc_row["checks"] += m["checks"]
        loc_row["orders"] += 1

        totals["orders"] += 1
        for key in ("gross", "net", "tax", "tips", "discounts", "checks"):
            totals[key] += m[key]

        for key, value in m["hourly_sales"].items():
            hourly[key] = hourly.get(key, 0.0) + value
        for key, value in m["payments"].items():
            payments[key] = payments.get(key, 0.0) + value
        for key, value in m["server_sales"].items():
            servers[key] = servers.get(key, 0.0) + value
        for key, value in m["ritual_dayparts"].items():
            ritual_dayparts[key] += value

    for loc, raw in labor:
        if isinstance(raw, str):
            raw = json.loads(raw)
        hours, ot, cost = labor_metrics(raw or {})
        loc_row = by_location.setdefault(
            loc,
            {"net": 0.0, "checks": 0, "orders": 0, "labor_cost": 0.0, "labor_hours": 0.0},
        )
        loc_row["labor_hours"] += hours
        loc_row["labor_cost"] += cost
        totals["labor_hours"] += hours
        totals["ot_hours"] += ot
        totals["labor_cost"] += cost

    for row in by_location.values():
        row["avg_check"] = row["net"] / row["checks"] if row["checks"] else 0.0
        row["labor_pct"] = row["labor_cost"] / row["net"] * 100 if row["net"] else 0.0

    totals["avg_check"] = totals["net"] / totals["checks"] if totals["checks"] else 0.0
    totals["labor_pct"] = totals["labor_cost"] / totals["net"] * 100 if totals["net"] else 0.0

    hour_order = [f"{h} AM" for h in [4,5,6,7,8,9,10,11]] + ["12 PM"] + [f"{h} PM" for h in range(1,12)] + ["12 AM", "1 AM", "2 AM", "3 AM"]
    hourly_sorted = [(h, hourly.get(h, 0.0)) for h in hour_order if h in hourly]

    return {
        "totals": totals,
        "by_location": by_location,
        "hourly": hourly_sorted,
        "payments": sorted(payments.items(), key=lambda x: x[1], reverse=True),
        "servers": sorted(servers.items(), key=lambda x: x[1], reverse=True)[:10],
        "ritual_dayparts": ritual_dayparts,
        "last_sync": last_sync,
        "first_order": first_order,
    }


TEMPLATE = r'''<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kenner Group Owner Dashboard</title>
<style>
:root{--bg:#0a0c0f;--panel:#14171c;--border:#252a31;--text:#f6f4ef;--muted:#959ba4;--accent:#e9e4da}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,Arial,sans-serif}.wrap{max-width:1320px;margin:auto;padding:28px}.top{display:flex;justify-content:space-between;align-items:end;gap:20px;flex-wrap:wrap}.brand{font-size:12px;letter-spacing:.24em;color:#aaa}.title{font-size:36px;font-weight:700;margin:5px 0}.subtitle{color:var(--muted);font-size:13px}.filters{display:flex;gap:9px;flex-wrap:wrap}.filters select{background:#171a1f;color:#fff;border:1px solid #323740;border-radius:9px;padding:10px 12px}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:22px}.card{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:18px}.k{font-size:11px;color:#a5a9b0;text-transform:uppercase;letter-spacing:.10em}.v{font-size:29px;font-weight:750;margin-top:8px}.section{margin-top:26px}.section h2{font-size:18px;margin-bottom:10px}.table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--border);border-radius:14px;overflow:hidden}.table th,.table td{padding:12px 14px;border-bottom:1px solid var(--border);text-align:right}.table th{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:#aeb3ba}.table th:first-child,.table td:first-child{text-align:left}.bar{height:8px;background:#2d323a;border-radius:99px;overflow:hidden}.fill{height:100%;background:var(--accent)}.muted{color:var(--muted);font-size:12px}.cols{display:grid;grid-template-columns:1.25fr 1fr;gap:16px}.notice{margin-top:18px;background:#111419;border:1px solid var(--border);border-radius:10px;padding:12px;color:#aeb3ba;font-size:12px}@media(max-width:900px){.grid{grid-template-columns:repeat(2,1fr)}.cols{grid-template-columns:1fr}}@media(max-width:500px){.grid{grid-template-columns:1fr}.title{font-size:29px}.wrap{padding:18px}}
</style>
</head>
<body><div class="wrap">
<div class="top">
  <div><div class="brand">KENNER GROUP</div><div class="title">Owner Dashboard</div><div class="subtitle">Toast operating view · {{days}} day window{% if location!='All' %} · {{location}}{% endif %}</div></div>
  <form class="filters" method="get">
    <select name="location" onchange="this.form.submit()">{% for x in ['All','Ritual','Dogwood','Madison Maison'] %}<option {% if x==location %}selected{% endif %}>{{x}}</option>{% endfor %}</select>
    <select name="days" onchange="this.form.submit()">{% for x in [1,2,7,14,30,90] %}<option value="{{x}}" {% if x==days %}selected{% endif %}>{{x}} days</option>{% endfor %}</select>
  </form>
</div>
<div class="grid">
  <div class="card"><div class="k">Net Sales</div><div class="v">${{'%.0f'|format(d.totals.net)}}</div></div>
  <div class="card"><div class="k">Labor</div><div class="v">${{'%.0f'|format(d.totals.labor_cost)}}</div><div class="muted">{{'%.1f'|format(d.totals.labor_pct)}}% of net sales</div></div>
  <div class="card"><div class="k">Average Check</div><div class="v">${{'%.2f'|format(d.totals.avg_check)}}</div></div>
  <div class="card"><div class="k">Checks / Orders</div><div class="v">{{d.totals.checks}} / {{d.totals.orders}}</div></div>
  <div class="card"><div class="k">Discounts</div><div class="v">${{'%.0f'|format(d.totals.discounts)}}</div></div>
  <div class="card"><div class="k">Tips</div><div class="v">${{'%.0f'|format(d.totals.tips)}}</div></div>
  <div class="card"><div class="k">Labor Hours</div><div class="v">{{'%.1f'|format(d.totals.labor_hours)}}</div><div class="muted">OT {{'%.1f'|format(d.totals.ot_hours)}}</div></div>
  <div class="card"><div class="k">Gross Sales</div><div class="v">${{'%.0f'|format(d.totals.gross)}}</div></div>
</div>
<div class="section"><h2>Location Comparison</h2><table class="table"><tr><th>Location</th><th>Net Sales</th><th>Checks</th><th>Avg Check</th><th>Labor $</th><th>Labor %</th></tr>{% for loc,x in d.by_location.items() %}<tr><td>{{loc}}</td><td>${{'%.0f'|format(x.net)}}</td><td>{{x.checks}}</td><td>${{'%.2f'|format(x.avg_check)}}</td><td>${{'%.0f'|format(x.labor_cost)}}</td><td>{{'%.1f'|format(x.labor_pct)}}%</td></tr>{% endfor %}</table></div>
{% if location in ['All','Ritual'] %}<div class="section"><h2>Ritual Dayparts</h2><table class="table">{% for n,v in d.ritual_dayparts.items() %}<tr><td>{{n}}</td><td>${{'%.0f'|format(v)}}</td></tr>{% endfor %}</table></div>{% endif %}
<div class="section cols"><div><h2>Sales by Hour</h2><table class="table">{% set mx=(d.hourly|map(attribute=1)|max) if d.hourly else 1 %}{% for h,v in d.hourly %}<tr><td>{{h}}</td><td style="width:55%"><div class="bar"><div class="fill" style="width:{{(v/mx*100) if mx else 0}}%"></div></div></td><td>${{'%.0f'|format(v)}}</td></tr>{% endfor %}</table></div><div><h2>Payment Mix</h2><table class="table">{% for n,v in d.payments %}<tr><td>{{n}}</td><td>${{'%.0f'|format(v)}}</td></tr>{% endfor %}</table></div></div>
<div class="section"><h2>Top Server / Employee Sales</h2><table class="table">{% for n,v in d.servers %}<tr><td>{{n}}</td><td>${{'%.0f'|format(v)}}</td></tr>{% endfor %}</table></div>
<div class="notice">Last sync: {% if d.last_sync %}{{d.last_sync[0]}} · {{d.last_sync[1]}}{% else %}not available{% endif %}. Historical reporting begins with the first records stored in this database{% if d.first_order %} ({{d.first_order}}){% endif %}; older ranges will remain incomplete until we run a backfill.</div>
</div></body></html>'''


@app.get("/")
@requires_auth
def dashboard():
    try:
        days = max(1, min(365, int(request.args.get("days", "7"))))
    except Exception:
        days = 7
    location = request.args.get("location", "All")
    if location not in ["All"] + LOCATIONS:
        location = "All"
    try:
        data = load_dashboard(days, location)
        return render_template_string(TEMPLATE, d=data, days=days, location=location)
    except Exception as exc:
        return Response(f"Dashboard error: {exc}", status=500)


@app.get("/health")
def health():
    return jsonify(
        {
            "status": "ok",
            "database_configured": bool(DATABASE_URL),
            "password_configured": bool(DASHBOARD_PASSWORD),
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
