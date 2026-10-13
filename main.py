import json
import os
import re
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

IST = timezone(timedelta(hours=5, minutes=30))
HERE = os.path.dirname(os.path.abspath(__file__))

MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]
REGIONS = ["North", "South", "East", "West", "Central"]
PRODUCTS = ["Juicer", "Toaster", "Mixer", "Grinder", "Blender", "Kettle",
            "Air Fryer", "Rice Cooker"]
DEFAULT_YEAR = 2026


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_records():
    with open(os.path.join(HERE, "orders.json"), encoding="utf-8") as f:
        data = json.load(f)
    rates = data["rates"]["usd_per_unit"]

    # keep only the latest version of each order id
    latest = {}
    for o in data["orders"]:
        u = parse_ts(o["updated_at"])
        cur = latest.get(o["id"])
        if cur is None or u > cur[0]:
            latest[o["id"]] = (u, o)

    records = []
    for _, o in latest.values():
        created = parse_ts(o["created_at"]).astimezone(IST)
        records.append({
            "id": o["id"],
            "customer": o["customer"],
            "region": o["region"],
            "product": o["product"],
            "qty": o["qty"],
            "status": o["status"],
            "usd": o["amount"] * rates[o["currency"]],
            "date": created.date(),
        })
    return records, rates


def month_range(year, month):
    start = date(year, month, 1)
    nxt = date(year + (month == 12), (month % 12) + 1, 1)
    return start, nxt - timedelta(days=1)


def parse_dates(q):
    ql = q.lower()

    iso = re.findall(r"\d{4}-\d{2}-\d{2}", q)
    if iso:
        ds = [date.fromisoformat(x) for x in iso]
        if len(ds) >= 2:
            return min(ds[0], ds[1]), max(ds[0], ds[1])
        d = ds[0]
        if re.search(r"\b(before|prior to|until)\b", ql):
            return None, d - timedelta(days=1)
        if re.search(r"\b(after|since)\b", ql):
            return d + timedelta(days=1), None
        return d, d

    ym = re.search(r"\b(20\d{2})\b", ql)
    year = int(ym.group(1)) if ym else DEFAULT_YEAR

    qm = re.search(r"\bq([1-4])\b", ql)
    if qm:
        n = int(qm.group(1))
        return month_range(year, 3 * n - 2)[0], month_range(year, 3 * n)[1]

    found = []
    for i, name in enumerate(MONTHS, 1):
        if name == "may" and not re.search(
                r"\b(in|of|during|for|from|since|to|and)\s+may\b|\bmay\s+20\d{2}", ql):
            continue
        if re.search(r"\b" + name + r"\b", ql):
            found.append(i)
    if found:
        return month_range(year, min(found))[0], month_range(year, max(found))[1]

    if ym:
        return date(year, 1, 1), date(year, 12, 31)
    return None, None


def parse_filters(q):
    ql = q.lower()
    f = {}
    for r in REGIONS:
        if re.search(r"\b" + r.lower() + r"\b", ql):
            f["region"] = r
    for p in PRODUCTS:
        pat = p.lower().replace(" ", r"[\s-]?") + r"s?\b"
        if re.search(r"\b" + pat, ql):
            f["product"] = p
    c = re.search(r"\bc\d{3,5}\b", ql)
    if c:
        f["customer"] = c.group(0).upper()
    f["start"], f["end"] = parse_dates(q)
    return f


def matches(r, f):
    if f.get("region") and r["region"] != f["region"]:
        return False
    if f.get("product") and r["product"] != f["product"]:
        return False
    if f.get("customer") and r["customer"] != f["customer"]:
        return False
    if f.get("start") and r["date"] < f["start"]:
        return False
    if f.get("end") and r["date"] > f["end"]:
        return False
    return True


def clean(x):
    x = round(x, 2)
    return int(x) if float(x).is_integer() else x


def answer_question(q, records, rates):
    ql = q.lower()
    f = parse_filters(q)
    rows = [r for r in records if matches(r, f)]

    if "refund" in ql:
        status = "refunded"
    elif "void" in ql or "cancel" in ql:
        status = "void"
    else:
        status = "paid"
    rows = [r for r in rows if r["status"] == status]

    # currency of the answer
    cur = "USD"
    if re.search(r"\b(inr|rupees?)\b", ql):
        cur = "INR"
    elif re.search(r"\b(eur|euros?)\b", ql):
        cur = "EUR"
    divisor = rates[cur]

    # ---- ranking questions: top / highest / lowest ... ----
    rank_words = r"\b(top|highest|best|most|largest|biggest|lowest|least|worst|fewest|smallest)\b"
    if re.search(rank_words, ql):
        dim = None
        m = re.search(r"(which|what|who|top|highest|best|most|lowest|least)\s+(?:\d+\s+)?(?:\w+\s+)?(customer|product|region)", ql)
        if m:
            dim = m.group(2)
        else:
            pos = {d: ql.find(d) for d in ("customer", "product", "region") if d in ql}
            if pos:
                dim = min(pos, key=pos.get)
        if dim:
            if re.search(r"number of orders|most orders|order count|how many orders|orders", ql) and "revenue" not in ql:
                measure = "count"
            elif re.search(r"units|quantity|quantities|pieces", ql):
                measure = "qty"
            else:
                measure = "usd"
            agg = defaultdict(float)
            for r in rows:
                agg[r[dim]] += 1 if measure == "count" else (r["qty"] if measure == "qty" else r["usd"])
            if not agg:
                return None
            reverse = not re.search(r"\b(lowest|least|worst|fewest|smallest)\b", ql)
            ordered = sorted(agg.items(), key=lambda kv: (-kv[1] if reverse else kv[1], kv[0]))
            n = re.search(r"top\s+(\d+)", ql)
            if n:
                return [k for k, _ in ordered[:int(n.group(1))]]
            return ordered[0][0]

    # ---- counting questions ----
    if "how many" in ql or "number of" in ql or re.search(r"\bcount\b", ql):
        if "customer" in ql:
            return len({r["customer"] for r in rows})
        if re.search(r"units|quantity|pieces", ql):
            return clean(sum(r["qty"] for r in rows))
        return len(rows)

    if re.search(r"units|quantity|pieces", ql):
        return clean(sum(r["qty"] for r in rows))

    if re.search(r"\b(average|mean|avg)\b", ql):
        if not rows:
            return 0
        return clean(sum(r["usd"] for r in rows) / len(rows) / divisor)

    # ---- default: total money ----
    return clean(sum(r["usd"] for r in rows) / divisor)


STATE = {}


@asynccontextmanager
async def lifespan(app):
    STATE["records"], STATE["rates"] = load_records()
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


class Question(BaseModel):
    question: str


@app.get("/")
def health():
    return {"status": "ok", "orders_loaded": len(STATE.get("records", []))}


@app.post("/")
def ask(body: Question):
    try:
        ans = answer_question(body.question, STATE["records"], STATE["rates"])
    except Exception:
        ans = 0
    return {"answer": ans}