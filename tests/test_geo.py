import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lambda", "order_handler"))
sys.path.insert(0, HERE)

import geo  # noqa: E402
from geo_fixture import DATA  # noqa: E402

GAZ = geo.Gazetteer(DATA, {"streets": {"los martires": "Avenida de los Mártires"}})


def near(res, lat, lng, m=60):
    return res["lat"] is not None and geo.dist_m((res["lat"], res["lng"]), (lat, lng)) <= m


class Tokenize(unittest.TestCase):
    def test_cuban_grammar(self):
        t = geo.tokenize("Ave. de los Mártires No. 45 e/ Cisneros y Avellaneda, Rpto Garrido")
        self.assertEqual(t, ["avenida", "de", "los", "martires", "#", "45", "entre", "cisneros", "y",
                             "avellaneda", "reparto", "garrido"])

    def test_ordinals_and_esq(self):
        self.assertEqual(geo.tokenize("3ra esq. 12"), ["3", "esquina", "12"])
        self.assertEqual(geo.tokenize("Tercera e/ Primera y 2da"), ["3", "entre", "1", "y", "2"])

    def test_name_key(self):
        self.assertEqual(geo.name_key(geo.tokenize("Avenida de los Mártires")), ["martires"])
        self.assertEqual(geo.name_key(geo.tokenize("Calle 3ra")), ["3"])
        self.assertEqual(geo.name_key(geo.tokenize("Calle A")), ["a"])
        self.assertEqual(geo.name_key(geo.tokenize("Carlos J. Finlay")), ["carlos", "finlay"])

    def test_phonetic(self):
        self.assertEqual(geo.phon("sisneros"), geo.phon("cisneros"))
        self.assertEqual(geo.phon("abellaneda"), geo.phon("avellaneda"))


class Parse(unittest.TestCase):
    def test_full(self):
        p = geo.parse_address("Maceo #45 e/ Cisneros y Avellaneda, Rpto Garrido")
        self.assertEqual(p["street"], ["maceo"])
        self.assertEqual(p["number"], "45")
        self.assertEqual(p["cross1"], ["cisneros"])
        self.assertEqual(p["cross2"], ["avellaneda"])
        self.assertEqual(p["area"], ["garrido"])

    def test_number_glued(self):
        p = geo.parse_address("Maceo 123 entre Cisneros y Avellaneda")
        self.assertEqual((p["street"], p["number"]), (["maceo"], "123"))

    def test_numbered_street(self):
        p = geo.parse_address("3ra 456 e/ 12 y 14 reparto Vista Hermosa")
        self.assertEqual(p["street"], ["3"])
        self.assertEqual(p["number"], "456")
        self.assertEqual((p["cross1"], p["cross2"]), (["12"], ["14"]))
        self.assertEqual(p["area"], ["vista", "hermosa"])

    def test_area_first_and_free_segment(self):
        p = geo.parse_address("Rpto Garrido, 3ra e/ 12 y 14")
        self.assertEqual(p["area"], ["garrido"])
        self.assertEqual(p["street"], ["3"])
        p = geo.parse_address("Maceo 45, Garrido")
        self.assertEqual(p["free"], [["garrido"]])

    def test_landmark(self):
        p = geo.parse_address("Frente al parque Agramonte")
        self.assertEqual(p["lm"], ["parque", "agramonte"])


class Geocode(unittest.TestCase):
    def test_between(self):
        r = geo.geocode("Maceo #45 e/ Cisneros y Avellaneda", GAZ)
        self.assertEqual(r["src"], "between")
        self.assertTrue(near(r, 21.3810, -77.9165), r)
        self.assertGreaterEqual(r["conf"], 0.8)
        self.assertIn("Maceo", r["label"])

    def test_misspelled(self):
        r = geo.geocode("Macéo # 45 e/ Sisneros y Abellaneda", GAZ)
        self.assertEqual(r["src"], "between")
        self.assertTrue(near(r, 21.3810, -77.9165), r)

    def test_honorific_dropped(self):
        # "Martí" para "José Martí", "Finlay" para "Carlos J. Finlay"
        r = geo.geocode("Calle República esq. a Martí", GAZ)
        self.assertEqual(r["src"], "corner")
        self.assertTrue(near(r, 21.3830, -77.9150), r)
        r = geo.geocode("Finlay esq Cisneros", GAZ)
        self.assertEqual(r["src"], "corner")
        self.assertTrue(near(r, 21.3700, -77.9170), r)

    def test_cross_order_reversed(self):
        a = geo.geocode("Maceo e/ Cisneros y Avellaneda", GAZ)
        b = geo.geocode("Maceo e/ Avellaneda y Cisneros", GAZ)
        self.assertTrue(near(a, b["lat"], b["lng"], 5))

    def test_numbered_street_disambiguated_by_reparto(self):
        g = geo.geocode("3ra #456 e/ 12 y 14 Rpto Garrido", GAZ)
        self.assertEqual(g["src"], "between")
        self.assertTrue(near(g, 21.3950, -77.9010), g)
        v = geo.geocode("3ra #456 e/ 12 y 14 Rpto Vista Hermosa", GAZ)
        self.assertTrue(near(v, 21.3600, -77.9510), v)

    def test_numbered_street_without_reparto_is_flagged_ambiguous(self):
        r = geo.geocode("3ra #456 e/ 12 y 14", GAZ)
        self.assertEqual(r["src"], "between")
        self.assertTrue(r["matched"].get("ambiguous"))
        self.assertLessEqual(r["conf"], 0.45)

    def test_comma_order(self):
        r = geo.geocode("Rpto Vista Hermosa, 3ra e/ 12 y 14", GAZ)
        self.assertTrue(near(r, 21.3600, -77.9510), r)
        r = geo.geocode("3ra e/ 12 y 14, Garrido", GAZ)
        self.assertTrue(near(r, 21.3950, -77.9010), r)

    def test_landmark(self):
        r = geo.geocode("frente al parque Agramonte", GAZ)
        self.assertEqual(r["src"], "landmark")
        self.assertTrue(near(r, 21.3815, -77.9165, 10), r)
        r = geo.geocode("Parque Agramonte", GAZ)
        self.assertEqual(r["src"], "landmark")

    def test_street_only(self):
        r = geo.geocode("Ave. de los Mártires 12", GAZ)
        self.assertEqual(r["src"], "street")
        self.assertLess(r["conf"], 0.5)

    def test_street_only_uses_reparto_to_pick_cluster(self):
        r = geo.geocode("Calle A, Rpto Vista Hermosa", GAZ)
        self.assertEqual(r["src"], "street")
        self.assertTrue(near(r, 21.3610, -77.9510, 300), r)

    def test_only_reparto(self):
        r = geo.geocode("Edificio 5 apto 3, Reparto Garrido", GAZ)
        self.assertEqual(r["src"], "area")
        self.assertTrue(near(r, 21.3950, -77.9010, 5))
        self.assertLessEqual(r["conf"], 0.35)

    def test_reparto_alone(self):
        r = geo.geocode("Garrido", GAZ)
        self.assertEqual(r["src"], "area")

    def test_street_alias(self):
        r = geo.geocode("los martires 12", GAZ)
        self.assertEqual(r["src"], "street")

    def test_unknown_and_empty(self):
        for txt in ("", "   ", "asdf qwer zxcv", "Edificio 15 apto 3 Rpto Lenin"):
            r = geo.geocode(txt, GAZ)
            self.assertEqual(r["src"], "none", txt)
            self.assertIsNone(r["lat"])

    def test_false_friend_not_matched(self):
        # "Maceo" no debe confundirse con "Mayor" ni con calles de otro nombre
        r = geo.geocode("Calle Mayor 5 e/ Zulueta y Lopez", GAZ)
        self.assertEqual(r["src"], "none")

    def test_memory_wins(self):
        key = geo.address_key("Maceo #45 e/ Cisneros y Avellaneda")
        seen = []

        def mem(k):
            seen.append(k)
            return (21.39, -77.90, "worker") if k == key else None

        r = geo.geocode("maceo # 45 E/ cisneros y avellaneda", GAZ, memory=mem)
        self.assertEqual(r["src"], "memory")
        self.assertEqual((r["lat"], r["lng"]), (21.39, -77.9))
        self.assertEqual(seen, [key])

    def test_empty_gazetteer_degrades(self):
        r = geo.geocode("Maceo e/ A y B", geo.Gazetteer({}))
        self.assertEqual(r["src"], "none")

    def test_address_key_ignores_case_accents_punctuation(self):
        a = geo.address_key("Ave. de los Mártires #45, Rpto. Garrido")
        b = geo.address_key("avenida de los martires # 45 reparto garrido")
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
