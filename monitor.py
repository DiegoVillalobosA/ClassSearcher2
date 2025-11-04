import os, json, hashlib, time, random, urllib.request, urllib.parse, sys, traceback
from playwright.sync_api import sync_playwright

# URL del buscador de ASU (puedes sobreescribirla en el workflow con la var de entorno URL)
URL = os.getenv("URL", "https://catalog.apps.asu.edu/catalog/classes")

# Lista de clases a revisar (subject, number, term)
# Esto también lo sobreescribimos en el workflow, pero aquí va un default
QUERIES = json.loads(os.getenv(
    "QUERIES_JSON",
    '[{"subject":"CSE","number":"412","term":"Spring 2026"}]'
))

# Selectores de la página.
# OJO: estos son de ejemplo. Cuando veas los selectores reales en la página de ASU,
# los cambias aquí o en el workflow (mejor en el workflow).
SEL = json.loads(os.getenv(
    "SELECTORS_JSON",
    # placeholders
    '{"subject":"input[name=\\"subject\\"]",'
    '"number":"input[name=\\"number\\"]",'
    '"term":"select[name=\\"term\\"]",'
    '"search":"button:has-text(\\"Search\\")",'
    '"table":"table.results"}'
))

STATE = "state.json"  # donde guardamos el último snapshot

# Telegram (vienen de GitHub Secrets en el workflow)
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")


# ========== utilidades ==========

def hash_rows(rows):
    """Crea un hash estable del resultado para saber si hubo cambios."""
    return hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def notify(text: str):
    """Manda mensaje a Telegram si hay token/chat, si no imprime."""
    if TG_TOKEN and TG_CHAT:
        try:
            data = urllib.parse.urlencode({
                "chat_id": TG_CHAT,
                "text": text
            }).encode()
            urllib.request.urlopen(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                data=data,
                timeout=10
            )
            return
        except Exception:
            # si falla Telegram, que por lo menos imprima
            pass
    print("NOTIFY:", text)


def extract_rows(page):
    """
    Lee la tabla de resultados y devuelve una lista de dicts.
    IMPORTANTE: ajusta los índices de las columnas a la tabla real de ASU.
    """
    page.wait_for_selector(SEL["table"], timeout=60000)
    rows = []
    for tr in page.query_selector_all(f'{SEL["table"]} tbody tr'):
        tds = tr.query_selector_all("td")
        # ajusta si tu tabla tiene menos columnas
        if len(tds) < 5:
            continue
        rows.append({
            "nrc":     tds[0].inner_text().strip(),
            "course":  tds[1].inner_text().strip(),
            "seats":   tds[2].inner_text().strip(),
            "wait":    tds[3].inner_text().strip(),
            "time":    tds[4].inner_text().strip(),
        })
    return rows


# ========== main ==========

def run():
    # pequeño random para no pegar siempre exacto
    time.sleep(random.uniform(0, 5))

    with sync_playwright() as p:
        browser = p.chromium.launch()  # headless
        page = browser.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)

        all_rows = []

        for q in QUERIES:
            # rellenar form
            page.fill(SEL["subject"], q["subject"])
            page.fill(SEL["number"],  q["number"])
            page.select_option(SEL["term"], label=q["term"])
            page.click(SEL["search"])

            rows = extract_rows(page)
            for r in rows:
                r["_query"] = f'{q["subject"]}{q["number"]}-{q["term"]}'
            all_rows.extend(rows)

        browser.close()

    # nuevo snapshot
    new_state = {
        "hash": hash_rows(all_rows),
        "rows": all_rows,
        "ts": int(time.time())
    }

    # leer snapshot anterior
    try:
        old_state = json.load(open(STATE, "r"))
    except Exception:
        old_state = {"hash": None, "rows": []}

    if old_state.get("hash") != new_state["hash"]:
        # hubo cambios → armar diff simple
        old_map = {r["nrc"]: r for r in old_state.get("rows", []) if "nrc" in r}
        new_map = {r["nrc"]: r for r in new_state["rows"] if "nrc" in r}

        added   = [new_map[k] for k in new_map.keys() - old_map.keys()]
        removed = [old_map[k] for k in old_map.keys() - new_map.keys()]
        changed = []
        for k in new_map.keys() & old_map.keys():
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
                f'Seats {o["seats"]}→{n["seats"]}, '
                f'Wait {o["wait"]}→{n["wait"]}'
            )

        msg = "\n".join(lines) or "Class watcher: detected changes."
        notify(msg)

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