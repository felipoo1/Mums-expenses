"""Mum's Monthly Spend: monthly expenditure reconciliation for Susan.

Categories: S Susan (Mum), F Family food, T Transport, G Groceries, U Utilities, M Medical, H Household.
X = not Mum's spending (left out). Money coming in is never counted.
A fixed cash amount for groceries (default $500) is added to G every month.

Secrets needed (Streamlit > Settings > Secrets):
  SUPABASE_DB_URL   = "postgresql://..."   (same as the cafe app)
  ANTHROPIC_API_KEY = "sk-ant-..."         (same as the cafe app)
Optional:
  APP_PIN           = "1234"               (leave out for no PIN screen)
  ANTHROPIC_MODEL   = "claude-sonnet-5-5"
"""
import base64
import hashlib
import io
import json
import re
from datetime import date, datetime

import pandas as pd
import psycopg2
import psycopg2.extras
import streamlit as st
from PIL import Image, ImageOps

st.set_page_config(page_title="Mum's Monthly Spend", page_icon="🧾", layout="centered")

CATS = {
    "S": ("Susan (Mum)", "#1240e0"),
    "F": ("Family food", "#e2581b"),
    "T": ("Transport", "#0f8a7e"),
    "G": ("Groceries", "#7a4bd8"),
    "U": ("Utilities", "#c4900a"),
    "M": ("Medical", "#d12f5c"),
    "H": ("Household", "#5c6b1f"),
}
XNAME = "Not Mum's (left out)"
ALLK = list(CATS)
CAT_OPTIONS = [""] + ALLK + ["X"]
MN = ["January", "February", "March", "April", "May", "June", "July",
      "August", "September", "October", "November", "December"]
DEFAULT_CASH = 500.0


def cat_label(k):
    if not k:
        return "- choose -"
    if k == "X":
        return "X · " + XNAME
    return f"{k} · {CATS[k][0]}"


def money(n):
    n = float(n or 0)
    return ("-" if n < 0 else "") + "${:,.2f}".format(abs(n))


def r2(n):
    try:
        return round(float(n), 2)
    except (TypeError, ValueError):
        return 0.0


def merch(desc):
    words = re.sub(r"[^A-Z ]", " ", str(desc or "").upper()).split()
    return " ".join([w for w in words if len(w) > 1][:3])


# ---------------------------------------------------------------- styling
st.markdown("""
<style>
.block-container{padding-top:1.2rem;max-width:860px}
h1,h2,h3{letter-spacing:-.01em}
.big{font-size:44px;font-weight:700;line-height:1;margin:4px 0 6px}
.stamp{display:inline-block;font-weight:700;padding:6px 12px;border-radius:8px;border:2px solid}
.ok{color:#0f7a43}.bad{color:#b4231d}
.bar{display:flex;align-items:center;gap:8px;margin:5px 0}
.bar .k{color:#fff;font-weight:700;width:30px;height:30px;border-radius:7px;display:flex;align-items:center;justify-content:center;flex:none}
.bar .t{flex:1;height:26px;border:1px solid #d5dcf0;border-radius:6px;position:relative;overflow:hidden;min-width:0}
.bar .t i{position:absolute;left:0;top:0;bottom:0}
.bar .t span{position:absolute;left:8px;top:3px;font-size:13px;white-space:nowrap}
.bar .v{width:92px;text-align:right;font-weight:600;flex:none}
.pill{color:#fff;font-size:12px;font-weight:600;padding:2px 8px;border-radius:99px;white-space:nowrap}
.line{border:1px solid #e3e7f3;border-radius:10px;padding:10px 12px;margin-bottom:6px}
.muted{color:#6b7280;font-size:13px}
div[data-testid="stButton"] button{min-height:44px}
</style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------- PIN gate
def pin_gate():
    # No APP_PIN in the secrets = no PIN screen; the app opens straight away.
    pin_set = str(st.secrets.get("APP_PIN", "")).strip()
    if st.session_state.get("authed") or not pin_set:
        return
    # A private link (app address + ?key=PIN) opens straight in, so Mum never has to type anything.
    if st.query_params.get("key", "") == pin_set:
        st.session_state.authed = True
        return
    st.title("Mum's monthly spend")
    pin = st.text_input("Enter your PIN", type="password", max_chars=8)
    if st.button("Open", type="primary"):
        if pin and pin == str(st.secrets.get("APP_PIN", "")):
            st.session_state.authed = True
            st.rerun()
        else:
            st.error("That PIN is not right. Please try again.")
    st.stop()


pin_gate()


# ---------------------------------------------------------------- database
@st.cache_resource
def _conn():
    c = psycopg2.connect(st.secrets["SUPABASE_DB_URL"])
    c.autocommit = True
    return c


def conn():
    c = _conn()
    if c.closed:
        _conn.clear()
        c = _conn()
    return c


def q(sql, params=None, fetch=False):
    for attempt in range(2):
        try:
            with conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                return cur.fetchall() if fetch else None
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            _conn.clear()
            if attempt:
                raise


SCHEMA_VERSION = 3


@st.cache_resource
def ensure_schema(_v=SCHEMA_VERSION):
    q("""
    CREATE TABLE IF NOT EXISTS mum_months(
      month TEXT PRIMARY KEY,
      cash NUMERIC(12,2) NOT NULL DEFAULT 500);
    CREATE TABLE IF NOT EXISTS mum_sources(
      id SERIAL PRIMARY KEY,
      month TEXT NOT NULL,
      kind TEXT NOT NULL,
      name TEXT,
      label TEXT,
      period TEXT,
      printed NUMERIC(12,2),
      file_hash TEXT,
      added TIMESTAMPTZ DEFAULT now());
    CREATE TABLE IF NOT EXISTS mum_lines(
      id SERIAL PRIMARY KEY,
      month TEXT NOT NULL,
      source_id INTEGER REFERENCES mum_sources(id) ON DELETE CASCADE,
      tx_date DATE,
      descr TEXT,
      amt NUMERIC(12,2) NOT NULL DEFAULT 0,
      cat TEXT NOT NULL DEFAULT '',
      done BOOLEAN NOT NULL DEFAULT FALSE,
      ok BOOLEAN NOT NULL DEFAULT FALSE);
    CREATE INDEX IF NOT EXISTS mum_lines_month ON mum_lines(month);
    CREATE INDEX IF NOT EXISTS mum_sources_month ON mum_sources(month);
    CREATE TABLE IF NOT EXISTS mum_rules(
      merchant TEXT PRIMARY KEY,
      cat TEXT NOT NULL);
    ALTER TABLE mum_months ENABLE ROW LEVEL SECURITY;
    ALTER TABLE mum_sources ENABLE ROW LEVEL SECURITY;
    ALTER TABLE mum_lines ENABLE ROW LEVEL SECURITY;
    ALTER TABLE mum_rules ENABLE ROW LEVEL SECURITY;
    ALTER TABLE mum_lines ADD COLUMN IF NOT EXISTS dup BOOLEAN NOT NULL DEFAULT FALSE;
    ALTER TABLE mum_lines ADD COLUMN IF NOT EXISTS dup_ok BOOLEAN NOT NULL DEFAULT FALSE;
    """)
    # v2: every line is filed under the month of its own transaction date,
    # and each document under the month most of its lines fall in.
    refile()
    return True


def refile():
    q("""UPDATE mum_lines SET month = to_char(tx_date, 'YYYY-MM')
         WHERE tx_date IS NOT NULL AND month <> to_char(tx_date, 'YYYY-MM')""")
    q("""UPDATE mum_sources s SET month = sub.mm
         FROM (SELECT source_id, mode() WITHIN GROUP (ORDER BY month) AS mm
               FROM mum_lines WHERE source_id IS NOT NULL GROUP BY source_id) sub
         WHERE s.id = sub.source_id AND s.month <> sub.mm""")


ensure_schema()


def load_month(m):
    row = q("SELECT cash FROM mum_months WHERE month=%s", (m,), True)
    cash = float(row[0]["cash"]) if row else DEFAULT_CASH
    lines = q("SELECT * FROM mum_lines WHERE month=%s ORDER BY tx_date NULLS LAST, id", (m,), True)
    # documents with a line in this month (a statement can cross months), plus empty ones filed here
    sources = q("""SELECT * FROM mum_sources WHERE month=%s
                   OR id IN (SELECT DISTINCT source_id FROM mum_lines WHERE month=%s AND source_id IS NOT NULL)
                   ORDER BY id""", (m, m), True)
    ids = [s["id"] for s in sources]
    whole = {r["source_id"]: r for r in q(
        """SELECT source_id, SUM(amt) AS total, COUNT(*) AS n, MIN(month) AS m1, MAX(month) AS m2
           FROM mum_lines WHERE source_id = ANY(%s) GROUP BY source_id""", (ids,), True)} if ids else {}
    for s in sources:
        s["printed"] = None if s["printed"] is None else float(s["printed"])
        w = whole.get(s["id"])
        s["doc_total"] = r2(w["total"]) if w else 0.0
        s["doc_n"] = int(w["n"]) if w else 0
        s["m1"], s["m2"] = (w["m1"], w["m2"]) if w else (s["month"], s["month"])
    for l in lines:
        l["amt"] = float(l["amt"])
    return cash, sources, lines


def set_cash(m, v):
    q("INSERT INTO mum_months(month,cash) VALUES(%s,%s) ON CONFLICT(month) DO UPDATE SET cash=EXCLUDED.cash", (m, r2(v)))


def load_rules():
    return {r["merchant"]: r["cat"] for r in q("SELECT merchant,cat FROM mum_rules", fetch=True)}


def save_rule(desc, cat):
    m = merch(desc)
    if m and cat:
        q("INSERT INTO mum_rules(merchant,cat) VALUES(%s,%s) ON CONFLICT(merchant) DO UPDATE SET cat=EXCLUDED.cat", (m, cat))


def find_doubles():
    """Groups of lines (any month) with the same date, amount and shop that are not yet decided."""
    rows = q("""SELECT l.id, l.month, l.tx_date, l.descr, l.amt, l.cat, l.dup, l.dup_ok, l.source_id,
                       COALESCE(s.label, 'Added by hand') AS source, COALESCE(s.name, '') AS file
                FROM mum_lines l LEFT JOIN mum_sources s ON s.id = l.source_id
                WHERE NOT l.dup ORDER BY l.id""", fetch=True)
    groups = {}
    for r in rows:
        # a double needs ALL three to match: same date, same amount, same name
        name = " ".join(str(r["descr"] or "").upper().split())
        key = (r["tx_date"], r2(r["amt"]), name)
        groups.setdefault(key, []).append(r)
    return [g for g in groups.values() if len(g) > 1 and not all(x["dup_ok"] for x in g)]


# ---------------------------------------------------------------- calculations
def totals(cash, lines):
    t = {k: 0.0 for k in ALLK + ["X", ""]}
    c = {k: 0 for k in ALLK + ["X", ""]}
    t["G"] += cash
    for l in lines:
        k = l["cat"] if l["cat"] in t else ""
        t[k] += l["amt"]
        c[k] += 1
    t = {k: r2(v) for k, v in t.items()}
    return t, c, r2(sum(t[k] for k in ALLK))


def src_info(sources, lines):
    out = []
    for s in sources:
        ls = [l for l in lines if l["source_id"] == s["id"]]
        read = r2(sum(l["amt"] for l in ls))          # this month's part
        pr = s["printed"]
        doc = s.get("doc_total", read)                  # whole document, all months
        out.append({**s, "lines": ls, "read": read, "doc": doc,
                    "diff": None if pr is None else r2(pr - doc)})
    return out


def is_reconciled(sources, lines):
    if not lines:
        return False
    if any(not l["cat"] or not l["done"] or not l["ok"] for l in lines):
        return False
    return all(s["diff"] == 0 for s in src_info(sources, lines))


# ---------------------------------------------------------------- reading scans with Claude
GUIDE = """Category letters:
T = transport (Grab or Gojek rides, taxis, ComfortDelGro, CDG Zig, TADA, SimplyGo, EZ-Link, TransitLink, SMRT, SBS, bus, MRT, petrol, parking, ERP)
G = groceries (NTUC FairPrice, Sheng Siong, Giant, Cold Storage, Prime, Don Don Donki, RedMart, wet market, minimart)
F = family food (restaurants, cafes, hawker, food court, Kopitiam, Koufu, bakery, bubble tea, McDonald's, KFC, GrabFood, foodpanda, Deliveroo)
U = utilities (SP Group, SP Services, PUB, City Energy, Singtel, StarHub, M1, Circles, MyRepublic, Simba, town council, conservancy)
M = medical (clinic, polyclinic, hospital, dental, TCM, pharmacy, Guardian, Watsons, Unity, optical, physiotherapy, health screening)
H = household (things for the home: furniture, appliances, kitchenware, bedding, cleaning supplies, hardware, repairs, aircon servicing, plumber, IKEA, Courts, Harvey Norman, Gain City, Best Denki, Daiso, Mr DIY, Japan Home, Taobao or Shopee orders for the home)
X = ATM cash withdrawals, transfers between her own accounts, payments to the card account itself
S = everything else that is Susan's own spending (clothes, personal shopping, hair, gifts, subscriptions, insurance, shopping sites, others)"""

PROMPT_STATEMENT = """These are pages of a Singapore bank or credit-card statement belonging to Susan.
List ONLY money going OUT: purchases, card charges, debits, GIRO, NETS, PayNow sent, fees, withdrawals.
Leave out entirely: money coming IN (credits, refunds, deposits, salary, interest, items marked CR), opening or closing balances, previous balance, sub-totals and repeated summary rows.
If the same row appears twice because photos overlap, list it once.
Reply with only JSON in this shape:
{"bank":"DBS POSB","period":"01 Oct 2026 - 31 Oct 2026","printedTotalDebits":1234.56,"lines":[{"date":"2026-10-03","desc":"NTUC FAIRPRICE TAMPINES","amount":45.20,"cat":"G"}]}
printedTotalDebits = the statement's own printed total of withdrawals or debits or new charges for the period if one is shown, else null.
date as YYYY-MM-DD; if no year is shown use the year of the statement period. desc = the merchant text as printed. amount = positive number, no currency sign.
""" + GUIDE + """
Read every row on every page. Do not skip rows and do not invent rows. If there are no outgoing rows reply {"bank":"","period":"","printedTotalDebits":null,"lines":[]}."""

PROMPT_BILL = """These are pages of a Singapore utility bill (electricity, water, gas, phone, internet or town council).
Reply with only JSON in this shape:
{"provider":"SP Group","period":"01 Oct 2026 - 31 Oct 2026","billDate":"2026-11-05","amount":123.45}
amount = this bill's current charges for the period including GST, as a positive number with no currency sign. Do not add overdue balances from earlier bills. If unreadable use null. Never invent values."""


def pdf_text(data):
    """Pull the text out of a downloaded (digital) PDF, keeping the column layout.
    Returns "" for scanned PDFs that are only pictures."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        pages = []
        for i, page in enumerate(reader.pages):
            try:
                t = page.extract_text(extraction_mode="layout")
            except Exception:  # noqa: BLE001
                t = page.extract_text() or ""
            pages.append(f"===== Page {i + 1} =====\n{t}")
        text = "\n".join(pages)
        return text if len(re.sub(r"\s", "", text)) > 100 else ""
    except Exception:  # noqa: BLE001
        return ""


def file_blocks(files):
    blocks = []
    for f in files:
        data = f.getvalue()
        if f.name.lower().endswith(".pdf") or f.type == "application/pdf":
            text = pdf_text(data)
            if text:
                blocks.append({"type": "text", "text": (
                    f"Text of the PDF file {f.name}, copied exactly from the document. Columns are kept in place "
                    "by spacing, so use the column headings (for example Withdrawal / Deposit, or Debit / Credit) "
                    "to tell money out from money in.\n\n" + text)})
                continue
            blocks.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                          "data": base64.b64encode(data).decode()}})
        else:
            img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
            img.thumbnail((2400, 2400))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=88)
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                       "data": base64.b64encode(buf.getvalue()).decode()}})
    return blocks


def parse_json(text):
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=0)
    end = max(text.rfind("}"), text.rfind("]"))
    return json.loads(text[start:end + 1])


def read_with_claude(files, kind):
    import anthropic
    client = anthropic.Anthropic(api_key=st.secrets["ANTHROPIC_API_KEY"])
    model = st.secrets.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    content = file_blocks(files) + [{"type": "text", "text": PROMPT_BILL if kind == "bill" else PROMPT_STATEMENT}]
    resp = client.messages.create(model=model, max_tokens=16000, timeout=300,
                                  messages=[{"role": "user", "content": content}])
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return parse_json(text)


def save_reading(m, kind, name, fhash, j, rules):
    if kind == "bill":
        amount = j.get("amount")
        printed = r2(amount) if isinstance(amount, (int, float)) else None
        label, period = j.get("provider") or "Utility bill", j.get("period") or ""
    else:
        p = j.get("printedTotalDebits")
        printed = r2(p) if isinstance(p, (int, float)) else None
        label, period = j.get("bank") or "Statement", j.get("period") or ""
    sid = q("""INSERT INTO mum_sources(month,kind,name,label,period,printed,file_hash)
               VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (m, kind, name, label, period, printed, fhash), True)[0]["id"]
    per_month = {}
    if kind == "bill":
        d = safe_date(j.get("billDate"), m)
        q("INSERT INTO mum_lines(month,source_id,tx_date,descr,amt,cat) VALUES(%s,%s,%s,%s,%s,'U')",
          (ym(d), sid, d, f"{label} {period}".strip(), printed or 0))
        per_month[ym(d)] = 1
    else:
        for l in j.get("lines") or []:
            amt = r2(l.get("amount"))
            if amt <= 0:
                continue
            desc = str(l.get("desc") or "")
            cat = rules.get(merch(desc)) or (l.get("cat") if l.get("cat") in CAT_OPTIONS else "")
            d = safe_date(l.get("date"), m)
            q("INSERT INTO mum_lines(month,source_id,tx_date,descr,amt,cat) VALUES(%s,%s,%s,%s,%s,%s)",
              (ym(d), sid, d, desc, amt, cat))
            per_month[ym(d)] = per_month.get(ym(d), 0) + 1
    if per_month:   # file the document under the month most of its lines are in
        main = max(per_month, key=per_month.get)
        q("UPDATE mum_sources SET month=%s WHERE id=%s", (main, sid))
    return printed, per_month


def ym(d):
    return f"{d.year}-{d.month:02d}"


def month_name(k):
    return f"{MN[int(k[5:]) - 1]} {k[:4]}"


def safe_date(s, m):
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return date(int(m[:4]), int(m[5:]), 1)


# ---------------------------------------------------------------- month picker
today = date.today()
if "month" not in st.session_state:
    pm = date(today.year - 1, 12, 1) if today.month == 1 else date(today.year, today.month - 1, 1)
    st.session_state.month = pm.strftime("%Y-%m")
if "up_key" not in st.session_state:
    st.session_state.up_key = 0


def shift_month(n):
    y, mo = int(st.session_state.month[:4]), int(st.session_state.month[5:]) + n
    y += (mo - 1) // 12
    mo = (mo - 1) % 12 + 1
    st.session_state.month = f"{y}-{mo:02d}"
    st.session_state.pick_m = MN[mo - 1]
    st.session_state.pick_y = y


def pick_changed():
    st.session_state.month = f"{st.session_state.pick_y}-{MN.index(st.session_state.pick_m) + 1:02d}"


cur = st.session_state.month
st.session_state.setdefault("pick_m", MN[int(cur[5:]) - 1])
st.session_state.setdefault("pick_y", int(cur[:4]))

st.title("Mum's monthly spend")
c1, c2, c3, c4 = st.columns([1, 3, 2, 1], vertical_alignment="bottom")
c1.button("‹", on_click=shift_month, args=(-1,), width="stretch", help="Previous month")
c2.selectbox("Month", MN, key="pick_m", on_change=pick_changed)
years = sorted({today.year - 2, today.year - 1, today.year, today.year + 1, int(cur[:4])})
c3.selectbox("Year", years, key="pick_y", on_change=pick_changed)
c4.button("›", on_click=shift_month, args=(1,), width="stretch", help="Next month")

cur = st.session_state.month
mlabel = f"{MN[int(cur[5:]) - 1]} {cur[:4]}"
cash, sources, lines = load_month(cur)
T, C, SPEND = totals(cash, lines)
SI = src_info(sources, lines)
REC = is_reconciled(sources, lines)

# ---------------------------------------------------------------- summary picture
st.markdown(f"<div class='muted'>Total spend for {mlabel}</div><div class='big'>{money(SPEND)}</div>"
            f"<div class='muted'>Includes {money(cash)} cash for groceries. Money coming in is never counted.</div>",
            unsafe_allow_html=True)
mx = max([T[k] for k in ALLK] + [1])
st.markdown("".join(
    f"<div class='bar'><span class='k' style='background:{c}'>{k}</span><div class='t'>"
    f"<i style='width:{T[k] / mx * 100:.1f}%;background:{c}33;border-right:3px solid {c}'></i><span>{n}</span></div>"
    f"<span class='v'>{money(T[k])}</span></div>" for k, (n, c) in CATS.items()), unsafe_allow_html=True)
if lines:
    waiting = sum(1 for l in lines if not l["ok"] and not l.get("dup"))
    DOUBLES = find_doubles()
    unticked = 0
    bad_docs = sum(1 for s in SI if s["diff"] != 0)
    why = []
    if waiting:
        why.append(f"{waiting} line(s) not checked yet")
    if unticked:
        why.append(f"{unticked} line(s) not ticked in step 3")
    if DOUBLES:
        why.append(f"{len(DOUBLES)} possible double entr{'y' if len(DOUBLES) == 1 else 'ies'} to check")
    if bad_docs:
        why.append(f"{bad_docs} document total(s) do not match yet")
    st.markdown(f"<p><span class='stamp {'ok' if REC else 'bad'}'>{'✓ Reconciled' if REC else 'Not reconciled yet'}</span>"
                f"<br><span class='muted'>{'. '.join(why)}</span></p>", unsafe_allow_html=True)

tab1, tab2, tab4, tab5 = st.tabs(["1 · Upload",
                                  f"2 · Check ({sum(1 for l in lines if not l['ok'] and not l.get('dup'))})",
                                  "3 · Year", "All records"])

# ---------------------------------------------------------------- 1 upload
with tab1:
    st.subheader("Upload this month's documents")
    st.caption("PDFs downloaded from the bank or SP app are best. Photos also work: take them flat, in good light, with the whole page showing. "
               "You can upload several months at once: every line is filed under the month of its own date.")
    kind = st.radio("What are you uploading?", ["Bank or card statement", "Utility bill (U)"], horizontal=True)
    kind = "bill" if kind.startswith("Utility") else "statement"
    files = st.file_uploader("Choose files", type=["pdf", "jpg", "jpeg", "png", "webp"],
                             accept_multiple_files=True, key=f"up{st.session_state.up_key}")
    together = False
    if files and len(files) > 1 and all(not f.name.lower().endswith(".pdf") for f in files):
        together = st.checkbox("These photos are pages of ONE document", value=True)
    if files and st.button("Read these files", type="primary"):
        rules = load_rules()
        known = {s["file_hash"]: s for s in q("SELECT file_hash,month,label FROM mum_sources WHERE file_hash IS NOT NULL", fetch=True)}
        groups = [files] if together else [[f] for f in files]
        for g in groups:
            name = ", ".join(f.name for f in g)
            fhash = hashlib.sha256(b"".join(f.getvalue() for f in g)).hexdigest()
            if fhash in known:
                k = known[fhash]
                st.warning(f"{name}: already uploaded before ({k['label']}, {k['month']}). Skipped so nothing is counted twice.")
                continue
            with st.spinner(f"Reading {name}… this can take a minute"):
                try:
                    j = read_with_claude(g, kind)
                    printed, per_month = save_reading(cur, kind, name, fhash, j, rules)
                    n = sum(per_month.values())
                    where = ", ".join(f"{month_name(k)}: {v}" for k, v in sorted(per_month.items()))
                    known[fhash] = {"label": name, "month": cur}
                    if kind == "bill":
                        st.success(f"✓ {name}: bill amount {money(printed or 0)}, filed under {where.split(':')[0]}")
                    else:
                        st.success(f"✓ {name}: {n} line(s) added, filed by date ({where})" + ("" if printed is not None else
                                   ". No printed total found, please type it in step 2."))
                except Exception as e:  # noqa: BLE001
                    st.error(f"✗ {name}: could not be read. Please try again or use a clearer copy. ({type(e).__name__})")
        st.session_state.up_key += 1
        st.button("Done, show me the lines", type="primary")
        st.caption("Then open tab 2 · Check.")
    if sources:
        st.markdown("**Already uploaded this month**")
        for s in SI:
            st.markdown(f"- {s['label']} ({s['period'] or 'no period'}): {len(s['lines'])} line(s) in this month, {money(s['read'])}")

# ---------------------------------------------------------------- 3 reconcile
def pill(k, dup=False):
    if dup:
        return "<span class='pill' style='background:#6b7280'>Double entry · left out</span>"
    if not k:
        return "<span class='bad'>not tagged</span>"
    col = CATS[k][1] if k in CATS else "#6b7280"
    return f"<span class='pill' style='background:{col}'>{cat_label(k)}</span>"


def report_html():
    rows = "".join(f"<tr><td>{cat_label(k)}</td><td class=n>{money(T[k])}</td></tr>" for k in ALLK)
    srcs = ""
    for s in SI:
        ls = "".join(f"<tr><td>{l['tx_date'] or ''}</td><td>{l['descr']}</td><td>{'Double entry (left out)' if l.get('dup') else cat_label(l['cat'])}</td>"
                     f"<td class=n>{money(l['amt'])}</td><td>{'✓' if l['ok'] else '✗'}</td></tr>" for l in s["lines"])
        srcs += (f"<h3>{s['label']} · {s['period'] or ''}</h3><table><tr><th>Date</th><th>Description</th><th>Category</th>"
                 f"<th class=n>Amount</th><th>Checked</th></tr>{ls}<tr class=t><td></td><td>Total of lines</td><td></td>"
                 f"<td class=n>{money(s['read'])}</td><td></td></tr>"
                 + (f"<tr><td></td><td>Whole document (all months)</td><td></td><td class=n>{money(s['doc'])}</td><td></td></tr>" if s['doc'] != s['read'] else "")
                 + f"<tr><td></td><td>Printed on document</td><td></td>"
                 f"<td class=n>{'-' if s['printed'] is None else money(s['printed'])}</td><td></td></tr><tr><td></td>"
                 f"<td>Difference</td><td></td><td class=n>{'-' if s['diff'] is None else money(s['diff'])}</td><td></td></tr></table>")
    man = [l for l in lines if l["source_id"] is None]
    if man:
        srcs += "<h3>Added by hand</h3><table>" + "".join(
            f"<tr><td>{l['tx_date'] or ''}</td><td>{l['descr']}</td><td>{cat_label(l['cat'])}</td><td class=n>{money(l['amt'])}</td>"
            f"<td>{'✓' if l['ok'] else '✗'}</td></tr>" for l in man) + "</table>"
    return (f"<!DOCTYPE html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            f"<title>Reconciliation {mlabel}</title><style>body{{font:14px/1.5 Arial,sans-serif;max-width:800px;margin:24px auto;padding:0 14px}}"
            f"table{{width:100%;border-collapse:collapse}}td,th{{padding:5px;border-bottom:1px solid #ddd;text-align:left}}"
            f".n{{text-align:right;white-space:nowrap}}.t td{{font-weight:bold;border-top:2px solid #111}}</style></head><body>"
            f"<h1>Mum's monthly spend: reconciliation report, {mlabel}</h1><p>Status: <b>{'Reconciled' if REC else 'Not reconciled yet'}</b>"
            f" · Prepared {date.today():%d/%m/%Y}</p><h3>Summary by category</h3><table>{rows}<tr class=t><td>Total</td>"
            f"<td class=n>{money(SPEND)}</td></tr></table><p>G includes {money(cash)} cash for groceries.</p>"
            f"<h2>Line-by-line reconciliation</h2>{srcs}</body></html>")


def month_csv():
    rows = [{"Month": cur, "Category": "G", "Category name": "Groceries", "Date": "", "Description": "Cash for groceries (fixed)",
             "Source": "Cash", "Amount": r2(cash), "Checked": "Yes"}]
    names = {s["id"]: s["label"] for s in sources}
    for l in sorted(lines, key=lambda l: (l["cat"] or "~", str(l["tx_date"]))):
        rows.append({"Month": cur, "Category": l["cat"], "Category name": cat_label(l["cat"]).split(" · ")[-1],
                     "Date": l["tx_date"], "Description": l["descr"], "Source": names.get(l["source_id"], "Added by hand"),
                     "Amount": r2(l["amt"]), "Checked": "Yes" if l["ok"] else "No"})
    return pd.DataFrame(rows).to_csv(index=False)


def cb_cat(lid, desc):
    v = st.session_state.get(f"cat{lid}", "")
    q("UPDATE mum_lines SET cat=%s WHERE id=%s", (v, lid))
    if not v:   # no letter = cannot stay checked
        q("UPDATE mum_lines SET ok=FALSE, done=FALSE WHERE id=%s", (lid,))
        st.session_state.pop(f"tk{lid}", None)
    else:
        save_rule(desc, v)


def cb_amt(lid):
    v = st.session_state.get(f"amt{lid}")
    q("UPDATE mum_lines SET amt=%s, ok=FALSE, done=FALSE WHERE id=%s", (r2(v), lid))
    st.session_state.pop(f"tk{lid}", None)


def cb_tick(lid):
    v = bool(st.session_state.get(f"tk{lid}"))
    q("UPDATE mum_lines SET ok=%s, done=%s WHERE id=%s", (v, v, lid))


def tick_many(ls, value):
    ids = [l["id"] for l in ls]
    if ids:
        q("UPDATE mum_lines SET ok=%s, done=%s WHERE id = ANY(%s)", (value, value, ids))
    for l in ls:
        st.session_state.pop(f"tk{l['id']}", None)


with tab2:
    st.subheader(f"Check each line: {mlabel}")
    st.caption("For each line: make sure the letter is right, then tick it once it matches the statement. "
               "Ticked lines drop out of view so you can see what is left. X means not Mum's spending.")
    doubles = find_doubles()
    if doubles:
        extras = [(g[0], r) for g in doubles for r in g[1:]]   # first copy is always kept
        st.error(f"⚠ {len(extras)} double entr{'y' if len(extras) == 1 else 'ies'} found: "
                 "same name, same amount and same date.")
        st.caption("The first copy of each is kept. Tick the extra copies to delete, or press Select all.")
        b1, b2 = st.columns(2)
        if b1.button("Select all", width="stretch"):
            for _, r in extras:
                st.session_state[f"dsel{r['id']}"] = True
            st.rerun()
        if b2.button("Clear selection", width="stretch"):
            for _, r in extras:
                st.session_state[f"dsel{r['id']}"] = False
            st.rerun()
        for gi, g in enumerate(doubles):
            with st.container(border=True):
                first = g[0]
                st.markdown(f"**{first['descr']}** · {first['tx_date']:%d/%m/%Y} · **{money(first['amt'])}**")
                st.markdown(f"<span class='muted'>Keep: copy 1 from {first['source']}</span>", unsafe_allow_html=True)
                for i, r in enumerate(g[1:], start=2):
                    st.checkbox(f"Delete copy {i} (from {r['source']})", key=f"dsel{r['id']}")
                if st.button("These are real, keep all", key=f"dupok{gi}"):
                    q("UPDATE mum_lines SET dup_ok=TRUE WHERE id = ANY(%s)", ([r["id"] for r in g],))
                    st.rerun()
        chosen = [(k, r) for k, r in extras if st.session_state.get(f"dsel{r['id']}")]
        if st.button(f"🗑 Delete {len(chosen)} selected double entr{'y' if len(chosen) == 1 else 'ies'}",
                     type="primary", disabled=not chosen, width="stretch"):
            for keep, r in chosen:
                if r["source_id"] is not None and r["source_id"] != keep["source_id"]:
                    # copy belongs to another statement: keep it on that statement so its printed total
                    # still matches, but take it out of Mum's spending
                    q("UPDATE mum_lines SET dup=TRUE, cat='X', done=TRUE, ok=TRUE WHERE id=%s", (r["id"],))
                else:
                    q("DELETE FROM mum_lines WHERE id=%s", (r["id"],))
                st.session_state.pop(f"dsel{r['id']}", None)
            st.success(f"Deleted {len(chosen)} double entr{'y' if len(chosen) == 1 else 'ies'}.")
            st.rerun()

    n_all = sum(1 for l in lines if not l.get("dup"))
    n_ok = sum(1 for l in lines if l["ok"] and not l.get("dup"))
    if lines:
        st.markdown(f"**{n_ok} of {n_all}** lines checked")
        st.progress(n_ok / n_all if n_all else 1.0)
    can_tick = [l for l in lines if l["cat"] and not l["ok"] and not l.get("dup")]
    if can_tick and st.button(f"✓ Tick all {len(can_tick)} line(s) that have a letter", type="primary"):
        tick_many(can_tick, True)
        st.rerun()
    show_all = st.toggle("Show checked lines too", value=False)

    new_cash = st.number_input("Cash for groceries this month $", value=float(cash), min_value=0.0, step=10.0, format="%.2f")
    if r2(new_cash) != r2(cash):
        set_cash(cur, new_cash)
        st.rerun()

    groups = [(s, s["lines"]) for s in SI]
    man = [l for l in lines if l["source_id"] is None]
    if man:
        groups.append(({"id": None, "label": "Added by hand", "period": "", "printed": None, "diff": None,
                        "read": r2(sum(l["amt"] for l in man))}, man))
    for s, ls in groups:
        with st.container(border=True):
            st.markdown(f"#### {s['label']}" + (f" · {s['period']}" if s.get("period") else ""))
            if s["id"] is not None:
                pr = st.number_input("Total printed on the document $", value=s["printed"], min_value=0.0, step=0.01,
                                     format="%.2f", key=f"pr{s['id']}", placeholder="type the total from the statement")
                if (pr is None) != (s["printed"] is None) or (pr is not None and r2(pr) != s["printed"]):
                    q("UPDATE mum_sources SET printed=%s WHERE id=%s", (None if pr is None else r2(pr), s["id"]))
                    st.rerun()
            real = [l for l in ls if not l.get("dup")]
            open_ls = [l for l in real if not l["ok"]]
            tickable = [l for l in open_ls if l["cat"]]
            if tickable and st.button(f"Tick all {len(tickable)} here", key=f"ta{s['id']}"):
                tick_many(tickable, True)
                st.rerun()
            shown = ls if show_all else open_ls
            for l in shown:
                lid = l["id"]
                with st.container(border=True):
                    st.markdown(f"**{l['descr'] or '(no description)'}**  \n<span class='muted'>"
                                f"{l['tx_date']:%d/%m/%Y}</span>" if l["tx_date"] else f"**{l['descr']}**",
                                unsafe_allow_html=True)
                    if l.get("dup"):
                        st.markdown(pill("X", True) + f" **{money(l['amt'])}**", unsafe_allow_html=True)
                        continue
                    a, b = st.columns([3, 2])
                    a.selectbox("Category", CAT_OPTIONS,
                                index=CAT_OPTIONS.index(l["cat"]) if l["cat"] in CAT_OPTIONS else 0,
                                format_func=cat_label, key=f"cat{lid}", on_change=cb_cat, args=(lid, l["descr"]))
                    b.number_input("Amount $", value=float(l["amt"]), min_value=0.0, step=0.01, format="%.2f",
                                   key=f"amt{lid}", on_change=cb_amt, args=(lid,))
                    st.checkbox("✓ Checked against the statement", value=bool(l["ok"]), key=f"tk{lid}",
                                on_change=cb_tick, args=(lid,), disabled=not l["cat"],
                                help=None if l["cat"] else "Choose a letter first")
                    with st.expander("Change date or description, or delete"):
                        nd = st.date_input("Date", value=l["tx_date"] or date(int(cur[:4]), int(cur[5:]), 1),
                                           key=f"dt{lid}", format="DD/MM/YYYY")
                        ns = st.text_input("Description", value=l["descr"] or "", key=f"ds{lid}")
                        x, y = st.columns(2)
                        if x.button("Save", key=f"sv{lid}", width="stretch"):
                            q("UPDATE mum_lines SET tx_date=%s, descr=%s, month=%s WHERE id=%s", (nd, ns, ym(nd), lid))
                            st.rerun()
                        if y.button("Delete line", key=f"del{lid}", width="stretch"):
                            st.session_state[f"confirm_del{lid}"] = True
                        if st.session_state.get(f"confirm_del{lid}"):
                            st.warning("Delete this line? Only do this if it is not real spending.")
                            if st.button("Yes, delete", key=f"del2{lid}"):
                                q("DELETE FROM mum_lines WHERE id=%s", (lid,))
                                st.rerun()
            if not shown and ls:
                st.markdown("<span class='ok'>✓ Every line here is checked</span>", unsafe_allow_html=True)
            diff = s.get("diff")
            spans = s.get("m1") and s.get("m2") and s["m1"] != s["m2"]
            if spans:
                st.markdown(f"This month's part: **{money(s['read'])}** ({len(ls)} line(s)). "
                            f"<span class='muted'>This document also has lines in other months "
                            f"({month_name(s['m1'])} to {month_name(s['m2'])}), so it is checked as a whole below.</span>",
                            unsafe_allow_html=True)
            st.markdown(f"{'Whole document: lines' if spans else 'Total of lines'} **{money(s.get('doc', s['read']))}** · Printed "
                        f"**{'-' if s.get('printed') is None else money(s['printed'])}** · Difference "
                        + ("<span class='bad'>type the printed total</span>" if s["id"] is not None and diff is None else
                           "" if s["id"] is None else
                           f"<span class='{'ok' if diff == 0 else 'bad'}'>{'✓ $0.00' if diff == 0 else money(diff)}</span>"),
                        unsafe_allow_html=True)
            if s["id"] is not None:
                if st.button("Remove this document and its lines", key=f"rm{s['id']}"):
                    st.session_state[f"confirm_rm{s['id']}"] = True
                if st.session_state.get(f"confirm_rm{s['id']}"):
                    st.warning("This removes the document and all its lines.")
                    if st.button("Yes, remove it", key=f"rm2{s['id']}"):
                        q("DELETE FROM mum_sources WHERE id=%s", (s["id"],))
                        st.rerun()
    if not lines:
        st.info("Upload a statement in step 1 and the lines appear here.")
    with st.expander("+ Add a line by hand"):
        with st.form("manual", clear_on_submit=True):
            d = st.date_input("Date", value=date(int(cur[:4]), int(cur[5:]), 1), format="DD/MM/YYYY")
            ds = st.text_input("Description")
            am = st.number_input("Amount $", min_value=0.0, step=0.01, format="%.2f")
            ct = st.selectbox("Category", CAT_OPTIONS, format_func=cat_label)
            if st.form_submit_button("Add line"):
                if am <= 0 or not ds:
                    st.error("Please fill in a description and an amount.")
                else:
                    q("INSERT INTO mum_lines(month,source_id,tx_date,descr,amt,cat) VALUES(%s,NULL,%s,%s,%s,%s)",
                      (ym(d), d, ds, r2(am), ct))
                    st.rerun()


    st.markdown("### Reconciliation report")
    a, b = st.columns(2)
    with a:
        st.markdown("**Summary by category**")
        st.markdown("<table style='width:100%'>" + "".join(
            f"<tr><td>{pill(k)}</td><td style='text-align:right'>{money(T[k])}</td></tr>" for k in ALLK) +
            f"<tr><td><b>Total</b></td><td style='text-align:right'><b>{money(SPEND)}</b></td></tr></table>",
            unsafe_allow_html=True)
    with b:
        read = r2(sum(l["amt"] for l in lines))
        calc = r2(read - T["X"] - T[""] + cash)
        agree = abs(calc - SPEND) < 0.005
        st.markdown("**Reconciliation**")
        st.markdown(f"""<table style='width:100%'>
<tr><td>Spending read from documents</td><td style='text-align:right'>{money(read)}</td></tr>
<tr><td>Less: not Mum's or double entries (X)</td><td style='text-align:right'>({money(T['X'])})</td></tr>
<tr><td>Less: no letter yet</td><td style='text-align:right'>({money(T[''])})</td></tr>
<tr><td>Add: cash for groceries</td><td style='text-align:right'>{money(cash)}</td></tr>
<tr><td><b>Total allocated</b></td><td style='text-align:right'><b>{money(calc)}</b></td></tr>
<tr><td>Check against summary</td><td style='text-align:right' class='{'ok' if agree else 'bad'}'>{'✓ agrees' if agree else '✗'}</td></tr>
</table>""", unsafe_allow_html=True)
    d1, d2 = st.columns(2)
    d1.download_button("Download report", report_html(), file_name=f"Mum reconciliation {cur}.html",
                       mime="text/html", width="stretch", type="primary")
    d2.download_button("Download CSV", month_csv(), file_name=f"Mum spend {cur}.csv", mime="text/csv",
                       width="stretch")


# ---------------------------------------------------------------- 4 year
with tab4:
    y = cur[:4]
    st.subheader(f"Monthly breakdown for {y}")
    allm = q("SELECT month,cash FROM mum_months WHERE month LIKE %s", (y + "-%",), True)
    cashmap = {r["month"]: float(r["cash"]) for r in allm}
    agg = q("""SELECT month,cat,SUM(amt) s, COUNT(*) n, BOOL_AND(done AND ok AND cat<>'') allok
               FROM mum_lines WHERE month LIKE %s GROUP BY month,cat""", (y + "-%",), True)
    rows = []
    for mo in range(1, 13):
        k = f"{y}-{mo:02d}"
        got = [a for a in agg if a["month"] == k]
        if not got:
            rows.append({"Month": MN[mo - 1][:3], **{c: None for c in ALLK}, "Total": None})
            continue
        vals = {c: 0.0 for c in ALLK}
        for a in got:
            if a["cat"] in vals:
                vals[a["cat"]] += float(a["s"])
        vals["G"] += cashmap.get(k, DEFAULT_CASH)
        rows.append({"Month": MN[mo - 1][:3], **{c: r2(v) for c, v in vals.items()}, "Total": r2(sum(vals.values()))})
    df = pd.DataFrame(rows)
    tot = {"Month": "Year", **{c: r2(df[c].fillna(0).sum()) for c in ALLK}, "Total": r2(df["Total"].fillna(0).sum())}
    df = pd.concat([df, pd.DataFrame([tot])], ignore_index=True)
    st.dataframe(df, hide_index=True, width="stretch",
                 column_config={c: st.column_config.NumberColumn(c, format="$%.2f") for c in ALLK + ["Total"]})
    st.caption("S Susan · F Family food · T Transport · G Groceries (incl. cash) · U Utilities · M Medical · H Household. Empty months show blank.")
    st.download_button("Download year CSV", df.to_csv(index=False), file_name=f"Mum spend {y} by month.csv", mime="text/csv")

# ---------------------------------------------------------------- all records (whole database)
with tab5:
    st.subheader("All records")
    st.caption("Every line from every month in one place, plus the monthly cash for groceries.")
    rows = q("""SELECT l.month, l.tx_date, l.descr, l.amt, l.cat, l.done, l.ok, l.dup,
                       COALESCE(s.label, 'Added by hand') AS source
                FROM mum_lines l LEFT JOIN mum_sources s ON s.id = l.source_id
                ORDER BY l.tx_date NULLS LAST, l.id""", fetch=True)
    cash_rows = q("SELECT month, cash FROM mum_months", fetch=True)
    cash_map = {r["month"]: float(r["cash"]) for r in cash_rows}
    for m in {r["month"] for r in rows}:
        cash_map.setdefault(m, DEFAULT_CASH)
    data = [{"Month": r["month"], "Date": r["tx_date"], "Description": r["descr"], "Category": r["cat"] or "",
             "Category name": cat_label(r["cat"]).split(" · ")[-1] if r["cat"] else "Not tagged",
             "Amount": r2(r["amt"]), "Source": r["source"],
             "Status": "Double entry (left out)" if r["dup"] else "Checked" if r["ok"] else "Not checked yet"} for r in rows]
    data += [{"Month": m, "Date": date(int(m[:4]), int(m[5:]), 1), "Description": "Cash for groceries (fixed)",
              "Category": "G", "Category name": "Groceries", "Amount": r2(v), "Source": "Cash", "Status": "Checked"}
             for m, v in cash_map.items()]
    if not data:
        st.info("Nothing saved yet. Upload a statement in step 1.")
    else:
        df = pd.DataFrame(data).sort_values(["Date", "Month"], na_position="last")
        f1, f2 = st.columns(2)
        months_avail = sorted(df["Month"].unique())
        m_from = f1.selectbox("From month", months_avail, index=0)
        m_to = f2.selectbox("To month", months_avail, index=len(months_avail) - 1)
        cats_pick = st.multiselect("Categories", CAT_OPTIONS, default=ALLK, format_func=lambda k: cat_label(k) if k else "Not tagged")
        search = st.text_input("Search description", placeholder="e.g. NTUC, Grab, clinic")
        v = df[(df["Month"] >= m_from) & (df["Month"] <= m_to) & (df["Category"].isin(cats_pick))]
        if search.strip():
            v = v[v["Description"].fillna("").str.contains(search.strip(), case=False, regex=False)]
        st.markdown(f"**{len(v)} record(s) · Total {money(v['Amount'].sum())}**")
        st.dataframe(v, hide_index=True, width="stretch",
                     column_config={"Amount": st.column_config.NumberColumn("Amount", format="$%.2f"),
                                    "Date": st.column_config.DateColumn("Date", format="DD/MM/YYYY")})
        st.download_button("Download these records (CSV)", v.to_csv(index=False),
                           file_name=f"Mum spend records {m_from} to {m_to}.csv", mime="text/csv")

with st.sidebar:
    st.markdown("### How to use")
    st.markdown("1. Pick the month at the top.\n2. **Upload**: statements first, then switch to Utility bill.\n"
                "3. **Check**: type each statement's printed total, make sure each letter is right, tick each line.\n"
                "4. When the stamp says **Reconciled**, download the report.")
    if str(st.secrets.get("APP_PIN", "")).strip() and st.button("Log out"):
        st.session_state.authed = False
        st.rerun()
