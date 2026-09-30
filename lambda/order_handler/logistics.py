"""
Logistica pura (sin AWS): hora de Cuba, franjas de entrega, telefonos y
calculo del ETA. Todo aqui se puede probar sin red y sin DynamoDB.

Como se calcula el ETA (sin que el telefono del repartidor tenga la pantalla
encendida):
  * Al salir con un lote, el panel manda por cada parada cuanto tarda el tramo
    ("sec") y cuantos metros mide ("m") segun la ruta de bicicleta.
  * Si llega una ubicacion reciente (Telegram en vivo), el tiempo restante hasta
    la proxima parada se escala por lo que falta de camino.
  * Si la ubicacion es vieja o no hay, se usa el plan: hora de salida (o de la
    ultima entrega) + tiempos de los tramos. Cada "Entregue" reancla el reloj,
    asi el ETA se corrige solo aunque no haya GPS.
"""
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone

BIKE_KMH = 12.0
DETOUR = 1.35           # las calles no van en linea recta
DWELL_SEC = 120         # tiempo en cada puerta antes de seguir
FRESH_GPS_SEC = 240     # una ubicacion mas vieja que esto ya no se considera "en vivo"
PREP_MIN = int(os.environ.get("PREP_MIN", "25") or 25)
SLOT_LEAD_MIN = int(os.environ.get("SLOT_LEAD_MIN", "30") or 30)

DEFAULT_SLOTS = [
    {"id": "desayuno", "label": "Desayuno", "start": "07:00", "end": "09:00"},
    {"id": "merienda", "label": "Merienda", "start": "15:00", "end": "17:00"},
]

# Rango aproximado de Cuba: descarta pines absurdos (0,0), otro pais, etc.
CUBA_BOUNDS = (19.5, 23.6, -85.1, -73.9)  # lat_min, lat_max, lng_min, lng_max


# ---------------------------------------------------------------- hora de Cuba
def _nth_sunday(year, month, n):
    d = datetime(year, month, 1)
    first_sunday = 1 + (6 - d.weekday()) % 7
    return datetime(year, month, first_sunday + 7 * (n - 1))


def _havana_offset_manual(utc_dt):
    """Cuba: UTC-5, y UTC-4 desde el 2do domingo de marzo hasta el 1er domingo
    de noviembre. Ambos cambios ocurren a las 05:00 UTC."""
    y = utc_dt.year
    start = _nth_sunday(y, 3, 2).replace(hour=5)
    end = _nth_sunday(y, 11, 1).replace(hour=5)
    naive = utc_dt.replace(tzinfo=None)
    return timedelta(hours=-4) if start <= naive < end else timedelta(hours=-5)


def havana(utc_dt=None):
    """datetime local de Cuba (con tzinfo fijo) para un instante UTC."""
    utc_dt = utc_dt or datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return utc_dt.astimezone(ZoneInfo("America/Havana"))
    except Exception:  # sin base de zonas horarias en el runtime
        off = _havana_offset_manual(utc_dt)
        return utc_dt.astimezone(timezone(off))


def day_str(local_dt):
    return local_dt.strftime("%Y-%m-%d")


def parse_hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


# ------------------------------------------------------------------- franjas
def load_slots(raw=None):
    """Franjas configuradas (JSON en la variable DELIVERY_SLOTS). Si el JSON esta
    mal, cae a las de por defecto en vez de romper el checkout."""
    raw = os.environ.get("DELIVERY_SLOTS", "") if raw is None else raw
    if not raw:
        return [dict(s) for s in DEFAULT_SLOTS]
    try:
        slots = json.loads(raw)
        out = []
        for s in slots:
            sid = re.sub(r"[^a-z0-9_]", "", str(s["id"]).lower())
            if not sid or sid == "asap":
                continue
            parse_hm(s["start"]); parse_hm(s["end"])
            if parse_hm(s["end"]) <= parse_hm(s["start"]):
                continue
            out.append({"id": sid, "label": str(s.get("label") or sid)[:30], "start": s["start"], "end": s["end"]})
        return out or [dict(s) for s in DEFAULT_SLOTS]
    except Exception:
        return [dict(s) for s in DEFAULT_SLOTS]


def slot_by_id(slots, sid):
    for s in slots:
        if s["id"] == sid:
            return s
    return None


def slot_text(slot):
    if not slot:
        return "Lo antes posible"
    return f"{slot['label']} {slot['start']}–{slot['end']}"


def check_slot(slots, slot_id, day, now_local, enforce_lead=True):
    """Valida la franja elegida. -> (day, slot_id, error). 'asap' solo es hoy.
    Un dia valido es hoy o manana. Hoy, la franja debe terminar al menos
    SLOT_LEAD_MIN minutos despues de ahora (hay que preparar el pedido)."""
    today = day_str(now_local)
    tomorrow = day_str(now_local + timedelta(days=1))
    slot_id = (slot_id or "asap").strip().lower()
    day = (day or today).strip()
    if day not in (today, tomorrow):
        return None, None, "Solo se puede pedir para hoy o mañana"
    if slot_id == "asap":
        if day != today:
            return None, None, "“Lo antes posible” es solo para hoy"
        return day, "asap", None
    slot = slot_by_id(slots, slot_id)
    if not slot:
        return None, None, "Franja de entrega inválida"
    if enforce_lead and day == today:
        now_min = now_local.hour * 60 + now_local.minute
        if now_min + SLOT_LEAD_MIN > parse_hm(slot["end"]):
            return None, None, f"La franja {slot_text(slot)} ya cerró para hoy"
    return day, slot_id, None


def public_config(now_local):
    """Lo que el checkout necesita para dibujar el selector."""
    slots = load_slots()
    now_min = now_local.hour * 60 + now_local.minute
    out = []
    for s in slots:
        out.append({**s, "open_today": now_min + SLOT_LEAD_MIN <= parse_hm(s["end"])})
    return {
        "today": day_str(now_local),
        "tomorrow": day_str(now_local + timedelta(days=1)),
        "now": now_local.strftime("%H:%M"),
        "slots": out,
        "prep_min": PREP_MIN,
    }


# ------------------------------------------------------------------ telefonos
def normalize_phone(raw):
    """Acepta '5 1234567', '+53 5123-4567', '(53) 51234567', '0053 51234567'.
    Devuelve E.164 ('+5351234567') o '' si no parece un telefono.
    8 digitos -> Cuba (+53). Ya con codigo de pais -> se respeta."""
    s = str(raw or "").strip()
    has_plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return ""
    if not has_plus and digits.startswith("00"):
        digits, has_plus = digits[2:], True
    if has_plus:
        out = "+" + digits
    elif len(digits) == 8:
        out = "+53" + digits
    elif len(digits) == 10 and digits.startswith("53"):
        out = "+" + digits
    else:
        out = "+" + digits
    return out if re.fullmatch(r"\+[0-9]{8,15}", out) else ""


# ------------------------------------------------------------- distancias / ETA
def hav_km(a_lat, a_lng, b_lat, b_lng):
    R = 6371.0088
    rad = math.pi / 180
    dp = (b_lat - a_lat) * rad
    dl = (b_lng - a_lng) * rad
    x = math.sin(dp / 2) ** 2 + math.cos(a_lat * rad) * math.cos(b_lat * rad) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(x))


def est_sec(a_lat, a_lng, b_lat, b_lng):
    return hav_km(a_lat, a_lng, b_lat, b_lng) * DETOUR / BIKE_KMH * 3600


def valid_latlng(lat, lng):
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    if math.isnan(lat) or math.isnan(lng):
        return None
    if not (CUBA_BOUNDS[0] <= lat <= CUBA_BOUNDS[1] and CUBA_BOUNDS[2] <= lng <= CUBA_BOUNDS[3]):
        return None
    return round(lat, 6), round(lng, 6)


def trip_etas(trip, rider, now_ts):
    """ETA (epoch, segundos) de cada parada pendiente del lote en curso.

    trip  = {"anchor_ts", "stops": [{"o","lat","lng","sec","m","done"}]}
    rider = {"lat","lng","ts"} o None
    -> {order_id: {"eta": epoch, "before": paradas_antes, "mode": "gps"|"plan"}}
    """
    if not trip:
        return {}
    pending = [s for s in trip.get("stops", []) if not s.get("done")]
    if not pending:
        return {}
    anchor_ts = float(trip.get("anchor_ts") or now_ts)
    dwell = float(trip.get("dwell", DWELL_SEC))
    fresh = bool(rider and rider.get("ts") and now_ts - float(rider["ts"]) <= FRESH_GPS_SEC
                 and rider.get("lat") is not None)
    out = {}
    prev_eta = None
    for i, s in enumerate(pending):
        sec = float(s.get("sec") or 0)
        m = float(s.get("m") or 0)
        if i == 0:
            if fresh:
                d_route = hav_km(float(rider["lat"]), float(rider["lng"]), float(s["lat"]), float(s["lng"])) * 1000 * DETOUR
                if m > 0 and sec > 0:
                    rem = sec * min(1.0, d_route / m)
                else:
                    rem = d_route / 1000 / BIKE_KMH * 3600
                eta, mode = now_ts + rem, "gps"
            else:
                eta, mode = max(anchor_ts + sec, now_ts + 60), "plan"
        else:
            eta = prev_eta + dwell + sec
            mode = out[pending[0]["o"]]["mode"]
        out[s["o"]] = {"eta": int(round(eta)), "before": i, "mode": mode}
        prev_eta = eta
    return out


def prekick_eta(order_created_ts, kitchen, pin, now_ts, prep_min=None):
    """Estimacion mientras el pedido aun no salio: preparacion + trayecto directo
    desde la cocina (sin considerar otros pedidos del mismo viaje)."""
    prep = (PREP_MIN if prep_min is None else prep_min) * 60
    travel = 15 * 60
    if kitchen and pin:
        travel = est_sec(kitchen[0], kitchen[1], pin[0], pin[1]) + DWELL_SEC
    eta = max(order_created_ts + prep, now_ts) + travel
    return int(round(eta))
