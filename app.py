import json
import os
from datetime import datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

import psycopg
from flask import Flask, Response, jsonify, render_template_string, request

app = Flask(__name__)
DATABASE_URL = os.environ.get("DATABASE_URL")
DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "owner")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD")
LOCAL_TZ = ZoneInfo(os.environ.get("DASHBOARD_TIMEZONE", "America/New_York"))

def requires_auth(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not DASHBOARD_PASSWORD:
            return Response("Dashboard password is not configured.", status=503)
        auth = request.authorization
        if not auth or auth.username != DASHBOARD_USER or auth.password != DASHBOARD_PASSWORD:
            return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Kenner Group Owner Dashboard"'})
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

def business_date_int(d):
    return int(d.strftime("%Y%m%d"))

def date_window(preset):
    today = datetime.now(LOCAL_TZ).date()
    if preset == "today":
        return today, today, "Today"
    if preset == "yesterday":
        d = today - timedelta(days=1)
        return d, d, "Yesterday"
    if preset == "last30":
        return today - timedelta(days=29), today, "Last 30 business dates"
    if preset == "mtd":
        return today.replace(day=1), today, "Month to date"
    if preset == "ytd":
        return today.replace(month=1, day=1), today, "Year to date"
    return today - timedelta(days=6), today, "Last 7 business dates"

def payment_label(payment):
    typ = payment.get("type") or payment.get("paymentMethod") or payment.get("cardType") or "Other"
    if isinstance(typ, dict):
        return typ.get("name") or typ.get("guid") or "Other"
    return str(typ)

def employee_label(value):
    if not isinstance(value, dict):
        return None
    name = f"{value.get('firstName') or ''} {value.get('lastName') or ''}".strip()
    return name or value.get("guid") or value.get("entityType")

def refund_amount(payment):
    total = 0.0
    refund = payment.get("refund")
    if isinstance(refund, dict):
        total += money(refund.get("refundAmount"))
    refunds = payment.get("refunds")
    if isinstance(refunds, list):
        total += sum(money(r.get("refundAmount")) for r in refunds if isinstance(r, dict))
    return total

def order_metrics(order, location_name):
    result = {
        "gross":0.0,"net":0.0,"tax":0.0,"tips":0.0,"discounts":0.0,"refunds":0.0,
        "checks":0,"payments":{},"servers":{},"hourly":{},
        "dayparts":{"Dinner (5-10 PM)":0.0,"Nightlife (10 PM-4 AM)":0.0,"Other":0.0}
    }
    if order.get("voided") or order.get("deleted") or order.get("excessFood"):
        return result

    for check in order.get("checks") or []:
        if check.get("voided") or check.get("deleted"):
            continue
        result["checks"] += 1

        # Toast documented net-sales starting point.
        check_net = money(check.get("amount"))

        for selection in check.get("selections") or []:
            if selection.get("voided"):
                continue
            if selection.get("deferred") or selection.get("selectionType") == "HOUSE_ACCOUNT_PAY_BALANCE":
                check_net -= money(selection.get("price"))

        for charge in check.get("appliedServiceCharges") or []:
            if charge.get("serviceChargeCategory") == "FUNDRAISING_CAMPAIGN":
                check_net -= money(charge.get("chargeAmount"))

        check_refunds = 0.0
        for payment in check.get("payments") or []:
            if payment.get("voided"):
                continue
            check_refunds += refund_amount(payment)
            result["tips"] += money(payment.get("tipAmount"))
            label = payment_label(payment)
            result["payments"][label] = result["payments"].get(label, 0.0) + money(payment.get("amount"))

        check_net -= check_refunds
        result["refunds"] += check_refunds
        result["net"] += check_net
        result["gross"] += money(check.get("totalAmount")) or check_net
        result["tax"] += money(check.get("taxAmount"))
        result["discounts"] += sum(abs(money(d.get("discountAmount") or d.get("amount"))) for d in (check.get("appliedDiscounts") or []))

        server = employee_label(check.get("server") or check.get("employee") or {})
        if server:
            result["servers"][server] = result["servers"].get(server, 0.0) + check_net

        dt = parse_dt(check.get("closedDate") or check.get("openedDate") or order.get("openedDate"))
        if dt:
            local_dt = dt.astimezone(LOCAL_TZ)
            hlabel = local_dt.strftime("%I %p").lstrip("0")
            result["hourly"][hlabel] = result["hourly"].get(hlabel, 0.0) + check_net
            if location_name == "Ritual":
                h = local_dt.hour
                if 17 <= h < 22:
                    result["dayparts"]["Dinner (5-10 PM)"] += check_net
                elif h >= 22 or h < 4:
                    result["dayparts"]["Nightlife (10 PM-4 AM)"] += check_net
                else:
                    result["dayparts"]["Other"] += check_net
    return result

def labor_metrics(entry):
    in_dt = parse_dt(entry.get("inDate") or entry.get("clockIn"))
    out_dt = parse_dt(entry.get("outDate") or entry.get("clockOut"))
    hours = (out_dt - in_dt).total_seconds() / 3600.0 if in_dt and out_dt and out_dt > in_dt else 0.0
    regular = money(entry.get("regularHours"))
    overtime = money(entry.get("overtimeHours"))
    if regular or overtime:
        hours = regular + overtime
    rate = money(entry.get("hourlyWage") or entry.get("wage") or entry.get("payRate"))
    labor_cost = money(entry.get("totalPay"))
    if not labor_cost and rate and hours:
        labor_cost = rate * hours
    return hours, overtime, labor_cost

def load_dashboard(preset="last7", location="All"):
    start_date, end_date, label = date_window(preset)
    start_bd, end_bd = business_date_int(start_date), business_date_int(end_date)
    location_sql = "" if location == "All" else " AND location_name = %s"
    op = [start_bd, end_bd] + ([] if location == "All" else [location])
    lp = [start_bd, end_bd] + ([] if location == "All" else [location])

    with psycopg.connect(DATABASE_URL) as conn:
        orders = conn.execute(
            "SELECT location_name, raw FROM toast_orders_raw WHERE business_date BETWEEN %s AND %s" + location_sql, op
        ).fetchall()
        labor = conn.execute(
            "SELECT location_name, raw FROM toast_time_entries_raw WHERE business_date BETWEEN %s AND %s" + location_sql, lp
        ).fetchall()
        last_sync = conn.execute("SELECT finished_at,status,detail FROM toast_sync_runs ORDER BY id DESC LIMIT 1").fetchone()

    totals = {"gross":0.0,"net":0.0,"tax":0.0,"tips":0.0,"discounts":0.0,"refunds":0.0,
              "checks":0,"orders":0,"labor_hours":0.0,"ot_hours":0.0,"labor_cost":0.0}
    by_location, hourly, payments, servers = {}, {}, {}, {}
    dayparts = {"Dinner (5-10 PM)":0.0,"Nightlife (10 PM-4 AM)":0.0,"Other":0.0}

    for loc, raw in orders:
        if isinstance(raw, str):
            raw = json.loads(raw)
        m = order_metrics(raw or {}, loc)
        row = by_location.setdefault(loc, {"net":0.0,"checks":0,"orders":0,"labor_cost":0.0,"labor_hours":0.0})
        row["net"] += m["net"]; row["checks"] += m["checks"]; row["orders"] += 1
        totals["orders"] += 1
        for k in ("gross","net","tax","tips","discounts","refunds","checks"):
            totals[k] += m[k]
        for k,v in m["hourly"].items(): hourly[k] = hourly.get(k,0.0)+v
        for k,v in m["payments"].items(): payments[k] = payments.get(k,0.0)+v
        for k,v in m["servers"].items(): servers[k] = servers.get(k,0.0)+v
        for k,v in m["dayparts"].items(): dayparts[k] += v

    for loc, raw in labor:
        if isinstance(raw, str):
            raw = json.loads(raw)
        h, ot, cost = labor_metrics(raw or {})
        row = by_location.setdefault(loc, {"net":0.0,"checks":0,"orders":0,"labor_cost":0.0,"labor_hours":0.0})
        row["labor_hours"] += h; row["labor_cost"] += cost
        totals["labor_hours"] += h; totals["ot_hours"] += ot; totals["labor_cost"] += cost

    for row in by_location.values():
        row["avg_check"] = row["net"]/row["checks"] if row["checks"] else 0.0
        row["labor_pct"] = row["labor_cost"]/row["net"]*100 if row["net"] else 0.0
    totals["avg_check"] = totals["net"]/totals["checks"] if totals["checks"] else 0.0
    totals["labor_pct"] = totals["labor_cost"]/totals["net"]*100 if totals["net"] else 0.0

    hour_order = [f"{h} AM" for h in [4,5,6,7,8,9,10,11]] + ["12 PM"] + [f"{h} PM" for h in range(1,12)] + ["12 AM","1 AM","2 AM","3 AM"]
    return {
        "totals":totals,"by_location":by_location,
        "hourly":[(h,hourly[h]) for h in hour_order if h in hourly],
        "payments":sorted(payments.items(),key=lambda x:x[1],reverse=True),
        "servers":sorted(servers.items(),key=lambda x:x[1],reverse=True)[:10],
        "dayparts":dayparts,"last_sync":last_sync,
        "label":label,"start_date":start_date,"end_date":end_date
    }

TEMPLATE = '''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kenner Group Owner Dashboard</title><style>
body{margin:0;background:#0a0c0f;color:#f6f4ef;font-family:Arial,sans-serif}.wrap{max-width:1320px;margin:auto;padding:28px}
.top{display:flex;justify-content:space-between;align-items:end;gap:20px;flex-wrap:wrap}.brand{font-size:12px;letter-spacing:.24em;color:#aaa}.title{font-size:36px;font-weight:700;margin:5px 0}
.muted{color:#959ba4;font-size:12px}.filters{display:flex;gap:9px}.filters select{background:#171a1f;color:#fff;border:1px solid #323740;border-radius:9px;padding:10px 12px}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:22px}.card{background:#14171c;border:1px solid #252a31;border-radius:14px;padding:18px}
.k{font-size:11px;color:#a5a9b0;text-transform:uppercase;letter-spacing:.1em}.v{font-size:29px;font-weight:750;margin-top:8px}.section{margin-top:26px}
.table{width:100%;border-collapse:collapse;background:#14171c;border:1px solid #252a31}.table th,.table td{padding:12px 14px;border-bottom:1px solid #252a31;text-align:right}.table th:first-child,.table td:first-child{text-align:left}
.cols{display:grid;grid-template-columns:1.25fr 1fr;gap:16px}.bar{height:8px;background:#2d323a;border-radius:99px;overflow:hidden}.fill{height:100%;background:#e9e4da}
.notice{margin-top:18px;background:#111419;border:1px solid #252a31;border-radius:10px;padding:12px;color:#aeb3ba;font-size:12px}
@media(max-width:900px){.grid{grid-template-columns:repeat(2,1fr)}.cols{grid-template-columns:1fr}}@media(max-width:500px){.grid{grid-template-columns:1fr}.title{font-size:29px}}
</style></head><body><div class="wrap">
<div class="top"><div><div class="brand">KENNER GROUP</div><div class="title">Owner Dashboard</div><div class="muted">{{d.label}} · {{d.start_date}} through {{d.end_date}}{% if location!='All' %} · {{location}}{% endif %}</div></div>
<form class="filters" method="get"><select name="location" onchange="this.form.submit()">{% for x in ['All','Ritual','Dogwood','Madison Maison'] %}<option {% if x==location %}selected{% endif %}>{{x}}</option>{% endfor %}</select>
<select name="preset" onchange="this.form.submit()">{% for v,t in [('today','Today'),('yesterday','Yesterday'),('last7','Last 7 Days'),('last30','Last 30 Days'),('mtd','MTD'),('ytd','YTD')] %}<option value="{{v}}" {% if v==preset %}selected{% endif %}>{{t}}</option>{% endfor %}</select></form></div>
<div class="grid">
<div class="card"><div class="k">Net Sales</div><div class="v">${{'%.2f'|format(d.totals.net)}}</div></div>
<div class="card"><div class="k">Labor</div><div class="v">${{'%.0f'|format(d.totals.labor_cost)}}</div><div class="muted">{{'%.1f'|format(d.totals.labor_pct)}}% of net sales</div></div>
<div class="card"><div class="k">Average Check</div><div class="v">${{'%.2f'|format(d.totals.avg_check)}}</div></div>
<div class="card"><div class="k">Checks / Orders</div><div class="v">{{d.totals.checks}} / {{d.totals.orders}}</div></div>
<div class="card"><div class="k">Discounts</div><div class="v">${{'%.0f'|format(d.totals.discounts)}}</div></div>
<div class="card"><div class="k">Refunds</div><div class="v">${{'%.0f'|format(d.totals.refunds)}}</div></div>
<div class="card"><div class="k">Tips</div><div class="v">${{'%.0f'|format(d.totals.tips)}}</div></div>
<div class="card"><div class="k">Labor Hours</div><div class="v">{{'%.1f'|format(d.totals.labor_hours)}}</div></div>
</div>
<div class="section"><h2>Location Comparison</h2><table class="table"><tr><th>Location</th><th>Net Sales</th><th>Checks</th><th>Avg Check</th><th>Labor $</th><th>Labor %</th></tr>{% for loc,x in d.by_location.items() %}<tr><td>{{loc}}</td><td>${{'%.2f'|format(x.net)}}</td><td>{{x.checks}}</td><td>${{'%.2f'|format(x.avg_check)}}</td><td>${{'%.0f'|format(x.labor_cost)}}</td><td>{{'%.1f'|format(x.labor_pct)}}%</td></tr>{% endfor %}</table></div>
{% if location in ['All','Ritual'] %}<div class="section"><h2>Ritual Dayparts</h2><table class="table">{% for n,v in d.dayparts.items() %}<tr><td>{{n}}</td><td>${{'%.2f'|format(v)}}</td></tr>{% endfor %}</table></div>{% endif %}
<div class="section cols"><div><h2>Sales by Hour</h2><table class="table">{% set mx=(d.hourly|map(attribute=1)|max) if d.hourly else 1 %}{% for h,v in d.hourly %}<tr><td>{{h}}</td><td style="width:55%"><div class="bar"><div class="fill" style="width:{{(v/mx*100) if mx else 0}}%"></div></div></td><td>${{'%.0f'|format(v)}}</td></tr>{% endfor %}</table></div><div><h2>Payment Mix</h2><table class="table">{% for n,v in d.payments %}<tr><td>{{n}}</td><td>${{'%.0f'|format(v)}}</td></tr>{% endfor %}</table></div></div>
<div class="section"><h2>Top Server / Employee Sales</h2><table class="table">{% for n,v in d.servers %}<tr><td>{{n}}</td><td>${{'%.0f'|format(v)}}</td></tr>{% endfor %}</table></div>
<div class="notice">Net sales now follows Toast's documented Orders API method and date filtering uses Toast businessDate.</div>
<div class="section muted">Last sync: {% if d.last_sync %}{{d.last_sync[0]}} · {{d.last_sync[1]}}{% else %}not available{% endif %}</div>
</div></body></html>'''

@app.route("/")
@requires_auth
def dashboard():
    preset = request.args.get("preset", "last7")
    if preset not in ("today","yesterday","last7","last30","mtd","ytd"):
        preset = "last7"
    location = request.args.get("location", "All")
    if location not in ("All","Ritual","Dogwood","Madison Maison"):
        location = "All"
    try:
        return render_template_string(TEMPLATE, d=load_dashboard(preset,location), preset=preset, location=location)
    except Exception as exc:
        return Response(f"Dashboard error: {exc}", status=500)

@app.route("/health")
def health():
    return jsonify({"status":"ok","database_configured":bool(DATABASE_URL),"password_configured":bool(DASHBOARD_PASSWORD)})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","10000")))
