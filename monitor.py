import os, json, hashlib, time, random, urllib.request, urllib.parse, sys, traceback
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ========== Helpers para cargar JSON desde variables de entorno ==========

def load_json_env(key: str, default_val):
    """
    Intenta leer key del entorno y parsear JSON.
    Si falla (None o JSON inválido), retorna default_val y emite un WARN.
    """
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default_val
    try:
        return json.loads(raw)
    except Exception as e:
        print(f"WARN: invalid {key}; using defaults. Error:", e)
        return default_val

# ========== Config básica ==========

URL = os.getenv("URL", "https://catalog.apps.asu.edu/catalog/classes")

DEFAULT_QUERIES = [{"subject": "CSE", "number": "412", "term": "Spring 2026"}]
QUERIES = load_json_env("QUERIES_JSON", DEFAULT_QUERIES)

# Selectores por defecto (PLACEHOLDER: ajústalos a la página real de ASU)
DEFAULT_SEL = {
    "subject": 'input[name="subject"]',
    "number":  'input[name="number"]',
    "term":    'select[name="term"]',
    "search":  'button:has-text("Search")',
    "table":   "table.results"
}
SEL = load_json_env("SELECTORS_JSON", DEFAULT_SEL)

STATE = "state.json"  # snapshot

# Telegram (vienen de GitHub Secrets en el workflow)
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")

# ========== Utilidades ==========

def hash_rows(rows):
    return hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()

def notify(text: str):
    if TG_TOKEN and TG_CHAT:
        try:
            data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text}).encode()
            urllib.request.urlopen(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                data=data,
                timeout=10
            )
            return
        except Exception as e:
            print("WARN: Telegram send failed ->", e)
    print("NOTIFY:", text)

def extract_rows(page):
    """
    Lee la tabla de resultados y devuelve una lista de dicts.
    IMPORTANTE: ajusta los índices de las columnas a la tabla real de ASU.
    """
    try:
        page.wait_for_selector(SEL["table"], timeout=60000)
    except PWTimeout:
        raise RuntimeError(f"No apareció la tabla con selector '{SEL['table']}' (timeout)")

    rows = []
    for tr in page.query_selector_all(f'{SEL["table"]} tbody tr'):
        tds = tr.query_selector_all("td")
        if len(tds) < 5:
            # ajusta si tu tabla tiene menos/más columnas
            continue
        rows.append({
            "nrc":     tds[0].inner_text().strip(),
            "course":  tds[1].inner_text().strip(),
            "seats":   tds[2].inner_text().strip(),
            "wait":    tds[3].inner_text().strip(),
            "time":    tds[4].inner_text().strip(),
        })
    return rows

# ========== Main ==========

def run():
    # Pequeño jitter para no caer siempre en el mismo segundo
    time.sleep(random.uniform(0, 5))

    with sync_playwright() as p:
        browser = p.chromium.launch()  # headless=True por defecto
        page = browser.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)

        all_rows = []

        for q in QUERIES:
            # Validación mínima de campos esperados
            subj = q.get("subject", "").strip()
            num  = q.get("number", "").strip()
            term = q.get("term", "").strip()

            if not (subj and num and term):
                print("WARN: query inválida (faltan subject/number/term):", q)
                continue

            # Rellenar formulario (ajusta estos selectores al HTML real)
            page.fill(SEL["subject"], subj)
            page.fill(SEL["number"],  num)
            page.select_option(SEL["term"], label=term)
            page.click(SEL["search"])

            rows = extract_rows(page)
            for r in rows:
                r["_query"] = f'{subj}{num}-{term}'
            all_rows.extend(rows)

        browser.close()

    # Crear nuevo snapshot
    new_state = {"hash": hash_rows(all_rows), "rows": all_rows, "ts": int(time.time())}

    # Leer snapshot anterior
    try:
        with open(STATE, "r") as f:
            old_state = json.load(f)
    except Exception:
        old_state = {"hash": None, "rows": []}

    # Comparar
    if old_state.get("hash") != new_state["hash"]:
        old_map = {r.get("nrc"): r for r in old_state.get("rows", []) if r.get("nrc")}
        new_map = {r.get("nrc"): r for r in new_state["rows"] if r.get("nrc")}

        added   = [new_map[k] for k in (new_map.keys() - old_map.keys())]
        removed = [old_map[k] for k in (old_map.keys() - new_map.keys())]
        changed = []
        for k in (new_map.keys() & old_map.keys()):
            o, n = old_map[k], new_map[k]
            if (o.get("seats"), o.get("wait"), o.get("time")) != (n.get("seats"), n.get("wait"), n.get("time")):
                changed.append((o, n))

        lines = []
        for r in added[:6]:
            lines.append(f'➕ NRC {r["nrc"]} {r["course"]} | Seats {r["seats"]} | Wait {r["wait"]}')
        for r in removed[:6]:
            lines.append(f'➖ NRC {r["nrc"]} {r["course"]}')
        for o, n in changed[:6]:
            lines.append(
                f'🔁 NRC {n["nrc"]} {n["course"]}: '
                f'Seats {o["seats"]}→{n["seats"]}, Wait {o["wait"]}→{n["wait"]}'
            )

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
        # Nunca “revienta silencioso”: imprime stacktrace para depurar en Actions
        print("ERROR:\n", traceback.format_exc())
        sys.exit(1)
