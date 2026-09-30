"""
Lambda: pedidos, cupones, seguimiento para el cliente, panel de trabajadores,
ubicacion en vivo del repartidor (Telegram) y confirmacion por WhatsApp (Twilio).
Sin frameworks a proposito: paquete chico y arranque rapido = menos costo.
Las notificaciones (Telegram, WhatsApp) salen por HTTPS con urllib, sin SDKs.

Mapa de rutas (API Gateway -> este Lambda), todo bajo /api:
  publico   GET  /config              franjas de entrega y centro del mapa
            POST /geocode             direccion escrita -> pin estimado
            POST /orders              crear pedido
            POST /coupons/validate    validar cupon
            GET  /track/{id}?t=...    estado + ETA de UN pedido (con su token)
  panel     GET  /panel/orders        pedidos del dia + repartidor + ruta en curso
  (clave)   POST /panel/orders/{id}   cambiar estado / fijar pin / cambiar franja / no llego
            POST /panel/trip          salir con un lote (guarda tiempos por parada)
            POST /panel/rider         GPS del telefono (cuando el panel esta abierto)
            POST /panel/kitchen       mover el punto de fabricacion
  webhooks  POST /telegram/webhook    ubicacion en vivo del repartidor
            POST /whatsapp/webhook    respuestas de Twilio (pausado por ahora)
            POST /orders/{id}/no-show marcar "no llego" (token de admin o clave del panel)
"""
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from xml.sax.saxutils import escape as xml_escape

import boto3
from boto3.dynamodb.conditions import Attr, Key

import geo
import logistics as lg

ORDERS_TABLE = os.environ["ORDERS_TABLE"]
COUPONS_TABLE = os.environ["COUPONS_TABLE"]
CUSTOMERS_TABLE = os.environ["CUSTOMERS_TABLE"]
GEO_TABLE = os.environ["GEO_TABLE"]

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
TELEGRAM_WORKER_IDS = os.environ.get("TELEGRAM_WORKER_IDS", "")  # ids separados por coma
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")  # e.g. +14155238886
WEBHOOK_PUBLIC_URL = os.environ.get("WEBHOOK_PUBLIC_URL", "")  # https://yourdomain/api/whatsapp/webhook
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
WORKER_KEY = os.environ.get("WORKER_KEY", "")
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")
KITCHEN_LAT = float(os.environ.get("KITCHEN_LAT") or geo.DEFAULT_CENTER[0])
KITCHEN_LNG = float(os.environ.get("KITCHEN_LNG") or geo.DEFAULT_CENTER[1])

STRIKES_TO_BLACKLIST = 2

dynamodb = boto3.resource("dynamodb")
orders_table = dynamodb.Table(ORDERS_TABLE)
coupons_table = dynamodb.Table(COUPONS_TABLE)
customers_table = dynamodb.Table(CUSTOMERS_TABLE)
geo_table = dynamodb.Table(GEO_TABLE)  # cocina, repartidor, viaje en curso, pines recordados

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type, X-Worker-Key",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Cache-Control": "no-store",
}

# The business's real catalog -- source of truth for prices. The browser
# NEVER decides the price; it only sends ids and quantities, the server computes it.
PRODUCTS = {
    "cafe_con_leche":    {"name": "Café con leche",              "price": 600},
    "batido_proteina":   {"name": "Batido de proteína",          "price": 1800},
    "batido_frutas":     {"name": "Batido de frutas",            "price": 1000},
    "coctel_coco":       {"name": "Cóctel de coco",               "price": None},
    "sandwich_jq":       {"name": "Sandwich J&Q",                 "price": 1300},
    "sandwich_pollo":    {"name": "Sandwich de pollo",            "price": 1400},
    "arepa_parmesano":   {"name": "Arepa con parmesano",          "price": 1600},
    "waffles":           {"name": "Waffles",                      "price": None},
    "tortilla_huevo":    {"name": "Tortilla huevo/queso/cebolla", "price": 1500},
}

ACTIVE_STATUSES = ("nuevo", "preparando", "listo", "en_camino")
ALL_STATUSES = ACTIVE_STATUSES + ("entregado", "cancelado")
MAX_ACTIVE_PER_PHONE = 3          # pedidos en curso por telefono (contra spam)
TRACK_RIDER_MAX_AGE = 15 * 60     # no se le muestra al cliente una posicion mas vieja que esto
COUPON_RE = re.compile(r"^[A-Za-z0-9_-]{3,20}$")

YES_WORDS = {"si", "sí", "s", "yes", "y", "confirmo", "confirmar"}
NO_WORDS = {"no", "n", "cancelo", "cancelar"}


def _response(status, body):
    return {
        "statusCode": status,
        "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False, default=_json_default),
    }


def _twiml_response(message_text):
    xml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        f"<Response><Message>{xml_escape(message_text)}</Message></Response>"
    )
    return {"statusCode": 200, "headers": {"Content-Type": "text/xml"}, "body": xml}


def _json_default(o):
    if isinstance(o, Decimal):
        return int(o) if o % 1 == 0 else float(o)
    raise TypeError


def _parse_items(items):
    """Validate cart items. Returns (clean_items, None) or (None, error_msg)."""
    if not items or not isinstance(items, list):
        return None, "El pedido necesita al menos un producto"

    clean_items = []
    for it in items:
        item_id = it.get("id") if isinstance(it, dict) else None
        qty = it.get("qty", 1) if isinstance(it, dict) else 1
        product = PRODUCTS.get(item_id)
        if not product:
            return None, f"Producto inválido: {item_id}"
        if product["price"] is None:
            return None, f"{product['name']} no está disponible todavía"
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            return None, "Cantidad inválida"
        if qty < 1 or qty > 20:
            return None, "Cantidad fuera de rango (1-20)"
        clean_items.append({"id": item_id, "qty": qty, "name": product["name"], "unit_price": product["price"]})

    return clean_items, None


def _subtotal(clean_items):
    return sum(i["unit_price"] * i["qty"] for i in clean_items)


def _lookup_coupon(code):
    """Returns (coupon_item, error_msg). coupon_item is None if not applicable."""
    if not code:
        return None, None
    code = code.strip().upper()
    if not COUPON_RE.match(code):
        return None, "Código de cupón inválido"

    res = coupons_table.get_item(Key={"code": code})
    coupon = res.get("Item")
    if not coupon or not coupon.get("active", False):
        return None, "Cupón no válido"

    expires_at = coupon.get("expires_at")
    if expires_at:
        try:
            if datetime.now(timezone.utc) > datetime.fromisoformat(expires_at):
                return None, "Cupón vencido"
        except ValueError:
            pass

    max_uses = int(coupon.get("max_uses", 0))
    used_count = int(coupon.get("used_count", 0))
    if max_uses and used_count >= max_uses:
        return None, "Cupón agotado"

    return coupon, None


def _apply_discount(subtotal, coupon):
    if not coupon:
        return 0
    discount_type = coupon.get("discount_type")
    value = int(coupon.get("discount_value", 0))
    if discount_type == "percent":
        discount = round(subtotal * value / 100)
    elif discount_type == "fixed":
        discount = value
    else:
        discount = 0
    return max(0, min(discount, subtotal))


def _handle_validate_coupon(payload):
    code = (payload.get("code") or "").strip()
    items = payload.get("items") or []

    clean_items, err = _parse_items(items)
    if err:
        return _response(400, {"error": err})

    subtotal = _subtotal(clean_items)

    coupon, err = _lookup_coupon(code)
    if err:
        return _response(200, {"valid": False, "error": err, "subtotal": subtotal})

    discount = _apply_discount(subtotal, coupon)
    return _response(200, {
        "valid": True,
        "code": coupon["code"],
        "subtotal": subtotal,
        "discount": discount,
        "total": subtotal - discount,
    })


def _get_customer(phone):
    return customers_table.get_item(Key={"phone": phone}).get("Item")


def _upsert_customer(phone, name, alias, final_total):
    now = datetime.now(timezone.utc).isoformat()
    customers_table.update_item(
        Key={"phone": phone},
        UpdateExpression=(
            "SET #n = :name, alias = :alias, last_order_at = :now, "
            "first_order_at = if_not_exists(first_order_at, :now) "
            "ADD order_count :one, total_spent :total"
        ),
        ExpressionAttributeNames={"#n": "name"},
        ExpressionAttributeValues={
            ":name": name,
            ":alias": alias,
            ":now": now,
            ":one": 1,
            ":total": final_total,
        },
    )


def _h(x):
    """Escapa texto del cliente para Telegram (HTML). Con Markdown, un '_' o '*' en un
    nombre o direccion hacia fallar el aviso completo."""
    return html.escape(str(x if x is not None else ""), quote=False)


def _tg_api(method, params):
    if not TELEGRAM_BOT_TOKEN:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=5)
        return True
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"Telegram {method} failed: {e}")
        return False


def _tg_send(chat_id, text, html_mode=False):
    params = {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
    if html_mode:
        params["parse_mode"] = "HTML"
    return _tg_api("sendMessage", params)


def _notify_telegram(text):
    """Aviso al chat del negocio. 'text' es HTML (usa _h() con lo que escribe el cliente)."""
    if not TELEGRAM_CHAT_ID:
        return
    _tg_send(TELEGRAM_CHAT_ID, text, html_mode=True)


GEO_SRC_TEXT = {
    "customer": "✅ pin del cliente", "worker": "✅ pin confirmado", "memory": "✅ dirección ya conocida",
    "between": "≈ entre esquinas (revisar)", "corner": "≈ esquina (revisar)", "landmark": "≈ lugar de referencia (revisar)",
    "street": "⚠️ solo la calle (fijar pin)", "area": "⚠️ solo el reparto (fijar pin)", "none": "❓ sin ubicar (fijar pin)",
}


def _notify_new_order_telegram(order):
    lines = [f"{i['qty']}x {_h(i['name'])}" for i in order["items"]]
    who = _h(order["name"]) + (f" ({_h(order['alias'])})" if order.get("alias") else "")
    text = (
        f"🆕 <b>Nuevo pedido</b> #{order['order_id'][:8]}\n"
        f"👤 {who}\n📞 {_h(order['phone'])}\n📍 {_h(order['address'])}\n"
        f"🕒 {_h(order['slot_label'])} · {_h(order['delivery_day'])}\n"
        f"🗺 {GEO_SRC_TEXT.get(order['geo_src'], order['geo_src'])}"
    )
    if order.get("lat") is not None:
        text += f"\nhttps://www.google.com/maps?q={order['lat']},{order['lng']}"
    text += "\n\n" + "\n".join(lines) + f"\n\n💰 Total: {order['total']} CUP"
    if order.get("notes"):
        text += f"\n📝 {_h(order['notes'])}"
    if SITE_URL:
        text += f"\n\nPanel: {SITE_URL}/panel.html"
    _notify_telegram(text)


def _send_whatsapp(phone, body):
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM and phone):
        return False
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    data = urllib.parse.urlencode({
        "From": f"whatsapp:{TWILIO_WHATSAPP_FROM}",
        "To": f"whatsapp:{phone}",
        "Body": body,
    }).encode()
    auth = base64.b64encode(f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode()).decode()
    req = urllib.request.Request(url, data=data, headers={"Authorization": f"Basic {auth}"})
    try:
        urllib.request.urlopen(req, timeout=5)
        return True
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"WhatsApp notify failed: {e}")
        return False


def _send_confirmation_request(phone, name, items, final_total):
    lines = ", ".join(f"{i['qty']}x {i['name']}" for i in items)
    body = (
        f"Hola {name}, ¡somos TREVol! 🌿\n\n"
        f"Pediste: {lines}\n"
        f"Total: {final_total} CUP (se paga al recibir)\n\n"
        f"Para confirmar tu pedido responde *SI*.\n"
        f"Si ya no lo quieres, responde *NO* para cancelarlo."
    )
    _send_whatsapp(phone, body)


# ------------------------------------------------------------------ helpers
def _now():
    return datetime.now(timezone.utc)


def _dec(x):
    """float -> Decimal (boto3 no acepta float), recursivo."""
    if isinstance(x, float):
        return Decimal(str(x))
    if isinstance(x, dict):
        return {k: _dec(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_dec(v) for v in x]
    return x


def _f(x):
    return float(x) if x is not None else None


def _set_fields(table, key, fields, must_exist_attr=None):
    """SET dinamico seguro: todos los nombres/valores con placeholders (asi 'status',
    'name', etc. no chocan con palabras reservadas de DynamoDB)."""
    names, values, parts = {}, {}, []
    for i, (k, v) in enumerate(fields.items()):
        names[f"#f{i}"] = k
        values[f":v{i}"] = _dec(v)
        parts.append(f"#f{i} = :v{i}")
    kw = {}
    if must_exist_attr:
        kw["ConditionExpression"] = f"attribute_exists({must_exist_attr})"
    table.update_item(Key=key, UpdateExpression="SET " + ", ".join(parts),
                      ExpressionAttributeNames=names, ExpressionAttributeValues=values, **kw)


def _cond_failed(e):
    return isinstance(e, dynamodb.meta.client.exceptions.ConditionalCheckFailedException)


def _hash(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:20]


def _headers(event):
    return {k.lower(): v for k, v in (event.get("headers") or {}).items()}


def _body_json(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _worker_ok(event):
    key = _headers(event).get("x-worker-key", "")
    return bool(WORKER_KEY) and hmac.compare_digest(key.encode(), WORKER_KEY.encode())


def _unauthorized():
    return _response(401, {"error": "Clave incorrecta"})


def _iso_ts(iso):
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return _now().timestamp()


# ------------------------------------------- estado en la tabla "geo" (clave pk)
def _geo_get(pk):
    return geo_table.get_item(Key={"pk": pk}).get("Item")


def _get_kitchen():
    it = _geo_get("kitchen")
    if it and it.get("lat") is not None:
        return _f(it["lat"]), _f(it["lng"])
    return KITCHEN_LAT, KITCHEN_LNG


def _set_kitchen(lat, lng):
    _set_fields(geo_table, {"pk": "kitchen"}, {"lat": lat, "lng": lng, "updated_at": _now().isoformat()})


def _get_rider():
    return _geo_get("rider")


def _set_rider(lat, lng, src, ts, acc=None):
    """Guarda la ultima posicion del repartidor. Ignora las mas viejas que la guardada
    (Telegram puede reentregar mensajes). -> True si se guardo."""
    values = {":la": _dec(lat), ":ln": _dec(lng), ":ts": _dec(int(ts)), ":src": src}
    expr = "SET lat = :la, lng = :ln, #ts = :ts, src = :src"
    if acc is not None:
        expr += ", acc = :acc"
        values[":acc"] = _dec(round(float(acc)))
    try:
        geo_table.update_item(
            Key={"pk": "rider"}, UpdateExpression=expr,
            ConditionExpression="attribute_not_exists(#ts) OR #ts < :ts",
            ExpressionAttributeNames={"#ts": "ts"}, ExpressionAttributeValues=values)
        return True
    except Exception as e:
        if _cond_failed(e):
            return False
        raise


def _rider_public(rider, now_ts):
    if not rider or rider.get("lat") is None:
        return None
    return {"lat": _f(rider["lat"]), "lng": _f(rider["lng"]), "ts": int(rider["ts"]),
            "age": max(0, int(now_ts - float(rider["ts"]))), "src": rider.get("src", "")}


def _save_trip(trip):
    if trip is None:
        geo_table.update_item(Key={"pk": "rider"}, UpdateExpression="REMOVE #t", ExpressionAttributeNames={"#t": "trip"})
    else:
        _set_fields(geo_table, {"pk": "rider"}, {"trip": trip})


# ----------------------------------------------------- pines: memoria y geocodificador
def _geo_memory_lookup(phone=None):
    def lookup(akey):
        h = _hash(akey)
        it = _geo_get("a#" + h)                       # confirmado por un trabajador: vale para todos
        if it and it.get("lat") is not None:
            return _f(it["lat"]), _f(it["lng"]), "worker"
        if phone:
            it = _geo_get(f"p#{phone}#{h}")           # puesto por el propio cliente: solo para su telefono
            if it and it.get("lat") is not None:
                return _f(it["lat"]), _f(it["lng"]), "customer"
        return None
    return lookup


def _geo_remember(address, lat, lng, who, phone=None):
    h = _hash(geo.address_key(address, geo.load_default().extra_abbrev))
    pk = "a#" + h if who == "worker" else f"p#{phone}#{h}"
    _set_fields(geo_table, {"pk": pk}, {"lat": lat, "lng": lng, "src": who, "updated_at": _now().isoformat()})


def _geocode(address, phone=None):
    try:
        return geo.geocode(address, geo.load_default(), memory=_geo_memory_lookup(phone))
    except Exception as e:  # el pedido nunca debe fallar por el geocodificador
        print(f"geocode failed: {e}")
        return dict(geo.NONE)


def _customer_lookup(phone, raw):
    """Busca al cliente por el telefono normalizado y por como estaba guardado antes
    (pedidos viejos guardaron el numero tal cual, sin +53)."""
    seen = []
    for p in (phone, re.sub(r"\D", "", raw or ""), (raw or "").strip()):
        if p and p not in seen:
            seen.append(p)
            c = _get_customer(p)
            if c:
                return c
    return None


def _active_orders_of(phone):
    res = orders_table.query(IndexName="phone-index", KeyConditionExpression=Key("phone").eq(phone),
                             ScanIndexForward=False, Limit=10)
    cutoff = (_now() - timedelta(hours=12)).isoformat()
    return [o for o in res.get("Items", []) if o.get("status") in ACTIVE_STATUSES and o.get("created_at", "") >= cutoff]


def _handle_create_order(payload):
    name = (payload.get("name") or "").strip()
    alias = (payload.get("alias") or "").strip()[:40]
    address = (payload.get("address") or "").strip()
    raw_phone = (payload.get("phone") or "").strip()
    phone = lg.normalize_phone(raw_phone)
    items = payload.get("items") or []
    notes = (payload.get("notes") or "").strip()[:300]
    coupon_code = (payload.get("coupon_code") or "").strip()

    if not name or len(name) > 80:
        return _response(400, {"error": "Nombre requerido (máximo 80 caracteres)"})
    if not address or len(address) > 200:
        return _response(400, {"error": "Dirección requerida (máximo 200 caracteres)"})
    if not phone:
        return _response(400, {"error": "Teléfono inválido. Ejemplo: 5 123 4567"})

    customer = _customer_lookup(phone, raw_phone)
    if customer and customer.get("blacklisted"):
        return _response(403, {"error": "No podemos procesar tu pedido. Contacta al negocio para más información."})

    now = _now()
    now_local = lg.havana(now)
    day, slot_id, err = lg.check_slot(lg.load_slots(), payload.get("slot"), payload.get("day"), now_local)
    if err:
        return _response(400, {"error": err})
    slot = lg.slot_by_id(lg.load_slots(), slot_id)

    clean_items, err = _parse_items(items)
    if err:
        return _response(400, {"error": err})

    if len(_active_orders_of(phone)) >= MAX_ACTIVE_PER_PHONE:
        return _response(429, {"error": "Ya tienes varios pedidos en curso. Espera a recibirlos o escríbenos."})

    coupon, err = _lookup_coupon(coupon_code) if coupon_code else (None, None)
    if coupon_code and err:
        return _response(400, {"error": err})

    # --- ubicacion: el pin del cliente manda; si no, la estimamos por la direccion
    pin = payload.get("pin") if isinstance(payload.get("pin"), dict) else {}
    cust_ll = lg.valid_latlng(pin.get("lat"), pin.get("lng")) if pin.get("confirmed") else None
    if cust_ll:
        lat, lng, geo_src, geo_conf, geo_note = cust_ll[0], cust_ll[1], "customer", 1.0, ""
    else:
        g = _geocode(address, phone)
        lat, lng, geo_src, geo_conf, geo_note = g["lat"], g["lng"], g["src"], g["conf"], g.get("label", "")

    subtotal = _subtotal(clean_items)
    discount = _apply_discount(subtotal, coupon)
    final_total = subtotal - discount

    # If the coupon has a usage cap, reserve it atomically -- if two orders
    # arrive at the same time and there's no room left, one of the two fails
    # here instead of letting more uses through than are allowed.
    if coupon and int(coupon.get("max_uses", 0)):
        try:
            coupons_table.update_item(
                Key={"code": coupon["code"]},
                UpdateExpression="ADD used_count :one",
                # un cupon recien creado puede no tener used_count todavia
                ConditionExpression=Attr("used_count").not_exists() | Attr("used_count").lt(int(coupon["max_uses"])),
                ExpressionAttributeValues={":one": 1},
            )
        except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
            return _response(400, {"error": "Cupón agotado"})
    elif coupon:
        coupons_table.update_item(
            Key={"code": coupon["code"]},
            UpdateExpression="ADD used_count :one",
            ExpressionAttributeValues={":one": 1},
        )

    order_id = str(uuid.uuid4())
    track_token = secrets.token_urlsafe(9)

    order = {
        "order_id": order_id,
        "created_at": now.isoformat(),
        "name": name,
        "alias": alias,
        "address": address,
        "phone": phone,
        "items": clean_items,
        "notes": notes,
        "coupon_code": coupon["code"] if coupon else "",
        "subtotal": subtotal,
        "discount": discount,
        "total": final_total,
        "status": "nuevo",
        "no_show": False,
        "delivery_day": day,
        "slot": slot_id,
        "slot_label": lg.slot_text(slot),
        "geo_src": geo_src,
        "geo_conf": geo_conf,
        "geo_note": geo_note,
        "track_token": track_token,
    }
    if lat is not None:
        order["lat"], order["lng"] = lat, lng
    orders_table.put_item(Item=_dec(order))

    if cust_ll:  # recuerda el pin de este cliente para su proximo pedido a la misma direccion
        try:
            _geo_remember(address, lat, lng, "customer", phone)
        except Exception as e:
            print(f"remember failed: {e}")

    try:
        _upsert_customer(phone, name, alias, final_total)
    except Exception as e:  # never block the order over this
        print(f"Customer upsert failed: {e}")

    # WhatsApp confirmation is paused for now (Cuba deliverability via Twilio/Meta
    # is unconfirmed) -- orders just notify the business over Telegram.
    _notify_new_order_telegram(order)

    return _response(201, {"ok": True, "order_id": order_id, "total": final_total, "track_token": track_token,
                           "delivery_day": day, "slot": slot_id, "slot_label": order["slot_label"]})


def _handle_config():
    now_local = lg.havana(_now())
    cfg = lg.public_config(now_local)
    c = geo.load_default().center
    cfg["center"] = {"lat": c[0], "lng": c[1]}
    return _response(200, cfg)


def _handle_geocode(payload):
    address = (payload.get("address") or "").strip()[:200]
    if len(address) < 4:
        return _response(200, {"src": "none", "lat": None, "lng": None, "conf": 0})
    g = _geocode(address)
    return _response(200, {k: g[k] for k in ("lat", "lng", "conf", "src", "label")})


# ----------------------------------------------------------------------- ETA
def _order_eta(order, trip, rider, kitchen, now_ts):
    """ETA de un pedido: {"eta": epoch|None, "mode": gps|plan|estimate|slot, "before": paradas_antes}."""
    st = order.get("status")
    pin = (_f(order["lat"]), _f(order["lng"])) if order.get("lat") is not None else None
    fresh = bool(rider and rider.get("ts") and now_ts - float(rider["ts"]) <= lg.FRESH_GPS_SEC and rider.get("lat") is not None)
    if st == "en_camino":
        e = lg.trip_etas(trip, rider, now_ts).get(order["order_id"]) if trip else None
        if e:
            return {"eta": e["eta"], "mode": e["mode"], "before": e["before"]}
        if pin:
            base = (_f(rider["lat"]), _f(rider["lng"])) if fresh else kitchen
            return {"eta": int(now_ts + lg.est_sec(base[0], base[1], pin[0], pin[1]) + lg.DWELL_SEC),
                    "mode": "estimate", "before": 0}
        return None
    if st in ("nuevo", "preparando", "listo"):
        if order.get("slot", "asap") != "asap":
            return {"eta": None, "mode": "slot", "before": None}
        return {"eta": lg.prekick_eta(_iso_ts(order.get("created_at")), kitchen, pin, now_ts), "mode": "estimate", "before": None}
    return None


def _times_of(order):
    return {s: order[f"t_{s}"] for s in ALL_STATUSES if order.get(f"t_{s}")}


def _handle_track(event, order_id):
    token = ((event.get("queryStringParameters") or {}).get("t") or "").encode()
    order = orders_table.get_item(Key={"order_id": order_id or "-"}).get("Item")
    real = (order or {}).get("track_token", "")
    # 200 + found:false en todos los casos malos: no revela si el pedido existe
    # (y evita que CloudFront cambie un 404 de la API por su pagina de error).
    if not order or not real or not hmac.compare_digest(token, real.encode()):
        return _response(200, {"found": False})

    now_ts = _now().timestamp()
    rider = _get_rider()
    trip = rider.get("trip") if rider else None
    kitchen = _get_kitchen()
    out = {
        "found": True,
        "status": order["status"],
        "name": (order.get("name") or "").split(" ")[0],
        "items": [{"name": i["name"], "qty": i["qty"]} for i in order.get("items", [])],
        "total": order.get("total"),
        "delivery_day": order.get("delivery_day"),
        "slot": order.get("slot", "asap"),
        "slot_label": order.get("slot_label", ""),
        "created_at": order.get("created_at"),
        "times": _times_of(order),
        "eta": _order_eta(order, trip, rider, kitchen, now_ts),
        "pin": ({"lat": _f(order["lat"]), "lng": _f(order["lng"])} if order.get("lat") is not None else None),
        "server_ts": int(now_ts),
    }
    if order["status"] == "en_camino":
        r = _rider_public(rider, now_ts)
        if r and r["age"] <= TRACK_RIDER_MAX_AGE:
            out["rider"] = {"lat": r["lat"], "lng": r["lng"], "age": r["age"]}
    return _response(200, out)


# ----------------------------------------------------------------------- panel
def _panel_order(o, etas):
    return {
        "order_id": o["order_id"], "short": o["order_id"][:8], "created_at": o.get("created_at"),
        "name": o.get("name"), "alias": o.get("alias", ""), "phone": o.get("phone"), "address": o.get("address"),
        "notes": o.get("notes", ""), "items": o.get("items", []), "total": o.get("total"),
        "coupon_code": o.get("coupon_code", ""), "status": o.get("status"), "no_show": bool(o.get("no_show")),
        "delivery_day": o.get("delivery_day"), "slot": o.get("slot", "asap"), "slot_label": o.get("slot_label", ""),
        "lat": _f(o["lat"]) if o.get("lat") is not None else None,
        "lng": _f(o["lng"]) if o.get("lng") is not None else None,
        "geo_src": o.get("geo_src", "none"), "geo_conf": _f(o.get("geo_conf", 0)), "geo_note": o.get("geo_note", ""),
        "times": _times_of(o), "eta": etas.get(o["order_id"]),
    }


def _query_day(day):
    items, kw = [], {"IndexName": "day-index", "KeyConditionExpression": Key("delivery_day").eq(day)}
    while True:
        res = orders_table.query(**kw)
        items += res.get("Items", [])
        lek = res.get("LastEvaluatedKey")
        if not lek or len(items) >= 500:
            return items
        kw["ExclusiveStartKey"] = lek


def _panel_trip_public(trip):
    if not trip:
        return None
    return {"anchor_ts": int(trip.get("anchor_ts", 0)), "started_at": int(trip.get("started_at", 0)),
            "stops": [{"o": s["o"], "lat": _f(s["lat"]), "lng": _f(s["lng"]), "sec": _f(s.get("sec")),
                       "m": _f(s.get("m")), "done": bool(s.get("done"))} for s in trip.get("stops", [])]}


DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _handle_panel_orders(event):
    if not _worker_ok(event):
        return _unauthorized()
    now = _now()
    now_local = lg.havana(now)
    today = lg.day_str(now_local)
    day = (event.get("queryStringParameters") or {}).get("day") or today
    if not DAY_RE.match(day):
        return _response(400, {"error": "Día inválido"})
    orders = _query_day(day)
    if day == today:  # pendientes que quedaron del dia anterior
        yesterday = lg.day_str(now_local - timedelta(days=1))
        orders += [o for o in _query_day(yesterday) if o.get("status") in ACTIVE_STATUSES]
    now_ts = now.timestamp()
    rider = _get_rider()
    trip = rider.get("trip") if rider else None
    etas = lg.trip_etas(trip, rider, now_ts) if trip else {}
    orders.sort(key=lambda o: o.get("created_at", ""))
    kl = _get_kitchen()
    cfg = lg.public_config(now_local)
    return _response(200, {
        "day": day, "today": today, "tomorrow": cfg["tomorrow"], "slots": cfg["slots"],
        "server_ts": int(now_ts), "kitchen": {"lat": kl[0], "lng": kl[1]},
        "center": {"lat": geo.load_default().center[0], "lng": geo.load_default().center[1]},
        "rider": _rider_public(rider, now_ts), "trip": _panel_trip_public(trip),
        "orders": [_panel_order(o, etas) for o in orders],
    })


def _trip_of(rider):
    t = rider.get("trip") if rider else None
    return t if t and t.get("stops") else None


def _trip_stop_done(order_id, now_ts, delivered):
    """El pedido salio del lote (entregado o retirado). Si se entrego, el reloj del plan
    se reancla ahi: asi el ETA de los demas se corrige aunque no haya GPS."""
    trip = _trip_of(_get_rider())
    if not trip:
        return
    changed = False
    for s in trip["stops"]:
        if s["o"] == order_id and not s.get("done"):
            s["done"] = True
            changed = True
            if delivered:
                trip["anchor_lat"], trip["anchor_lng"], trip["anchor_ts"] = s["lat"], s["lng"], int(now_ts)
    if changed:
        _save_trip(trip)


def _trip_move_stop(order_id, lat, lng):
    trip = _trip_of(_get_rider())
    if not trip:
        return
    changed = False
    for s in trip["stops"]:
        if s["o"] == order_id and not s.get("done"):
            s["lat"], s["lng"] = _dec(lat), _dec(lng)
            changed = True
    if changed:
        _save_trip(trip)


def _apply_no_show(order):
    order_id = order["order_id"]
    _set_fields(orders_table, {"order_id": order_id}, {"no_show": True}, must_exist_attr="order_id")
    phone = order.get("phone")
    strikes, blacklisted = 0, False
    if phone:
        res = customers_table.update_item(
            Key={"phone": phone},
            UpdateExpression="ADD strikes :one",
            ExpressionAttributeValues={":one": 1},
            ReturnValues="UPDATED_NEW",
        )
        strikes = int(res["Attributes"]["strikes"])
        if strikes >= STRIKES_TO_BLACKLIST:
            blacklisted = True
            customers_table.update_item(
                Key={"phone": phone},
                UpdateExpression="SET blacklisted = :true",
                ExpressionAttributeValues={":true": True},
            )
            _notify_telegram(f"🚫 Cliente {_h(order.get('name', ''))} ({_h(phone)}) puesto en <b>lista negra</b> tras {strikes} incumplimientos")
    return {"ok": True, "strikes": strikes, "blacklisted": blacklisted}


def _handle_panel_update_order(event, order_id):
    if not _worker_ok(event):
        return _unauthorized()
    body = _body_json(event)
    if body is None:
        return _response(400, {"error": "JSON inválido"})
    order = orders_table.get_item(Key={"order_id": order_id or "-"}).get("Item")
    if not order:
        return _response(400, {"error": "Pedido no encontrado"})
    key = {"order_id": order_id}
    now = _now()
    action = body.get("action")

    if action == "status":
        new = body.get("status")
        if new not in ALL_STATUSES:
            return _response(400, {"error": "Estado inválido"})
        fields = {"status": new}
        if new != "nuevo":
            fields[f"t_{new}"] = now.isoformat()
        _set_fields(orders_table, key, fields, must_exist_attr="order_id")
        if new == "entregado":
            _trip_stop_done(order_id, now.timestamp(), delivered=True)
        elif new != "en_camino":
            _trip_stop_done(order_id, now.timestamp(), delivered=False)
    elif action == "pin":
        ll = lg.valid_latlng(body.get("lat"), body.get("lng"))
        if not ll:
            return _response(400, {"error": "Pin inválido o fuera de Cuba"})
        _set_fields(orders_table, key, {"lat": ll[0], "lng": ll[1], "geo_src": "worker", "geo_conf": 1.0, "geo_note": ""},
                    must_exist_attr="order_id")
        try:
            _geo_remember(order.get("address", ""), ll[0], ll[1], "worker")  # la proxima vez esta direccion ya se conoce
        except Exception as e:
            print(f"remember failed: {e}")
        _trip_move_stop(order_id, ll[0], ll[1])
    elif action == "slot":
        day, slot_id, err = lg.check_slot(lg.load_slots(), body.get("slot"), body.get("day"), lg.havana(now), enforce_lead=False)
        if err:
            return _response(400, {"error": err})
        _set_fields(orders_table, key, {"delivery_day": day, "slot": slot_id,
                                        "slot_label": lg.slot_text(lg.slot_by_id(lg.load_slots(), slot_id))},
                    must_exist_attr="order_id")
    elif action == "no_show":
        _apply_no_show(order)
    else:
        return _response(400, {"error": "Acción inválida"})

    fresh = orders_table.get_item(Key=key).get("Item") or order
    rider = _get_rider()
    trip = rider.get("trip") if rider else None
    etas = lg.trip_etas(trip, rider, now.timestamp()) if trip else {}
    return _response(200, {"ok": True, "order": _panel_order(fresh, etas), "trip": _panel_trip_public(_trip_of(rider))})


def _handle_panel_trip(event):
    """Sale con un lote. body: {"stops":[{"order_id","sec","m"},...], "start":{"lat","lng"}}
    sec/m = duracion (s) y largo (m) del tramo anterior->esta parada, segun la ruta de bici.
    Lista vacia = terminar el viaje."""
    if not _worker_ok(event):
        return _unauthorized()
    body = _body_json(event)
    if body is None:
        return _response(400, {"error": "JSON inválido"})
    stops_in = body.get("stops") or []
    now = _now()
    now_ts = int(now.timestamp())
    if not stops_in:
        _save_trip(None)
        return _response(200, {"ok": True, "trip": None})
    if not isinstance(stops_in, list) or len(stops_in) > 25:
        return _response(400, {"error": "Lote inválido (máximo 25 paradas)"})

    stops, orders = [], []
    for s in stops_in:
        oid = s.get("order_id") if isinstance(s, dict) else None
        o = orders_table.get_item(Key={"order_id": oid or "-"}).get("Item")
        if not o:
            return _response(400, {"error": "Pedido no encontrado en el lote"})
        if o.get("status") in ("entregado", "cancelado"):
            return _response(400, {"error": f"#{o['order_id'][:8]} ya está {o['status']}"})
        if o.get("lat") is None:
            return _response(400, {"error": f"#{o['order_id'][:8]} no tiene pin: fíjalo en el mapa primero"})
        try:
            sec = max(0, min(int(float(s.get("sec") or 0)), 3 * 3600))
            m = max(0, min(int(float(s.get("m") or 0)), 60000))
        except (TypeError, ValueError):
            return _response(400, {"error": "Tiempos del lote inválidos"})
        stops.append({"o": oid, "lat": _f(o["lat"]), "lng": _f(o["lng"]), "sec": sec, "m": m, "done": False})
        orders.append(o)

    start = lg.valid_latlng((body.get("start") or {}).get("lat"), (body.get("start") or {}).get("lng"))
    if not start:
        start = _get_kitchen()
    trip = {"started_at": now_ts, "anchor_ts": now_ts, "anchor_lat": start[0], "anchor_lng": start[1],
            "dwell": lg.DWELL_SEC, "stops": stops}
    for o in orders:
        fields = {"status": "en_camino"}
        if o.get("status") != "en_camino":
            fields["t_en_camino"] = now.isoformat()
        _set_fields(orders_table, {"order_id": o["order_id"]}, fields, must_exist_attr="order_id")
    _save_trip(trip)
    rider = _get_rider()
    return _response(200, {"ok": True, "trip": _panel_trip_public(_trip_of(rider)),
                           "etas": lg.trip_etas(_trip_of(rider), rider, now.timestamp())})


def _handle_panel_rider(event):
    if not _worker_ok(event):
        return _unauthorized()
    body = _body_json(event) or {}
    ll = lg.valid_latlng(body.get("lat"), body.get("lng"))
    if not ll:
        return _response(400, {"error": "Ubicación inválida"})
    saved = _set_rider(ll[0], ll[1], "panel", int(_now().timestamp()), body.get("acc"))
    return _response(200, {"ok": True, "saved": saved})


def _handle_panel_kitchen(event):
    if not _worker_ok(event):
        return _unauthorized()
    body = _body_json(event) or {}
    ll = lg.valid_latlng(body.get("lat"), body.get("lng"))
    if not ll:
        return _response(400, {"error": "Ubicación inválida"})
    _set_kitchen(ll[0], ll[1])
    return _response(200, {"ok": True, "kitchen": {"lat": ll[0], "lng": ll[1]}})


# --------------------------------------------------- Telegram: ubicacion en vivo
def _tg_worker_ids():
    ids = {x.strip() for x in TELEGRAM_WORKER_IDS.split(",") if x.strip()}
    if TELEGRAM_CHAT_ID:
        ids.add(str(TELEGRAM_CHAT_ID))
    return ids


HELP_TEXT = (
    "📍 Para que el cliente vea por dónde vas:\n"
    "1) Toca el clip 📎 → Ubicación → «Compartir mi ubicación en tiempo real».\n"
    "2) Elige «hasta que la desactive» u 8 horas.\n"
    "Hazlo una vez al salir; no hace falta dejar Telegram abierto ni la pantalla encendida."
)


def _handle_telegram_webhook(event):
    got = _headers(event).get("x-telegram-bot-api-secret-token", "")
    if not TELEGRAM_WEBHOOK_SECRET or not hmac.compare_digest(got.encode(), TELEGRAM_WEBHOOK_SECRET.encode()):
        return _response(403, {"error": "forbidden"})
    update = _body_json(event) or {}
    msg = update.get("message") or update.get("edited_message")
    ok = _response(200, {"ok": True})
    if not isinstance(msg, dict):
        return ok
    user_id = (msg.get("from") or {}).get("id")
    chat_id = (msg.get("chat") or {}).get("id")
    allowed = str(user_id) in _tg_worker_ids() or str(chat_id) in _tg_worker_ids()
    text = (msg.get("text") or "").strip().lower()
    loc = msg.get("location")

    if text.startswith("/start") or text.startswith("/id") or text.startswith("/ayuda"):
        if allowed:
            _tg_send(chat_id, "Hola 👋 Ya estás autorizado como repartidor.\n\n" + HELP_TEXT)
        else:
            _tg_send(chat_id, f"Hola 👋 Tu id de Telegram es {user_id}. Pásaselo al dueño para que te autorice como repartidor.")
        return ok

    if isinstance(loc, dict) and "latitude" in loc:
        if not allowed:
            _tg_send(chat_id, f"No estás autorizado como repartidor. Tu id de Telegram es {user_id}; pásaselo al dueño.")
            return ok
        ll = lg.valid_latlng(loc.get("latitude"), loc.get("longitude"))
        if not ll:
            return ok
        now_ts = int(_now().timestamp())
        ts = min(int(msg.get("edit_date") or msg.get("date") or now_ts), now_ts)
        saved = _set_rider(ll[0], ll[1], "telegram", ts, loc.get("horizontal_accuracy"))
        if saved and "edit_date" not in msg and "live_period" in loc:
            _tg_send(chat_id, "📍 Ubicación en vivo recibida. Los clientes ya ven tu avance.")
        elif saved and "edit_date" not in msg and "live_period" not in loc:
            _tg_send(chat_id, "📍 Recibí una ubicación fija, no en tiempo real. Para el avance en vivo:\n" + HELP_TEXT)
    return ok


def _validate_twilio_signature(url, params, signature):
    if not signature or not TWILIO_AUTH_TOKEN:
        return False
    s = url
    for key in sorted(params.keys()):
        s += key + params[key]
    computed = base64.b64encode(
        hmac.new(TWILIO_AUTH_TOKEN.encode(), s.encode(), hashlib.sha1).digest()
    ).decode()
    return hmac.compare_digest(computed, signature)


def _find_pending_order(phone):
    res = orders_table.query(
        IndexName="phone-index",
        KeyConditionExpression=Key("phone").eq(phone),
        ScanIndexForward=False,  # most recent first
        Limit=10,
    )
    for item in res.get("Items", []):
        if item.get("confirmation_status") == "pendiente":
            return item
    return None


def _handle_whatsapp_webhook(event):
    raw_body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw_body).decode()

    params = {k: v[0] for k, v in urllib.parse.parse_qs(raw_body).items()}

    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    signature = headers.get("x-twilio-signature", "")

    if WEBHOOK_PUBLIC_URL and not _validate_twilio_signature(WEBHOOK_PUBLIC_URL, params, signature):
        print("Invalid Twilio signature, ignoring the message")
        return {"statusCode": 403, "headers": {"Content-Type": "text/plain"}, "body": "Forbidden"}

    from_raw = params.get("From", "")
    phone = from_raw.replace("whatsapp:", "").strip()
    body_text = (params.get("Body") or "").strip().lower()
    # strips simple accents so "sí" -> "si"
    body_text = body_text.replace("í", "i").replace("á", "a")

    if not phone:
        return _twiml_response("No pudimos leer tu número. Contáctanos directamente.")

    order = _find_pending_order(phone)
    if not order:
        return _twiml_response("No encontramos un pedido pendiente de confirmar a tu nombre.")

    if body_text in YES_WORDS:
        orders_table.update_item(
            Key={"order_id": order["order_id"]},
            UpdateExpression="SET confirmation_status = :s, confirmed_at = :now",
            ExpressionAttributeValues={":s": "confirmado", ":now": datetime.now(timezone.utc).isoformat()},
        )
        _notify_telegram(f"✅ Cliente <b>confirmó</b> el pedido #{order['order_id'][:8]} ({_h(order.get('name', ''))})")
        return _twiml_response("¡Gracias! Tu pedido quedó confirmado. Te lo llevamos pronto 🌿")

    if body_text in NO_WORDS:
        orders_table.update_item(
            Key={"order_id": order["order_id"]},
            UpdateExpression="SET confirmation_status = :s, confirmed_at = :now",
            ExpressionAttributeValues={":s": "cancelado", ":now": datetime.now(timezone.utc).isoformat()},
        )
        _notify_telegram(f"❌ Cliente <b>canceló</b> el pedido #{order['order_id'][:8]} ({_h(order.get('name', ''))})")
        return _twiml_response("Entendido, cancelamos tu pedido. ¡Gracias por avisarnos!")

    return _twiml_response("No entendimos tu respuesta. Por favor responde *SI* para confirmar o *NO* para cancelar.")


def _handle_mark_no_show(event, order_id):
    token = _headers(event).get("x-admin-token", "")
    admin_ok = bool(ADMIN_TOKEN) and hmac.compare_digest(token.encode(), ADMIN_TOKEN.encode())
    if not (admin_ok or _worker_ok(event)):
        return _response(401, {"error": "No autorizado"})
    order = orders_table.get_item(Key={"order_id": order_id or "-"}).get("Item")
    if not order:
        return _response(400, {"error": "Pedido no encontrado"})
    return _response(200, _apply_no_show(order))


# --------------------------------------------------------------- despacho
def _match(path, pattern):
    return re.search(r"(?:^|/)api/" + pattern + r"/?$", path)


def handler(event, context):
    http = event.get("requestContext", {}).get("http", {})
    method = http.get("method", "POST")
    path = http.get("path", "")
    pp = event.get("pathParameters") or {}

    if method == "OPTIONS":
        return _response(200, {"ok": True})

    try:
        return _dispatch(event, method, path, pp)
    except Exception as e:  # nunca una pantalla en blanco: el error queda en CloudWatch
        print(f"UNHANDLED {method} {path}: {type(e).__name__}: {e}")
        return _response(500, {"error": "Error interno. Intenta de nuevo en un momento."})


def _dispatch(event, method, path, pp):
    if method == "GET":
        if _match(path, "config"):
            return _handle_config()
        m = _match(path, r"track/([^/]+)")
        if m:
            return _handle_track(event, pp.get("order_id") or m.group(1))
        if _match(path, "panel/orders"):
            return _handle_panel_orders(event)
        return _response(400, {"error": "Ruta desconocida"})

    # webhooks: cuerpo propio (Twilio manda formulario, Telegram su propio JSON)
    if _match(path, "whatsapp/webhook"):
        return _handle_whatsapp_webhook(event)
    if _match(path, "telegram/webhook"):
        return _handle_telegram_webhook(event)

    m = _match(path, r"orders/([^/]+)/no-show")
    if m:
        return _handle_mark_no_show(event, pp.get("order_id") or m.group(1))
    m = _match(path, r"panel/orders/([^/]+)")
    if m:
        return _handle_panel_update_order(event, pp.get("order_id") or m.group(1))
    if _match(path, "panel/trip"):
        return _handle_panel_trip(event)
    if _match(path, "panel/rider"):
        return _handle_panel_rider(event)
    if _match(path, "panel/kitchen"):
        return _handle_panel_kitchen(event)

    payload = _body_json(event)
    if payload is None:
        return _response(400, {"error": "JSON inválido"})
    if _match(path, "coupons/validate"):
        return _handle_validate_coupon(payload)
    if _match(path, "geocode"):
        return _handle_geocode(payload)
    if _match(path, "orders"):
        return _handle_create_order(payload)
    return _response(400, {"error": "Ruta desconocida"})
