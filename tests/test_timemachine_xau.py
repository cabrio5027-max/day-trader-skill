import datetime
import gzip
import io
import json
import os
import random
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load

tm = load("trader-timemachine", "timemachine")

LAST_DAY = datetime.date(2026, 10, 23)    # a Friday
HOLIDAY = "2026-09-07"                    # Labor Day: Globex closes early, a short session
GAPPY = "2026-09-15"                      # five thin overnight bars missing
NOW = "2026-10-23 10:50"
NOW_UTC = tm.parse_now(NOW)


def gold_chart(seed=7):
    """A synthetic Yahoo v8 chart for GC=F, 5-minute bars on the CME Globex day:
    6:00 p.m. New York the evening before to 5:00 p.m., Sunday evening to Friday."""
    rng = random.Random(seed)
    stamps, o, h, l, c, v = [], [], [], [], [], []
    price = 4100.0
    day = LAST_DAY - datetime.timedelta(days=62)
    while day <= LAST_DAY:
        if day.weekday() < 5:
            start = datetime.datetime.combine(day - datetime.timedelta(days=1), datetime.time(18, 0))
            nbars = 276
            if day.isoformat() == HOLIDAY:
                nbars = 228       # closes at 1:00 p.m.
            for i in range(nbars):
                if day.isoformat() == GAPPY and 40 <= i < 45:
                    continue
                local = start + datetime.timedelta(minutes=5 * i)
                ts = int((tm.eastern_to_utc(local) - tm.EPOCH).total_seconds())
                op = price
                cl = op * (1 + rng.gauss(0, 0.0009))
                hi = max(op, cl) * (1 + abs(rng.gauss(0, 0.0004)))
                lo = min(op, cl) * (1 - abs(rng.gauss(0, 0.0004)))
                stamps.append(ts)
                o.append(round(op, 1)); h.append(round(hi, 1)); l.append(round(lo, 1)); c.append(round(cl, 1))
                v.append(rng.randint(50, 900))
                price = cl
            # the 5:00 p.m. maintenance-hour print some feeds carry: outside the session
            if day.isoformat() not in (HOLIDAY,):
                local = start + datetime.timedelta(minutes=5 * 276)
                stamps.append(int((tm.eastern_to_utc(local) - tm.EPOCH).total_seconds()))
                o.append(price); h.append(price); l.append(price); c.append(price); v.append(0)
        day += datetime.timedelta(days=1)
    return json.dumps({"chart": {"result": [{
        "meta": {"symbol": "GC=F", "dataGranularity": "5m", "regularMarketTime": stamps[-1]},
        "timestamp": stamps,
        "indicators": {"quote": [{"open": o, "high": h, "low": l, "close": c, "volume": v}]},
    }], "error": None}})


TEXT = gold_chart()


def plan(**kw):
    raw = {"symbol": "XAUUSD", "side": "buy", "date": LAST_DAY.isoformat(), "entry": 4110.0,
           "stop": 4100.0, "target": 4130.0, "entry_time": "10:15 AM", "setup": "orb",
           "opening_range": {"minutes": 15}, "stop_rule": "pct", "interval": "5m"}
    raw.update(kw)
    return tm.normalize_plan(raw)


class TestMarket(unittest.TestCase):
    def test_market_by_symbol(self):
        self.assertEqual(tm.market_for("XAUUSD")["name"], "xau")
        self.assertEqual(tm.market_for("xauusdf")["name"], "xau")
        self.assertEqual(tm.market_for("GC=F")["name"], "xau")
        self.assertEqual(tm.market_for("TSLA")["name"], "us-stock")
        self.assertEqual(tm.market_for("GOLD")["name"], "us-stock")     # Barrick, a stock
        self.assertEqual(tm.market_for("TSLA", "xau")["name"], "xau")
        with self.assertRaises(tm.TMError):
            tm.market_for("XAUUSD", "moon")

    def test_trading_date_rolls_after_six(self):
        x = tm.MARKETS["xau"]
        self.assertEqual(tm.trading_date(datetime.datetime(2026, 10, 18, 18, 0), x).isoformat(), "2026-10-19")
        self.assertEqual(tm.trading_date(datetime.datetime(2026, 10, 19, 16, 55), x).isoformat(), "2026-10-19")
        self.assertEqual(tm.trading_date(datetime.datetime(2026, 10, 18, 18, 0), tm.MARKETS["us-stock"]).isoformat(),
                         "2026-10-18")


class TestPlan(unittest.TestCase):
    def test_xauusd_plan(self):
        p = plan()
        self.assertEqual((p["market"], p["data_symbol"]), ("xau", "GC=F"))
        self.assertEqual(p["entry_bar"], 194)                 # 10:15 AM is 16h15m after 6:00 PM
        self.assertEqual(p["entry_time"], "10:15 AM")
        self.assertEqual(p["opening_range"]["start"], "8:20 AM")
        self.assertEqual(p["opening_range"]["start_bar"], 172)

    def test_overnight_entry(self):
        self.assertEqual(plan(entry_time="8:00 PM")["entry_bar"], 23)

    def test_entry_outside_the_day(self):
        for t in ("6:00 PM", "5:00 PM", "5:30 PM"):
            with self.assertRaises(tm.TMError, msg=t) as cm:
                plan(entry_time=t)
            self.assertIn("6:05 PM and 4:55 PM", str(cm.exception))

    def test_data_symbol_and_range_start(self):
        self.assertEqual(plan(data_symbol="MGC=F")["data_symbol"], "MGC=F")
        self.assertEqual(plan(symbol="GC=F")["data_symbol"], "GC=F")
        p = plan(opening_range={"minutes": 30, "start": "3:00 AM"})
        self.assertEqual((p["opening_range"]["start"], p["opening_range"]["start_bar"]), ("3:00 AM", 108))

    def test_stock_plans_keep_their_shape(self):
        p = tm.normalize_plan({"symbol": "TSLA", "date": "2026-10-19", "entry": 364.18, "stop": 358.32,
                               "target": 370.04, "entry_time": "10:15 AM"})
        for key in ("market", "data_symbol"):
            self.assertNotIn(key, p)
        self.assertNotIn("start_bar", p["opening_range"])


class TestBars(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.x = tm.MARKETS["xau"]
        cls.meta, cls.bars, cls.dropped = tm.parse_bars(tm.chart_result(TEXT, "GC=F"), NOW_UTC, cls.x)
        cls.days = tm.split_sessions(cls.bars)

    def test_sunday_evening_opens_monday(self):
        mon = self.days["2026-10-19"]
        self.assertEqual((mon[0]["start"], len(mon)), ("18:00", 276))
        self.assertEqual(mon[275]["start"], "16:55")
        self.assertNotIn("2026-10-18", self.days)

    def test_maintenance_hour_dropped(self):
        self.assertGreater(self.dropped["outside_hours"], 30)

    def test_gaps_filled_and_holiday_left_out(self):
        full, short = tm.full_sessions(self.days, LAST_DAY.isoformat(), self.x)
        dates = [d for d, _ in full]
        self.assertIn(GAPPY, dates)
        self.assertNotIn(HOLIDAY, dates)
        self.assertEqual([s["date"] for s in short], [HOLIDAY])
        D = dict(full)[GAPPY]
        self.assertEqual(len(D), 276)
        self.assertEqual(sum(1 for b in D if b.get("filled")), 5)
        self.assertEqual(D[40]["c"], D[39]["c"])
        # a stock market never fills: the same gappy session is short there
        self.assertEqual(tm.full_sessions({GAPPY: dict(self.days[GAPPY])}, "2099-01-01")[0], [])


class TestRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = tm.time_machine(TEXT, plan(), NOW_UTC, replays=3000)

    def test_odds(self):
        m = self.doc["mc"]
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=3)
        self.assertEqual(m["bars_left"], 275 - 194)
        self.assertEqual(m["until"], "17:00")
        self.assertEqual((m["band_times"][0], m["band_times"][-1]), ("10:15", "17:00"))
        self.assertGreaterEqual(m["pool_sessions"], 20)

    def test_same_seed_same_numbers(self):
        again = tm.time_machine(TEXT, plan(), NOW_UTC, replays=3000)
        self.assertEqual(again["mc"]["p_target"], self.doc["mc"]["p_target"])
        self.assertEqual(again["history"]["trades"], self.doc["history"]["trades"])

    def test_history_uses_the_comex_open(self):
        h = self.doc["history"]
        self.assertEqual(h["rule"]["or_start"], "8:20 AM")
        self.assertGreater(h["triggered"], 0)
        for t in h["trades"]:
            hh, rest = t["time"].split(":")
            mins = (int(hh) % 12 + (12 if rest.endswith("PM") else 0)) * 60 + int(rest[:2])
            self.assertTrue(8 * 60 + 40 <= mins <= 17 * 60, t)

    def test_report(self):
        text = "\n".join(tm.report(self.doc))
        self.assertIn("risk 10.00 an oz", text)
        self.assertIn("from GC=F's own 5-min bars", text)
        self.assertIn("15-min opening range from 8:20 AM", text)
        self.assertIn("Session: gold, CME Globex day 6:00 PM to 5:00 PM New York (GC=F data).", text)
        self.assertIn("stand-in for XAUUSD", text)
        self.assertIn("Fed decisions", text)
        self.assertIn("Filled 5 missing bar(s)", text)
        self.assertEqual(self.doc["data"]["short_sessions_left_out"], [{"date": HOLIDAY, "bars": 228}])
        self.assertTrue(tm.render_chart(self.doc)[-2].strip().startswith("10:15"))

    def test_until(self):
        doc = tm.time_machine(TEXT, plan(), NOW_UTC, replays=200, until="12:00")
        self.assertEqual(doc["mc"]["bars_left"], (12 * 60 - 18 * 60) % 1440 // 5 - 1 - 194)
        for bad in ("17:05", "18:00", "10:15"):
            with self.assertRaises(tm.TMError, msg=bad):
                tm.time_machine(TEXT, plan(), NOW_UTC, replays=200, until=bad)

    def test_wrong_data(self):
        with self.assertRaises(tm.TMError) as cm:
            tm.time_machine(TEXT, plan(data_symbol="SI=F"), NOW_UTC, replays=10)
        self.assertIn("the data is for GC=F", str(cm.exception))


class TestCli(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.file = os.path.join(self.t, "GC=F-60d5m.json.gz")
        with gzip.open(self.file, "wt", encoding="utf-8") as fh:
            fh.write(TEXT)

    def tearDown(self):
        shutil.rmtree(self.t)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = tm.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_run_the_same_way_as_a_stock(self):
        res = os.path.join(self.t, "tm.json")
        code, out, err = self.run_cli("run", "--symbol", "XAUUSD", "--date", LAST_DAY.isoformat(),
                                      "--entry", "4110", "--stop", "4100", "--target", "4130",
                                      "--entry-time", "10:15 AM", "--offline", self.t, "--now", NOW,
                                      "--replays", "500", "--write", res)
        self.assertEqual(code, 0, err)
        self.assertIn("XAUUSD buy, Friday 2026-10-23", out)
        with open(res, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual((doc["data"]["symbol"], doc["data"]["market"], doc["plan"]["market"]), ("GC=F", "xau", "xau"))
        code, out, err = self.run_cli("chart", "--from", res)
        self.assertEqual(code, 0, err)
        self.assertIn("XAUUSD 2026-10-23", out)

    def test_plan_file_and_range_start_flag(self):
        pf = os.path.join(self.t, "plan.json")
        with open(pf, "w", encoding="utf-8") as fh:
            json.dump({"symbol": "XAUUSD", "side": "buy", "date": LAST_DAY.isoformat(), "entry": 4110,
                       "stop": 4100, "target": 4130, "entry_time": "10:15 AM", "setup": "orb",
                       "opening_range": {"minutes": 15}, "interval": "5m"}, fh)
        code, out, err = self.run_cli("run", "--plan", pf, "--offline", self.file, "--now", NOW,
                                      "--replays", "300", "--range-start", "9:30 AM", "--json")
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertEqual(doc["plan"]["opening_range"]["start"], "9:30 AM")
        self.assertEqual(doc["history"]["rule"]["or_start"], "9:30 AM")


if __name__ == "__main__":
    unittest.main()
