"""UK support, pipeline exports and the answer bank (added in 1.1.0)."""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, SKILLS, load

offer = load("hunter-salary", "offer")
brief = load("hunter-research", "brief")
ats = load("hunter-resume", "ats")
ans = load("hunter-apply", "answers")
pipeline = load("hunter-tracker", "pipeline")

UK_PROFILE = os.path.join(SKILLS, "hunter-apply", "profile.uk.example.json")
PIPE = os.path.join(SKILLS, "hunter-tracker", "pipeline.example.json")
RESEARCH = os.path.join(FIX, "hunter-research")


def call(fn, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fn(argv)
    return code, out.getvalue(), err.getvalue()


def read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class UKTakeHome(unittest.TestCase):
    def setUp(self):
        self.rates = offer.uk_rates("2026-27")

    def test_rates_carry_their_source(self):
        self.assertIn("gov.uk", self.rates["url"])
        self.assertEqual(self.rates["personal_allowance"], 12570)

    def test_basic_rate_with_plan2_and_salary_sacrifice(self):
        r = offer.uk_take_home(45000, self.rates, loans=["plan2"], pension_pct=5, pension="sacrifice")
        self.assertEqual(r["income_tax"], 6036.0)          # (42,750 - 12,570) x 20%
        self.assertEqual(r["ni"], 2414.4)                  # (42,750 - 12,570) x 8%
        self.assertEqual(r["student_loans"][0]["repay"], 1202.85)  # (42,750 - 29,385) x 9%
        self.assertEqual(r["take_home"], 33096.75)

    def test_allowance_taper_and_additional_rate(self):
        r = offer.uk_take_home(130000, self.rates)
        self.assertEqual(r["personal_allowance"], 0.0)
        self.assertEqual(r["income_tax"], 44703.0)
        self.assertEqual(r["ni"], 4610.6)
        half = offer.uk_take_home(110000, self.rates)
        self.assertEqual(half["personal_allowance"], 7570.0)

    def test_scottish_bands(self):
        r = offer.uk_take_home(45000, self.rates, scotland=True)
        self.assertEqual(r["income_tax"], 6882.05)
        self.assertEqual(r["region"], "Scotland")

    def test_relief_at_source_costs_eighty_percent(self):
        r = offer.uk_take_home(30000, self.rates, pension_pct=5, pension="relief-at-source")
        plain = offer.uk_take_home(30000, self.rates)
        self.assertAlmostEqual(plain["take_home"] - r["take_home"], 1200.0, places=2)

    def test_errors(self):
        with self.assertRaises(offer.OfferError):
            offer.uk_take_home(40000, self.rates, loans=["plan9"])
        with self.assertRaises(offer.OfferError):
            offer.uk_take_home(40000, self.rates, loans=["plan1", "plan2"])
        with self.assertRaises(offer.OfferError):
            offer.uk_rates("1999-00")

    def test_cli_prints_pounds_and_the_source(self):
        code, out, _ = call(offer.main, ["uk", "--gross", "45000", "--student-loan", "plan2", "--profile", ""])
        self.assertEqual(code, 0)
        self.assertIn("£34,514", out)
        self.assertIn("gov.uk/guidance/rates-and-thresholds-for-employers-2026-to-2027", out)
        self.assertEqual(offer.money(1500, "EUR"), "€1,500")

    def test_oews_year_follows_the_calendar(self):
        self.assertEqual(offer.default_oews_year(datetime.date(2026, 11, 3)), 2025)
        self.assertEqual(offer.default_oews_year(datetime.date(2027, 3, 1)), 2025)
        self.assertEqual(offer.default_oews_year(datetime.date(2027, 5, 1)), 2026)


class CompaniesHouse(unittest.TestCase):
    def test_lookup_picks_the_active_exact_match(self):
        f = brief.Fetcher(offline=RESEARCH)
        ch = brief.fetch_ch(f, company="Example Bank", key="k", today=datetime.date(2026, 11, 3))
        self.assertEqual(ch["number"], "09999999")
        self.assertEqual([d["name"] for d in ch["directors"]], ["EXAMPLE, Alex", "SAMPLE, Robin"])
        self.assertEqual(ch["accounts_filing"]["date"], "2026-07-20")
        self.assertEqual(len(ch["filings"]), 2)  # the 2022 filing is outside the year

    def test_brief_uses_companies_house_when_there_is_no_sec(self):
        code, out, _ = call(brief.main, ["build", "--company", "Example Bank", "--uk", "--offline", RESEARCH,
                                         "--today", "2026-11-03", "--stdout", "--profile", ""])
        self.assertEqual(code, 0)
        self.assertIn("company number 09999999", out)
        self.assertIn("find-and-update.company-information.service.gov.uk/company/09999999", out)
        self.assertNotIn("Private companies do not file with the SEC", out)

    def test_no_key_says_it_was_skipped(self):
        old = os.environ.pop("COMPANIES_HOUSE_API_KEY", None)
        try:
            ns = brief.main.__globals__["argparse"].Namespace(
                companies=brief.COMPANIES, company="Example Bank", ticker=None, cik=None, feed=[], today="2026-11-03",
                user_agent=None, profile="", offline=None, days=180, uk=True, ch_number=None, ch_key=None)
            _, _, _, _, _, notes = brief.gather(ns, need_feeds=False)
            self.assertTrue(any("Companies House skipped" in n for n in notes))
        finally:
            if old is not None:
                os.environ["COMPANIES_HOUSE_API_KEY"] = old


class UKCV(unittest.TestCase):
    CV = ("# Resume\n![me](me.jpg)\nDate of birth: 01/02/2001\nNI: AB 12 34 56 C\n## Experience\n"
          "- Optimized the pipeline and analyzed 4 datasets across the organization\n"
          "## Education\nBSc Economics, GPA 3.8\n")

    def test_flags_what_a_uk_cv_leaves_out(self):
        r = ats.cv_check(self.CV)
        joined = " | ".join(r["fix"])
        for word in ("photo", "date of birth", "National Insurance", "analyzed -> analysed", "US-style dates",
                     "'Resume' heading"):
            self.assertIn(word, joined)
        self.assertTrue(any("2:1" in c for c in r["consider"]))

    def test_posting_spellings_are_kept(self):
        r = ats.cv_check("Optimized the API", jd_text="Experience with query optimization; optimized code")
        self.assertFalse(any("optimized" in f for f in r["fix"]))

    def test_clean_uk_cv_and_british_verbs_score(self):
        cv = ("## Experience\n- Optimised checkout and cut failed payments 30% for 2m customers in 2025\n"
              "## Education\nBSc Computer Science, First\nFull right to work in the UK\n")
        r = ats.cv_check(cv)
        self.assertEqual(r["fix"], [])
        self.assertIn("optimised", ats.STRONG_VERBS)
        self.assertTrue(ats.check_bullet("Modelled churn for 3 product lines and cut losses 12% in a year")["verb"])


class UKApply(unittest.TestCase):
    def setUp(self):
        self.p = read(UK_PROFILE)
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def q(self, text):
        return ans.answer(text, self.p, workspace=self.tmp)

    def test_uk_questions(self):
        self.assertEqual(self.q("What is your notice period?")["answer"], "One month")
        self.assertEqual(self.q("Will you require a Skilled Worker visa to work in the UK?")["answer"], "No")
        self.assertEqual(self.q("Degree classification (or predicted grade)")["answer"], "2:1")
        self.assertIn("Maths A*", self.q("A-level results")["answer"])
        for text in ("Do you have any unspent criminal convictions?", "National Insurance number",
                     "Sexual orientation", "Religion or belief"):
            r = self.q(text)
            self.assertEqual(r["status"], "sensitive", text)
            self.assertIsNone(r["answer"], text)

    def test_uk_salary_history_note(self):
        self.assertIn("UK employers may ask", self.q("What is your current salary?")["note"])

    def test_answer_bank_round_trip_and_check(self):
        bank = ans.load_bank(self.tmp)
        with self.assertRaises(ans.ApplyError):
            ans.bank_set(bank, "why_company", "text", "", company="Monzo")
        with self.assertRaises(ans.ApplyError):
            ans.bank_set(bank, "why_company", "text", "yes")
        ans.bank_set(bank, "why_company", "I want to build payments for 13 million customers.", "yes", "Monzo")
        ans.bank_set(bank, "why_sector", "Fintech touches everyone.", "yes")
        ans.save_json(ans.bank_path(self.tmp), bank)
        r = ans.answer("Why do you want to work at Monzo?", self.p, role_id="monzo-software-engineer",
                       workspace=self.tmp)
        self.assertIn("13 million", r["answer"])
        self.assertIn("answers.json", r["source"])
        other = ans.answer("Why do you want to work here?", self.p, role_id="wise-engineer", workspace=self.tmp)
        self.assertIsNone(other["answer"])
        new = ans.bank_check("I cut costs 40% and served 13 million customers since 2019.",
                             ans.load_bank(self.tmp), "Monzo")
        self.assertEqual(new, ["40%"])

    def test_bank_cli(self):
        code, out, _ = call(ans.main, ["--workspace", self.tmp, "bank", "set", "--key", "why_leaving",
                                       "--text", "I want to work on larger systems.", "--approved", "yes"])
        self.assertEqual(code, 0)
        code, out, _ = call(ans.main, ["--workspace", self.tmp, "bank", "get", "--key", "why_leaving"])
        self.assertEqual(out.strip(), "I want to work on larger systems.")


class Exports(unittest.TestCase):
    def setUp(self):
        self.data = read(PIPE)
        self.today = datetime.date(2026, 10, 16)

    def test_ics(self):
        text = pipeline.to_ics(self.data, self.today)
        self.assertTrue(text.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertTrue(text.endswith("END:VCALENDAR\r\n"))
        self.assertEqual(text.count("BEGIN:VEVENT"), text.count("END:VEVENT"))
        self.assertIn("SUMMARY:Amazon: Online assessment", text)
        self.assertIn("DTSTART;VALUE=DATE:20261019", text)
        self.assertIn("Software Engineer\\, Product", text)   # commas escaped
        for line in text.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75)

    def test_booked_chat_is_timed_and_not_doubled(self):
        data = json.loads(json.dumps(self.data))
        t = next(t for t in data["threads"] if t["status"] == "booked")
        t["booked"] = {"when": "2026-10-22 10:00", "minutes": 30, "where": "Google Meet", "approved": "yes"}
        events = pipeline.calendar_events(data, self.today)
        timed = [e for e in events if e.get("start")]
        self.assertEqual(len(timed), 1)
        self.assertEqual(timed[0]["start"].hour, 10)
        self.assertFalse(any(e.get("day") == datetime.date(2026, 10, 22) and e["uid"].startswith("role-google")
                             for e in events))
        self.assertIn("DTSTART:20261022T100000", pipeline.to_ics(data, self.today))

    def test_html_dashboard_escapes_and_counts(self):
        data = json.loads(json.dumps(self.data))
        data["roles"][0]["company"] = "<script>x</script>"
        page = pipeline.to_html(data, self.today)
        self.assertNotIn("<script>x", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("5 roles", page)
        self.assertIn("1 contact.", page)
        self.assertNotIn("{{", page)

    def test_export_cli(self):
        tmp = tempfile.mkdtemp()
        try:
            out = os.path.join(tmp, "p.ics")
            code, text, _ = call(pipeline.main, ["--file", PIPE, "export", "--format", "ics", "--out", out,
                                                 "--today", "2026-10-16"])
            self.assertEqual(code, 0)
            self.assertIn("calendar entries", text)
            self.assertTrue(os.path.isfile(out))
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
