"""Ciudad sintetica para probar el geocodificador (coordenadas inventadas, NO reales)."""
import math

STEP = 0.0003  # ~33 m entre muestras


def line(lat1, lng1, lat2, lng2):
    n = max(1, int(max(abs(lat2 - lat1), abs(lng2 - lng1)) / STEP))
    pts = []
    for i in range(n + 1):
        t = i / n
        pts += [round((lat1 + (lat2 - lat1) * t) * 1e5), round((lng1 + (lng2 - lng1) * t) * 1e5)]
    return pts


def way(name, lat1, lng1, lat2, lng2, alts=()):
    return {"n": name, "a": list(alts), "p": line(lat1, lng1, lat2, lng2)}


DATA = {
    "v": 1,
    "center": [21.3808, -77.9169],
    "ways": [
        way("Antonio Maceo", 21.3810, -77.9250, 21.3810, -77.9050),
        way("Independencia", 21.3820, -77.9250, 21.3820, -77.9050),
        way("República", 21.3830, -77.9250, 21.3830, -77.9050),
        way("Cisneros", 21.3690, -77.9170, 21.3900, -77.9170),
        way("Avellaneda", 21.3690, -77.9160, 21.3900, -77.9160),
        way("José Martí", 21.3690, -77.9150, 21.3900, -77.9150, alts=["Calle Martí"]),
        way("Avenida de los Mártires", 21.3700, -77.9050, 21.3950, -77.9050),
        way("Carlos J. Finlay", 21.3700, -77.9300, 21.3700, -77.9100),
        # Garrido
        way("Calle 3ra", 21.3950, -77.9040, 21.3950, -77.8980),
        way("Calle 12", 21.3920, -77.9020, 21.3980, -77.9020),
        way("Calle 14", 21.3920, -77.9000, 21.3980, -77.9000),
        # Vista Hermosa (mismos nombres numerados, otro lugar)
        way("Calle 3ra", 21.3600, -77.9540, 21.3600, -77.9480),
        way("Calle 12", 21.3570, -77.9520, 21.3630, -77.9520),
        way("Calle 14", 21.3570, -77.9500, 21.3630, -77.9500),
        way("Calle A", 21.3610, -77.9540, 21.3610, -77.9480),
        way("Calle B", 21.3620, -77.9540, 21.3620, -77.9480),
    ],
    "areas": [
        {"n": "Reparto Garrido", "c": [21.3950, -77.9010], "r": 700},
        {"n": "Reparto Vista Hermosa", "c": [21.3600, -77.9510], "r": 700},
    ],
    "pois": [
        {"n": "Parque Ignacio Agramonte", "c": [21.3815, -77.9165]},
        {"n": "Hospital Provincial Manuel Ascunce Domenech", "c": [21.3700, -77.9200]},
    ],
}
