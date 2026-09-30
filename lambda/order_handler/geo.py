"""
Geocodificador OFFLINE de direcciones de Camaguey (sin Google, sin API de pago).

Convierte un texto como
    "Ave. de los Martires #45 e/ Cisneros y Independencia, Rpto Garrido"
en una coordenada estimada, aunque este mal escrito. Usa un catalogo local
(camaguey.json, generado con tools/build_gazetteer.py desde OpenStreetMap) y
entiende la gramatica de direcciones cubanas:

    e/  = entre          esq. / esquina a   = en la esquina con
    Rpto / Repto = reparto        Ave = avenida       #45 / No. 45 = numero
    "3ra e/ 12 y 14" = calle numerada     "frente al parque X" = punto de referencia

Que tan segura es la coordenada se dice en "conf" (0..1) y "src":
    between  entre dos esquinas (punto medio de la cuadra)    conf ~0.9
    corner   esquina                                          conf ~0.85
    landmark punto de referencia (parque, escuela...)         conf ~0.75
    street   solo la calle (punto mas cercano al reparto)     conf ~0.45
    area     solo el reparto (su centro)                      conf ~0.30
    none     no se pudo ubicar                                conf 0
El pin que pone el cliente siempre manda; esto es para cuando no lo pone.
"""
import json
import math
import os
import re
import unicodedata
from difflib import SequenceMatcher, get_close_matches

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CENTER = (21.3808, -77.9169)  # Parque Ignacio Agramonte, Camaguey

# ----------------------------------------------------------------- vocabulario
ABBREV = {
    "rpto": "reparto", "repto": "reparto", "rep": "reparto", "reprto": "reparto", "reparto": "reparto",
    "ave": "avenida", "av": "avenida", "avda": "avenida", "aven": "avenida",
    "ctra": "carretera", "carr": "carretera", "crta": "carretera",
    "cll": "calle", "cal": "calle", "clle": "calle",
    "edif": "edificio", "edifi": "edificio", "ed": "edificio",
    "apto": "apartamento", "apt": "apartamento", "ap": "apartamento", "apart": "apartamento",
    "bl": "bloque", "bloq": "bloque",
    "esq": "esquina", "esqu": "esquina",
    "prol": "prolongacion", "prolong": "prolongacion",
    "circ": "circunvalacion", "circunv": "circunvalacion",
    "fte": "frente", "frnte": "frente",
    "gral": "general", "cmdte": "comandante", "cnel": "coronel", "dr": "doctor", "sta": "santa", "sto": "santo",
}
ORDINAL_WORDS = {
    "primera": "1", "primero": "1", "segunda": "2", "segundo": "2", "tercera": "3", "tercero": "3",
    "cuarta": "4", "cuarto": "4", "quinta": "5", "quinto": "5", "sexta": "6", "sexto": "6",
    "septima": "7", "septimo": "7", "octava": "8", "octavo": "8", "novena": "9", "noveno": "9",
    "decima": "10", "decimo": "10",
}
ORDINAL_RE = re.compile(r"^(\d{1,3})(?:ra|da|ta|ma|va|na|ro|do|to|mo|vo|no|er|era|ero|o|a)$")
STOP = {"de", "del", "la", "las", "los", "el", "y", "e", "a", "al", "en", "con", "por"}
TYPE_WORDS = {"calle", "avenida", "carretera", "camino", "callejon", "paseo", "pasaje", "prolongacion",
              "circunvalacion", "autopista", "vial", "calzada", "sendero"}
AREA_TYPES = {"reparto", "residencial", "barrio", "zona", "poblado", "urbanizacion", "consejo", "popular"}

# palabra que abre un campo -> nombre del campo
MARKERS = {
    "entre": "entre", "esquina": "corner", "#": "num",
    "reparto": "area", "residencial": "area", "barrio": "area", "zona": "area", "poblado": "area",
    "urbanizacion": "area",
    "frente": "lm", "cerca": "lm", "detras": "lm", "enfrente": "lm", "lado": "lm", "junto": "lm",
    "edificio": "unit", "apartamento": "unit", "bloque": "unit", "casa": "unit", "altos": "unit",
    "local": "unit", "piso": "unit", "entrada": "unit", "escalera": "unit", "planta": "unit", "bajos": "unit",
}
LM_FILLER = {"frente", "cerca", "detras", "enfrente", "lado", "junto", "casi", "de", "del", "al", "a", "la", "el"}


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def phon(t):
    """Forma 'fonetica' para perdonar faltas tipicas: b/v, s/z/c, ll/y, h muda, qu/k."""
    t = t.replace("ch", "X")
    t = re.sub(r"qu(?=[ei])", "k", t)
    t = re.sub(r"c(?=[ei])", "s", t)
    t = t.replace("c", "k").replace("q", "k")
    t = t.replace("z", "s").replace("v", "b").replace("w", "u")
    t = t.replace("ll", "y").replace("h", "")
    t = re.sub(r"g(?=[ei])", "j", t)
    t = re.sub(r"gu(?=[ei])", "g", t)
    t = re.sub(r"(.)\1+", r"\1", t)
    return t


def tokenize(text, extra_abbrev=None):
    """Texto libre -> lista de tokens normalizados (sin acentos, abreviaturas
    expandidas, ordinales como digitos, 'e/' como 'entre', '#' como marcador)."""
    s = strip_accents(str(text or "").lower())
    s = re.sub(r"\be\s*/\s*", " entre ", s)                    # e/
    s = re.sub(r"\bc\s*/\s*", " calle ", s)                    # c/
    s = re.sub(r"\bs\s*/\s*n\b", " ", s)                       # s/n
    s = re.sub(r"\b(?:no|num|nro|numero)\b\.?\s*(?=\d)", " # ", s)   # No. 45
    s = s.replace("#", " # ").replace("º", " ").replace("°", " ").replace("ª", " ")
    s = re.sub(r"(?<=\d)\s*/\s*(?=\d)", " ", s)
    s = re.sub(r"[^a-z0-9#\s]", " ", s)
    ab = dict(ABBREV)
    if extra_abbrev:
        ab.update(extra_abbrev)
    out = []
    for t in s.split():
        t = ab.get(t, t)
        t = ORDINAL_WORDS.get(t, t)
        m = ORDINAL_RE.match(t)
        if m:
            t = m.group(1)
        out.append(t)
    return out


def name_key(tokens):
    """Tokens de un nombre de calle/reparto sin 'calle', 'de la', iniciales sueltas..."""
    toks = list(tokens)
    while len(toks) > 1 and (toks[0] in TYPE_WORDS or toks[0] in AREA_TYPES):
        toks = toks[1:]
    if len(toks) == 1:
        return toks
    keep = [t for t in toks if t not in STOP]
    if not keep:
        return toks
    multi = [t for t in keep if len(t) > 1 or t.isdigit()]
    return multi or keep


def dist_m(a, b):
    dy = (a[0] - b[0]) * 110540.0
    dx = (a[1] - b[1]) * 111320.0 * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(dx, dy)


def name_score(q, c):
    """Parecido entre el nombre escrito (q) y uno del catalogo (c): 0..1."""
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    if [phon(t) for t in q] == [phon(t) for t in c]:
        return 0.96
    matched, total = 0, 0.0
    used = set()
    for t in q:
        best, bj = 0.0, -1
        for j, u in enumerate(c):
            if t.isdigit() or u.isdigit() or len(t) == 1 or len(u) == 1:
                r = 1.0 if t == u else 0.0
            else:
                r = max(SequenceMatcher(None, t, u).ratio(),
                        SequenceMatcher(None, phon(t), phon(u)).ratio() * 0.98)
                if r < 0.8:
                    r = 0.0
            if r > best:
                best, bj = r, j
        if best > 0:
            matched += 1
            used.add(bj)
        total += best
    if not matched:
        return 0.0
    base = total / len(q)
    cover_c = len(used) / len(c)
    score = base * (0.86 + 0.14 * cover_c)
    if matched < len(q):
        score *= 0.75
    return score


# ------------------------------------------------------------------- catalogo
class Gazetteer:
    MIN_SCORE = 0.72

    def __init__(self, data=None, aliases=None):
        data = data or {}
        aliases = aliases or {}
        self.extra_abbrev = {k.lower(): v.lower() for k, v in (aliases.get("abbreviations") or {}).items()}
        self.center = tuple(data.get("center") or DEFAULT_CENTER)
        self.bbox = data.get("bbox")
        self.ways = []
        self.areas = []
        self.pois = []
        self._idx = {"way": {}, "area": {}, "poi": {}}
        self._alias = {
            "way": self._alias_map(aliases.get("streets")),
            "area": self._alias_map(aliases.get("areas")),
            "poi": self._alias_map(aliases.get("landmarks")),
        }
        for w in data.get("ways", []):
            self._add("way", {"disp": w["n"], "names": [w["n"]] + list(w.get("a", [])), "p": w["p"],
                              "m": w.get("m"), "cache": None, "bb": None})
        for a in data.get("areas", []):
            self._add("area", {"disp": a["n"], "names": [a["n"]] + list(a.get("a", [])), "c": tuple(a["c"]), "r": float(a.get("r", 600))})
        for p in data.get("pois", []):
            self._add("poi", {"disp": p["n"], "names": [p["n"]] + list(p.get("a", [])), "c": tuple(p["c"])})
        self._vocab = {k: list(v.keys()) for k, v in self._idx.items()}
        self._ix_cache = {}

    # -- helpers de carga
    def _alias_map(self, d):
        out = {}
        for k, v in (d or {}).items():
            if str(k).startswith("_"):
                continue
            out[" ".join(name_key(tokenize(k, self.extra_abbrev)))] = name_key(tokenize(v, self.extra_abbrev))
        return out

    def _add(self, kind, rec):
        lst = {"way": self.ways, "area": self.areas, "poi": self.pois}[kind]
        rec["keys"] = [name_key(tokenize(n, self.extra_abbrev)) for n in rec["names"]]
        i = len(lst)
        lst.append(rec)
        for key in rec["keys"]:
            for t in key:
                self._idx[kind].setdefault(phon(t), set()).add(i)

    def _rec(self, kind, i):
        return {"way": self.ways, "area": self.areas, "poi": self.pois}[kind][i]

    def empty(self):
        return not (self.ways or self.areas or self.pois)

    # -- busqueda por nombre
    def find(self, kind, tokens, limit=5):
        """Candidatos [(puntaje, indice)] para un nombre escrito. Si el nombre esta en
        aliases.json se prueba primero el oficial; si ese no aparece, el escrito."""
        key = name_key(tokens)
        if not key:
            return []
        alias = self._alias[kind].get(" ".join(key))
        for k in ([alias, key] if alias else [key]):
            res = self._find_key(kind, k, limit)
            if res:
                return res
        return []

    def _find_key(self, kind, key, limit):
        idx = self._idx[kind]
        cand = set()
        for t in key:
            pt = phon(t)
            if pt in idx:
                cand |= idx[pt]
            elif len(pt) >= 3 and not pt.isdigit():
                for close in get_close_matches(pt, self._vocab[kind], n=4, cutoff=0.74):
                    cand |= idx[close]
        scored = []
        for i in cand:
            rec = self._rec(kind, i)
            s = max(name_score(key, k) for k in rec["keys"])
            if s >= self.MIN_SCORE:
                scored.append((s, i))
        longer = (lambda i: len(self.ways[i]["p"])) if kind == "way" else (lambda i: 0)
        scored.sort(key=lambda x: (-round(x[0], 3), -longer(x[1])))
        return scored[:limit]

    # -- geometria de las calles
    def pts(self, i):
        w = self.ways[i]
        if w["cache"] is None:
            p = w["p"]
            w["cache"] = [(p[k] / 1e5, p[k + 1] / 1e5) for k in range(0, len(p) - 1, 2)]
        return w["cache"]

    def bbox_of(self, i):
        w = self.ways[i]
        if w["bb"] is None:
            P = self.pts(i)
            la = [q[0] for q in P]
            lo = [q[1] for q in P]
            w["bb"] = (min(la), min(lo), max(la), max(lo))
        return w["bb"]

    def intersect(self, ia, ib, max_m=120.0):
        """Punto donde se cruzan dos calles (o casi): -> ((lat,lng), distancia) o None."""
        if ia == ib:
            return None
        ck = (min(ia, ib), max(ia, ib), max_m)
        if ck not in self._ix_cache:
            self._ix_cache[ck] = self._intersect(ia, ib, max_m)
        return self._ix_cache[ck]

    def _intersect(self, ia, ib, max_m):
        a, b = self.bbox_of(ia), self.bbox_of(ib)
        pad = 0.0016
        if a[2] + pad < b[0] or b[2] + pad < a[0] or a[3] + pad < b[1] or b[3] + pad < a[1]:
            return None
        A = [p for p in self.pts(ia) if b[0] - pad <= p[0] <= b[2] + pad and b[1] - pad <= p[1] <= b[3] + pad]
        B = [p for p in self.pts(ib) if a[0] - pad <= p[0] <= a[2] + pad and a[1] - pad <= p[1] <= a[3] + pad]
        best, bp = 1e18, None
        for p in A:
            for q in B:
                d = dist_m(p, q)
                if d < best:
                    best, bp = d, ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2)
        return (bp, best) if bp and best <= max_m else None

    def nearest_point(self, i, ref):
        best, bp = 1e18, None
        for p in self.pts(i):
            d = dist_m(p, ref)
            if d < best:
                best, bp = d, p
        return bp, best

    def middle_point(self, i):
        """Punto 'central' de la calle (el mas cercano a su centro si el catalogo lo trae)."""
        m = self.ways[i].get("m")
        if m:
            return (m[0] / 1e5, m[1] / 1e5)
        P = self.pts(i)
        return P[len(P) // 2]


# -------------------------------------------------------------------- parseo
def _parse_segment(toks):
    fields = [["street", []]]
    for t in toks:
        if t in MARKERS:
            kind = MARKERS[t]
            # "zona 3" / "poblado X": la palabra es parte del nombre del area
            fields.append([kind, [t] if t in ("zona", "poblado") else []])
            continue
        fields[-1][1].append(t)
    return fields


def parse_address(text, extra_abbrev=None):
    """-> dict(street, number, cross1, cross2, corner, area, lm, free, tokens)
    con listas de tokens. Las comas separan segmentos; el primero que trae calle
    manda, y los segmentos sueltos (p. ej. ', Garrido') quedan en 'free'."""
    out = {"street": [], "number": None, "cross1": [], "cross2": [], "corner": [], "area": [], "lm": [],
           "free": [], "tokens": tokenize(text, extra_abbrev)}
    for seg in re.split(r"[,;\n]+", str(text or "")):
        toks = tokenize(seg, extra_abbrev)
        if not toks:
            continue
        for kind, tk in _parse_segment(toks):
            if not tk and kind != "street":
                continue
            if kind == "street":
                if not out["street"] and tk:
                    out["street"] = tk
                elif tk:
                    out["free"].append(tk)
            elif kind == "num":
                if out["number"] is None and tk[0].isdigit():
                    out["number"] = tk[0]
            elif kind == "entre" and not out["cross1"]:
                cut = next((i for i, t in enumerate(tk) if t in ("y", "e", "con") and 0 < i < len(tk) - 1), None)
                if cut is None:
                    out["cross1"] = tk
                else:
                    out["cross1"], out["cross2"] = tk[:cut], tk[cut + 1:]
            elif kind == "corner" and not out["corner"]:
                out["corner"] = tk
            elif kind == "area" and not out["area"]:
                out["area"] = tk
            elif kind == "lm" and not out["lm"]:
                out["lm"] = [t for t in tk if t not in LM_FILLER] or tk

    # numero de la casa pegado al nombre: "maceo 123", "3 456"
    st = out["street"]
    if st:
        while st and st[0] in ("y", "e"):
            st = st[1:]
        digits_tail = 0
        for t in reversed(st):
            if t.isdigit():
                digits_tail += 1
            else:
                break
        words = [t for t in st if not t.isdigit() and t not in TYPE_WORDS]
        if words and digits_tail:
            if out["number"] is None:
                out["number"] = st[-digits_tail]
            st = st[:-digits_tail]
        elif not words and len([t for t in st if t.isdigit()]) >= 2:
            nums = [t for t in st if t.isdigit()]
            if out["number"] is None:
                out["number"] = nums[1]
            st = [t for t in st if t in TYPE_WORDS] + [nums[0]]
        out["street"] = st
    return out


def address_key(text, extra_abbrev=None):
    """Clave estable de una direccion (para recordar pines ya confirmados)."""
    toks = [t for t in tokenize(text, extra_abbrev) if t not in ("#",)]
    return " ".join(toks)


# ---------------------------------------------------------------- geocodificar
def _mk(lat, lng, conf, src, label, matched=None):
    return {"lat": round(lat, 6), "lng": round(lng, 6), "conf": round(conf, 2), "src": src,
            "label": label, "matched": matched or {}}


NONE = {"lat": None, "lng": None, "conf": 0.0, "src": "none", "label": "", "matched": {}}


def geocode(text, gaz, memory=None):
    """Direccion escrita -> {"lat","lng","conf","src","label","matched"}.
    memory(address_key) -> (lat,lng,src) | None: pines ya confirmados antes."""
    text = (text or "").strip()
    if not text:
        return dict(NONE)
    if memory:
        hit = memory(address_key(text, gaz.extra_abbrev))
        if hit:
            return _mk(hit[0], hit[1], 0.95, "memory", "Dirección ya confirmada antes", {"memory": hit[2]})
    if gaz.empty():
        return dict(NONE)

    p = parse_address(text, gaz.extra_abbrev)

    # --- reparto (si lo dijeron)
    area = None
    if p["area"]:
        a = gaz.find("area", p["area"], 3)
        if a:
            area = (a[0][0], gaz.areas[a[0][1]])
    if not area and p["free"]:          # ", Garrido" suelto tras una coma
        for fr in p["free"]:
            a = gaz.find("area", fr, 3)
            if a:
                area = (a[0][0], gaz.areas[a[0][1]])
                break
    area_c = area[1]["c"] if area else None
    area_r = area[1]["r"] if area else None

    def near_area_bonus(pt):
        if not area_c:
            return 0.0
        d = dist_m(pt, area_c)
        if d <= max(area_r * 1.6, 1200):
            return 0.12
        if d > 3500:
            return -0.25
        return 0.0

    street_c = gaz.find("way", p["street"], 80) if p["street"] else []
    x_c = gaz.find("way", p["cross1"], 80) if p["cross1"] else []
    y_c = gaz.find("way", p["cross2"], 80) if p["cross2"] else []
    corner_c = gaz.find("way", p["corner"], 80) if p["corner"] else []

    def disp(i):
        return gaz.ways[i]["disp"]

    area_label = f", {area[1]['disp']}" if area else ""
    matched = {"street": None, "cross": [], "area": area[1]["disp"] if area else None}

    best = None  # (score, result)
    alts = []    # todas las ofertas, para detectar direcciones ambiguas

    def offer(score, res):
        nonlocal best
        alts.append((score, res))
        if best is None or score > best[0]:
            best = (score, res)

    # --- calle + entre X y Y  -> punto medio de la cuadra
    if street_c and x_c and y_c:
        for ss, si in street_c:
            for xs, xi in x_c:
                ix = gaz.intersect(si, xi)
                if not ix:
                    continue
                for ys, yi in y_c:
                    if yi in (xi, si):
                        continue
                    iy = gaz.intersect(si, yi)
                    if not iy:
                        continue
                    gap = dist_m(ix[0], iy[0])
                    if gap < 15 or gap > 900:
                        continue
                    mid = ((ix[0][0] + iy[0][0]) / 2, (ix[0][1] + iy[0][1]) / 2)
                    sc = (ss + xs + ys) / 3 + near_area_bonus(mid) - min(gap, 900) / 6000
                    conf = 0.9 * (0.7 + 0.3 * (ss + xs + ys) / 3)
                    label = f"{disp(si)} entre {disp(xi)} y {disp(yi)}{area_label}"
                    offer(sc + 1.0, _mk(mid[0], mid[1], conf, "between", label,
                                        {**matched, "street": disp(si), "cross": [disp(xi), disp(yi)]}))

    # --- calle + una sola esquina reconocida (entre X y ?, o "esquina X")
    if best is None and street_c:
        singles = [("entre", x_c), ("entre", y_c), ("corner", corner_c)]
        for kind, cands in singles:
            for ss, si in street_c:
                for xs, xi in cands:
                    ix = gaz.intersect(si, xi)
                    if not ix:
                        continue
                    sc = (ss + xs) / 2 + near_area_bonus(ix[0])
                    conf = (0.85 if kind == "corner" else 0.7) * (0.7 + 0.3 * (ss + xs) / 2)
                    word = "esquina con" if kind == "corner" else "cerca de"
                    label = f"{disp(si)} {word} {disp(xi)}{area_label}"
                    offer(sc + 0.5, _mk(ix[0][0], ix[0][1], conf, "corner" if kind == "corner" else "between", label,
                                        {**matched, "street": disp(si), "cross": [disp(xi)]}))

    # --- dos calles que se cruzan sin decir cual es "la calle" ("esq. Maceo y Cisneros")
    if best is None and not street_c and x_c and y_c:
        for xs, xi in x_c:
            for ys, yi in y_c:
                ix = gaz.intersect(xi, yi)
                if ix:
                    offer((xs + ys) / 2, _mk(ix[0][0], ix[0][1], 0.8 * (0.7 + 0.3 * (xs + ys) / 2), "corner",
                                             f"{disp(xi)} y {disp(yi)}{area_label}", {**matched, "cross": [disp(xi), disp(yi)]}))

    # --- punto de referencia (frente al parque X...)
    if best is None and p["lm"]:
        pl = gaz.find("poi", p["lm"], 3)
        if pl:
            s, i = pl[0]
            c = gaz.pois[i]["c"]
            offer(s, _mk(c[0], c[1], 0.75 * (0.7 + 0.3 * s), "landmark", gaz.pois[i]["disp"], {**matched, "landmark": gaz.pois[i]["disp"]}))

    # --- solo la calle
    if best is None and street_c:
        pick = None
        for ss, si in street_c:
            if area_c:
                pt, d = gaz.nearest_point(si, area_c)
                sc = ss + near_area_bonus(pt) - min(d, 6000) / 20000
            else:
                pt, sc = gaz.middle_point(si), ss
            if pick is None or sc > pick[0]:
                pick = (sc, si, pt)
        sc, si, pt = pick
        conf = (0.5 if area_c else 0.4) * (0.7 + 0.3 * street_c[0][0])
        offer(sc, _mk(pt[0], pt[1], conf, "street", f"{disp(si)}{area_label}", {**matched, "street": disp(si)}))

    # --- texto suelto sin marcadores: puede ser un lugar o un reparto ("parque agramonte", "garrido")
    if best is None and not p["lm"]:
        loose = ([p["street"]] if p["street"] and not street_c else []) + p["free"]
        if not loose and not p["area"] and not p["street"]:
            loose = [p["tokens"]]
        for whole in loose:
            pl = gaz.find("poi", whole, 3)
            if pl:
                s, i = pl[0]
                c = gaz.pois[i]["c"]
                offer(s, _mk(c[0], c[1], 0.7 * (0.7 + 0.3 * s), "landmark", gaz.pois[i]["disp"], {**matched, "landmark": gaz.pois[i]["disp"]}))
                break
            if not area:
                al = gaz.find("area", whole, 3)
                if al:
                    s, i = al[0]
                    area = (s, gaz.areas[i])

    # --- solo el reparto
    if best is None and area:
        c = area[1]["c"]
        offer(area[0], _mk(c[0], c[1], 0.3 * (0.7 + 0.3 * area[0]), "area", area[1]["disp"], matched))

    if best is None:
        return dict(NONE)
    # Ambigua: dos lugares distintos (>400 m) con casi el mismo puntaje, tipico de
    # calles numeradas ("3ra e/ 12 y 14") que se repiten en varios repartos.
    top = best[1]
    for sc, r in alts:
        if r is top or r["lat"] is None:
            continue
        if sc >= best[0] - 0.04 and dist_m((top["lat"], top["lng"]), (r["lat"], r["lng"])) > 400:
            top = {**top, "conf": min(top["conf"], 0.45), "matched": {**top["matched"], "ambiguous": True}}
            break
    return top


# -------------------------------------------------------------- carga del catalogo
_default = None


def load_default(path=None, aliases_path=None):
    """Carga (una vez por instancia del Lambda) camaguey.json + aliases.json.
    Si el catalogo no existe todavia, devuelve uno vacio: geocode() responde 'none'
    y el pedido igual se guarda (el trabajador fija el pin en el panel)."""
    global _default
    if _default is not None and path is None:
        return _default
    path = path or os.path.join(HERE, "camaguey.json")
    aliases_path = aliases_path or os.path.join(HERE, "aliases.json")
    data, aliases = {}, {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    try:
        with open(aliases_path, encoding="utf-8") as f:
            aliases = json.load(f)
    except (OSError, ValueError):
        aliases = {}
    g = Gazetteer(data, aliases)
    if path == os.path.join(HERE, "camaguey.json"):
        _default = g
    return g
