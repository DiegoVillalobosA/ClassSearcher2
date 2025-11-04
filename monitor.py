import os, json, hashlib, time, random, urllib.request, urllib.parse, sys, traceback, re
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ---------- util: JSON seguro desde env ----------
def load_json_env(key: str, default_val):
    raw = os.getenv(key)
    if not raw or not raw.strip():
        return default_val
    try:
        return json.loads(raw)
    except Exception as e:
        print(f"WARN: invalid {key}; using defaults. Error:", e)
        return default_val

# ---------- config ----------
URL = os.getenv("URL", "https://catalog.apps.asu.edu/catalog/classes")
DEFAULT_QUERIES = [{"subject": "CSE", "number": "412", "term": "Spring 2026"}]
QUERIES = load_json_env("QUERIES_JSON", DEFAULT_QUERIES)

STATE = "state.json"
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")

# ---------- notificaciones ----------
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

# ---------- helpers de localización ----------
def first_locator(page, kind, value, timeout=6000, name_regex=False):
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
    # 1) select nativo
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
    # 2) combobox accesible (muy común en UI custom)
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
    # 3) fallback por texto
    label = first_locator(page, "text", "Term")
    if label:
        try: label.click()
        except Exception: pass
        opt3 = first_locator(page, "text", term_label_text)
        if opt3:
            opt3.click(); return True
    raise RuntimeError("No se pudo seleccionar el Term (ajusta lógica o dime el locator).")

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
    raise RuntimeError("No encontré el botón de búsqueda (Search Classes).")

def wait_results_table(page):
    # Varias heurísticas; la de Open Seats suele ser estable en ASU
    for k, v in [
        ("css", 'table:has-text("Open Seats")'),
        ("css", "table.results"),
        ("css", "table#results"),
        ("css", 'table:has(th)'),
        ("css", 'div[class*="result" i] table'),
    ]:
        try:
            loc = page.locator(v).first
            loc.wait_for(state="visible", timeout=60000)
            print(f"DEBUG: table via {k}='{v}'")
            return loc
        except Exception:
            continue
    # Como extra, el texto grande de cabecera: "Results for Spring 2026, CSE, 412"
    hdr = first_locator(page, "text", "Results for", timeout=6000)
    if hdr:
        # espera un poco extra y reintenta tablas genéricas
        page.wait_for_timeout(1000)
        for v in ["table", "table:has(th)"]:
            try:
                loc = page.locator(v).first
                loc.wait_for(state="visible", timeout=6000)
                print(f"DEBUG: table via fallback '{v}'")
                return loc
            except Exception:
                pass
    raise RuntimeError("No apareció la tabla de resultados; ajusta selector de tabla.")

def extract_rows(page):
    tbl = wait_results_table(page)
    rows = []
    trs = tbl.locator("tbody tr")
    count = trs.count()
    for i in range(count):
        tds = trs.nth(i).locator("td")
        n = tds.count()
        vals = [tds.nth(j).inner_text().strip() for j in range(n)]
        if n >= 5:
            rows.append({
                "nrc":     vals[0],
                "course":  vals[1],
                "seats":   vals[10] if n > 10 else (vals[2] if n > 2 else ""),  # Open Seats suele estar en la 11ª col
                "wait":    "",   # agrega si existe columna de waitlist en tu vista
                "time":    vals[6] if n > 6 else "",
                "_raw":    vals
            })
        else:
            rows.append({"_raw": vals})
    return rows

# ---------- main ----------
def run():
    time.sleep(random.uniform(0, 5))  # jitter
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
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

            # espera a que cargue algo de red y render
            page.wait_for_load_state("networkidle")
            rows = extract_rows(page)
            for r in rows:
                r["_query"] = f"{subj}{num}-{term}"
            all_rows.extend(rows)

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
