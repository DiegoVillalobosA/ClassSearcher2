import os, json, hashlib, time, random, urllib.request, urllib.parse, sys, traceback, re
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# -------- util: JSON seguro desde env --------
def load_json_env(key: str, default_val):
    raw = os.getenv(key)
    if not raw or not raw.strip():
        return default_val
    try:
        return json.loads(raw)
    except Exception as e:
        print(f"WARN: invalid {key}; using defaults. Error:", e)
        return default_val

# -------- config --------
URL = os.getenv("URL", "https://catalog.apps.asu.edu/catalog/classes")
DEFAULT_QUERIES = [{"subject": "CSE", "number": "412", "term": "Spring 2026"}]
QUERIES = load_json_env("QUERIES_JSON", DEFAULT_QUERIES)

STATE = "state.json"
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")

DEBUG_DIR = "debug"
VIDEO_DIR = "recordings"

# -------- notificaciones --------
def notify(text: str):
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

# -------- helpers de localización --------
def first_locator(page, kind, value, timeout=8000, name_regex=False):
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
            if name_regex:
                loc = page.get_by_role(role, name=re.compile(name, re.I))
            else:
                loc = page.get_by_role(role, name=name)
        elif kind == "aria":
            loc = page.locator(value)
        else:
            return None
        loc.first.wait_for(state="visible", timeout=timeout)
        return loc.first
    except Exception:
        return None

def find_subject_input(page):
    for k, v in [
        ("placeholder", "Subject"),
        ("label", "Subject"),
        ("aria", 'input[aria-label*="Subject" i]'),
        ("css", '#subject'),
        ("css", 'input[name="subject"]'),
        ("css", 'input[id*="subject" i]'),
    ]:
        loc = first_locator(page, k, v)
        if loc:
            print(f"DEBUG: subject via {k}='{v}'")
            return loc
    raise RuntimeError("No se encontró el campo 'Subject'.")

def find_number_input(page):
    for k, v in [
        ("placeholder", "Number"),
        ("label", "Number"),
        ("label", "Course Number"),
        ("aria", 'input[aria-label*="Number" i]'),
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

def select_term_value(page, term_label_text):
    # select nativo
    for k, v in [
        ("css", 'select[name="term"]'),
        ("css", "#term"),
        ("aria", 'select[aria-label*="Term" i]'),
        ("label", "Term"),
    ]:
        loc = first_locator(page, k, v)
        if loc:
            print(f"DEBUG: term select via {k}='{v}'")
            try:
                loc.select_option(label=term_label_text)
                return True
            except Exception as e:
                print("DEBUG: select_option falló, probamos combobox...", e)
                break
    # combobox accesible
    combo = first_locator(page, "role", ("combobox", "Term"), name_regex=True)
    if combo:
        print("DEBUG: term via role=combobox ~ 'Term'")
        combo.click()
        opt = first_locator(page, "role", ("option", term_label_text))
        if opt:
            opt.click(); return True
        opt2 = first_locator(page, "text", term_label_text)
        if opt2:
            opt2.click(); return True
    # fallback por texto
    label = first_locator(page, "text", "Term")
    if label:
        try: label.click()
        except Exception: pass
        opt3 = first_locator(page, "text", term_label_text)
        if opt3:
            opt3.click(); return True
    raise RuntimeError("No se pudo seleccionar el Term.")

def click_search(page):
    for k, v, regex in [
        ("role", ("button", "Search Classes"), False),
        ("role", ("button", r"Search\s*Classes"), True),
        ("text", "Search Classes", False),
        ("css", 'button:has-text("Search Classes")', False),
        ("role", ("button", "Search"), False),
        ("text", "Search", False),
        ("css", 'input[type="submit"]', False),
    ]:
        loc = first_locator(page, k, v, name_regex=regex)
        if loc:
            print(f"DEBUG: search via {k}='{v}'")
            loc.click()
            return True
    raise RuntimeError("No encontré el botón de búsqueda.")

def wait_results_table(page):
    # espera cabecera "Results for ..."
    try:
        first_locator(page, "text", "Results for").wait_for(state="visible", timeout=15000)
        print("DEBUG: 'Results for' visible")
    except Exception:
        pass

    # intenta varias tablas y muestra diagnóstico
    tables = page.locator("table")
    try:
        tables.first.wait_for(state="visible", timeout=15000)
    except Exception:
        pass

    count = tables.count()
    print(f"DEBUG: tables found = {count}")
    chosen = None
    for i in range(min(count, 10)):
        ths = tables.nth(i).locator("th")
        headers = [h.strip() for h in ths.all_inner_texts()]
        print(f"DEBUG: table[{i}] headers = {headers}")
        # busca una que tenga columnas típicas
        if any("course" in h.lower() for h in headers) and any("open seats" in h.lower() for h in headers):
            chosen = tables.nth(i)
            print(f"DEBUG: using table[{i}]")
            break

    if not chosen:
        # otros intentos
        for k, v in [
            ('css', 'table:has-text("Open Seats")'),
            ('css', 'div[class*="result" i] table'),
            ('css', 'main table'),
        ]:
            try:
                loc = page.locator(v).first
                loc.wait_for(state="visible", timeout=10000)
                print(f"DEBUG: table via {k}='{v}'")
                chosen = loc
                break
            except Exception:
                continue

    if not chosen:
        # guardar diagnóstico visual
        os.makedirs(DEBUG_DIR, exist_ok=True)
        try:
            page.screenshot(path=f"{DEBUG_DIR}/after-search.png", full_page=True)
            with open(f"{DEBUG_DIR}/after-search.html", "w", encoding="utf-8") as f:
                f.write(page.content())
            print("DEBUG: saved debug/after-search.png & .html")
        except Exception as e:
            print("DEBUG: failed to save debug artifacts:", e)
        raise RuntimeError("No apareció la tabla de resultados; ajusta selector de tabla.")

    return chosen

def find_col(headers, needle):
    needle = needle.lower()
    for i, h in enumerate(headers):
        if needle in h.lower():
            return i
    return None

def extract_rows(page):
    tbl = wait_results_table(page)
    headers = [h.strip() for h in tbl.locator("th").all_inner_texts()]
    print(f"DEBUG: chosen headers = {headers}")

    idx_course = find_col(headers, "course")
    idx_number = find_col(headers, "number")
    idx_open   = find_col(headers, "open seats")
    idx_time   = find_col(headers, "time") or find_col(headers, "start")

    rows = []
    trs = tbl.locator("tbody tr")
    for i in range(trs.count()):
        tds = trs.nth(i).locator("td")
        texts = [tds.nth(j).inner_text().strip() for j in range(tds.count())]
        rows.append({
            "nrc":   texts[idx_number] if idx_number is not None and idx_number < len(texts) else "",
            "course":texts[idx_course] if idx_course is not None and idx_course < len(texts) else "",
            "seats": texts[idx_open]   if idx_open   is not None and idx_open   < len(texts) else "",
            "time":  texts[idx_time]   if idx_time   is not None and idx_time   < len(texts) else "",
            "_raw":  texts
        })
    return rows

# -------- main --------
def run():
    time.sleep(random.uniform(0, 5))  # jitter
    os.makedirs(DEBUG_DIR, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        # graba video (podrás verlo en Actions como artifact)
        context = browser.new_context(record_video_dir=VIDEO_DIR, viewport={"width": 1366, "height": 768})
        page = context.new_page()

        page.goto(URL, wait_until="domcontentloaded", timeout=60000)

        all_rows = []
        for q in QUERIES:
            subj = q.get("subject", "").strip()
            num  = q.get("number", "").strip()
            term = q.get("term", "").strip()
            if not (subj and num and term):
                print("WARN: query inválida:", q); continue

            subj_input = find_subject_input(page)
            num_input  = find_number_input(page)
            subj_input.fill(subj)
            num_input.fill(num)

            select_term_value(page, term)
            click_search(page)

            page.wait_for_load_state("networkidle")
            rows = extract_rows(page)
            for r in rows:
                r["_query"] = f"{subj}{num}-{term}"
            all_rows.extend(rows)

        # cierra guardando los videos
        context.close()
        browser.close()

    new_state = {"hash": hash_rows(all_rows), "rows": all_rows, "ts": int(time.time())}
    try:
        old_state = json.load(open(STATE, "r"))
    except Exception:
        old_state = {"hash": None, "rows": []}

    if old_state.get("hash") != new_state["hash"]:
        o = {r.get("nrc"): r for r in old_state.get("rows", []) if r.get("nrc")}
        n = {r.get("nrc"): r for r in new_state["rows"] if r.get("nrc")}
        added   = [n[k] for k in (n.keys() - o.keys())]
        removed = [o[k] for k in (o.keys() - n.keys())]
        changed = []
        for k in (n.keys() & o.keys()):
            a, b = o[k], n[k]
            if (a.get("seats"), a.get("time")) != (b.get("seats"), b.get("time")):
                changed.append((a, b))

        lines = []
        for r in added[:6]:   lines.append(f'➕ NRC {r.get("nrc","")} {r.get("course","")} | Open Seats {r.get("seats","?")}')
        for r in removed[:6]: lines.append(f'➖ NRC {r.get("nrc","")} {r.get("course","")}')
        for a, b in changed[:6]:
            lines.append(f'🔁 NRC {b.get("nrc","")} {b.get("course","")}: Open Seats {a.get("seats","?")}→{b.get("seats","?")}')
        notify("\n".join(lines) or "Class watcher: detected changes.")

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
