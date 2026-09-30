import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lambda", "order_handler"))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))

import build_gazetteer as bg  # noqa: E402
import geo  # noqa: E402


def way(name, pts, **tags):
    return {"type": "way", "tags": {"name": name, "highway": "residential", **tags},
            "geometry": [{"lat": a, "lon": b} for a, b in pts]}


STREETS = {"elements": [
    # Maceo en dos tramos que se tocan + alias en old_name
    way("Antonio Maceo", [(21.3810, -77.9250), (21.3810, -77.9150)], old_name="Calle Maceo; San Luis"),
    way("Antonio Maceo", [(21.3810, -77.9150), (21.3810, -77.9050)]),
    way("Cisneros", [(21.3750, -77.9170), (21.3900, -77.9170)]),
    way("Avellaneda", [(21.3750, -77.9160), (21.3900, -77.9160)]),
    # dos "Calle 3ra" lejanas -> dos tramos distintos
    way("Calle 3ra", [(21.3950, -77.9040), (21.3950, -77.8980)]),
    way("Calle 3ra", [(21.3600, -77.9540), (21.3600, -77.9480)]),
    {"type": "node", "id": 1},  # ruido
]}
PLACES = {"elements": [
    {"type": "node", "lat": 21.3950, "lon": -77.9010, "tags": {"name": "Garrido", "place": "suburb"}},
    {"type": "way", "center": {"lat": 21.36, "lon": -77.951}, "bounds": {"minlat": 21.355, "minlon": -77.956, "maxlat": 21.365, "maxlon": -77.946},
     "tags": {"name": "Vista Hermosa", "landuse": "residential"}},
    {"type": "node", "lat": 21.3815, "lon": -77.9165, "tags": {"name": "Parque Ignacio Agramonte", "leisure": "park"}},
    {"type": "node", "lat": 21.3815, "lon": -77.9165, "tags": {"name": "Parque Ignacio Agramonte", "leisure": "park"}},  # duplicado
    {"type": "node", "lat": 21.38, "lon": -77.91, "tags": {"name": "ab", "shop": "yes"}},  # nombre muy corto
]}


class Build(unittest.TestCase):
    def setUp(self):
        self.data = bg.build(STREETS, PLACES, (21.32, -77.99, 21.44, -77.85))

    def test_clusters_touching_ways_and_splits_distant_same_name(self):
        by = {}
        for w in self.data["ways"]:
            by.setdefault(w["n"], []).append(w)
        self.assertEqual(len(by["Antonio Maceo"]), 1)
        self.assertEqual(len(by["Calle 3ra"]), 2)

    def test_alt_names_split_on_semicolon(self):
        maceo = [w for w in self.data["ways"] if w["n"] == "Antonio Maceo"][0]
        self.assertEqual(maceo["a"], ["Calle Maceo", "San Luis"])

    def test_densified(self):
        cis = [w for w in self.data["ways"] if w["n"] == "Cisneros"][0]
        pts = geo.Gazetteer({"ways": [cis]}).pts(0)
        gaps = [geo.dist_m(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
        self.assertLessEqual(max(gaps), 31)

    def test_places(self):
        self.assertEqual({a["n"] for a in self.data["areas"]}, {"Garrido", "Vista Hermosa"})
        vh = [a for a in self.data["areas"] if a["n"] == "Vista Hermosa"][0]
        self.assertGreater(vh["r"], 300)
        self.assertEqual([p["n"] for p in self.data["pois"]], ["Parque Ignacio Agramonte"])

    def test_output_is_json_serializable_and_geocodes(self):
        data = json.loads(json.dumps(self.data, ensure_ascii=False))
        gaz = geo.Gazetteer(data)
        r = geo.geocode("Maceo e/ Cisneros y Avellaneda", gaz)
        self.assertEqual(r["src"], "between")
        self.assertLess(geo.dist_m((r["lat"], r["lng"]), (21.3810, -77.9165)), 40)
        # nombre viejo del catalogo (old_name) tambien funciona
        r = geo.geocode("San Luis esq Cisneros", gaz)
        self.assertEqual(r["src"], "corner")
        # reparto desambigua la 3ra
        r = geo.geocode("3ra, Reparto Vista Hermosa", gaz)
        self.assertLess(geo.dist_m((r["lat"], r["lng"]), (21.3600, -77.9510)), 400)


if __name__ == "__main__":
    unittest.main()
