"""
Lambda: receives orders, validates coupons, and handles order confirmation
over WhatsApp (Twilio). No heavy frameworks on purpose -- keeps the package
small and cold starts fast (less billed execution time = cheaper).
Notifications (Telegram, WhatsApp) go out over plain HTTPS with urllib, no
extra SDKs.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from xml.sax.saxutils import escape as xml_escape

import boto3
from boto3.dynamodb.conditions import Attr, Key

ORDERS_TABLE = os.environ["ORDERS_TABLE"]
COUPONS_TABLE = os.environ["COUPONS_TABLE"]
CUSTOMERS_TABLE = os.environ["CUSTOMERS_TABLE"]

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")  # e.g. +14155238886
WEBHOOK_PUBLIC_URL = os.environ.get("WEBHOOK_PUBLIC_URL", "")  # https://yourdomain/api/whatsapp/webhook
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

STRIKES_TO_BLACKLIST = 2

dynamodb = boto3.resource("dynamodb")
orders_table = dynamodb.Table(ORDERS_TABLE)
coupons_table = dynamodb.Table(COUPONS_TABLE)
customers_table = dynamodb.Table(CUSTOMERS_TABLE)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
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

PHONE_RE = re.compile(r"^\+?[0-9]{7,15}$")
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


def _notify_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
    }).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=5)
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"Telegram notify failed: {e}")


def _notify_new_order_telegram(order_id, name, phone, address, items, final_total, notes):
    lines = [f"{i['qty']}x {i['name']}" for i in items]
    text = (
        f"🆕 *Nuevo pedido* #{order_id[:8]}\n"
        f"👤 {name}" + (f"\n📞 {phone}" if phone else "") +
        f"\n📍 {address}\n\n"
        + "\n".join(lines) +
        f"\n\n💰 Total: {final_total} CUP\n"
        f"⏳ Esperando confirmación por WhatsApp"
    )
    if notes:
        text += f"\n📝 {notes}"
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


def _handle_create_order(payload):
    name = (payload.get("name") or "").strip()
    alias = (payload.get("alias") or "").strip()[:40]
    address = (payload.get("address") or "").strip()
    phone = (payload.get("phone") or "").strip()
    items = payload.get("items") or []
    notes = (payload.get("notes") or "").strip()[:300]
    coupon_code = (payload.get("coupon_code") or "").strip()

    if not name or len(name) > 80:
        return _response(400, {"error": "Nombre requerido (máximo 80 caracteres)"})
    if not address or len(address) > 200:
        return _response(400, {"error": "Dirección requerida (máximo 200 caracteres)"})
    if not PHONE_RE.match(phone):
        return _response(400, {"error": "Teléfono inválido"})

    customer = _get_customer(phone)
    if customer and customer.get("blacklisted"):
        return _response(403, {"error": "No podemos procesar tu pedido. Contacta al negocio para más información."})

    clean_items, err = _parse_items(items)
    if err:
        return _response(400, {"error": err})

    coupon, err = _lookup_coupon(coupon_code) if coupon_code else (None, None)
    if coupon_code and err:
        return _response(400, {"error": err})

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
                ConditionExpression=Attr("used_count").lt(int(coupon["max_uses"])),
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
    now = datetime.now(timezone.utc).isoformat()

    orders_table.put_item(Item={
        "order_id": order_id,
        "created_at": now,
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
        "confirmation_status": "pendiente",
        "no_show": False,
    })

    try:
        _upsert_customer(phone, name, alias, final_total)
    except Exception as e:  # never block the order over this
        print(f"Customer upsert failed: {e}")

    _notify_new_order_telegram(order_id, name, phone, address, clean_items, final_total, notes)
    _send_confirmation_request(phone, name, clean_items, final_total)

    return _response(201, {"ok": True, "order_id": order_id, "total": final_total})


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
        _notify_telegram(f"✅ Cliente *confirmó* el pedido #{order['order_id'][:8]} ({order.get('name','')})")
        return _twiml_response("¡Gracias! Tu pedido quedó confirmado. Te lo llevamos pronto 🌿")

    if body_text in NO_WORDS:
        orders_table.update_item(
            Key={"order_id": order["order_id"]},
            UpdateExpression="SET confirmation_status = :s, confirmed_at = :now",
            ExpressionAttributeValues={":s": "cancelado", ":now": datetime.now(timezone.utc).isoformat()},
        )
        _notify_telegram(f"❌ Cliente *canceló* el pedido #{order['order_id'][:8]} ({order.get('name','')})")
        return _twiml_response("Entendido, cancelamos tu pedido. ¡Gracias por avisarnos!")

    return _twiml_response("No entendimos tu respuesta. Por favor responde *SI* para confirmar o *NO* para cancelar.")


def _handle_mark_no_show(event):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    token = headers.get("x-admin-token", "")
    if not ADMIN_TOKEN or not hmac.compare_digest(token, ADMIN_TOKEN):
        return _response(401, {"error": "No autorizado"})

    order_id = (event.get("pathParameters") or {}).get("order_id", "")
    res = orders_table.get_item(Key={"order_id": order_id})
    order = res.get("Item")
    if not order:
        return _response(404, {"error": "Pedido no encontrado"})

    orders_table.update_item(
        Key={"order_id": order_id},
        UpdateExpression="SET no_show = :true",
        ExpressionAttributeValues={":true": True},
    )

    phone = order.get("phone")
    strikes = 0
    blacklisted = False
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
            _notify_telegram(f"🚫 Cliente {order.get('name','')} ({phone}) puesto en *lista negra* tras {strikes} incumplimientos")

    return _response(200, {"ok": True, "strikes": strikes, "blacklisted": blacklisted})


def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "POST")
    path = event.get("requestContext", {}).get("http", {}).get("path", "")

    if method == "OPTIONS":
        return _response(200, {"ok": True})

    if path.endswith("/whatsapp/webhook"):
        return _handle_whatsapp_webhook(event)

    if path.endswith("/no-show"):
        return _handle_mark_no_show(event)

    try:
        payload = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "JSON inválido"})

    if path.endswith("/coupons/validate"):
        return _handle_validate_coupon(payload)
    return _handle_create_order(payload)
