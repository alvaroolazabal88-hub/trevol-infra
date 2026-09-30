import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lambda", "order_handler"))

import logistics as lg  # noqa: E402


class Havana(unittest.TestCase):
    def test_manual_rule_matches_tz_database(self):
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo("America/Havana")
        except Exception:
            self.skipTest("sin base de zonas horarias")
        d = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2027, 12, 31, tzinfo=timezone.utc)
        bad = []
        while d < end:
            manual = d.astimezone(timezone(lg._havana_offset_manual(d)))
            real = d.astimezone(ZoneInfo("America/Havana"))
            if manual.replace(tzinfo=None) != real.replace(tzinfo=None):
                bad.append(d)
            d += timedelta(hours=1)
        self.assertEqual(bad, [], bad[:3])

    def test_known_dates(self):
        self.assertEqual(lg.havana(datetime(2026, 9, 29, 13, 0, tzinfo=timezone.utc)).strftime("%H:%M"), "09:00")
        self.assertEqual(lg.havana(datetime(2026, 12, 1, 13, 0, tzinfo=timezone.utc)).strftime("%H:%M"), "08:00")
        # cerca de medianoche: el dia local es distinto del UTC
        self.assertEqual(lg.day_str(lg.havana(datetime(2026, 9, 30, 2, 0, tzinfo=timezone.utc))), "2026-09-29")


class Phones(unittest.TestCase):
    def test_normalize(self):
        cases = {
            "5 123 4567": "+5351234567", "51234567": "+5351234567", "+53 5123-4567": "+5351234567",
            "(53) 51234567": "+5351234567", "0053 51234567": "+5351234567", "5351234567": "+5351234567",
            "32 291234": "+5332291234", "+1 (407) 555-0100": "+14075550100", "": "", "abc": "", "123": "", "+12": "",
            "1" * 20: "",
        }
        for raw, want in cases.items():
            self.assertEqual(lg.normalize_phone(raw), want, raw)


class Slots(unittest.TestCase):
    def setUp(self):
        self.slots = lg.load_slots("")
        self.now = lg.havana(datetime(2026, 9, 29, 13, 0, tzinfo=timezone.utc))  # 09:00

    def test_bad_config_falls_back(self):
        for raw in ("{malo", "[]", '[{"id":"x","start":"9","end":"10"}]', '[{"id":"a","start":"10:00","end":"09:00"}]'):
            self.assertEqual([s["id"] for s in lg.load_slots(raw)], ["desayuno", "merienda"], raw)
        custom = lg.load_slots('[{"id":"Cena!","label":"Cena","start":"18:00","end":"20:00"}]')
        self.assertEqual(custom[0]["id"], "cena")

    def test_check(self):
        ok = lambda s, d=None, **k: lg.check_slot(self.slots, s, d, self.now, **k)
        self.assertEqual(ok(None), ("2026-09-29", "asap", None))
        self.assertEqual(ok("merienda")[2], None)
        self.assertIsNotNone(ok("desayuno")[2])                      # ya cerro hoy
        self.assertIsNone(ok("desayuno", "2026-09-30")[2])
        self.assertIsNone(ok("desayuno", enforce_lead=False)[2])     # el panel puede reprogramar sin margen
        self.assertIsNotNone(ok("asap", "2026-09-30")[2])
        self.assertIsNotNone(ok("merienda", "2026-10-01")[2])
        self.assertIsNotNone(ok("nope")[2])

    def test_lead_boundary(self):
        eight_thirty = self.now.replace(hour=8, minute=30)
        self.assertIsNone(lg.check_slot(self.slots, "desayuno", None, eight_thirty)[2])   # 08:30 + 30 = 09:00 justo
        eight_thirty_one = self.now.replace(hour=8, minute=31)
        self.assertIsNotNone(lg.check_slot(self.slots, "desayuno", None, eight_thirty_one)[2])


class Eta(unittest.TestCase):
    def stops(self):
        return {"anchor_ts": 1000, "dwell": 120, "stops": [
            {"o": "a", "lat": 21.3808, "lng": -77.9100, "sec": 600, "m": 2000, "done": False},
            {"o": "b", "lat": 21.3808, "lng": -77.9020, "sec": 300, "m": 1000, "done": False},
        ]}

    def test_plan_mode(self):
        r = lg.trip_etas(self.stops(), None, 1100)
        self.assertEqual((r["a"]["eta"], r["a"]["mode"], r["a"]["before"]), (1600, "plan", 0))
        self.assertEqual(r["b"]["eta"], 1600 + 120 + 300)

    def test_plan_never_in_the_past(self):
        r = lg.trip_etas(self.stops(), None, 5000)
        self.assertEqual(r["a"]["eta"], 5060)

    def test_gps_mode_scales_remaining_time(self):
        trip = self.stops()
        # a ~1.1 km en linea recta de "a" -> ~1.5 km de calle vs 2000 m planeados
        rider = {"lat": 21.3808, "lng": -77.9200, "ts": 1100}
        r = lg.trip_etas(trip, rider, 1110)
        self.assertEqual(r["a"]["mode"], "gps")
        d_route = lg.hav_km(21.3808, -77.9200, 21.3808, -77.9100) * 1000 * lg.DETOUR
        self.assertAlmostEqual(r["a"]["eta"], 1110 + 600 * d_route / 2000, delta=1)
        self.assertLess(r["a"]["eta"], 1110 + 600)

    def test_gps_far_behind_plan_is_capped_at_planned_leg(self):
        rider = {"lat": 21.30, "lng": -77.80, "ts": 1100}    # muy lejos
        r = lg.trip_etas(self.stops(), rider, 1110)
        self.assertEqual(r["a"]["eta"], 1110 + 600)

    def test_stale_gps_ignored(self):
        rider = {"lat": 21.3808, "lng": -77.9100, "ts": 1000}
        r = lg.trip_etas(self.stops(), rider, 1000 + lg.FRESH_GPS_SEC + 1)
        self.assertEqual(r["a"]["mode"], "plan")

    def test_done_stops_and_empty(self):
        t = self.stops()
        t["stops"][0]["done"] = True
        self.assertEqual(list(lg.trip_etas(t, None, 1100)), ["b"])
        t["stops"][1]["done"] = True
        self.assertEqual(lg.trip_etas(t, None, 1100), {})
        self.assertEqual(lg.trip_etas(None, None, 1), {})

    def test_gps_without_leg_data_uses_distance(self):
        t = {"anchor_ts": 1000, "stops": [{"o": "a", "lat": 21.3808, "lng": -77.9100}]}
        r = lg.trip_etas(t, {"lat": 21.3808, "lng": -77.9200, "ts": 1100}, 1100)
        self.assertEqual(r["a"]["mode"], "gps")
        self.assertGreater(r["a"]["eta"], 1100)

    def test_latlng_validation(self):
        self.assertEqual(lg.valid_latlng("21.38", "-77.9"), (21.38, -77.9))
        for bad in ((0, 0), (None, 1), ("x", "y"), (40, -77), (21, float("nan")), (21.4, 50)):
            self.assertIsNone(lg.valid_latlng(*bad), bad)

    def test_prekick(self):
        eta = lg.prekick_eta(1000, (21.3808, -77.9169), (21.3808, -77.9100), 1000)
        self.assertGreater(eta, 1000 + lg.PREP_MIN * 60)
        self.assertEqual(lg.prekick_eta(1000, None, None, 1000), 1000 + lg.PREP_MIN * 60 + 900)


if __name__ == "__main__":
    unittest.main()
