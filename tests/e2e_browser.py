"""
Prueba de punta a punta en Chromium real: checkout (index.html), seguimiento (seguir.html) y panel
(panel.html) contra el Lambda REAL (handler.py) con DynamoDB simulado.

Todo lo de fuera se simula: /api/* -> handler, mosaicos de OSM -> PNG vacio, OSRM -> respuesta calculada aqui.
No prueba: internet real, OSRM real, Telegram real, CloudFront.

Uso:  python3 tests/e2e_browser.py            (necesita playwright + chromium)
"""
import http.server
import json
import math
import os
import re
import socketserver
import sys
import threading
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_handler as T  # noqa: E402  (prepara AWS falso, reloj fijo y variables de entorno)

from playwright.sync_api import sync_playwright  # noqa: E402

H, fake_aws = T.H, T.fake_aws
SITE = os.path.join(HERE, "..", "site")
PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082")
FAILS = []
ERRORS = []  # errores de consola / excepciones de pagina


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


# ------------------------------------------------------------------ servidor estatico
class Quiet(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=SITE, **k)

    def log_message(self, *a):
        pass


socketserver.TCPServer.allow_reuse_address = True
HTTPD = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Quiet)
PORT = HTTPD.server_address[1]
threading.Thread(target=HTTPD.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}"


# ------------------------------------------------------------------ simulacion de red
def hav(a, b):
    R = 6371000
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(x))


def osrm(url):
    m = re.search(r"/(table|route)/v1/driving/([^?]+)", url)
    kind, coords = m.group(1), m.group(2)
    pts = [tuple(map(float, c.split(",")))[::-1] for c in coords.split(";")]  # (lat,lng)
    if kind == "table":
        return {"code": "Ok", "durations": [[hav(a, b) * 1.3 / 3.33 for b in pts] for a in pts]}
    legs = [{"duration": hav(pts[i], pts[i + 1]) * 1.3 / 3.33, "distance": hav(pts[i], pts[i + 1]) * 1.3} for i in range(len(pts) - 1)]
    return {"code": "Ok", "routes": [{"legs": legs, "geometry": {"type": "LineString", "coordinates": [[p[1], p[0]] for p in pts]}}]}


def install_network(ctx, osrm_mode="ok"):
    calls = {"api": [], "osrm": 0, "tiles": 0}

    def api(route, request):
        u = urllib.parse.urlparse(request.url)
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        body = request.post_data
        r = H.handler(T.ev(request.method, u.path, body, headers={k.lower(): v for k, v in request.headers.items()},
                           qs=qs or None), None)
        calls["api"].append((request.method, u.path, r["statusCode"]))
        route.fulfill(status=r["statusCode"], headers={"content-type": "application/json"}, body=r["body"])

    def tiles(route, request):
        calls["tiles"] += 1
        route.fulfill(status=200, content_type="image/png", body=PNG)

    def router(route, request):
        calls["osrm"] += 1
        if osrm_mode == "ok":
            route.fulfill(status=200, content_type="application/json", body=json.dumps(osrm(request.url)))
        else:
            route.abort()

    ctx.route(re.compile(r".*/api/.*"), api)
    ctx.route(re.compile(r"https://.*tile\.openstreetmap\.org/.*"), tiles)
    ctx.route(re.compile(r"https://[^/]*openstreetmap[^/]*/.*"), lambda r, q: tiles(r, q) if "/tile" in q.url or q.url.endswith(".png") else router(r, q))
    ctx.route(re.compile(r"https://router\.project-osrm\.org/.*"), router)
    return calls


def new_page(ctx):
    pg = ctx.new_page()
    pg.on("pageerror", lambda e: ERRORS.append(f"pageerror: {e}"))
    pg.on("console", lambda m: ERRORS.append(f"console.{m.type}: {m.text}") if m.type == "error" and "Failed to load resource" not in m.text else None)
    pg.on("dialog", lambda d: d.accept())  # confirm() de "¿Cancelar?" etc.
    return pg


def reset_world():
    fake_aws.reset()
    T.SENT.clear()
    T.CLOCK["now"] = T.T0
    H._KITCHEN_CACHE = None if hasattr(H, "_KITCHEN_CACHE") else None


# ------------------------------------------------------------------ 1) checkout
def test_checkout(browser):
    print("\n[index.html] checkout con franja + pin + confirmación")
    reset_world()
    ctx = browser.new_context(viewport={"width": 400, "height": 800}, permissions=[])
    calls = install_network(ctx)
    pg = new_page(ctx)
    pg.goto(BASE + "/index.html")
    pg.wait_for_selector("[data-add]")
    pg.click("[data-add='cafe_con_leche']")
    pg.click("[data-add='cafe_con_leche']")
    pg.click("#cartFab")
    pg.click("#btnGoCheckout")
    pg.fill("#fName", "Ana Pérez")
    pg.fill("#fAlias", "Anita")
    with pg.expect_response(lambda r: r.url.endswith("/api/geocode"), timeout=6000):
        pg.fill("#fAddress", "Macéo #45 e/ Sisneros y Abellaneda, Rpto Garrido")
    pg.wait_for_timeout(300)
    line = pg.inner_text("#geoLine")
    check(any(w in line.lower() for w in ("maceo", "aproxim", "ubicaci", "pin", "entre")), f"estima la dirección mal escrita: «{line[:80]}»")
    check(any(p == "/api/geocode" for _, p, _ in calls["api"]), "llamó a /api/geocode")
    pg.fill("#fPhone", "5 123 4567")
    chips = pg.locator("#slotBox button, #slotBox .chip, #slotBox [data-slot]")
    check(chips.count() >= 2, f"muestra franjas de entrega ({chips.count()})")
    # elegir la franja de merienda de hoy
    merienda = pg.locator("#slotBox [data-slot='merienda']").first
    if merienda.count():
        merienda.click()
    pg.fill("#fNotes", "sin azúcar")
    pg.click("#btnSubmitOrder")
    pg.wait_for_selector("#sheetConfirm.open, #sheetConfirm.show, #sheetConfirm.active", timeout=8000)
    msg = pg.inner_text("#confirmMsg")
    check(len(msg) > 5, f"confirmación mostrada: «{msg[:70]}»")
    posts = [c for c in calls["api"] if c[0] == "POST" and c[1] == "/api/orders"]
    check(posts and posts[0][2] == 201, "POST /api/orders → 201")
    orders = list(fake_aws.TABLES["orders"].items.values())
    check(len(orders) == 1, "1 pedido guardado")
    if orders:
        o = orders[0]
        check(o["phone"] == "+5351234567", f"teléfono normalizado ({o['phone']})")
        check(o.get("geo_src") in ("between", "corner"), f"geo_src = {o.get('geo_src')}")
        check(o.get("slot") == "merienda", f"slot = {o.get('slot')}")
        check(o.get("track_token"), "tiene track_token")
    check("#" in pg.get_attribute("#confirmTrack", "href") or "seguir.html" in (pg.get_attribute("#confirmTrack", "href") or ""), "enlace «Seguir mi pedido»")
    tr = pg.evaluate("localStorage.getItem('trevol_track')")
    check(bool(tr), "guardó trevol_track en localStorage")
    ctx.close()


def test_checkout_pin(browser):
    print("\n[index.html] el pin del cliente manda")
    reset_world()
    ctx = browser.new_context(viewport={"width": 400, "height": 900}, geolocation={"latitude": 21.39, "longitude": -77.90}, permissions=["geolocation"])
    calls = install_network(ctx)
    pg = new_page(ctx)
    pg.goto(BASE + "/index.html")
    pg.wait_for_selector("[data-add]")
    pg.click("[data-add='batido_frutas']")
    pg.click("#cartFab")
    pg.click("#btnGoCheckout")
    pg.fill("#fName", "Luis")
    pg.fill("#fAddress", "por la bodega de Pepe, casa azul")  # no se entiende
    pg.wait_for_timeout(1200)
    pg.fill("#fPhone", "51234567")
    # abrir mapa y marcar "Estoy aquí" + confirmar
    opener = pg.locator("#geoLine button, #geoLine a, #mapBox button").first
    if pg.locator("#mapBox").is_hidden():
        pg.locator("#geoLine button, #geoLine a").first.click()
    pg.wait_for_selector("#btnMyLoc", state="visible", timeout=5000)
    pg.click("#btnMyLoc")
    pg.wait_for_timeout(400)
    pg.click("#btnPinOk")
    pg.click("#slotBox [data-slot='asap']") if pg.locator("#slotBox [data-slot='asap']").count() else None
    pg.click("#btnSubmitOrder")
    pg.wait_for_selector("#sheetConfirm.open, #sheetConfirm.show, #sheetConfirm.active", timeout=8000)
    o = list(fake_aws.TABLES["orders"].items.values())[0]
    check(o.get("geo_src") == "customer", f"geo_src = {o.get('geo_src')} (pin del cliente)")
    check(abs(float(o["lat"]) - 21.39) < 0.02, f"lat guardada ≈ ubicación ({o['lat']})")
    ctx.close()


# ------------------------------------------------------------------ 2) seguimiento
def seed_orders():
    a = T.make_order()  # Maceo entre Cisneros y Avellaneda
    b = T.make_order(name="Beto", alias="", address="Independencia e/ Cisneros y Avellaneda", phone="5 222 3333")
    c = T.make_order(name="Carla", alias="", address="calle sin nombre conocido", phone="5 333 4444")
    return a, b, c


def test_panel(browser):
    print("\n[panel.html] flujo completo del trabajador")
    reset_world()
    a, b, c = seed_orders()
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    calls = install_network(ctx)
    pg = new_page(ctx)
    pg.goto(BASE + "/panel.html")
    check(pg.is_visible("#login") and pg.is_hidden("#app"), "sin clave: pantalla de entrada")
    pg.fill("#keyInput", "clave-mala")
    pg.click("#loginForm button")
    pg.wait_for_function("document.getElementById('loginErr').textContent.includes('incorrecta')", timeout=5000)
    check(True, "clave mala → mensaje de error")
    pg.fill("#keyInput", "clave-de-prueba")
    pg.click("#loginForm button")
    pg.wait_for_selector("#app:not(.hidden)", timeout=5000)
    pg.wait_for_selector(".order")
    check(pg.locator(".order").count() == 3, f"3 pedidos en pantalla ({pg.locator('.order').count()})")
    check(pg.evaluate("localStorage.getItem('trevol_worker_key')") == "clave-de-prueba", "clave guardada")
    txt = pg.inner_text("#colOrders")
    check("Ana Pérez" in txt and "Anita" in txt, "nombre y alias visibles")
    check("sin pin" in txt, "el pedido de Carla marca «sin pin»")
    check("aprox" in txt or "entre esquinas" in txt, "los pedidos con pin marcan su calidad")

    # XSS: un nombre malicioso no ejecuta HTML
    evil = T.make_order(name='<img src=x onerror="window.__xss=1">', phone="5 444 5555")
    pg.reload()
    pg.wait_for_selector(".order")
    check(pg.evaluate("window.__xss") is None, "nombre con HTML no se ejecuta (escapado)")
    fake_aws.TABLES["orders"].items.pop(evil["order_id"])

    # avanzar estados de a y b hasta 'listo'
    def click_status(short, label):
        pg.locator(f"#o-{short} .btn", has_text=label).first.click()
        pg.wait_for_timeout(300)

    sa, sb = a["order_id"], b["order_id"]
    pg.reload()
    pg.wait_for_selector(".order")
    for oid in (sa, sb):
        pg.locator(f"#o-{oid} [data-st='preparando']").first.click()
        pg.wait_for_selector(f"#o-{oid}.st-preparando")
        pg.locator(f"#o-{oid} [data-st='listo']").first.click()
        pg.wait_for_selector(f"#o-{oid}.st-listo")
    check(fake_aws.TABLES["orders"].items[sa]["status"] == "listo", "el estado llegó a la base de datos")

    # ruta: ir a pestaña de ruta (escritorio: ambas columnas visibles)
    pg.wait_for_selector("#btnPlan:not([disabled])")
    pg.click("#btnPlan")
    pg.wait_for_selector("#btnStart", timeout=8000)
    check(pg.locator("#routeBox .stop").count() == 2, "plan con 2 paradas")
    check(calls["osrm"] > 0, "consultó OSRM (simulado)")
    check(pg.locator("#mapWrap .tm-num").count() == 2, "2 marcadores numerados en el mapa")
    check(pg.locator("#mapWrap .tm-home").count() == 1, "marcador de cocina")
    check(calls["tiles"] > 0, "cargó mosaicos del mapa")
    pg.click("#btnStart")
    pg.wait_for_selector("#btnEndTrip", timeout=6000)
    items = fake_aws.TABLES["orders"].items
    check(items[sa]["status"] == "en_camino" and items[sb]["status"] == "en_camino", "«Salí» pone los pedidos en camino")
    trip = fake_aws.TABLES["geo"].items["rider"].get("trip")
    check(trip and len(trip["stops"]) == 2 and all(s["sec"] > 0 and s["m"] > 0 for s in trip["stops"]), "el viaje guardó tiempos y metros por tramo")

    # el cliente ve su ETA en seguir.html
    tok = items[sa]["track_token"]
    cust = new_page(ctx)
    cust.goto(f"{BASE}/seguir.html?o={sa}&t={tok}")
    cust.wait_for_function("document.body.innerText.includes('camino')", timeout=6000)
    body = cust.inner_text("body")
    check(re.search(r"\d{1,2}:\d{2}", body) is not None, "el cliente ve una hora estimada")
    cust.close()

    # entregar la primera parada desde el panel
    first = pg.locator("#routeBox .panelbox .stop .btn", has_text="Entregué").first
    first.click()
    pg.wait_for_function("document.querySelectorAll('#routeBox .panelbox:first-child .stop').length === 1", timeout=6000)
    done = [o for o in items.values() if o["status"] == "entregado"]
    check(len(done) == 1, "«Entregué» marca entregado")
    check(pg.locator("#mapWrap .tm-num").count() == 1, "el mapa solo numera lo pendiente")

    # GPS del repartidor por Telegram → el panel lo muestra
    now = int(T.CLOCK["now"].timestamp())
    T.call("POST", "/api/telegram/webhook", {"edited_message": {"message_id": 1, "date": now, "from": {"id": 222}, "chat": {"id": 222},
           "location": {"latitude": 21.381, "longitude": -77.9165}}}, headers={"x-telegram-bot-api-secret-token": "whsecret"})
    pg.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    pg.wait_for_function("document.getElementById('riderBar').classList.contains('live') || document.getElementById('riderBar').classList.contains('stale')", timeout=6000)
    check(pg.locator("#mapWrap .tm-dot").count() == 1, "punto del repartidor en el mapa")

    # fijar pin del pedido sin pin
    sc = c["order_id"]
    pg.locator(f"#o-{sc} .geo").click()
    pg.wait_for_selector("#pinMap")
    pg.click("#pinSave")
    pg.wait_for_selector("#modal", state="detached", timeout=6000)
    check(items[sc]["geo_src"] == "worker" and items[sc].get("lat") is not None, "pin fijado por el trabajador se guarda")
    check(any(k.startswith("a#") for k in fake_aws.TABLES["geo"].items), "la dirección quedó en memoria para todos")

    # reprogramar
    pg.locator(f"#o-{sc} summary").click()
    pg.locator(f"#o-{sc} [data-act='slot']").click()
    pg.wait_for_selector(".chip")
    pg.locator(".chip", has_text="Merienda").last.click()
    pg.wait_for_selector("#modal", state="detached", timeout=6000)
    check(items[sc]["slot"] == "merienda" and items[sc]["delivery_day"] == "2026-09-30", "reprogramado para mañana")

    # cancelar (un pedido nuevo de hoy) y terminar viaje
    d = T.make_order(name="Diego", phone="5 777 8888")
    sd = d["order_id"]
    pg.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    pg.wait_for_selector(f"#o-{sd}", timeout=6000)
    pg.locator(f"#o-{sd} summary").click()
    pg.locator(f"#o-{sd} [data-st='cancelado']").click()
    pg.wait_for_function(f"document.querySelector('#o-{sd}') === null", timeout=6000)
    check(items[sd]["status"] == "cancelado", "cancelado (pasa a «terminados»)")
    pg.locator(".done-toggle").click()
    check(pg.locator(f"#o-{sd}.st-cancelado").count() == 1, "«Ver terminados» lo muestra")

    pg.click("#btnEndTrip")
    pg.wait_for_selector("#btnEndTrip", state="detached", timeout=6000)
    check(True, "terminar viaje")

    # cambio de clave: 401 → vuelve al login
    old = H.WORKER_KEY
    H.WORKER_KEY = "otra-clave"
    pg.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    pg.wait_for_selector("#login:not(.hidden)", timeout=6000)
    check(pg.evaluate("localStorage.getItem('trevol_worker_key')") is None, "401 borra la clave y vuelve al login")
    H.WORKER_KEY = old
    ctx.close()


def test_panel_mobile_and_fallback(browser):
    print("\n[panel.html] móvil + OSRM caído (estimación en línea recta) + pedido nuevo")
    reset_world()
    a, b, c = seed_orders()
    ctx = browser.new_context(viewport={"width": 390, "height": 800}, has_touch=True)
    ctx.add_init_script("localStorage.setItem('trevol_worker_key','clave-de-prueba')")
    calls = install_network(ctx, osrm_mode="down")
    pg = new_page(ctx)
    pg.goto(BASE + "/panel.html")
    pg.wait_for_selector(".order")
    check(pg.is_hidden("#colRoute"), "móvil: la ruta va en otra pestaña")
    for oid in (a["order_id"],):
        pg.locator(f"#o-{oid} [data-st='preparando']").first.click()
        pg.wait_for_selector(f"#o-{oid}.st-preparando")
        pg.locator(f"#o-{oid} [data-st='listo']").first.click()
        pg.wait_for_selector(f"#o-{oid}.st-listo")
    pg.locator(f"#o-{a['order_id']} [data-act='route1']").click()
    check(pg.is_visible("#colRoute"), "«Agregar a la ruta» abre la pestaña de ruta")
    pg.click("#btnPlan")
    pg.wait_for_selector("#btnStart", timeout=8000)
    check("línea recta" in pg.inner_text("#routeBox"), "avisa que la ruta es estimada")
    check(pg.locator("#mapWrap .tm-num").count() == 1, "mapa con parada numerada")
    # pedido nuevo entra → aviso
    T.make_order(name="Nuevo Cliente", phone="5 999 0000")
    pg.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    pg.wait_for_function("document.title.startsWith('(')", timeout=6000)
    check(True, "el título muestra pedidos nuevos")
    pg.wait_for_selector(".toast", timeout=3000)
    check("Nuevo Cliente" in pg.inner_text(".toast"), "aviso de pedido nuevo")
    ctx.close()


def test_seguir_states(browser):
    print("\n[seguir.html] estados")
    reset_world()
    a = T.make_order()
    oid, tok = a["order_id"], a["track_token"]
    ctx = browser.new_context(viewport={"width": 390, "height": 800})
    install_network(ctx)
    pg = new_page(ctx)
    pg.goto(f"{BASE}/seguir.html?o={oid}&t={tok}")
    pg.wait_for_function("document.body.innerText.length > 80", timeout=6000)
    check("Ana" in pg.inner_text("body") or "pedido" in pg.inner_text("body").lower(), "muestra el pedido nuevo")
    # token malo → mensaje, sin romper
    pg.goto(f"{BASE}/seguir.html?o={oid}&t=malo")
    pg.wait_for_timeout(1500)
    check(len(pg.inner_text("body")) > 10, "token inválido no rompe la página")
    # entregado limpia el seguimiento
    fake_aws.TABLES["orders"].items[oid]["status"] = "entregado"
    pg.evaluate("localStorage.setItem('trevol_track', JSON.stringify({o:'%s',t:'%s'}))" % (oid, tok))
    pg.goto(f"{BASE}/seguir.html?o={oid}&t={tok}")
    pg.wait_for_function("document.body.innerText.toLowerCase().includes('entregado')", timeout=6000)
    check(True, "muestra «entregado»")
    ctx.close()


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("CHROMIUM", "/opt/pw-browsers/chromium"), args=["--no-sandbox"]) if os.path.exists("/opt/pw-browsers/chromium") else p.chromium.launch(args=["--no-sandbox"])
        for t in (test_checkout, test_checkout_pin, test_panel, test_panel_mobile_and_fallback, test_seguir_states):
            try:
                t(browser)
            except Exception as e:  # noqa: BLE001
                FAILS.append(f"{t.__name__}: {type(e).__name__}: {str(e)[:300]}")
                print(f"  FAIL {t.__name__}: {type(e).__name__}: {str(e)[:400]}")
        browser.close()
    print("\nErrores de consola/página:", len(ERRORS))
    for e in ERRORS[:15]:
        print("   ", e[:200])
    print("\nRESULTADO:", "TODO BIEN" if not FAILS and not ERRORS else f"{len(FAILS)} fallos, {len(ERRORS)} errores de consola")
    sys.exit(1 if FAILS or ERRORS else 0)


if __name__ == "__main__":
    main()
