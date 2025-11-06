import os, json, hashlib, time, random, urllib.request, urllib.parse, sys, traceback, re
from playwright.sync_api import sync_playwright

# =========================
# Utilidades
# =========================
def load_json_env(key: str, default_val):
    raw = os.getenv(key)
    if not raw or not raw.strip():
        return default_val
    try:
        return json.loads(raw)
    except Exception as e:
        print(f"WARN: invalid {key}; using defaults. Error:", e)
        return default_val

def notify(text: str):
    TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
    TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")
    if TG_TOKEN and TG_CHAT:
        try:
            data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text}).encode()
            urllib.request.urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data, timeout=10)
            return
        except Exception as e:
            print("WARN: Telegram send failed ->", e)
    print("NOTIFY:", text)

def hash_rows(rows):
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

# =========================
# Config
# =========================
URL = os.getenv("URL", "https://catalog.apps.asu.edu/catalog/classes")
QUERIES = load_json_env("QUERIES_JSON", [
    {"subject":"CSE","number":"412","term":"Spring 2026"}
])

# Solo excluir “ASU Online” (iCourse SÍ entra)
LOCATION_EXCLUDE_REGEX = os.getenv("LOCATION_EXCLUDE_REGEX", r"(?i)\bASU\s*Online\b")

STATE = "state.json"
NOTIFY_STATE = "notify_state.json"   # <-- FICHERO NUEVO: persistimos el último ping de “no cambios”
DEBUG_DIR = "debug"
VIDEO_DIR = "recordings"

# Jitter anti-patrón exacto
JITTER_MIN = int(os.getenv("JITTER_MIN_SEC", "0"))
JITTER_MAX = int(os.getenv("JITTER_MAX_SEC", "240"))

# Ping “no change” cada X segundos (default 1h)
NOCHANGE_NOTIFY_INTERVAL = int(os.getenv("NOCHANGE_NOTIFY_INTERVAL_SEC", "3600"))
# Permitir desactivar pings de no-cambios si quieres (1=on, 0=off)
NOCHANGE_PING = int(os.getenv("NOCHANGE_PING", "1"))

# ======== TRIGGERS configurables ========
# Notificar si pasa 0 -> >=1 asientos
TRIGGER_ZERO_TO_POSITIVE = int(os.getenv("TRIGGER_ZERO_TO_POSITIVE", "1"))  # 1=on
# Notificar si caen >= este umbral de golpe
TRIGGER_DROP_THRESHOLD = int(os.getenv("TRIGGER_DROP_THRESHOLD", "5"))      # e.g., 5

# =========================
# Helpers de localización
# =========================
def first_locator(page, kind, value, timeout=9000, name_regex=False):
    import re as _re
    try:
        if kind == "label":
            loc = page.get_by_label(value, exact=False)
        elif kind == "placeholder":
            loc = page.get_by_placeholder(value, exact=False)
        elif kind == "css":
            loc = page.locator(value)
        elif kind == "text":
            loc = page.get_by_text(value, exact=False)
        elif kind == "role":
            role, name = value
            loc = page.get_by_role(role, name=_re.compile(name, _re.I)) if name_regex else page.get_by_role(role, name=name)
        else:
            return None
        loc.first.wait_for(state="visible", timeout=timeout)
        return loc.first
    except Exception:
        return None

def wait_hydrated(page, target_term_text: str):
    first_locator(page, "role", ("button", "Search Classes")).wait_for(state="visible", timeout=20000)
    try:
        page.wait_for_function(
            """(term) => {
                const txt = document.body.innerText || '';
                return (!txt.includes('Previous Terms')) || txt.includes(term);
            }""",
            arg=target_term_text,
            timeout=15000
        )
    except Exception:
        pass
    page.wait_for_timeout(500)

def get_subject_input(page):
    for k, v in [
        ("placeholder", "Subject"),
        ("label", "Subject"),
        ("css", 'input[aria-label*="Subject" i]'),
        ("css", '#subject'),
        ("css", 'input[name="subject"]'),
        ("css", 'input[id*="subject" i]'),
    ]:
        loc = first_locator(page, k, v)
        if loc: return loc
    raise RuntimeError("No se encontró el campo 'Subject'.")

def get_number_input(page):
    for k, v in [
        ("placeholder", "Number"),
        ("label", "Number"),
        ("label", "Course Number"),
        ("css", 'input[aria-label*="Number" i]'),
        ("css", '#number'),
        ("css", 'input[name="number"]'),
        ("css", 'input[name="catalogNbr"]'),
        ("css", '#catalogNbr'),
        ("css", 'input[id*="number" i]'),
        ("css", 'input[id*="catalog" i]'),
    ]:
        loc = first_locator(page, k, v)
        if loc: return loc
    raise RuntimeError("No se encontró el campo 'Number'.")

def set_term(page, term_label_text):
    for k, v in [
        ("css", 'select[name="term"]'),
        ("css", "#term"),
        ("css", 'select[aria-label*="Term" i]'),
        ("label", "Term"),
    ]:
        loc = first_locator(page, k, v)
        if loc:
            try:
                loc.select_option(label=term_label_text)
                return
            except Exception:
                break
    combo = first_locator(page, "role", ("combobox", "Term"), name_regex=True)
    if combo:
        combo.click()
        opt = first_locator(page, "role", ("option", term_label_text))
        if opt: opt.click(); return
        opt2 = first_locator(page, "text", term_label_text)
        if opt2: opt2.click(); return
    label = first_locator(page, "text", "Term")
    if label:
        try: label.click()
        except Exception: pass
        opt3 = first_locator(page, "text", term_label_text)
        if opt3: opt3.click(); return
    raise RuntimeError("No se pudo seleccionar el Term.")

def click_search(page):
    clicked = False
    for k, v, regex in [
        ("role", ("button", "Search Classes"), False),
        ("role", ("button", r"Search\s*Classes"), True),
        ("text", "Search Classes", False),
        ("css", 'button:has-text("Search Classes")', False),
    ]:
        loc = first_locator(page, k, v, name_regex=regex)
        if loc:
            loc.click()
            clicked = True
            break
    if not clicked:
        raise RuntimeError("No encontré el botón de búsqueda.")
    page.keyboard.press("Enter")
    page.wait_for_timeout(800)

def ensure_filters_applied(page, term, subj, num):
    try:
        first_locator(page, "text", "Results for", timeout=15000)
        txt = page.inner_text("body")
        return (term in txt) and (subj in txt) and (num in txt)
    except Exception:
        return False

# =========================
# Extracción de resultados
# =========================
def find_col(headers, needle):
    needle = needle.lower()
    for i, h in enumerate(headers):
        if needle in h.lower():
            return i
    return None

def parse_open_seats(s: str):
    m = re.search(r'(\d+)\s*of\s*(\d+)', s or "", re.I)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None

def should_exclude_location(location_text: str) -> bool:
    if not location_text:
        return False
    try:
        return re.search(LOCATION_EXCLUDE_REGEX, location_text, flags=re.I) is not None
    except re.error:
        return False

def extract_from_table_like(component, is_aria=False):
    headers = [h.strip() for h in (
        component.locator('[role="columnheader"]') if is_aria else component.locator('th')
    ).all_inner_texts()]

    idx_course = find_col(headers, "course")
    idx_title  = find_col(headers, "title")
    idx_num    = find_col(headers, "number")
    idx_instr  = find_col(headers, "instructor")
    idx_days   = find_col(headers, "days")
    idx_start  = find_col(headers, "start")
    idx_end    = find_col(headers, "end")
    idx_loc    = find_col(headers, "location")
    idx_open   = find_col(headers, "open seats")

    rows = []
    row_sel = '[role="row"]' if is_aria else 'tbody tr'
    cell_sel = '[role="gridcell"], [role="cell"]' if is_aria else 'td'
    trs = component.locator(row_sel)
    count = trs.count()
    for i in range(count):
        cells = trs.nth(i).locator(cell_sel)
        if cells.count() == 0:
            continue
        texts = [cells.nth(j).inner_text().strip() for j in range(cells.count())]

        def get(idx):
            return texts[idx].strip() if idx is not None and idx < len(texts) else ""

        course = get(idx_course)
        title  = get(idx_title)
        num    = get(idx_num)          # Class #
        instr  = get(idx_instr)
        days   = get(idx_days)
        start  = get(idx_start)
        endt   = get(idx_end)
        loc    = get(idx_loc)
        open_s = get(idx_open)

        if should_exclude_location(loc):
            continue

        open_now, open_tot = parse_open_seats(open_s)
        rows.append({
            "class_id": num,
            "course": course,
            "title": title,
            "instructor": instr,
            "days": days,
            "start": start,
            "end": endt,
            "location": loc or "",
            "open_text": open_s or "",
            "open_now": open_now if open_now is not None else 0,
            "open_total": open_tot if open_tot is not None else None,
        })
    return rows

def extract_textual(page, subj, num):
    body_txt = page.inner_text("body")
    lines = [l.strip() for l in body_txt.splitlines()]
    rows = []

    course_pat = re.compile(rf'^{re.escape(subj)}\s+{re.escape(num)}\b', re.I)
    i = 0
    while i < len(lines):
        if course_pat.search(lines[i]):
            course_label = lines[i].strip()
            j = i + 1
            while j < len(lines) and not lines[j]:
                j += 1
            title = lines[j] if j < len(lines) else ""

            k = j + 1
            class_id = ""
            for t in lines[k:k+15]:
                m = re.match(r'^\d{4,6}$', t)
                if m:
                    class_id = m.group(0); break

            open_text = ""
            for t in lines[k:k+25]:
                m2 = re.search(r'(\d+)\s+of\s+(\d+)', t, re.I)
                if m2:
                    open_text = f"{m2.group(1)} of {m2.group(2)}"; break
            open_now, open_tot = parse_open_seats(open_text)

            start_time = ""
            for t in lines[k:k+15]:
                m3 = re.search(r'\b(\d{1,2}:\d{2}\s*(AM|PM))\b', t, re.I)
                if m3:
                    start_time = m3.group(1); break

            loc = ""
            for t in lines[k:k+20]:
                if " - " in t or "iCourse" in t:
                    loc = t; break

            if not should_exclude_location(loc):
                rows.append({
                    "class_id": class_id,
                    "course": f"{subj} {num}",
                    "title": title,
                    "instructor": "",
                    "days": "",
                    "start": start_time,
                    "end": "",
                    "location": loc,
                    "open_text": open_text,
                    "open_now": open_now if open_now is not None else 0,
                    "open_total": open_tot if open_tot is not None else None,
                })
            i = k + 10
            continue
        i += 1
    return rows

def wait_component_or_none(page):
    for sel in ['[role="grid"]', '[role="table"]']:
        try:
            comp = page.locator(sel).first
            comp.wait_for(state="visible", timeout=12000)
            return ("aria", comp)
        except Exception:
            continue
    try:
        tbl = page.locator("table").first
        tbl.wait_for(state="visible", timeout=8000)
        return ("html", tbl)
    except Exception:
        return (None, None)

def extract_rows(page, subj, num):
    typ, comp = wait_component_or_none(page)
    if typ == "aria":
        return extract_from_table_like(comp, is_aria=True)
    elif typ == "html":
        return extract_from_table_like(comp, is_aria=False)
    else:
        return extract_textual(page, subj, num)

# =========================
# Flujo principal
# =========================
def reset_search(page):
    btn = first_locator(page, "text", "Clear filters", timeout=2000)
    if btn:
        try: btn.click()
        except Exception: pass
    else:
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(500)

def apply_filters_and_search(page, subj, num, term, tries=3):
    for _ in range(tries):
        wait_hydrated(page, term)
        s_in = get_subject_input(page)
        n_in = get_number_input(page)
        try:
            s_in.fill(""); n_in.fill("")
        except Exception:
            pass
        s_in.fill(subj)
        n_in.fill(num)
        set_term(page, term)
        click_search(page)
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1000)
        if ensure_filters_applied(page, term, subj, num):
            return True
        reset_search(page)
    return False

def group_key(q):
    return f'{q["subject"]}{q["number"]}-{q["term"]}'

def format_line(r, prev=None, triggered=False):
    green = (r["open_now"] or 0) > 0
    dot = "🟢" if green else "🔴"
    if triggered:
        dot += " 🟠"  # solo si el cambio cumple trigger

    # Δ visible solo si hay prev y cambió seats
    delta_txt = ""
    if prev is not None:
        po = prev.get("open_now", 0) or 0
        no = r.get("open_now", 0) or 0
        d = no - po
        if d != 0:
            delta_txt = f' (Δ{("+" if d>0 else "")}{d})'

    seats = r.get("open_text") or (f'{r["open_now"]} of {r["open_total"]}' if r.get("open_total") else f'{r["open_now"]}')
    pieces = [
        f'Class #{(r.get("class_id") or "").strip()}',
        f'{(r.get("course") or "").strip()} - {(r.get("title") or "").strip()}'.strip(" -"),
        f'Open {seats}{delta_txt}',
    ]
    if r.get("location"):
        pieces.append(r["location"].strip())
    if r.get("start"):
        pieces.append(r["start"].strip())
    if r.get("instructor"):
        pieces.append(r["instructor"].strip())
    return f'{dot} ' + " — ".join(pieces)

def run():
    # Jitter
    if JITTER_MAX >= JITTER_MIN and JITTER_MAX > 0:
        time.sleep(random.uniform(JITTER_MIN, JITTER_MAX))

    os.makedirs(DEBUG_DIR, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1366, "height": 768})
        page = context.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)

        all_rows = []
        for q in QUERIES:
            subj = q.get("subject","").strip()
            num  = q.get("number","").strip()
            term = q.get("term","").strip()
            if not (subj and num and term):
                print("WARN: query inválida:", q); continue

            ok = apply_filters_and_search(page, subj, num, term, tries=3)
            if not ok:
                context.close(); browser.close()
                raise RuntimeError(f"No se pudo aplicar filtros para {subj} {num} ({term})")

            rows = extract_rows(page, subj, num)
            for r in rows:
                r["_q"] = group_key(q)
            all_rows.extend(rows)

            page.wait_for_timeout(600)
            reset_search(page)

        context.close()
        browser.close()

    # ===== Estado actual vs anterior
    new_state = {"hash": hash_rows(all_rows), "rows": all_rows, "ts": int(time.time())}
    try:
        old_state = json.load(open(STATE, "r"))
    except Exception:
        old_state = {"hash": None, "rows": []}

    prev_by_id = {r.get("class_id"): r for r in old_state.get("rows", []) if r.get("class_id")}
    curr_by_id = {r.get("class_id"): r for r in new_state["rows"] if r.get("class_id")}

    # ==== TRIGGERS de notificación ====
    # Solo consideramos “cambios interesantes”:
    #  - 0 -> >=1 (si TRIGGER_ZERO_TO_POSITIVE=1)
    #  - caída >= TRIGGER_DROP_THRESHOLD
    triggered_ids = set()
    for k in (curr_by_id.keys() & prev_by_id.keys()):
        prev = prev_by_id[k]
        curr = curr_by_id[k]
        po = prev.get("open_now", 0) or 0
        no = curr.get("open_now", 0) or 0
        fired = False

        if TRIGGER_ZERO_TO_POSITIVE and po == 0 and no > 0:
            fired = True
        elif (po - no) >= TRIGGER_DROP_THRESHOLD:
            fired = True

        if fired:
            triggered_ids.add(k)

    any_change = len(triggered_ids) > 0

    # Agrupar y ordenar para impresión
    groups = {}
    for r in all_rows:
        groups.setdefault(r["_q"], []).append(r)
    for g in groups.values():
        g.sort(key=lambda x: (x.get("open_now") or 0), reverse=True)

    lines = []
    header = "🔔 **CHANGES (triggered)**" if any_change else "⏱️ **Hourly check (no changes)**"
    lines.append(header)

    for qkey in sorted(groups.keys()):
        lines.append(f"\n— {qkey} —")
        for r in groups[qkey]:
            prev = prev_by_id.get(r.get("class_id"))
            trig = r.get("class_id") in triggered_ids
            lines.append(format_line(r, prev=prev, triggered=trig))

    # ===== Pinging horario y guardado de estado
    now = int(time.time())
    try:
        notify_state = json.load(open(NOTIFY_STATE, "r"))
    except Exception:
        notify_state = {"last_nochange_ping": 0}

    if any_change:
        notify("\n".join(lines))
        with open(STATE, "w") as f: json.dump(new_state, f)
        notify_state["last_nochange_ping"] = now   # reset del reloj horario
        with open(NOTIFY_STATE, "w") as f: json.dump(notify_state, f)
        print("CHANGED")
    else:
        with open(STATE, "w") as f: json.dump(new_state, f)
        # solo pings horarios si están habilitados
        if NOCHANGE_PING and (now - notify_state.get("last_nochange_ping", 0) >= NOCHANGE_NOTIFY_INTERVAL):
            notify("\n".join(lines))
            notify_state["last_nochange_ping"] = now
            with open(NOTIFY_STATE, "w") as f: json.dump(notify_state, f)
        print("NOCHANGE")

if __name__ == "__main__":
    try:
        run()
    except Exception:
        print("ERROR:\n", traceback.format_exc())
        sys.exit(1)
