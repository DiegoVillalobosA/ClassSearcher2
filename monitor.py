import os, json, hashlib, time, random, urllib.request, urllib.parse, sys, traceback, re
from playwright.sync_api import sync_playwright

# -------------------- utils --------------------
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

# -------------------- config --------------------
URL = os.getenv("URL", "https://catalog.apps.asu.edu/catalog/classes")
QUERIES = load_json_env("QUERIES_JSON", [{"subject":"CSE","number":"412","term":"Spring 2026"}])
STATE = "state.json"
DEBUG_DIR = "debug"
VIDEO_DIR = "recordings"

# Excluir SOLO estas ubicaciones (texto, case-insensitive)
LOCATION_EXCLUDE = [s.strip().lower() for s in os.getenv("LOCATION_EXCLUDE", "ASU Online").split("|") if s.strip()]

# Jitter aleatorio (para que cada corrida no sea a minuto exacto)
JITTER_MIN_SEC = int(os.getenv("JITTER_MIN_SEC", "300"))  # 5 min
JITTER_MAX_SEC = int(os.getenv("JITTER_MAX_SEC", "600"))  # 10 min

# -------------------- locator helpers --------------------
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
        if loc:
            print(f"DEBUG: subject via {k}='{v}'")
            return loc
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
        if loc:
            print(f"DEBUG: number via {k}='{v}'")
            return loc
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
            print(f"DEBUG: term select via {k}='{v}'")
            try:
                loc.select_option(label=term_label_text)
                return
            except Exception as e:
                print("DEBUG: select_option fallo, probando combobox...", e)
                break
    combo = first_locator(page, "role", ("combobox", "Term"), name_regex=True)
    if combo:
        print("DEBUG: term via role=combobox")
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
            print(f"DEBUG: search via {k}='{v}'")
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
        ok = (term in txt) and (subj in txt) and (num in txt)
        print(f"DEBUG: header check -> {ok}")
        return ok
    except Exception:
        return False

# -------------------- resultados --------------------
def wait_component_or_none(page):
    for sel in ['[role="grid"]', '[role="table"]']:
        try:
            comp = page.locator(sel).first
            comp.wait_for(state="visible", timeout=12000)
            print(f"DEBUG: found {sel}")
            return ("grid", comp) if sel == '[role="grid"]' else ("table-aria", comp)
        except Exception:
            continue
    try:
        tbl = page.locator("table").first
        tbl.wait_for(state="visible", timeout=8000)
        print("DEBUG: found <table>")
        return ("table", tbl)
    except Exception:
        return (None, None)

def find_col(headers, needle):
    needle = needle.lower()
    for i, h in enumerate(headers):
        if needle in h.lower():
            return i
    return None

def extract_from_html_table(tbl):
    headers = [h.strip() for h in tbl.locator("th").all_inner_texts()]
    print(f"DEBUG: headers = {headers}")

    idx_course = find_col(headers, "course")
    idx_number = find_col(headers, "number")
    idx_open   = find_col(headers, "open seats")
    idx_start  = find_col(headers, "start") or find_col(headers, "time")
    idx_instr  = find_col(headers, "instructor")
    idx_loc    = find_col(headers, "location")

    # algunas filas traen celdas "extra" (expander / favoritos). Calculamos offset:
    trs = tbl.locator("tbody tr")
    if trs.count() == 0:
        return []
    first_tds = trs.nth(0).locator("td")
    offset = max(0, first_tds.count() - len(headers))
    print(f"DEBUG: column offset = {offset}")

    def _get(texts, idx):
        if idx is None: return ""
        pos = offset + idx
        return texts[pos] if 0 <= pos < len(texts) else ""

    rows = []
    for i in range(trs.count()):
        tds = trs.nth(i).locator("td")
        texts = [tds.nth(j).inner_text().strip() for j in range(tds.count())]
        instr = _get(texts, idx_instr)
        # sanity: si por error es un número de 5-6 dígitos, no es profe
        if re.fullmatch(r"\d{4,6}", instr or ""):
            instr = ""
        rows.append({
            "nrc":        _get(texts, idx_number),
            "course":     _get(texts, idx_course),
            "seats":      _get(texts, idx_open),
            "time":       _get(texts, idx_start),
            "instructor": instr,
            "location":   _get(texts, idx_loc),
            "_raw":       texts
        })
    return rows

def extract_from_aria_grid(grid):
    headers = [h.strip() for h in grid.locator('[role="columnheader"]').all_inner_texts()]
    print(f"DEBUG: ARIA headers = {headers}")

    idx_course = find_col(headers, "course")
    idx_number = find_col(headers, "number")
    idx_open   = find_col(headers, "open seats")
    idx_start  = find_col(headers, "start") or find_col(headers, "time")
    idx_instr  = find_col(headers, "instructor")
    idx_loc    = find_col(headers, "location")

    all_rows = grid.locator('[role="row"]')
    # primera fila real
    first_cells = all_rows.nth(1).locator('[role="gridcell"], [role="cell"]') if all_rows.count() > 1 else grid.locator('[role="gridcell"], [role="cell"]')
    offset = max(0, first_cells.count() - len(headers))
    print(f"DEBUG: ARIA column offset = {offset}")

    def _get(texts, idx):
        if idx is None: return ""
        pos = offset + idx
        return texts[pos] if 0 <= pos < len(texts) else ""

    rows = []
    for i in range(all_rows.count()):
        cells = all_rows.nth(i).locator('[role="gridcell"], [role="cell"]')
        if cells.count() == 0:
            continue
        texts = [cells.nth(j).inner_text().strip() for j in range(cells.count())]
        instr = _get(texts, idx_instr)
        if re.fullmatch(r"\d{4,6}", instr or ""):
            instr = ""
        rows.append({
            "nrc":        _get(texts, idx_number),
            "course":     _get(texts, idx_course),
            "seats":      _get(texts, idx_open),
            "time":       _get(texts, idx_start),
            "instructor": instr,
            "location":   _get(texts, idx_loc),
            "_raw":       texts
        })
    return rows

def extract_textual(page, subj, num):
    os.makedirs(DEBUG_DIR, exist_ok=True)
    body_txt = page.inner_text("body")
    with open(f"{DEBUG_DIR}/after-search-text.txt", "w", encoding="utf-8") as f:
        f.write(body_txt)

    lines = [l.strip() for l in body_txt.splitlines()]
    rows = []
    course_pat = re.compile(rf'^{re.escape(subj)}\s+{re.escape(num)}\b', re.IGNORECASE)
    weekday_line = re.compile(r'^(M|T|W|Th|F|Sa|Su|MW|TTh|M W|T Th)\b', re.IGNORECASE)

    i = 0
    while i < len(lines):
        if course_pat.search(lines[i]):
            j = i + 1
            while j < len(lines) and not lines[j]:
                j += 1
            title = lines[j].strip() if j < len(lines) else ""

            instr = ""
            k = j + 1
            for t in lines[k:k+8]:
                if not t: continue
                if weekday_line.search(t): continue
                if re.search(r'\b(AM|PM)\b', t, re.IGNORECASE): continue
                if "Open Seats" in t or "Add" == t: continue
                if re.search(r'(Tempe|Downtown|West|Poly|Online|iCourse)', t, re.IGNORECASE): continue
                if re.fullmatch(r"\d{4,6}", t): continue
                instr = t; break

            nrc = ""
            for t in lines[k:k+15]:
                m = re.match(r'^\d{4,6}$', t)
                if m: nrc = m.group(0); break

            open_seats = ""
            for t in lines[k:k+25]:
                m2 = re.search(r'(\d+)\s+of\s+(\d+)', t, re.IGNORECASE)
                if m2: open_seats = f"{m2.group(1)} of {m2.group(2)}"; break

            start_time = ""
            for t in lines[k:k+15]:
                m3 = re.search(r'\b(\d{1,2}:\d{2}\s*(AM|PM))\b', t, re.IGNORECASE)
                if m3: start_time = m3.group(1); break

            location = ""
            for t in lines[k:k+25]:
                if re.search(r'\bASU\s+Online\b', t, re.IGNORECASE):
                    location = "ASU Online"; break
                m4 = re.search(r'(Tempe|Downtown|West|Poly)[^\n]*', t, re.IGNORECASE)
                if m4: location = m4.group(0).strip(); break
                if re.search(r'\biCourse\b', t, re.IGNORECASE) and not location:
                    location = "iCourse"

            rows.append({
                "nrc": nrc,
                "course": f"{subj} {num} - {title}",
                "seats": open_seats,
                "time": start_time,
                "instructor": instr,
                "location": location,
            })
            i = k + 10
            continue
        i += 1

    print(f"DEBUG: textual rows found = {len(rows)}")
    return rows

def extract_rows(page, subj, num):
    typ, comp = wait_component_or_none(page)
    if typ == "table":
        return extract_from_html_table(comp)
    elif typ in ("grid", "table-aria"):
        return extract_from_aria_grid(comp)
    else:
        return extract_textual(page, subj, num)

# -------------------- flujo principal --------------------
def apply_filters_and_search(page, subj, num, term, tries=3):
    for attempt in range(1, tries+1):
        print(f"DEBUG: attempt {attempt}")
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

        print("DEBUG: filters not applied, retrying...")
        page.wait_for_timeout(700)
    return False

def parse_open_pair(s):
    m = re.search(r'(\d+)\s*of\s*(\d+)', s or '', re.I)
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)

def run():
    # --- JITTER aleatorio 5-10 min ---
    jitter = random.randint(JITTER_MIN_SEC, JITTER_MAX_SEC)
    print(f"DEBUG: jitter sleep {jitter}s")
    time.sleep(jitter)

    os.makedirs(DEBUG_DIR, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(record_video_dir=VIDEO_DIR, viewport={"width": 1366, "height": 768})
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
                page.screenshot(path=f"{DEBUG_DIR}/failed-{subj}{num}.png", full_page=True)
                with open(f"{DEBUG_DIR}/failed-{subj}{num}.html","w",encoding="utf-8") as f:
                    f.write(page.content())
                raise RuntimeError("No se pudo aplicar filtros tras reintentos.")

            rows = extract_rows(page, subj, num)
            for r in rows:
                r["_query"] = f"{subj}{num}-{term}"
            all_rows.extend(rows)

        context.close()
        browser.close()

    # Filtro: excluir "ASU Online"
    if LOCATION_EXCLUDE:
        all_rows = [r for r in all_rows if not any(x in (r.get("location","").lower()) for x in LOCATION_EXCLUDE)]

    # Estado nuevo/antiguo
    new_state = {"hash": hash_rows(all_rows), "rows": all_rows, "ts": int(time.time())}
    try:
        old_state = json.load(open(STATE, "r"))
    except Exception:
        old_state = {"hash": None, "rows": []}

    old_rows = old_state.get("rows", [])
    old_map = {r.get("nrc"): r for r in old_rows if r.get("nrc")}
    new_map = {r.get("nrc"): r for r in all_rows if r.get("nrc")}

    def parse_open_pair(s):
        m = re.search(r'(\d+)\s*of\s*(\d+)', s or '', re.I)
        return (int(m.group(1)), int(m.group(2))) if m else (None, None)

    # Construir mensaje SIEMPRE
    header = f'Class Watch — {time.strftime("%Y-%m-%d %H:%M UTC")}'
    lines = [header]

    for nrc, r in sorted(new_map.items(), key=lambda kv: kv[0]):
        cur_open, cur_tot = parse_open_pair(r.get("seats"))
        prev = old_map.get(nrc)
        if prev:
            prev_open, _ = parse_open_pair(prev.get("seats"))
            if prev_open is not None and cur_open is not None:
                diff = cur_open - prev_open
                delta_txt = f"(Δ {diff:+d})" if diff != 0 else "(same)"
            else:
                delta_txt = "(same)"
        else:
            delta_txt = "(new)"

        if cur_open is None:
            open_str = r.get("seats","?")
            emoji = "⚪️"
        else:
            open_str = f"{cur_open}/{cur_tot}"
            emoji = "🟢" if cur_open > 0 else "🔴"

        instructor = (r.get("instructor","") or "TBA").strip()
        location   = (r.get("location","") or "-").strip()
        start_time = (r.get("time","") or "-").strip()

        lines.append(
            f'{emoji} Class {nrc} — {r.get("course","")} — {instructor} — '
            f'{location} — {start_time} — Open {open_str} {delta_txt}'
        )

    removed = [o for k,o in old_map.items() if k not in new_map]
    if removed:
        gone = ", ".join(sorted([x.get("nrc","") for x in removed]))
        lines.append(f'Removed: {gone}')

    # Enviar SIEMPRE
    notify("\n".join(lines) if len(lines) > 1 else "No sections found (after filtering).")

    # Solo actualizar state cuando cambie
    if old_state.get("hash") != new_state["hash"]:
        with open(STATE, "w") as f:
            json.dump(new_state, f)
        print("CHANGED")
    else:
        print("NOCHANGE") 

if __name__ == "__main__":
    try:
        run()
    except Exception:
        print("ERROR:\n", traceback.format_exc())
        sys.exit(1)
