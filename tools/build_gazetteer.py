#!/usr/bin/env python3
"""
Genera lambda/order_handler/camaguey.json: el catalogo de calles, repartos y
lugares que usa el geocodificador. Los datos vienen de OpenStreetMap (Overpass API).

Correr UNA VEZ en tu computadora (necesita internet), y otra vez cuando quieras
refrescar los datos:

    python3 tools/build_gazetteer.py

Despues:
    python3 tools/build_gazetteer.py --check "Maceo #45 e/ Cisneros y Avellaneda"
    cd envs/prod && terraform apply        # sube el catalogo nuevo con el Lambda

Solo usa la libreria estandar de Python 3.8+. No hace falta instalar nada.

Opciones utiles:
    --bbox S,W,N,E      zona a bajar (por defecto, Camaguey ciudad con margen)
    --out RUTA          donde escribir el catalogo
    --save-raw CARPETA  guarda las respuestas crudas de Overpass
    --from-raw CARPETA  reconstruye el catalogo desde respuestas guardadas (sin internet)
"""
import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_OUT = os.path.join(ROOT, "lambda", "order_handler", "camaguey.json")
DEFAULT_BBOX = (21.32, -77.99, 21.44, -77.85)  # S, W, N, E: ciudad de Camaguey y alrededores
ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
HIGHWAYS = ("motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|"
            "pedestrian|service|road|track|primary_link|secondary_link|tertiary_link")
NAME_TAGS = ("alt_name", "old_name", "loc_name", "short_name", "official_name", "name:es", "int_name")
SAMPLE_M = 30.0        # separacion maxima entre puntos guardados de una calle
JOIN_M = 80.0          # dos tramos con el mismo nombre a menos de esto son "la misma calle"


def q_streets(b):
    s, w, n, e = b
    return (f'[out:json][timeout:180];way["highway"~"^({HIGHWAYS})$"]["name"]({s},{w},{n},{e});out geom tags;')


def q_places(b):
    s, w, n, e = b
    box = f"({s},{w},{n},{e})"
    return ("[out:json][timeout:180];("
            f'nwr["place"~"^(suburb|neighbourhood|quarter|village|hamlet|locality|city_block|borough|town)$"]["name"]{box};'
            f'nwr["landuse"="residential"]["name"]{box};'
            f'nwr["amenity"]["name"]{box};nwr["shop"]["name"]{box};nwr["leisure"]["name"]{box};'
            f'nwr["tourism"]["name"]{box};nwr["historic"]["name"]{box};nwr["office"]["name"]{box};'
            ");out center tags bb;")


def overpass(query, tries=3):
    last = None
    for attempt in range(tries):
        for url in ENDPOINTS:
            try:
                data = urllib.parse.urlencode({"data": query}).encode()
                req = urllib.request.Request(url, data=data, headers={"User-Agent": "trevol-gazetteer/1.0"})
                with urllib.request.urlopen(req, timeout=240) as r:
                    return json.loads(r.read().decode("utf-8"))
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as e:
                last = e
                print(f"  {url} fallo: {e}", file=sys.stderr)
        time.sleep(5 * (attempt + 1))
    raise SystemExit(f"Overpass no respondio: {last}")


# ------------------------------------------------------------------ geometria
def dist_m(a, b):
    dy = (a[0] - b[0]) * 110540.0
    dx = (a[1] - b[1]) * 111320.0 * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(dx, dy)


def densify(pts, max_gap=SAMPLE_M):
    """Agrega puntos intermedios para que dos vecinos esten a <= max_gap metros."""
    out = []
    for i, p in enumerate(pts):
        if i == 0:
            out.append(p)
            continue
        q = pts[i - 1]
        d = dist_m(q, p)
        k = int(d // max_gap)
        for j in range(1, k + 1):
            t = j / (k + 1)
            out.append((q[0] + (p[0] - q[0]) * t, q[1] + (p[1] - q[1]) * t))
        out.append(p)
    return out


def cluster(polylines, join_m=JOIN_M):
    """Agrupa tramos con el mismo nombre que se tocan (union-find sobre una grilla)."""
    n = len(polylines)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    cell = 0.001
    grid = {}
    for i, pl in enumerate(polylines):
        for p in pl:
            grid.setdefault((int(p[0] / cell), int(p[1] / cell)), []).append((i, p))
    for (cy, cx), items in grid.items():
        near = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                near += grid.get((cy + dy, cx + dx), [])
        for i, p in items:
            for j, q in near:
                if i != j and find(i) != find(j) and dist_m(p, q) <= join_m:
                    parent[find(i)] = find(j)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return [[polylines[i] for i in g] for g in groups.values()]


def enc(pts):
    out = []
    for la, lo in pts:
        out += [round(la * 1e5), round(lo * 1e5)]
    return out


# ----------------------------------------------------------------- transformar
def split_names(tags):
    names = set()
    for k in NAME_TAGS:
        for v in str(tags.get(k, "")).split(";"):
            v = v.strip()
            if v:
                names.add(v)
    names.discard(tags.get("name", ""))
    return sorted(names)


def build_ways(streets_json):
    by_name = {}
    for el in streets_json.get("elements", []):
        if el.get("type") != "way" or not el.get("geometry"):
            continue
        tags = el.get("tags", {})
        name = (tags.get("name") or "").strip()
        if not name:
            continue
        pts = [(g["lat"], g["lon"]) for g in el["geometry"]]
        rec = by_name.setdefault(name, {"lines": [], "alts": set()})
        rec["lines"].append(densify(pts))
        rec["alts"].update(split_names(tags))
    ways = []
    for name, rec in sorted(by_name.items()):
        for group in cluster(rec["lines"]):
            pts = [p for pl in group for p in pl]
            if not pts:
                continue
            cy = sum(p[0] for p in pts) / len(pts)
            cx = sum(p[1] for p in pts) / len(pts)
            medoid = min(pts, key=lambda p: dist_m(p, (cy, cx)))
            w = {"n": name, "p": enc(pts), "m": [round(medoid[0] * 1e5), round(medoid[1] * 1e5)]}
            if rec["alts"]:
                w["a"] = sorted(rec["alts"])
            ways.append(w)
    return ways


def build_places(places_json):
    areas, pois, seen = [], [], set()
    for el in places_json.get("elements", []):
        tags = el.get("tags", {})
        name = (tags.get("name") or "").strip()
        if len(name) < 3:
            continue
        if "lat" in el:
            c = (el["lat"], el["lon"])
        elif "center" in el:
            c = (el["center"]["lat"], el["center"]["lon"])
        else:
            continue
        alts = split_names(tags)
        is_area = "place" in tags or tags.get("landuse") == "residential"
        if is_area:
            r = 600.0
            bb = el.get("bounds")
            if bb:
                r = max(300.0, dist_m((bb["minlat"], bb["minlon"]), (bb["maxlat"], bb["maxlon"])) / 2)
            rec = {"n": name, "c": [round(c[0], 5), round(c[1], 5)], "r": int(min(r, 3500))}
            if alts:
                rec["a"] = alts
            key = ("a", name.lower(), round(c[0], 3), round(c[1], 3))
            if key not in seen:
                seen.add(key)
                areas.append(rec)
        else:
            rec = {"n": name, "c": [round(c[0], 5), round(c[1], 5)]}
            if alts:
                rec["a"] = alts
            key = ("p", name.lower(), round(c[0], 3), round(c[1], 3))
            if key not in seen:
                seen.add(key)
                pois.append(rec)
    return areas, pois


def build(streets_json, places_json, bbox):
    ways = build_ways(streets_json)
    areas, pois = build_places(places_json)
    s, w, n, e = bbox
    return {
        "v": 1,
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "source": "OpenStreetMap contributors (ODbL)",
        "center": [round((s + n) / 2, 5), round((w + e) / 2, 5)],
        "bbox": [s, w, n, e],
        "ways": ways, "areas": areas, "pois": pois,
    }


def report(data):
    ways = data["ways"]
    names = {w["n"] for w in ways}
    with_alts = sum(1 for w in ways if w.get("a"))
    pts = sum(len(w["p"]) // 2 for w in ways)
    print(f"  calles (tramos): {len(ways)}   nombres distintos: {len(names)}   con nombre alterno: {with_alts}")
    print(f"  repartos/zonas:  {len(data['areas'])}   lugares: {len(data['pois'])}   puntos de calle: {pts}")
    if len(names) < 100:
        print("  AVISO: muy pocas calles. OpenStreetMap puede estar incompleto en esa zona,"
              " o el --bbox no cubre la ciudad. El sistema funciona igual; el panel te deja fijar pines"
              " y aprende de cada correccion.")
    if not data["areas"]:
        print("  AVISO: no hay repartos con nombre. Sin ellos las calles numeradas repetidas"
              " ('3ra e/ 12 y 14') no se pueden desambiguar sin el pin del cliente.")


def check(path, text):
    sys.path.insert(0, os.path.join(ROOT, "lambda", "order_handler"))
    import geo
    gaz = geo.load_default(path)
    r = geo.geocode(text, gaz)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    if r["lat"] is not None:
        print(f"  mapa: https://www.openstreetmap.org/?mlat={r['lat']}&mlon={r['lng']}#map=18/{r['lat']}/{r['lng']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bbox", default=",".join(str(x) for x in DEFAULT_BBOX), help="S,W,N,E")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--save-raw", metavar="DIR")
    ap.add_argument("--from-raw", metavar="DIR")
    ap.add_argument("--check", metavar="DIRECCION", help="prueba una direccion contra el catalogo ya generado")
    a = ap.parse_args()

    if a.check:
        check(a.out, a.check)
        return

    bbox = tuple(float(x) for x in a.bbox.split(","))
    if len(bbox) != 4:
        raise SystemExit("--bbox necesita 4 numeros: S,W,N,E")

    if a.from_raw:
        with open(os.path.join(a.from_raw, "streets.json"), encoding="utf-8") as f:
            streets = json.load(f)
        with open(os.path.join(a.from_raw, "places.json"), encoding="utf-8") as f:
            places = json.load(f)
    else:
        print("Bajando calles de OpenStreetMap (puede tardar un minuto)...")
        streets = overpass(q_streets(bbox))
        time.sleep(2)
        print("Bajando repartos y lugares...")
        places = overpass(q_places(bbox))
        if a.save_raw:
            os.makedirs(a.save_raw, exist_ok=True)
            for nm, obj in (("streets.json", streets), ("places.json", places)):
                with open(os.path.join(a.save_raw, nm), "w", encoding="utf-8") as f:
                    json.dump(obj, f)

    data = build(streets, places, bbox)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, a.out)
    print(f"Listo: {a.out}  ({os.path.getsize(a.out) / 1024:.0f} KB)")
    report(data)


if __name__ == "__main__":
    main()
