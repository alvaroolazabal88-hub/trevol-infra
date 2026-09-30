import json
import os
import sys
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lambda", "order_handler"))
sys.path.insert(0, HERE)

import fake_aws  # noqa: E402

fake_aws.make_tables()
fake_aws.install()
os.environ.update({
    "ORDERS_TABLE": "orders", "COUPONS_TABLE": "coupons", "CUSTOMERS_TABLE": "customers", "GEO_TABLE": "geo",
    "TELEGRAM_BOT_TOKEN": "TESTTOKEN", "TELEGRAM_CHAT_ID": "111", "TELEGRAM_WEBHOOK_SECRET": "whsecret",
    "TELEGRAM_WORKER_IDS": "222", "WORKER_KEY": "clave-de-prueba", "ADMIN_TOKEN": "admin-secret",
    "SITE_URL": "https://trevolcamaguey.com",
})

import geo  # noqa: E402
import logistics as lg  # noqa: E402
import handler as H  # noqa: E402
from geo_fixture import DATA  # noqa: E402

geo._default = geo.Gazetteer(DATA)

# martes 29-sep-2026, 09:00 en La Habana (UTC-4 con horario de verano)
T0 = datetime(2026, 9, 29, 13, 0, tzinfo=timezone.utc)
CLOCK = {"now": T0}
H._now = lambda: CLOCK["now"]

SENT = []


class _Resp:
    def read(self):
        return b"{}"


def fake_urlopen(req, timeout=None):
    SENT.append({"url": req.full_url, "data": urllib.parse.parse_qs((req.data or b"").decode())})
    return _Resp()


H.urllib.request.urlopen = fake_urlopen

WK = {"x-worker-key": "clave-de-prueba"}
KITCHEN = (21.3808, -77.9169)


def ev(method, path, body=None, headers=None, qs=None, pp=None):
    e = {"requestContext": {"http": {"method": method, "path": path}}, "headers": headers or {}}
    if body is not None:
        e["body"] = body if isinstance(body, str) else json.dumps(body)
    if qs:
        e["queryStringParameters"] = qs
    if pp:
        e["pathParameters"] = pp
    return e


def call(method, path, body=None, **kw):
    r = H.handler(ev(method, path, body, **kw), None)
    return r["statusCode"], json.loads(r["body"])


def order_body(**over):
    b = {"name": "Ana Pérez", "alias": "Anita", "address": "Maceo #45 e/ Cisneros y Avellaneda, Rpto Garrido",
         "phone": "5 123 4567", "items": [{"id": "cafe_con_leche", "qty": 2}], "notes": "sin azúcar"}
    b.update(over)
    return b


def make_order(**over):
    code, body = call("POST", "/api/orders", order_body(**over))
    assert code == 201, body
    return body


def orders_table():
    return fake_aws.TABLES["orders"]


def geo_table():
    return fake_aws.TABLES["geo"]


class Base(unittest.TestCase):
    def setUp(self):
        fake_aws.reset()
        SENT.clear()
        CLOCK["now"] = T0


class Config(Base):
    def test_config_slots_and_days(self):
        code, cfg = call("GET", "/api/config")
        self.assertEqual(code, 200)
        self.assertEqual((cfg["today"], cfg["tomorrow"], cfg["now"]), ("2026-09-29", "2026-09-30", "09:00"))
        by = {s["id"]: s for s in cfg["slots"]}
        self.assertFalse(by["desayuno"]["open_today"])   # 09:00 + 30 min de margen > fin 09:00
        self.assertTrue(by["merienda"]["open_today"])
        self.assertIn("lat", cfg["center"])
        self.assertNotIn("kitchen", cfg)                  # el punto de la cocina no es publico

    def test_unknown_route(self):
        self.assertEqual(call("GET", "/api/nada")[0], 400)
        self.assertEqual(call("POST", "/api/nada", {})[0], 400)


class CreateOrder(Base):
    def test_happy_path_and_normalization(self):
        r = make_order()
        it = orders_table().items[r["order_id"]]
        self.assertEqual(it["phone"], "+5351234567")
        self.assertEqual((it["status"], it["slot"], it["delivery_day"]), ("nuevo", "asap", "2026-09-29"))
        self.assertEqual(it["total"], 1200)
        self.assertEqual(it["geo_src"], "between")
        self.assertLess(geo.dist_m((float(it["lat"]), float(it["lng"])), (21.3810, -77.9165)), 60)
        self.assertEqual(it["track_token"], r["track_token"])
        self.assertGreaterEqual(len(r["track_token"]), 10)
        self.assertIn(r["order_id"], [o["order_id"] for o in orders_table().query(
            IndexName="day-index", KeyConditionExpression=fake_aws.Key("delivery_day").eq("2026-09-29"))["Items"]])

    def test_phone_variants(self):
        for raw in ("+53 5123-4567", "(53) 51234567", "0053 5 1234567", "51234567"):
            fake_aws.reset()
            make_order(phone=raw)
            self.assertEqual(list(orders_table().items.values())[0]["phone"], "+5351234567", raw)
        for bad in ("", "abc", "123"):
            self.assertEqual(call("POST", "/api/orders", order_body(phone=bad))[0], 400, bad)

    def test_telegram_alert_is_html_and_escaped(self):
        make_order(name="Ana_<b>*Test*", notes="a_b *c* <i>x</i> & más", address="Maceo_1 <script>")
        tg = [s for s in SENT if s["url"].endswith("/sendMessage")]
        self.assertEqual(len(tg), 1)
        text = tg[0]["data"]["text"][0]
        self.assertEqual(tg[0]["data"]["parse_mode"], ["HTML"])
        self.assertIn("Ana_&lt;b&gt;*Test*", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertNotIn("<script>", text)
        self.assertIn("/panel.html", text)
        self.assertEqual(tg[0]["data"]["chat_id"], ["111"])

    def test_customer_pin(self):
        r = make_order(pin={"lat": 21.39, "lng": -77.90, "confirmed": True}, address="algo raro sin calle")
        it = orders_table().items[r["order_id"]]
        self.assertEqual(it["geo_src"], "customer")
        self.assertEqual((float(it["lat"]), float(it["lng"])), (21.39, -77.90))
        self.assertEqual(it["geo_conf"], 1)
        self.assertIn("✅", [s for s in SENT if s["url"].endswith("/sendMessage")][0]["data"]["text"][0])

    def test_unconfirmed_or_bad_pin_is_ignored(self):
        r = make_order(pin={"lat": 21.39, "lng": -77.90, "confirmed": False}, address="algo raro sin calle")
        self.assertEqual(orders_table().items[r["order_id"]]["geo_src"], "none")
        self.assertNotIn("lat", orders_table().items[r["order_id"]])
        fake_aws.reset()
        r = make_order(pin={"lat": 0, "lng": 0, "confirmed": True}, address="algo raro sin calle")
        self.assertEqual(orders_table().items[r["order_id"]]["geo_src"], "none")
        fake_aws.reset()
        r = make_order(pin={"lat": "x", "lng": None, "confirmed": True})
        self.assertEqual(orders_table().items[r["order_id"]]["geo_src"], "between")

    def test_no_pin_no_geocodable_address_still_saves(self):
        r = make_order(address="cerca de la casa de mi tía")
        it = orders_table().items[r["order_id"]]
        self.assertEqual(it["geo_src"], "none")
        self.assertIn("sin ubicar", SENT[-1]["data"]["text"][0])

    def test_validations(self):
        for over in ({"name": ""}, {"address": ""}, {"items": []}, {"items": [{"id": "waffles", "qty": 1}]},
                     {"items": [{"id": "x", "qty": 1}]}, {"items": [{"id": "cafe_con_leche", "qty": 99}]}):
            self.assertEqual(call("POST", "/api/orders", order_body(**over))[0], 400, over)
        self.assertEqual(call("POST", "/api/orders", "{malo")[0], 400)

    def test_slots(self):
        self.assertEqual(call("POST", "/api/orders", order_body(slot="desayuno"))[0], 400)       # hoy ya cerro
        code, r = call("POST", "/api/orders", order_body(slot="desayuno", day="2026-09-30"))
        self.assertEqual(code, 201)
        self.assertEqual(orders_table().items[r["order_id"]]["slot_label"], "Desayuno 07:00–09:00")
        self.assertEqual(orders_table().items[r["order_id"]]["delivery_day"], "2026-09-30")
        self.assertEqual(call("POST", "/api/orders", order_body(slot="merienda"))[0], 201)      # hoy abierta
        self.assertEqual(call("POST", "/api/orders", order_body(slot="asap", day="2026-09-30"))[0], 400)
        self.assertEqual(call("POST", "/api/orders", order_body(day="2026-10-05"))[0], 400)
        self.assertEqual(call("POST", "/api/orders", order_body(slot="cena"))[0], 400)

    def test_blacklist_matches_old_raw_format(self):
        fake_aws.TABLES["customers"].put_item({"phone": "51234567", "blacklisted": True})
        code, body = call("POST", "/api/orders", order_body(phone="5 123 4567"))
        self.assertEqual(code, 403)

    def test_max_active_per_phone(self):
        for _ in range(3):
            make_order()
        code, body = call("POST", "/api/orders", order_body())
        self.assertEqual(code, 429)
        make_order(phone="5 999 9999")  # otro telefono si puede

    def test_coupon_without_used_count_and_cap(self):
        fake_aws.TABLES["coupons"].put_item({"code": "HOLA10", "active": True, "discount_type": "percent",
                                             "discount_value": 10, "max_uses": 1})
        r = make_order(coupon_code="hola10")
        self.assertEqual(orders_table().items[r["order_id"]]["discount"], 120)
        self.assertEqual(fake_aws.TABLES["coupons"].items["HOLA10"]["used_count"], 1)
        code, body = call("POST", "/api/orders", order_body(coupon_code="HOLA10", phone="5 777 7777"))
        self.assertEqual(code, 400)

    def test_customer_row_is_written(self):
        make_order()
        c = fake_aws.TABLES["customers"].items["+5351234567"]
        self.assertEqual((c["order_count"], c["total_spent"]), (1, 1200))

    def test_geocode_endpoint(self):
        code, g = call("POST", "/api/geocode", {"address": "Maceo e/ Cisneros y Avellaneda"})
        self.assertEqual((code, g["src"]), (200, "between"))
        code, g = call("POST", "/api/geocode", {"address": "xx"})
        self.assertEqual(g["src"], "none")

    def test_customer_pin_remembered_only_for_that_phone(self):
        addr = "cerca de la casa azul"
        make_order(pin={"lat": 21.39, "lng": -77.90, "confirmed": True}, address=addr)
        r2 = make_order(address=addr)                                  # mismo telefono, sin pin
        self.assertEqual(orders_table().items[r2["order_id"]]["geo_src"], "memory")
        r3 = make_order(address=addr, phone="5 888 8888")              # otro telefono: no hereda el pin
        self.assertEqual(orders_table().items[r3["order_id"]]["geo_src"], "none")


class Track(Base):
    def test_bad_token_and_missing(self):
        r = make_order()
        oid = r["order_id"]
        for t in ("", "mal", None):
            code, b = call("GET", f"/api/track/{oid}", qs={"t": t} if t is not None else None)
            self.assertEqual((code, b), (200, {"found": False}))
        self.assertEqual(call("GET", "/api/track/nope", qs={"t": r["track_token"]})[1], {"found": False})

    def test_asap_estimate_and_private_fields(self):
        r = make_order()
        code, t = call("GET", f"/api/track/{r['order_id']}", qs={"t": r["track_token"]})
        self.assertTrue(t["found"])
        self.assertEqual((t["status"], t["name"], t["eta"]["mode"]), ("nuevo", "Ana", "estimate"))
        self.assertGreater(t["eta"]["eta"], int(T0.timestamp()) + lg.PREP_MIN * 60)
        self.assertNotIn("rider", t)
        for leak in ("phone", "address", "track_token", "notes"):
            self.assertNotIn(leak, t)

    def test_slot_order_shows_window_not_eta(self):
        r = make_order(slot="merienda")
        t = call("GET", f"/api/track/{r['order_id']}", qs={"t": r["track_token"]})[1]
        self.assertEqual((t["eta"]["mode"], t["eta"]["eta"]), ("slot", None))
        self.assertEqual(t["slot_label"], "Merienda 15:00–17:00")


class Panel(Base):
    def test_auth(self):
        for h in ({}, {"x-worker-key": "mala"}, {"X-Worker-Key": ""}):
            self.assertEqual(call("GET", "/api/panel/orders", headers=h)[0], 401)
        self.assertEqual(call("POST", "/api/panel/trip", {"stops": []}, headers={})[0], 401)
        self.assertEqual(call("GET", "/api/panel/orders", headers={"X-Worker-Key": "clave-de-prueba"})[0], 200)

    def test_orders_of_the_day_and_yesterdays_pending(self):
        a = make_order()
        b = make_order(slot="desayuno", day="2026-09-30", phone="5 222 2222")
        CLOCK["now"] = T0 + timedelta(days=1)   # ya es 30-sep: el pedido de ayer sigue activo
        code, p = call("GET", "/api/panel/orders", headers=WK)
        ids = {o["order_id"] for o in p["orders"]}
        self.assertEqual(ids, {a["order_id"], b["order_id"]})
        self.assertEqual(p["today"], "2026-09-30")
        self.assertIsNone(p["rider"])
        self.assertEqual(call("GET", "/api/panel/orders", headers=WK, qs={"day": "2026-10-01"})[1]["orders"], [])
        self.assertEqual(call("GET", "/api/panel/orders", headers=WK, qs={"day": "mañana"})[0], 400)
        # entregado ayer ya no aparece en "hoy"
        call("POST", f"/api/panel/orders/{a['order_id']}", {"action": "status", "status": "entregado"}, headers=WK)
        ids = {o["order_id"] for o in call("GET", "/api/panel/orders", headers=WK)[1]["orders"]}
        self.assertEqual(ids, {b["order_id"]})

    def test_status_flow_and_guards(self):
        r = make_order()
        oid = r["order_id"]
        for st in ("preparando", "listo"):
            code, b = call("POST", f"/api/panel/orders/{oid}", {"action": "status", "status": st}, headers=WK)
            self.assertEqual((code, b["order"]["status"]), (200, st))
            self.assertIn(st, b["order"]["times"])
        self.assertEqual(call("POST", f"/api/panel/orders/{oid}", {"action": "status", "status": "volando"}, headers=WK)[0], 400)
        self.assertEqual(call("POST", f"/api/panel/orders/{oid}", {"action": "otra"}, headers=WK)[0], 400)
        n = len(orders_table().items)
        self.assertEqual(call("POST", "/api/panel/orders/no-existe", {"action": "status", "status": "listo"}, headers=WK)[0], 400)
        self.assertEqual(len(orders_table().items), n)   # no se crea un pedido fantasma
        self.assertEqual(call("POST", f"/api/panel/orders/{oid}", {"action": "status", "status": "listo"}, headers={})[0], 401)

    def test_worker_pin_is_remembered_for_everyone(self):
        r = make_order(address="frente a la bodega de Juan")
        oid = r["order_id"]
        self.assertEqual(orders_table().items[oid]["geo_src"], "none")
        code, b = call("POST", f"/api/panel/orders/{oid}", {"action": "pin", "lat": 21.372, "lng": -77.921}, headers=WK)
        self.assertEqual((code, b["order"]["geo_src"], b["order"]["lat"]), (200, "worker", 21.372))
        self.assertEqual(call("POST", f"/api/panel/orders/{oid}", {"action": "pin", "lat": 5, "lng": 5}, headers=WK)[0], 400)
        r2 = make_order(address="Frente a la bodega de Juan!", phone="5 444 4444")
        self.assertEqual(orders_table().items[r2["order_id"]]["geo_src"], "memory")
        self.assertEqual(float(orders_table().items[r2["order_id"]]["lat"]), 21.372)

    def test_reschedule(self):
        r = make_order()
        code, b = call("POST", f"/api/panel/orders/{r['order_id']}",
                       {"action": "slot", "slot": "desayuno", "day": "2026-09-30"}, headers=WK)
        self.assertEqual((code, b["order"]["slot"], b["order"]["delivery_day"]), (200, "desayuno", "2026-09-30"))
        self.assertEqual(call("POST", f"/api/panel/orders/{r['order_id']}", {"action": "slot", "slot": "x"}, headers=WK)[0], 400)

    def test_no_show_by_worker_and_admin(self):
        a = make_order()
        code, b = call("POST", f"/api/panel/orders/{a['order_id']}", {"action": "no_show"}, headers=WK)
        self.assertEqual(code, 200)
        self.assertTrue(orders_table().items[a["order_id"]]["no_show"])
        self.assertEqual(fake_aws.TABLES["customers"].items["+5351234567"]["strikes"], 1)
        # ruta original con token de admin
        b2 = make_order()
        self.assertEqual(call("POST", f"/api/orders/{b2['order_id']}/no-show", {}, headers={"x-admin-token": "mal"})[0], 401)
        code, out = call("POST", f"/api/orders/{b2['order_id']}/no-show", {}, headers={"x-admin-token": "admin-secret"})
        self.assertEqual((code, out["strikes"], out["blacklisted"]), (200, 2, True))
        self.assertEqual(call("POST", "/api/orders", order_body())[0], 403)
        self.assertEqual(call("POST", "/api/orders/nope/no-show", {}, headers=WK)[0], 400)

    def test_kitchen_and_rider_endpoints(self):
        self.assertEqual(call("POST", "/api/panel/kitchen", {"lat": 21.39, "lng": -77.93}, headers=WK)[0], 200)
        self.assertEqual(call("POST", "/api/panel/kitchen", {"lat": 1, "lng": 1}, headers=WK)[0], 400)
        p = call("GET", "/api/panel/orders", headers=WK)[1]
        self.assertEqual((p["kitchen"]["lat"], p["kitchen"]["lng"]), (21.39, -77.93))
        self.assertEqual(call("POST", "/api/panel/rider", {"lat": 21.38, "lng": -77.92, "acc": 12.6}, headers=WK)[0], 200)
        p = call("GET", "/api/panel/orders", headers=WK)[1]
        self.assertEqual(p["rider"]["src"], "panel")
        self.assertEqual(call("POST", "/api/panel/rider", {"lat": 99, "lng": 0}, headers=WK)[0], 400)


class Trip(Base):
    def _three(self):
        # tres pedidos con pin, en linea recta hacia el este de la cocina
        pts = [(21.3808, -77.9100), (21.3808, -77.9020), (21.3808, -77.8940)]
        ids = []
        for i, (la, ln) in enumerate(pts):
            r = make_order(phone=f"5 10{i} 0000", pin={"lat": la, "lng": ln, "confirmed": True}, address=f"casa {i}")
            ids.append(r)
        return pts, ids

    def _legs(self, pts):
        prev, out = KITCHEN, []
        for p in pts:
            sec = lg.est_sec(prev[0], prev[1], p[0], p[1])
            out.append({"sec": int(sec), "m": int(lg.hav_km(prev[0], prev[1], p[0], p[1]) * 1000 * lg.DETOUR)})
            prev = p
        return out

    def _start(self, pts, ids):
        legs = self._legs(pts)
        stops = [{"order_id": r["order_id"], **legs[i]} for i, r in enumerate(ids)]
        code, b = call("POST", "/api/panel/trip", {"stops": stops, "start": {"lat": KITCHEN[0], "lng": KITCHEN[1]}}, headers=WK)
        self.assertEqual(code, 200, b)
        return legs, b

    def track(self, r):
        return call("GET", f"/api/track/{r['order_id']}", qs={"t": r["track_token"]})[1]

    def test_trip_plan_etas_without_gps(self):
        pts, ids = self._three()
        legs, b = self._start(pts, ids)
        t0 = int(T0.timestamp())
        for r in ids:
            self.assertEqual(orders_table().items[r["order_id"]]["status"], "en_camino")
        etas = b["etas"]
        e0, e1, e2 = (etas[r["order_id"]] for r in ids)
        self.assertEqual((e0["mode"], e0["before"], e1["before"], e2["before"]), ("plan", 0, 1, 2))
        self.assertAlmostEqual(e0["eta"], t0 + legs[0]["sec"], delta=2)
        self.assertAlmostEqual(e1["eta"], e0["eta"] + lg.DWELL_SEC + legs[1]["sec"], delta=2)
        t = self.track(ids[1])
        self.assertEqual((t["status"], t["eta"]["mode"], t["eta"]["before"]), ("en_camino", "plan", 1))
        self.assertNotIn("rider", t)     # sin ubicacion reciente no se muestra un punto falso

    def test_eta_improves_with_live_gps_and_never_shows_stale_position(self):
        pts, ids = self._three()
        legs, _ = self._start(pts, ids)
        t0 = int(T0.timestamp())
        first = self.track(ids[0])["eta"]["eta"]
        # Telegram: el repartidor esta a mitad de camino de la primera parada, 100 s despues
        mid = ((KITCHEN[0] + pts[0][0]) / 2, (KITCHEN[1] + pts[0][1]) / 2)
        CLOCK["now"] = T0 + timedelta(seconds=100)
        upd = {"message": {"from": {"id": 222}, "chat": {"id": 222}, "date": t0 + 100,
                           "location": {"latitude": mid[0], "longitude": mid[1], "live_period": 28800}}}
        code, _ = call("POST", "/api/telegram/webhook", upd, headers={"x-telegram-bot-api-secret-token": "whsecret"})
        self.assertEqual(code, 200)
        t = self.track(ids[0])
        self.assertEqual(t["eta"]["mode"], "gps")
        self.assertLess(t["eta"]["eta"], first)          # va mas rapido que el plan: ETA baja
        self.assertAlmostEqual(t["eta"]["eta"] - (t0 + 100), legs[0]["sec"] / 2, delta=legs[0]["sec"] * 0.15)
        self.assertEqual(t["rider"]["lat"], mid[0])
        self.assertLessEqual(t["rider"]["age"], 1)
        # el segundo cliente tambien mejora (encadenado)
        self.assertEqual(self.track(ids[1])["eta"]["mode"], "gps")
        # 20 min despues sin senal: vuelve al plan y NO se muestra la posicion vieja
        CLOCK["now"] = T0 + timedelta(minutes=20)
        t = self.track(ids[0])
        self.assertEqual(t["eta"]["mode"], "plan")
        self.assertNotIn("rider", t)
        self.assertGreaterEqual(t["eta"]["eta"], int(CLOCK["now"].timestamp()) + 60)

    def test_delivered_button_reanchors_clock(self):
        pts, ids = self._three()
        legs, _ = self._start(pts, ids)
        CLOCK["now"] = T0 + timedelta(minutes=5)
        now_ts = int(CLOCK["now"].timestamp())
        code, b = call("POST", f"/api/panel/orders/{ids[0]['order_id']}", {"action": "status", "status": "entregado"}, headers=WK)
        self.assertEqual(code, 200)
        self.assertEqual(self.track(ids[0])["status"], "entregado")
        t1 = self.track(ids[1])
        self.assertEqual((t1["eta"]["before"], t1["eta"]["mode"]), (0, "plan"))
        self.assertAlmostEqual(t1["eta"]["eta"], now_ts + legs[1]["sec"], delta=2)
        t2 = self.track(ids[2])
        self.assertAlmostEqual(t2["eta"]["eta"], t1["eta"]["eta"] + lg.DWELL_SEC + legs[2]["sec"], delta=2)
        # el panel ve el viaje con la primera parada hecha
        p = call("GET", "/api/panel/orders", headers=WK)[1]
        self.assertEqual([s["done"] for s in p["trip"]["stops"]], [True, False, False])
        # entregar todo cierra el viaje
        for r in ids[1:]:
            call("POST", f"/api/panel/orders/{r['order_id']}", {"action": "status", "status": "entregado"}, headers=WK)
        self.assertIsNone(self.track(ids[0])["eta"])
        self.assertEqual(lg.trip_etas(geo_table().items["rider"]["trip"], None, now_ts), {})

    def test_cancelled_order_leaves_the_batch_without_moving_the_clock(self):
        pts, ids = self._three()
        self._start(pts, ids)
        anchor = geo_table().items["rider"]["trip"]["anchor_ts"]
        call("POST", f"/api/panel/orders/{ids[0]['order_id']}", {"action": "status", "status": "cancelado"}, headers=WK)
        trip = geo_table().items["rider"]["trip"]
        self.assertEqual(trip["anchor_ts"], anchor)
        self.assertEqual([s["done"] for s in trip["stops"]], [True, False, False])
        self.assertEqual(self.track(ids[1])["eta"]["before"], 0)

    def test_pin_fix_during_trip_moves_the_stop(self):
        pts, ids = self._three()
        self._start(pts, ids)
        call("POST", f"/api/panel/orders/{ids[2]['order_id']}", {"action": "pin", "lat": 21.39, "lng": -77.90}, headers=WK)
        stop = geo_table().items["rider"]["trip"]["stops"][2]
        self.assertEqual((float(stop["lat"]), float(stop["lng"])), (21.39, -77.90))

    def test_trip_validation(self):
        pts, ids = self._three()
        no_pin = make_order(phone="5 555 0000", address="cerca de la casa de mi tía")
        code, b = call("POST", "/api/panel/trip", {"stops": [{"order_id": no_pin["order_id"], "sec": 1, "m": 1}]}, headers=WK)
        self.assertEqual(code, 400)
        self.assertIn("pin", b["error"])
        self.assertEqual(call("POST", "/api/panel/trip", {"stops": [{"order_id": "fantasma"}]}, headers=WK)[0], 400)
        self.assertEqual(orders_table().items[no_pin["order_id"]]["status"], "nuevo")
        # terminar viaje
        self._start(pts, ids)
        self.assertEqual(call("POST", "/api/panel/trip", {"stops": []}, headers=WK)[1], {"ok": True, "trip": None})
        self.assertNotIn("trip", geo_table().items["rider"])
        self.assertEqual(call("POST", "/api/panel/trip", {"stops": [{"order_id": ids[0]["order_id"], "sec": "x"}]}, headers=WK)[0], 400)

    def test_en_camino_without_trip_falls_back_to_estimate(self):
        r = make_order(pin={"lat": 21.3808, "lng": -77.9100, "confirmed": True})
        call("POST", f"/api/panel/orders/{r['order_id']}", {"action": "status", "status": "en_camino"}, headers=WK)
        t = self.track(r)
        self.assertEqual((t["status"], t["eta"]["mode"], t["eta"]["before"]), ("en_camino", "estimate", 0))


class Telegram(Base):
    SECRET = {"x-telegram-bot-api-secret-token": "whsecret"}

    def loc_msg(self, uid=222, lat=21.38, lng=-77.92, date=None, edited=False, live=True):
        m = {"from": {"id": uid}, "chat": {"id": uid}, "date": date or int(T0.timestamp()),
             "location": {"latitude": lat, "longitude": lng}}
        if live:
            m["location"]["live_period"] = 28800
        if edited:
            m["edit_date"] = date or int(T0.timestamp())
        return {"edited_message" if edited else "message": m}

    def test_secret_required(self):
        for h in ({}, {"x-telegram-bot-api-secret-token": "otro"}):
            self.assertEqual(call("POST", "/api/telegram/webhook", self.loc_msg(), headers=h)[0], 403)
        self.assertNotIn("rider", geo_table().items)

    def test_authorized_location_saved_and_updates_follow(self):
        CLOCK["now"] = T0 + timedelta(seconds=60)   # los mensajes traen fecha; no pueden ser del futuro
        t0 = int(T0.timestamp())
        call("POST", "/api/telegram/webhook", self.loc_msg(lat=21.38, date=t0), headers=self.SECRET)
        self.assertEqual(float(geo_table().items["rider"]["lat"]), 21.38)
        self.assertTrue(any("en vivo recibida" in s["data"]["text"][0] for s in SENT))
        n = len(SENT)
        call("POST", "/api/telegram/webhook", self.loc_msg(lat=21.385, date=t0 + 10, edited=True), headers=self.SECRET)
        self.assertEqual(float(geo_table().items["rider"]["lat"]), 21.385)
        self.assertEqual(len(SENT), n)       # las actualizaciones no spamean al repartidor

    def test_older_update_is_ignored(self):
        CLOCK["now"] = T0 + timedelta(seconds=60)
        t0 = int(T0.timestamp())
        call("POST", "/api/telegram/webhook", self.loc_msg(lat=21.385, date=t0 + 30, edited=True), headers=self.SECRET)
        call("POST", "/api/telegram/webhook", self.loc_msg(lat=21.370, date=t0 + 5, edited=True), headers=self.SECRET)
        self.assertEqual(float(geo_table().items["rider"]["lat"]), 21.385)

    def test_future_timestamp_is_clamped(self):
        call("POST", "/api/telegram/webhook", self.loc_msg(date=int(T0.timestamp()) + 99999, edited=True), headers=self.SECRET)
        self.assertLessEqual(int(geo_table().items["rider"]["ts"]), int(T0.timestamp()))

    def test_unauthorized_user_is_told_their_id_and_not_saved(self):
        code, _ = call("POST", "/api/telegram/webhook", self.loc_msg(uid=999), headers=self.SECRET)
        self.assertEqual(code, 200)
        self.assertNotIn("rider", geo_table().items)
        self.assertIn("999", SENT[-1]["data"]["text"][0])
        self.assertEqual(SENT[-1]["data"]["chat_id"], ["999"])

    def test_owner_chat_id_is_authorized_by_default(self):
        call("POST", "/api/telegram/webhook", self.loc_msg(uid=111), headers=self.SECRET)
        self.assertIn("rider", geo_table().items)

    def test_start_and_static_location(self):
        call("POST", "/api/telegram/webhook", {"message": {"from": {"id": 999}, "chat": {"id": 999}, "text": "/start"}}, headers=self.SECRET)
        self.assertIn("999", SENT[-1]["data"]["text"][0])
        call("POST", "/api/telegram/webhook", {"message": {"from": {"id": 222}, "chat": {"id": 222}, "text": "/start"}}, headers=self.SECRET)
        self.assertIn("tiempo real", SENT[-1]["data"]["text"][0])
        call("POST", "/api/telegram/webhook", self.loc_msg(live=False), headers=self.SECRET)
        self.assertIn("fija", SENT[-1]["data"]["text"][0])

    def test_garbage_updates_do_not_crash(self):
        for body in ({}, {"message": {}}, {"message": {"from": {"id": 222}, "chat": {"id": 222},
                                                       "location": {"latitude": "x", "longitude": 1}}},
                     {"message": {"from": {"id": 222}, "chat": {"id": 222}, "location": {"latitude": 0, "longitude": 0}}},
                     {"channel_post": {}}):
            self.assertEqual(call("POST", "/api/telegram/webhook", body, headers=self.SECRET)[0], 200)
        self.assertNotIn("rider", geo_table().items)


class Robustness(Base):
    def test_unhandled_error_returns_json_500(self):
        orig = H._handle_config
        H._handle_config = lambda: 1 / 0
        try:
            code, b = call("GET", "/api/config")
        finally:
            H._handle_config = orig
        self.assertEqual(code, 500)
        self.assertIn("error", b)

    def test_options(self):
        r = H.handler(ev("OPTIONS", "/api/orders"), None)
        self.assertEqual(r["statusCode"], 200)
        self.assertIn("X-Worker-Key", r["headers"]["Access-Control-Allow-Headers"])
        self.assertEqual(r["headers"]["Cache-Control"], "no-store")

    def test_api_gateway_path_params_also_work(self):
        r = make_order()
        code, t = call("GET", "/api/track/xxx", qs={"t": r["track_token"]}, pp={"order_id": r["order_id"]})
        self.assertTrue(t["found"])


if __name__ == "__main__":
    unittest.main()
