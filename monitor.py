import os, json, hashlib, time, random, urllib.request, urllib.parse, sys, traceback, re
from pathlib import Path
from playwright.sync_api import sync_playwright

# =========================
# Config
# =========================
URL = os.getenv("URL", "https://catalog.apps.asu.edu/catalog/classes")
QUERIES = json.loads(os.getenv("QUERIES_JSON", '[{"subject":"CSE","number":"412","term":"Spring 2026"}]'))

# Filtro por ubicación (exclusión)
LOCATION_EXCLUDE = os.getenv("LOCATION_EXCLUDE", "ASU Online").strip().lower() or None

# Notificación “no changes” como keep-alive (1/hora por defecto)
NOCHANGE_NOTIFY_INTERVAL_SEC = int(os.getenv("NOCHANGE_NOTIFY_INTERVAL_SEC", "3600"))

# Jitter para que no sea exacto el intervalo real
JITTER_MIN_SEC = int(os.getenv("JITTER_MIN_SEC", "0"))
JITTER_MAX_SEC = int(os.getenv("JITTER_MAX_SEC", "240"))

# Archivos de estado
STATE = "state.json"                # snapshot de resultados
META  = "notify_state.json"         # última vez que avisamos sin cambios
DEBUG_DIR = "debug"
VIDEO_DIR = "recordings"

# Telegram
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")


# =========================
# Utilidades
# =========================
def hash_rows(rows):
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

def notify(text: str):
    """Envia a Telegram si hay token/chat; si falla, imprime."""
    if TG_TOKEN and TG_CHAT:
        try:
            data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text}).encode()
            urllib.request.urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data, timeout=10)
            return
        except Exception as e:
            print("WARN: Telegram send failed ->", e)
    print("NOTIFY:", text)

def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

def parse_open_seats(seats_text: str):
    """Convierte '2 of 170' -> 2 (int). Si no reconoce, devuelve None."""
    if not seats_text:
        return None
    m = re.search(r'(\d+)\s+of\s+\d+', seats_text, flags=re.I)
    return int(m.group(1)) if m else None

def first_locator(page, kind, value, timeout=12000, name_regex=False):
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
        # Evita capturar la pantalla intermedia de "Previous Terms"
        page.wait_for_function(
            """(term) => {
                const txt = document.body.innerText || '';
                return (!txt.includes('Previous Terms')) || txt.includes(term);
            }""",
            arg=target_term_text, timeout=15000
        )
    except Exception:
        pass
    page.wait_for_timeout(400)

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
    # Enter de respaldo por si el click no dispara
    try: page.keyboard.press("Enter")
    except Exception: pass
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

def reset_to_search(page, URL):
    # Si hay "Clear filters", úsalo; si no, vuelve al buscador
    btn = first_locator(page, "role", ("button", "Clear filters"))
    if btn:
        btn.click()
        page.wait_for_timeout(300)
        return
    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    wait_hydrated(page, "")

# ------- extracción de resultados -------
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
    idx_loc    = find_col(headers, "location")
    idx_instr  = find_col(headers, "instructor")

    rows = []
    trs = tbl.locator("tbody tr")
    for i in range(trs.count()):
        tds = trs.nth(i).locator("td")
        texts = [tds.nth(j).inner_text().strip() for j in range(tds.count())]
        row = {
            "nrc":    texts[idx_number] if idx_number is not None and idx_number < len(texts) else "",
            "course": texts[idx_course] if idx_course is not None and idx_course < len(texts) else "",
            "seats":  texts[idx_open]   if idx_open   is not None and idx_open   < len(texts) else "",
            "time":   texts[idx_start]  if idx_start  is not None and idx_start  < len(texts) else "",
            "loc":    texts[idx_loc]    if idx_loc    is not None and idx_loc    < len(texts) else "",
            "instr":  texts[idx_instr]  if idx_instr  is not None and idx_instr  < len(texts) else "",
        }
        rows.append(row)
    return rows

def extract_from_aria_grid(grid):
    headers = [h.strip() for h in grid.locator('[role="columnheader"]').all_inner_texts()]
    print(f"DEBUG: ARIA headers = {headers}")
    idx_course = find_col(headers, "course")
    idx_number = find_col(headers, "number")
    idx_open   = find_col(headers, "open seats")
    idx_start  = find_col(headers, "start") or find_col(headers, "time")
    idx_loc    = find_col(headers, "location")
    idx_instr  = find_col(headers, "instructor")

    rows = []
    all_rows = grid.locator('[role="row"]')
    for i in range(all_rows.count()):
        cells = all_rows.nth(i).locator('[role="gridcell"], [role="cell"]')
        if cells.count() == 0:
            continue
        texts = [cells.nth(j).inner_text().strip() for j in range(cells.count())]
        row = {
            "nrc":    texts[idx_number] if idx_number is not None and idx_number < len(texts) else "",
            "course": texts[idx_course] if idx_course is not None and idx_course < len(texts) else "",
            "seats":  texts[idx_open]   if idx_open   is not None and idx_open   < len(texts) else "",
            "time":   texts[idx_start]  if idx_start  is not None and idx_start  < len(texts) else "",
            "loc":    texts[idx_loc]    if idx_loc    is not None and idx_loc    < len(texts) else "",
            "instr":  texts[idx_instr]  if idx_instr  is not None and idx_instr  < len(texts) else "",
        }
        rows.append(row)
    return rows

def extract_textual(page, subj, num):
    """
    Fallback cuando no hay <table> ni ARIA grid.
    Intenta extraer: NRC, título, open seats, hora, location, instructor.
    """
    os.makedirs(DEBUG_DIR, exist_ok=True)
    body_txt = page.inner_text("body")
    with open(f"{DEBUG_DIR}/after-search-text.txt", "w", encoding="utf-8") as f:
        f.write(body_txt)

    lines = [l.strip() for l in body_txt.splitlines()]
    rows = []

    course_pat = re.compile(rf'^{re.escape(subj)}\s+{re.escape(num)}\b', re.IGNORECASE)
    i = 0
    while i < len(lines):
        if course_pat.search(lines[i]):
            course_label = lines[i].strip()
            # Título
            j = i + 1
            while j < len(lines) and not lines[j]:
                j += 1
            title = lines[j].strip() if j < len(lines) else ""

            # Buscar NRC y demás
            k = j + 1
            nrc = ""
            for t in lines[k:k+20]:
                m = re.match(r'^\d{4,6}$', t)
                if m: nrc = m.group(0); break

            open_seats = ""
            for t in lines[k:k+30]:
                m2 = re.search(r'(\d+)\s+of\s+(\d+)', t, re.IGNORECASE)
                if m2: open_seats = f"{m2.group(1)} of {m2.group(2)}"; break

            start_time = ""
            for t in lines[k:k+30]:
                m3 = re.search(r'\b(\d{1,2}:\d{2}\s*(AM|PM))\b', t, re.IGNORECASE)
                if m3: start_time = m3.group(1); break

            # Location: línea con campus típico (Tempe, Downtown, West Valley, etc.)
            loc = ""
            for t in lines[k:k+30]:
                if any(campus in t for campus in ["Tempe", "Downtown", "West Valley", "ASU Online", "Polytechnic", "West", "Online"]):
                    loc = t.strip()
                    break

            # Instructor: heurística simple
            instr = ""
            for idx in range(k, min(k+30, len(lines))):
                if re.search(r'Instructor', lines[idx], re.I):
                    # siguiente no vacía
                    p = idx + 1
                    while p < len(lines) and not lines[p]:
                        p += 1
                    if p < len(lines):
                        instr = lines[p].strip()
                    break

            rows.append({
                "nrc": nrc,
                "course": f"{course_label} - {title}".strip(" -"),
                "seats": open_seats,
                "time": start_time,
                "loc": loc,
                "instr": instr,
            })
            i = k + 10
            continue
        i += 1

    print(f"DEBUG: textual rows found = {len(rows)}")
    return rows

def wait_component_or_none(page):
    # grid / table ARIA
    for sel in ['[role="grid"]', '[role="table"]']:
        try:
            comp = page.locator(sel).first
            comp.wait_for(state="visible", timeout=12000)
            print(f"DEBUG: found {sel}")
            return ("grid", comp) if sel == '[role="grid"]' else ("table-aria", comp)
        except Exception:
            continue
    # table clásica
    try:
        tbl = page.locator("table").first
        tbl.wait_for(state="visible", timeout=8000)
        print("DEBUG: found <table>")
        return ("table", tbl)
    except Exception:
        return (None, None)

def extract_rows(page, subj, num):
    typ, comp = wait_component_or_none(page)
    if typ == "table":
        return extract_from_html_table(comp)
    elif typ in ("grid", "table-aria"):
        return extract_from_aria_grid(comp)
    else:
        return extract_textual(page, subj, num)

# =========================
# Lógica principal
# =========================
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
        page.wait_for_timeout(900)

        if ensure_filters_applied(page, term, subj, num):
            return True

        print("DEBUG: filters not applied, retrying...")
        page.wait_for_timeout(700)
        reset_to_search(page, URL)

    return False

def main():
    # Jitter
    if JITTER_MAX_SEC > 0 and JITTER_MAX_SEC >= JITTER_MIN_SEC:
        time.sleep(random.uniform(JITTER_MIN_SEC, JITTER_MAX_SEC))

    Path(DEBUG_DIR).mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(record_video_dir=VIDEO_DIR, viewport={"width": 1366, "height": 768})
        page = context.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)

        all_rows = []
        for q in QUERIES:
            reset_to_search(page, URL)
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
                raise RuntimeError(f"No se pudo aplicar filtros en {subj} {num} {term}")

            rows = extract_rows(page, subj, num)
            for r in rows:
                r["_query"] = f"{subj}{num}-{term}"
            all_rows.extend(rows)

        context.close()
        browser.close()

    # Filtro por ubicación (excluye ASU Online)
    if LOCATION_EXCLUDE:
        all_rows = [r for r in all_rows if LOCATION_EXCLUDE not in (r.get("loc","").lower())]

    # armar snapshot nuevo
    new_state = {"hash": hash_rows(all_rows), "rows": all_rows, "ts": int(time.time())}

    # cargar estado previo
    old_state = load_json(STATE, {"hash": None, "rows": []})
    meta = load_json(META, {"last_nochange_ts": 0})

    # Mapas por NRC (solo si NRC existe)
    old_map = {r.get("nrc"): r for r in old_state.get("rows", []) if r.get("nrc")}
    new_map = {r.get("nrc"): r for r in new_state["rows"] if r.get("nrc")}

    added   = [new_map[k] for k in (new_map.keys() - old_map.keys())]
    removed = [old_map[k] for k in (old_map.keys() - new_map.keys())]
    changed = []  # pares (old, new)
    for k in (new_map.keys() & old_map.keys()):
        a, b = old_map[k], new_map[k]
        # Consideramos cambio si seats/time/loc/instr varió
        if (a.get("seats"), a.get("time"), a.get("loc"), a.get("instr")) != (b.get("seats"), b.get("time"), b.get("loc"), b.get("instr")):
            changed.append((a, b))

    # Marcar estado y delta por fila para el render
    for k, row in new_map.items():
        old = old_map.get(k)
        open_now  = parse_open_seats(row.get("seats","") or "")
        open_prev = parse_open_seats(old.get("seats","") or "") if old else None
        delta = None
        if open_now is not None and open_prev is not None:
            delta = open_now - open_prev
        # Emojis:
        # estado principal
        status = "🟢" if (open_now is not None and open_now > 0) else "🔴"
        # update si cambió disponibilidad (delta != 0)
        update = "🟠" if (delta is not None and delta != 0) else ""
        row["_open_now"] = open_now
        row["_delta"] = delta
        row["_status"] = status
        row["_update"] = update

    # ¿Hubo cambios?
    any_change = bool(added or removed or changed)

    # Mensaje
    # agrupamos por query
    by_query = {}
    for r in new_state["rows"]:
        by_query.setdefault(r.get("_query","?"), []).append(r)

    lines = []
    header = "🔔 **CHANGES**" if any_change else "⏱️ Hourly check (no changes)"
    lines.append(header)

    for qkey, rows in by_query.items():
        # Qkey = "CSE412-Spring 2026"
        lines.append(f"\n— {qkey} —")
        # orden por open seats desc, luego NRC
        rows_sorted = sorted(rows, key=lambda x: (-(x.get("_open_now") or -1), str(x.get("nrc",""))))
        for r in rows_sorted:
            nrc   = r.get("nrc","")
            course= r.get("course","")
            seats = r.get("seats","?")
            loc   = r.get("loc","").strip()
            time_ = r.get("time","").strip()
            instr = r.get("instr","").strip()
            delta = r.get("_delta")
            delta_txt = f"(Δ{delta:+d})" if isinstance(delta,int) and delta!=0 else "(same)"
            # 🔴/🟢 + 🟠 si hubo variación de disponibilidad
            prefix = f'{r.get("_status","")} {r.get("_update","")}'.strip()
            line = f'{prefix} NRC {nrc} — {course} — Open {seats} {delta_txt}'
            if loc:   line += f' — {loc}'
            if time_: line += f' — {time_}'
            if instr: line += f' — {instr}'
            lines.append(line)

    # añadimos altas/bajas explícitas
    if added:
        for r in added[:6]:
            lines.append(f'➕ NEW NRC {r.get("nrc","")} — {r.get("course","")}')
    if removed:
        for r in removed[:6]:
            lines.append(f'➖ REMOVED NRC {r.get("nrc","")} — {r.get("course","")}')

    message = "\n".join(lines)

    # Política de notificación:
    # - Si hay cambios: notificar siempre y resetear reloj
    # - Si NO hay cambios: notificar sólo si pasó >= NOCHANGE_NOTIFY_INTERVAL_SEC desde el último ping
    now = int(time.time())
    should_notify = False
    if any_change:
        should_notify = True
        meta["last_nochange_ts"] = now  # reset
    else:
        if now - meta.get("last_nochange_ts", 0) >= NOCHANGE_NOTIFY_INTERVAL_SEC:
            should_notify = True
            meta["last_nochange_ts"] = now

    if should_notify:
        notify(message)

    # Guardar estados
    save_json(STATE, new_state)
    save_json(META, meta)

def run():
    try:
        main()
    except Exception:
        print("ERROR:\n", traceback.format_exc())
        sys.exit(1)

if __name__ == "__main__":
    run()
