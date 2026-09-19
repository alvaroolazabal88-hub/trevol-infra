"""
Lambda: recibe pedidos y validaciones de cupon del formulario de la pagina.
Sin frameworks pesados a proposito -- asi el paquete es chiquito y arranca
rapido (menos tiempo de ejecucion facturado = mas barato). Notificaciones
(Telegram, WhatsApp) van por HTTPS plano con urllib, sin SDKs extra.
"""
import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr

ORDERS_TABLE = os.environ["ORDERS_TABLE"]
COUPONS_TABLE = os.environ["COUPONS_TABLE"]
CUSTOMERS_TABLE = os.environ["CUSTOMERS_TABLE"]

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")  # ej: +14155238886

dynamodb = boto3.resource("dynamodb")
orders_table = dynamodb.Table(ORDERS_TABLE)
coupons_table = dynamodb.Table(COUPONS_TABLE)
customers_table = dynamodb.Table(CUSTOMERS_TABLE)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
}

# Catalogo real del negocio -- fuente de verdad de precios. El navegador
# NUNCA decide el precio; solo manda ids y cantidades, el servidor calcula.
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


def _response(status, body):
    return {
        "statusCode": status,
        "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False, default=_json_default),
    }


def _json_default(o):
    if isinstance(o, Decimal):
        return int(o) if o % 1 == 0 else float(o)
    raise TypeError


def _parse_items(items):
    """Valida items del carrito. Devuelve (clean_items, None) o (None, error_msg)."""
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
    """Devuelve (coupon_item, error_msg). coupon_item es None si no aplica."""
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


def _notify_telegram(order_id, name, phone, address, items, final_total, notes):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    lines = [f"{i['qty']}x {i['name']}" for i in items]
    text = (
        f"🆕 *Nuevo pedido* #{order_id[:8]}\n"
        f"👤 {name}" + (f"\n📞 {phone}" if phone else "") +
        f"\n📍 {address}\n\n"
        + "\n".join(lines) +
        f"\n\n💰 Total: {final_total} CUP"
    )
    if notes:
        text += f"\n📝 {notes}"

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


def _notify_whatsapp(phone, name):
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM and phone):
        return
    body = (
        f"Hola {name}, ¡tenemos tu orden! 🍽️ TREVol la está preparando. "
        f"Te contactamos en breve para confirmar el tiempo estimado de entrega. "
        f"Recuerda: se paga al recibir."
    )
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
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"WhatsApp notify failed: {e}")


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

    clean_items, err = _parse_items(items)
    if err:
        return _response(400, {"error": err})

    coupon, err = _lookup_coupon(coupon_code) if coupon_code else (None, None)
    if coupon_code and err:
        return _response(400, {"error": err})

    subtotal = _subtotal(clean_items)
    discount = _apply_discount(subtotal, coupon)
    final_total = subtotal - discount

    # Si hay cupon con tope de usos, lo reservamos atomicamente -- si dos
    # pedidos llegan a la vez y ya no queda cupo, uno de los dos falla aqui
    # en vez de dejar pasar mas usos de los permitidos.
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
    })

    try:
        _upsert_customer(phone, name, alias, final_total)
    except Exception as e:  # nunca bloquear el pedido por esto
        print(f"Customer upsert failed: {e}")

    _notify_telegram(order_id, name, phone, address, clean_items, final_total, notes)
    _notify_whatsapp(phone, name)

    return _response(201, {"ok": True, "order_id": order_id, "total": final_total})


def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "POST")
    path = event.get("requestContext", {}).get("http", {}).get("path", "")

    if method == "OPTIONS":
        return _response(200, {"ok": True})

    try:
        payload = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "JSON inválido"})

    if path.endswith("/coupons/validate"):
        return _handle_validate_coupon(payload)
    return _handle_create_order(payload)
