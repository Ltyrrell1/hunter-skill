import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

jobs = load("hunter-scout", "jobs")
DIR = os.path.join(FIX, "hunter-scout")
PROFILE = os.path.join(os.path.dirname(FIX), "..", "skills", "hunter-apply", "profile.example.json")
TODAY = datetime.date(2026, 10, 15)
EM = chr(0x2014)


def fixture(name):
    with open(os.path.join(DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


def board(company):
    doc = jobs.load_json(jobs.BOARDS)
    return [b for b in doc["boards"] if b["company"] == company][0]


def profile():
    with open(PROFILE, encoding="utf-8") as fh:
        return json.load(fh)


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = jobs.main(args)
    return code, out.getvalue(), err.getvalue()


class Feeds(unittest.TestCase):
    def test_greenhouse(self):
        got = jobs.from_greenhouse(board("Discord"), fixture("greenhouse-discord.json"))
        self.assertEqual(len(got), 3)
        ads = [j for j in got if j["title"] == "Senior Software Engineer, Ads"][0]
        self.assertEqual(ads["posted"], "2026-08-12")  # first_published, as published
        self.assertEqual(ads["pay"], {"min": 196000, "max": 245000, "currency": "USD", "period": "year"})
        self.assertEqual(ads["url"], "https://job-boards.greenhouse.io/discord/jobs/8697883002")
        self.assertEqual(ads["id"], "discord-senior-software-engineer-ads-8697883002")
        self.assertIn("Rust", ads["_text"])
        self.assertNotIn("<li>", ads["_text"])

    def test_lever(self):
        got = jobs.from_lever(board("Palantir"), fixture("lever-palantir.json"))
        infra = [j for j in got if "Infrastructure" in j["title"]][0]
        self.assertEqual(infra["location"], "New York, NY")
        self.assertEqual(infra["pay"]["min"], 135000)
        self.assertEqual(infra["pay"]["max"], 200000)
        self.assertRegex(infra["posted"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertTrue(infra["url"].startswith("https://jobs.lever.co/palantir/"))

    def test_ashby_compensation_and_intern_pay(self):
        got = {j["title"]: j for j in jobs.from_ashby(board("Ramp"), fixture("ashby-ramp.json"))}
        be = got["Software Engineer, Backend, Growth"]
        self.assertEqual(be["pay"], {"min": 200000, "max": 310000, "currency": "USD", "period": "year"})
        self.assertEqual(be["posted"], "2026-05-13")
        intern = got["Software Engineering Intern, Backend"]
        self.assertEqual(intern["pay"]["period"], "month")
        self.assertEqual(jobs.annual(intern["pay"]), 150000)

    def test_workday_relative_dates_and_locations(self):
        got = jobs.from_workday(board("NVIDIA"), fixture("workday-nvidia.json"), TODAY)
        by = {j["url"].rsplit("_", 1)[-1]: j for j in got}
        self.assertEqual(by["JR2016506"]["posted"], "2026-10-15")      # Posted Today
        self.assertEqual(by["JR2025935"]["posted"], "2026-10-14")      # Posted Yesterday
        self.assertEqual(by["JR1998124"]["posted"], "2026-10-08")      # Posted 7 Days Ago
        self.assertEqual(by["JR2020825"]["posted_note"], "30+ days ago")
        self.assertTrue(by["JR2020825"]["remote"])                     # US, Remote
        self.assertEqual(by["JR2016506"]["location"], "Israel, Raanana (+1 more)")
        self.assertTrue(by["JR2015068"]["url"].startswith(
            "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/"))

    def test_eightfold_titles_are_cleaned(self):
        got = jobs.from_eightfold(board("Netflix"), fixture("eightfold-netflix.json"))
        titles = [j["title"] for j in got]
        self.assertIn("Senior Software Engineer 5 - Validation & Safety Platform", titles)
        self.assertFalse(any(EM in t or "  " in t for t in titles))
        remote = {j["title"]: j["remote"] for j in got}
        self.assertTrue(remote["Software Engineer (L6) - Compute Runtime"])
        self.assertFalse(remote["Software Engineer 4 - TV & Web Player Platform"])  # remote_local
        self.assertEqual(got[0]["posted"], "2026-09-25")  # epoch read in UTC, any time zone

    def test_pay_text(self):
        self.assertEqual(jobs.pay_from_text("range is $211.4K - $290.6K")["max"], 290600)
        self.assertEqual(jobs.pay_from_text("$45 to $60 per hour")["period"], "hour")
        self.assertIsNone(jobs.pay_from_text("competitive pay"))


class Matching(unittest.TestCase):
    def test_titles(self):
        roles = ["Software Engineer", "Backend Engineer"]
        self.assertEqual(jobs.title_points("Senior Software Engineer, Ads", roles)[0], 35)
        self.assertEqual(jobs.title_points("Software Development Engineer II", roles)[0], 35)
        self.assertEqual(jobs.title_points("Backend Software Engineer - Defense", roles)[0], 35)
        self.assertEqual(jobs.title_points("Engineer, Backend Systems", ["Backend Engineer"])[0], 30)
        self.assertEqual(jobs.title_points("Product Designer", roles)[0], 0)
        self.assertEqual(jobs.excluded("Staff Software Engineer", ["Staff", "Intern"]), "Staff")
        self.assertIsNone(jobs.excluded("Software Engineer, Staffing Tools", ["Staff"]))
        self.assertEqual(jobs.excluded("High School Internship, Software Engineering", ["Intern"]), "Intern")

    def test_skills(self):
        text = "We use Golang, Python and k8s. Go to market with REST APIs. PostgreSQL."
        hits = jobs.skill_hits(text, ["Go", "Python", "Kubernetes", "APIs", "Rust", "PostgreSQL"])
        self.assertEqual(hits, ["Go", "Python", "Kubernetes", "APIs", "PostgreSQL"])
        self.assertEqual(jobs.skill_hits("good to go", ["Go"]), [])

    def test_places(self):
        self.assertTrue(jobs.is_us("Los Gatos, California, United States of America"))
        self.assertTrue(jobs.is_us("US, CA, Santa Clara"))
        self.assertTrue(jobs.non_us("Warsaw, Poland"))
        self.assertEqual(jobs.place_match("Mountain View, CA", ["San Francisco Bay Area"]), "San Francisco Bay Area")
        self.assertEqual(jobs.place_match("New York, NY (HQ)", ["Chicago, IL", "New York, NY"]), "New York, NY")
        self.assertIsNone(jobs.place_match("Austin, TX", ["Chicago, IL"]))

    def test_score_formula(self):
        prof = profile()
        job = {"company": "Discord", "title": "Software Engineer, Payments", "location": "San Francisco Bay Area",
               "remote": False, "posted": "2026-10-12",
               "pay": {"min": 170000, "max": 210000, "currency": "USD", "period": "year"},
               "_text": "Go, Python, gRPC, Kubernetes, PostgreSQL and Kafka. 2+ years of experience."}
        s, why, warn = jobs.score(job, prof, TODAY)
        self.assertEqual(s, 35 + 30 + 15 + 10 + 5 + 5)
        self.assertIn("title matches Software Engineer", why)
        self.assertIn("pays $170,000 to $210,000, clears your $160,000 floor", why)
        self.assertEqual(warn, [])
        job.update(pay={"min": 120000, "max": 150000, "currency": "USD", "period": "year"},
                   location="Warsaw, Poland", posted="2026-06-01", _text="8+ years with Go")
        s2, _, warn2 = jobs.score(job, prof, TODAY)
        self.assertLess(s2, 60)
        self.assertTrue(any("under your $160,000 floor" in w for w in warn2))
        self.assertTrue(any("asks for 8+ years" in w for w in warn2))
        self.assertTrue(any("outside your locations" in w for w in warn2))


class Search(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "hunter", "jobs.json")
        self.pipe = os.path.join(self.tmp.name, "hunter", "pipeline.json")
        self.base = ["search", "--offline", DIR, "--profile", PROFILE, "--today", "2026-10-15", "--out", self.out]

    def tearDown(self):
        self.tmp.cleanup()

    def test_search_ranks_filters_and_writes(self):
        code, out, err = run(self.base + ["--company", "Discord", "--company", "Ramp", "--company", "Palantir",
                                          "--company", "NVIDIA", "--company", "Netflix", "--company", "Google"])
        self.assertEqual(code, 0, err)
        doc = jobs.load_json(self.out)
        self.assertEqual(doc["as_of"], "2026-10-15")
        self.assertEqual(doc["scanned"], 18)
        scores = [j["score"] for j in doc["jobs"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        titles = [j["title"] for j in doc["jobs"]]
        self.assertNotIn("Software Engineering Intern, Backend", titles)  # excluded title word
        self.assertNotIn("Commercial Policy Lead", titles)               # not a target title
        self.assertNotIn("Machine Learning Engineer", titles)             # not a target title
        for j in doc["jobs"]:
            self.assertEqual(set(["id", "company", "title", "location", "url", "source", "posted", "score", "why"])
                             - set(j), set())
            self.assertNotIn("_text", j)
        self.assertEqual(doc["manual"][0]["company"], "Google")
        self.assertIn("Manual: Google", out)
        self.assertIn("robots.txt", out)
        self.assertIn("not a prediction of an offer", out)
        self.assertNotIn(EM, out)

    def test_days_and_remote_filters(self):
        code, out, err = run(self.base + ["--company", "Netflix", "--company", "NVIDIA", "--remote", "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertTrue(doc["jobs"])
        self.assertTrue(all(j["remote"] for j in doc["jobs"]))
        code, out, _ = run(self.base + ["--company", "NVIDIA", "--days", "7", "--json"])
        posted = [j["posted"] for j in json.loads(out)["jobs"]]
        self.assertTrue(posted and all(p >= "2026-10-08" for p in posted))

    def test_dream_companies_by_default_and_unknown_company(self):
        code, out, err = run(self.base + ["--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertEqual(doc["boards"], ["NVIDIA", "Netflix"])
        self.assertEqual(sorted(m["company"] for m in doc["manual"]), ["Amazon", "Google", "Meta"])
        code, out, _ = run(self.base + ["--company", "Acme Rockets", "--json"])
        self.assertEqual(code, 1)
        self.assertIn("Acme Rockets is not in boards.json", json.loads(out)["notes"][0])

    def test_pick_adds_roles_once(self):
        run(self.base + ["--company", "Discord"])
        first = jobs.load_json(self.out)["jobs"][0]["id"]
        with open(self.pipe, "w") as fh:
            json.dump({"roles": [], "contacts": [{"id": "c1", "name": "Maya C."}], "threads": []}, fh)
        code, out, _ = run(["pick", first, "--jobs", self.out, "--pipeline", self.pipe, "--today", "2026-10-15"])
        self.assertEqual(code, 0)
        pipe = jobs.load_json(self.pipe)
        role = pipe["roles"][0]
        self.assertEqual(role["id"], first)
        self.assertEqual(role["stage"], "saved")
        self.assertIsNone(role["applied"])
        self.assertEqual(sorted(role), sorted(["id", "company", "title", "url", "stage", "applied",
                                               "next_step", "next_date", "notes"]))
        self.assertEqual(pipe["contacts"][0]["name"], "Maya C.")  # other data kept
        code, out, _ = run(["pick", first, "--jobs", self.out, "--pipeline", self.pipe, "--today", "2026-10-15"])
        self.assertEqual(code, 1)
        self.assertIn("already in the pipeline", out)

    def test_needs_a_profile(self):
        code, _, err = run(["search", "--offline", DIR, "--profile", os.path.join(self.tmp.name, "none.json")])
        self.assertEqual(code, 2)
        self.assertIn("profile.example.json", err)


class Boards(unittest.TestCase):
    def test_bundled_boards(self):
        doc = jobs.load_json(jobs.BOARDS)
        names = [b["company"] for b in doc["boards"]]
        self.assertEqual(len(names), len(set(names)))
        for name in ("Google", "Meta", "NVIDIA", "Netflix", "Amazon"):
            self.assertIn(name, names)
        for b in doc["boards"]:
            if b["type"] in jobs.AUTO:
                self.assertTrue(b.get("token"), b["company"])
                # a live count, or (for boards added without a live call) a note saying how it was checked
                self.assertTrue(b.get("jobs_seen", 0) > 0 or b.get("note"), b["company"])
            else:
                self.assertTrue(b.get("why_manual") and b.get("route") and b.get("careers"), b["company"])

    def test_board_flag(self):
        self.assertEqual(jobs.parse_board_flag("Acme=lever:acme-co"),
                         {"company": "Acme", "type": "lever", "token": "acme-co"})
        with self.assertRaises(jobs.ScoutError):
            jobs.parse_board_flag("Acme=workday:acme")

    def test_check_offline(self):
        f = jobs.Fetcher(offline=DIR)
        self.assertIn("3 postings", jobs.check_board(f, board("Ramp")))
        self.assertIn("1731 postings", jobs.check_board(f, board("NVIDIA")))
        self.assertIn("154 postings", jobs.check_board(f, board("Netflix")))
        with self.assertRaises(jobs.ScoutError):
            jobs.check_board(f, board("Stripe"))

    def test_boards_command(self):
        code, out, _ = run(["boards"])
        self.assertEqual(code, 0)
        self.assertIn("manual  Amazon", out)
        self.assertIn("auto    Netflix         eightfold", out)
        self.assertIn("auto    Monzo           greenhouse monzo  [UK]", out)


class UK(unittest.TestCase):
    def test_us_detection_ignores_ordinary_words(self):
        for loc in ("Remote OR Hybrid", "Join us in London", "IN office, Berlin", "London, UK"):
            self.assertFalse(jobs.is_us(loc), loc)
        for loc in ("San Francisco, CA", "Austin, TX | Remote", "Remote - US", "Remote (US)",
                    "New York, NY (HQ)", "Washington, DC", "Cambridge, MA"):
            self.assertTrue(jobs.is_us(loc), loc)

    def test_gb_detection_and_namesakes(self):
        for loc in ("London, UK", "Edinburgh, Scotland", "Manchester", "Canary Wharf, London", "Remote, GB"):
            self.assertTrue(jobs.is_gb(loc), loc)
        for loc in ("Cambridge, MA", "Birmingham, AL", "Berlin, Germany"):
            self.assertFalse(jobs.is_gb(loc), loc)
        self.assertIsNone(jobs.place_match("Cambridge, MA", ["Cambridge, UK"]))
        self.assertEqual(jobs.place_match("Canary Wharf, London", ["London, UK"]), "London, UK")

    def test_profile_country(self):
        self.assertEqual(jobs.profile_country({}), "US")
        self.assertEqual(jobs.profile_country({"region": "uk"}), "GB")
        self.assertEqual(jobs.profile_country({"work_authorization": {"country": "United Kingdom"}}), "GB")

    def test_uk_profile_scores_london_remote_and_flags_the_us(self):
        prof = {"target_roles": ["Software Engineer"], "skills": ["Python"], "locations": ["London, UK"],
                "work_authorization": {"country": "GB"}, "relocate": True}
        today = jobs.parse_day("2026-11-03")
        base = {"company": "Acme", "title": "Software Engineer", "_text": "Python", "posted": "2026-11-01"}
        s1, why1, _ = jobs.score(dict(base, location="London, England", remote=False), prof, today)
        s2, _, warn2 = jobs.score(dict(base, location="New York, NY", remote=False), prof, today)
        s3, why3, _ = jobs.score(dict(base, location="Remote, EMEA", remote=True), prof, today)
        self.assertGreater(s1, s2)
        self.assertTrue(any("outside the UK" in w for w in warn2))
        self.assertTrue(any("remote" in w for w in why3))
        s4, why4, _ = jobs.score(dict(base, location="Manchester, UK", remote=False), prof, today)
        self.assertTrue(any("relocating" in w for w in why4))

    def test_ai_and_data_titles(self):
        roles = ["AI Engineer", "Data Scientist"]
        for title in ("Graduate Data Scientist", "Applied AI Engineer", "AI/ML Engineer", "LLM Engineer",
                      "Junior Machine Learning Engineer", "Applied Scientist"):
            self.assertEqual(jobs.title_points(title, roles)[0], 35, title)
        self.assertTrue(jobs.skill_hits("Experience with LLMs and RAG", ["large language models"]))

    def test_eu_greenhouse_boards_fall_back_to_the_eu_host(self):
        class F(object):
            def __init__(self):
                self.urls = []

            def get(self, url, name, body=None):
                self.urls.append(url)
                if "boards-api.eu.greenhouse.io" not in url:
                    raise jobs.ScoutError("HTTP 404")
                return {"name": "PolyAI"}
        f = F()
        line = jobs.check_board(f, {"company": "PolyAI", "type": "greenhouse", "token": "polyai", "eu": True})
        self.assertIn("PolyAI", line)
        self.assertEqual(len(f.urls), 2)
        with self.assertRaises(jobs.ScoutError):
            jobs.check_board(F(), {"company": "X", "type": "greenhouse", "token": "x"})

    def test_region_filter(self):
        doc = jobs.load_json(jobs.BOARDS)
        auto, manual, _ = jobs.pick_boards(doc["boards"], region="UK")
        self.assertIn("Monzo", [b["company"] for b in auto])
        self.assertNotIn("Stripe", [b["company"] for b in auto])
        self.assertIn("Wise", [b["company"] for b in manual])


if __name__ == "__main__":
    unittest.main()
