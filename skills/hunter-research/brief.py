#!/usr/bin/env python3
"""Company research brief for hunter-research. Standard library only.

Assembles what can be fetched publicly about a company into
hunter/companies/<company>.md: the SEC filings that describe the business
(latest 10-K and 10-Q, recent 8-Ks with what each one is about), recent posts
from the company's own blogs, and the interview loop from loops.json with a
source on every line. Claude and the user fill the "(fill in)" lines.

    python3 brief.py build --company Google --role "Software Engineer" --team "Google Cloud, Infrastructure"
    python3 brief.py build --company Stripe --feed https://stripe.com/blog/feed.rss   # any company
    python3 brief.py loop --company Amazon
    python3 brief.py sec --ticker NFLX --days 120
    python3 brief.py feeds --company NVIDIA --days 14
    python3 brief.py build --company Monzo --uk --role "Software Engineer"   # UK: Companies House
    python3 brief.py ch --company "Monzo Bank" --ch-number 09446231

Sources:
  SEC EDGAR submissions  https://data.sec.gov/submissions/CIK##########.json
  SEC ticker list        https://www.sec.gov/files/company_tickers.json
  Company RSS/Atom feeds listed in companies.json (or --feed URL)
SEC asks every script to send a User-Agent with a name and email: pass
--user-agent "Your Name you@example.com", set SEC_USER_AGENT, or put
"sec_user_agent" in hunter/profile.json. Without one the SEC part is skipped
and the brief says so. Offline: --offline DIR reads submissions-CIK*.json,
company_tickers.json and feed-<id>.xml from DIR (the tests use it).

UK companies (public or private) file with Companies House:
  Companies House API   https://api.company-information.service.gov.uk (free key, sent as Basic auth)
The key comes from --ch-key or COMPANIES_HOUSE_API_KEY (register an application at
developer.company-information.service.gov.uk). With --uk the company is found by name; with
--ch-number (or "ch_number" in companies.json) it is used as given. The brief then carries the
company's status, age, registered office, SIC codes, accounts dates, current directors and recent
filings, every line linked to its Companies House page. Offline: ch-search.json,
ch-company-<number>.json, ch-officers-<number>.json and ch-filings-<number>.json in DIR.
"""

import argparse
import base64
import datetime as _dt
import email.utils
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
COMPANIES = os.path.join(HERE, "companies.json")
LOOPS = os.path.join(HERE, "loops.json")
TEMPLATE = os.path.join(HERE, "brief.template.md")
SUBMISSIONS = "https://data.sec.gov/submissions/CIK%s.json"
TICKERS = "https://www.sec.gov/files/company_tickers.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/%d/%s/%s"
EDGAR_PAGE = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=%s"
FEED_UA = "hunter-skill/1.1 (company research; +https://github.com/Ltyrrell1/hunter-skill)"
CH_API = "https://api.company-information.service.gov.uk"
CH_PAGE = "https://find-and-update.company-information.service.gov.uk/company/%s"
PLACEHOLDERS = ("you@example.com", "admin@example.com")

ITEMS_8K = {
    "1.01": "material agreement", "1.02": "agreement ended", "1.03": "bankruptcy",
    "1.05": "cybersecurity incident", "2.01": "acquisition or sale of assets",
    "2.02": "earnings results", "2.03": "new debt", "2.05": "restructuring costs",
    "2.06": "impairment", "3.01": "delisting notice", "3.03": "shareholder rights change",
    "4.01": "auditor change", "4.02": "past financials no longer reliable",
    "5.01": "change in control", "5.02": "director or officer change",
    "5.03": "bylaws or fiscal year change", "5.07": "shareholder vote results",
    "7.01": "Regulation FD disclosure", "8.01": "other events",
}
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]


class BriefError(Exception):
    pass


def load_json(path, default=None):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return default


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "company"


def parse_day(value):
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(value or ""))
    return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def sec_user_agent(cli=None, profile_path="hunter/profile.json"):
    ua = (cli or os.environ.get("SEC_USER_AGENT") or "").strip()
    if not ua:
        ua = str((load_json(profile_path) or {}).get("sec_user_agent") or "").strip()
    if not ua or "@" not in ua or any(p in ua for p in PLACEHOLDERS[:1]):
        return None
    return ua


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    def __init__(self, sec_ua=None, offline=None, pause=0.2):
        self.sec_ua = sec_ua
        self.offline = offline
        self.pause = pause
        self._last = 0.0

    def get(self, url, name, sec=False, headers=None):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise BriefError("offline: no file %s" % name)
            with open(path, "rb") as fh:
                return fh.read()
        if sec and not self.sec_ua:
            raise BriefError("no SEC User-Agent set")
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        hdrs = {"User-Agent": self.sec_ua if sec else FEED_UA, "Accept-Encoding": "gzip"}
        hdrs.update(headers or {})
        req = urllib.request.Request(url, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as exc:
            raise BriefError("HTTP %s for %s" % (exc.code, url))
        except urllib.error.URLError as exc:
            raise BriefError("could not reach %s: %s" % (url, exc.reason))


# ---------------------------------------------------------------- SEC

def cik_for(fetcher, ticker):
    data = json.loads(fetcher.get(TICKERS, "company_tickers.json", sec=True).decode("utf-8"))
    t = ticker.upper().replace(".", "-")
    for v in data.values():
        if v["ticker"].upper() == t:
            return str(v["cik_str"]).zfill(10)
    raise BriefError("no SEC filer with ticker %s (private companies do not file with the SEC)" % ticker)


def filing_url(cik, accession, doc):
    return ARCHIVE % (int(cik), accession.replace("-", ""), doc)


def sec_summary(data, today, days=180):
    """Submissions JSON -> facts and filings that describe the business."""
    recent = (data.get("filings") or {}).get("recent") or {}
    rows = []
    for i, form in enumerate(recent.get("form", [])):
        rows.append({k: recent[k][i] for k in recent})
    cik = str(data.get("cik", "")).zfill(10)
    addr = (data.get("addresses") or {}).get("business") or {}
    fye = str(data.get("fiscalYearEnd") or "")
    out = {
        "name": data.get("name"),
        "cik": cik,
        "tickers": data.get("tickers") or [],
        "exchanges": data.get("exchanges") or [],
        "industry": data.get("sicDescription"),
        "hq": ", ".join(x.title() if x and len(x) > 2 else (x or "") for x in
                        (addr.get("street1"), addr.get("city"), addr.get("stateOrCountry")) if x),
        "fiscal_year_end": ("%s %d" % (MONTHS[int(fye[:2]) - 1], int(fye[2:]))) if len(fye) == 4 else None,
        "edgar": EDGAR_PAGE % cik,
        "annual": None, "quarterly": None, "proxy": None, "events": [],
    }
    for r in rows:
        url = filing_url(cik, r["accessionNumber"], r["primaryDocument"])
        rec = {"form": r["form"], "filed": r["filingDate"], "period": r.get("reportDate") or None, "url": url}
        if r["form"] in ("10-K", "20-F") and not out["annual"]:
            out["annual"] = rec
        elif r["form"] == "10-Q" and not out["quarterly"]:
            out["quarterly"] = rec
        elif r["form"] == "DEF 14A" and not out["proxy"]:
            out["proxy"] = rec
        elif r["form"] in ("8-K", "8-K/A"):
            filed = parse_day(r["filingDate"])
            if filed and (today - filed).days <= days:
                items = [x for x in re.findall(r"\d+\.\d+", r.get("items") or "") if x != "9.01"]
                rec["items"] = items
                rec["about"] = ", ".join(ITEMS_8K.get(x, "item " + x) for x in items) or "see filing"
                out["events"].append(rec)
    return out


def fetch_sec(fetcher, cik=None, ticker=None, today=None, days=180):
    if not cik:
        cik = cik_for(fetcher, ticker)
    cik = str(cik).zfill(10)
    data = json.loads(fetcher.get(SUBMISSIONS % cik, "submissions-CIK%s.json" % cik, sec=True).decode("utf-8"))
    return sec_summary(data, today, days)


# ---------------------------------------------------------------- feeds

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _child(el, name):
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def feed_day(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        d = email.utils.parsedate_to_datetime(value)
        if d is not None:
            return d.date() if d.tzinfo is None else d.astimezone(_dt.timezone.utc).date()
    except (TypeError, ValueError, IndexError):
        pass
    m = re.match(r"(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})?", value)
    if m:
        tz = m.group(5) or "Z"
        stamp = _dt.datetime.strptime("%s %s:%s:%s" % m.group(1, 2, 3, 4), "%Y-%m-%d %H:%M:%S")
        if tz != "Z":
            sign = 1 if tz[0] == "+" else -1
            stamp -= sign * _dt.timedelta(hours=int(tz[1:3]), minutes=int(tz[4:6]))
        return stamp.date()
    return parse_day(value)


def parse_feed(data, name):
    """RSS 2.0 or Atom -> [{title, link, date}] (dates in UTC)."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise BriefError("feed %s is not valid XML: %s" % (name, exc))
    out = []
    for el in root.iter():
        tag = _local(el.tag)
        if tag == "item":
            get = lambda n: ((_child(el, n).text if _child(el, n) is not None else "") or "").strip()
            out.append({"title": get("title"), "link": get("link") or get("guid"),
                        "date": feed_day(get("pubDate") or get("date"))})
        elif tag == "entry":
            title = _child(el, "title")
            link = ""
            for c in el:
                if _local(c.tag) == "link" and c.get("rel", "alternate") == "alternate":
                    link = c.get("href", "")
                    break
            stamp = _child(el, "published")
            if stamp is None:
                stamp = _child(el, "updated")
            out.append({"title": ((title.text if title is not None else "") or "").strip(), "link": link,
                        "date": feed_day(stamp.text if stamp is not None else "")})
    for it in out:
        it["title"] = re.sub(r"\s+", " ", it["title"]).replace(chr(0x2014), ",").strip()
    return out


def fetch_feeds(fetcher, feeds, today, days=30, per_feed=5, notes=None):
    notes = [] if notes is None else notes
    out = []
    for f in feeds:
        try:
            items = parse_feed(fetcher.get(f["url"], "feed-%s.xml" % f["id"]), f["name"])
        except BriefError as exc:
            notes.append("%s: %s" % (f["name"], exc))
            continue
        fresh = [i for i in items if i["date"] and 0 <= (today - i["date"]).days <= days]
        fresh.sort(key=lambda i: i["date"], reverse=True)
        if not any(i["date"] for i in items):
            fresh = items[:3]  # a feed without dates: its first items, in feed order
        for i in fresh[:per_feed]:
            out.append(dict(i, feed=f["name"]))
    out.sort(key=lambda i: i["date"] or _dt.date.min, reverse=True)
    return out


def day_text(d):
    return d.isoformat() if d else "undated"


# ---------------------------------------------------------------- loops

def find(mapping, company):
    for k, v in (mapping or {}).items():
        if k.lower() == (company or "").lower():
            return k, v
    return company, None


def loop_lines(entry):
    if not entry:
        return ["(fill in) No bundled loop for this company. Read its own careers and interview-prep pages "
                "and ask the recruiter. Mark anything from candidate reports as community-reported."]
    lines = ["In one line (community summary): %s" % entry.get("reel_summary")] if entry.get("reel_summary") else []
    off = [n for n in entry.get("notes", []) if n.get("kind") == "official"]
    com = [n for n in entry.get("notes", []) if n.get("kind") != "official"]
    lines += ["- %s [source: %s]" % (n["text"], n["source"]) for n in off]
    lines += ["- Community-reported, not confirmed by the company: %s" % n["text"] for n in com]
    return lines


# ---------------------------------------------------------------- Companies House (UK)

def ch_key(cli=None):
    key = (cli or os.environ.get("COMPANIES_HOUSE_API_KEY") or "").strip()
    return key or None


def _ch_get(fetcher, path, name, key):
    auth = "Basic " + base64.b64encode((key or "") .encode("utf-8") + b":").decode("ascii")
    data = fetcher.get(CH_API + path, name, headers={"Authorization": auth, "Accept": "application/json"})
    return json.loads(data.decode("utf-8"))


def _norm_name(n):
    n = re.sub(r"[^a-z0-9 ]", " ", (n or "").lower())
    return " ".join(w for w in n.split() if w not in ("ltd", "limited", "plc", "llp", "the", "uk", "group"))


def ch_find(fetcher, company, key):
    """The best active match for a company name: (number, title) or (None, reason)."""
    q = urllib.request.quote(company)
    res = _ch_get(fetcher, "/search/companies?q=%s&items_per_page=10" % q, "ch-search.json", key)
    items = res.get("items") or []
    if not items:
        return None, "no company called %s on Companies House" % company
    want = _norm_name(company)
    def rank(it):
        title = _norm_name(it.get("title"))
        return (it.get("company_status") != "active", title != want, not title.startswith(want), len(title))
    best = sorted(items, key=rank)[0]
    return best.get("company_number"), best.get("title")


def _address(a):
    a = a or {}
    parts = [a.get(k) for k in ("address_line_1", "address_line_2", "locality", "region", "postal_code", "country")]
    return ", ".join(p for p in parts if p)


def _describe(slug_text):
    return (slug_text or "").replace("-", " ").strip()


def ch_summary(company, officers, filings, today, days=365):
    num = company.get("company_number")
    page = CH_PAGE % num
    accounts = company.get("accounts") or {}
    last = (accounts.get("last_accounts") or {})
    active = [o for o in (officers or {}).get("items", []) if not o.get("resigned_on")]
    directors = [{"name": o.get("name"), "role": o.get("officer_role"), "appointed": o.get("appointed_on"),
                  "occupation": o.get("occupation")} for o in active if o.get("officer_role") in
                 ("director", "corporate-director", "llp-designated-member", "llp-member")][:10]
    recent = []
    for f in (filings or {}).get("items", []):
        d = parse_day(f.get("date"))
        if d and (today - d).days > days:
            continue
        recent.append({"date": f.get("date"), "category": f.get("category"), "type": f.get("type"),
                       "about": _describe(f.get("description")),
                       "url": "%s/filing-history/%s/document?format=pdf&download=0" % (page, f.get("transaction_id"))
                       if f.get("transaction_id") else page + "/filing-history"})
    accounts_filed = next((f for f in recent if f["category"] == "accounts"), None)
    created = parse_day(company.get("date_of_creation"))
    return {
        "name": company.get("company_name"), "number": num, "status": company.get("company_status"),
        "type": company.get("type"), "created": company.get("date_of_creation"),
        "age_years": (today - created).days // 365 if created else None,
        "office": _address(company.get("registered_office_address")),
        "sic": company.get("sic_codes") or [], "last_accounts": last.get("made_up_to"),
        "accounts_type": last.get("type"), "next_accounts_due": (accounts.get("next_accounts") or {}).get("due_on")
        or accounts.get("next_due"),
        "directors": directors, "filings": recent[:10], "accounts_filing": accounts_filed,
        "page": page, "officers_page": page + "/officers", "filings_page": page + "/filing-history",
    }


def fetch_ch(fetcher, company=None, number=None, key=None, today=None, days=365):
    if not number:
        number, title = ch_find(fetcher, company, key)
        if not number:
            raise BriefError(title)
    number = str(number).strip().upper()
    prof = _ch_get(fetcher, "/company/%s" % number, "ch-company-%s.json" % number, key)
    try:
        officers = _ch_get(fetcher, "/company/%s/officers?items_per_page=35" % number,
                           "ch-officers-%s.json" % number, key)
    except BriefError:
        officers = {}
    try:
        filings = _ch_get(fetcher, "/company/%s/filing-history?items_per_page=25" % number,
                          "ch-filings-%s.json" % number, key)
    except BriefError:
        filings = {}
    return ch_summary(prof, officers, filings, today or _dt.date.today(), days)


def ch_lines(ch):
    lines = ["- %s, company number %s (%s, %s), incorporated %s%s. Registered office: %s. Source: %s" % (
        ch["name"], ch["number"], ch["status"] or "status n/a", (ch["type"] or "type n/a").replace("-", " "),
        ch["created"] or "n/a", " (%d years ago)" % ch["age_years"] if ch["age_years"] is not None else "",
        ch["office"] or "n/a", ch["page"])]
    if ch["sic"]:
        lines.append("- Nature of business (SIC codes): %s. Look each code up in the ONS SIC 2007 list." %
                     ", ".join(ch["sic"]))
    if ch["last_accounts"]:
        lines.append("- Accounts: last made up to %s (%s); next due %s. %s" % (
            ch["last_accounts"], (ch["accounts_type"] or "type n/a").replace("-", " "),
            ch["next_accounts_due"] or "n/a",
            ("Latest accounts filing: %s" % ch["accounts_filing"]["url"]) if ch["accounts_filing"] else
            "Filings: %s" % ch["filings_page"]))
        lines.append("- Full or group accounts carry the strategic report: what the company does, its "
                     "turnover, headcount and risks. Small or micro-entity accounts say much less.")
    if ch["status"] and ch["status"] != "active":
        lines.append("- WARNING: Companies House lists this company as %s. Check you have the right one." % ch["status"])
    if ch["directors"]:
        names = ["%s (%s, since %s)" % (d["name"], (d["role"] or "").replace("-", " "), d["appointed"] or "n/a")
                 for d in ch["directors"]]
        lines.append("- Current directors (public register): %s. %s" % ("; ".join(names), ch["officers_page"]))
    lines.append("- (fill in) Two or three lines in your own words from the strategic report, including where "
                 "this team fits.")
    return lines


def ch_news_lines(ch):
    return ["- %s Companies House filing: %s %s" % (f["date"], f["about"], f["url"]) for f in ch["filings"][:6]]


# ---------------------------------------------------------------- the brief

def business_lines(sec, info, notes, ch=None):
    if not sec and ch:
        return ch_lines(ch)
    if not sec:
        lines = ["(fill in) No SEC filings in this brief. %s" % (
            "Private companies do not file with the SEC: use the company's own about, press and blog pages."
            if not (info or {}).get("cik") else "Set an SEC User-Agent and run it again for the 10-K.")]
        return lines
    t = ", ".join(sec["tickers"][:2])
    ex = sec["exchanges"][0] if sec["exchanges"] else ""
    lines = ["- %s%s. Industry code: %s. Headquarters on file with the SEC: %s. Fiscal year ends %s." % (
        sec["name"], " (%s on %s)" % (t, ex) if t else "", (sec["industry"] or "n/a").rstrip("."), sec["hq"] or "n/a",
        sec["fiscal_year_end"] or "n/a")]
    a = sec["annual"]
    if a:
        lines.append("- Annual report: %s for the year ended %s, filed %s: %s. Item 1 (Business) says what the "
                     "company does and how it makes money; Item 1A lists its risks." % (a["form"], a["period"], a["filed"], a["url"]))
    q = sec["quarterly"]
    if q:
        lines.append("- Latest quarter: 10-Q for the period ended %s, filed %s: %s" % (q["period"], q["filed"], q["url"]))
    if sec["proxy"]:
        lines.append("- Leadership and pay: proxy statement (DEF 14A) filed %s: %s" % (sec["proxy"]["filed"], sec["proxy"]["url"]))
    lines.append("- (fill in) Two or three lines in your own words from Item 1, including the segment this team sits in.")
    return lines


def news_lines(sec, posts, days, ch=None):
    lines = []
    if ch:
        lines += ch_news_lines(ch)
    if sec:
        earn = [e for e in sec["events"] if "2.02" in e.get("items", [])]
        for e in sec["events"][:8]:
            lines.append("- %s %s: %s %s" % (e["filed"], e["form"], e["about"], e["url"]))
        if earn:
            lines.append("- Latest results: the 8-K of %s (item 2.02) carries the press release as an exhibit. "
                         "Quote revenue and growth from it, never from memory." % earn[0]["filed"])
        if not sec["events"]:
            lines.append("- No 8-K filings in the last %d days." % days)
    for p in posts[:10]:
        lines.append("- %s %s: %s %s" % (day_text(p["date"]), p["feed"], p["title"], p["link"]))
    if not lines:
        lines.append("(fill in) Nothing fetched. Check the company's newsroom and blog.")
    lines.append("- (fill in) What this news means for the team you'd join, in one line.")
    return lines


def build(company, role, team, today, info, loops_entry, sec, posts, notes, days, candidate=None, ch=None):
    with open(TEMPLATE, encoding="utf-8") as fh:
        text = fh.read()
    sources = []
    if sec:
        sources.append("- SEC EDGAR filings for %s: %s" % (sec["name"], sec["edgar"]))
    if ch:
        sources.append("- Companies House record for %s (%s): %s" % (ch["name"], ch["number"], ch["page"]))
    for f in (info or {}).get("feeds", []):
        sources.append("- %s (feed): %s" % (f["name"], f["url"]))
    if (info or {}).get("careers"):
        sources.append("- Careers site: %s" % info["careers"])
    if (info or {}).get("investors"):
        sources.append("- Investor relations: %s" % info["investors"])
    for p in (loops_entry or {}).get("prep_pages", []):
        sources.append("- %s: %s" % (p["title"], p["url"]))
    culture = [p for p in (loops_entry or {}).get("prep_pages", [])
               if re.search(r"culture|principles|how we hire|hiring process", p["title"], re.I)]
    culture_lines = ["- Read the company's own words: %s (%s)" % (p["title"], p["url"]) for p in culture]
    culture_lines.append("- (fill in) What the company says it values, from its own pages, and what employees say "
                         "in public talks or posts (label those as personal views).")
    team_lines = ["- Team named in the posting: %s" % team if team else "- (fill in) The team, from the posting.",
                  "- (fill in) What the team builds, its stack and its recent launches, from the posting, the team's "
                  "engineering blog posts and public talks.",
                  "- (fill in) The hiring manager, if public: /hunter-connector researches them from public "
                  "professional sources only."]
    values = {
        "company": company, "role": role or "(fill in)", "team": team or "the team",
        "today": today.isoformat(), "for_line": " for %s" % candidate if candidate else "",
        "business": "\n".join(business_lines(sec, info, notes) if sec else business_lines(sec, info, notes, ch)),
        "news": "\n".join(news_lines(sec, posts, days, ch)),
        "culture": "\n".join(culture_lines),
        "team_section": "\n".join(team_lines),
        "loop": "\n".join(loop_lines(loops_entry)),
        "sources": "\n".join(sources) or "(fill in)",
    }
    for k, v in values.items():
        text = text.replace("{{%s}}" % k, v)
    if notes:
        text += "\n## Notes from the build\n\n" + "\n".join("- %s" % n for n in notes) + "\n"
    return text


# ---------------------------------------------------------------- CLI

def _today(value):
    return parse_day(value) if value else _dt.date.today()


def gather(args, need_sec=True, need_feeds=True):
    companies = (load_json(args.companies) or {}).get("companies", {})
    name, info = find(companies, args.company)
    info = dict(info or {})
    if getattr(args, "ticker", None):
        info["ticker"] = args.ticker
        info.pop("cik", None)
    if getattr(args, "cik", None):
        info["cik"] = args.cik
    feeds = list(info.get("feeds", []))
    for i, url in enumerate(getattr(args, "feed", None) or []):
        feeds.append({"id": "extra-%d" % (i + 1), "name": url.split("/")[2] if "//" in url else url, "url": url})
    info["feeds"] = feeds
    notes = []
    today = _today(args.today)
    ua = sec_user_agent(getattr(args, "user_agent", None), getattr(args, "profile", "hunter/profile.json"))
    fetcher = Fetcher(ua, offline=args.offline)
    sec = None
    if need_sec and (info.get("cik") or info.get("ticker")):
        if not ua and not args.offline:
            notes.append("SEC filings skipped: pass --user-agent \"Your Name you@example.com\", set SEC_USER_AGENT, "
                         "or add sec_user_agent to hunter/profile.json (SEC asks every script to identify itself)")
        else:
            try:
                sec = fetch_sec(fetcher, info.get("cik"), info.get("ticker"), today, args.days)
            except BriefError as exc:
                notes.append("SEC: %s" % exc)
    elif need_sec and not (getattr(args, "uk", False) or getattr(args, "ch_number", None) or info.get("ch_number")):
        notes.append("%s is not in companies.json and no --ticker was given: no SEC filings "
                     "(private companies do not file; for a UK company add --uk)" % name)
    ch = None
    number = getattr(args, "ch_number", None) or info.get("ch_number")
    if need_sec and (number or getattr(args, "uk", False)):
        key = ch_key(getattr(args, "ch_key", None))
        if not key and not args.offline:
            notes.append("Companies House skipped: set COMPANIES_HOUSE_API_KEY (a free key from "
                         "developer.company-information.service.gov.uk) or pass --ch-key")
        else:
            try:
                ch = fetch_ch(fetcher, args.company, number, key, today, max(args.days, 365))
            except BriefError as exc:
                notes.append("Companies House: %s" % exc)
    posts = fetch_feeds(fetcher, feeds, today, days=min(args.days, 60), notes=notes) if need_feeds else []
    args._ch = ch
    return name, info, today, sec, posts, notes


def main(argv=None):
    ap = argparse.ArgumentParser(description="Company research brief: SEC filings, company blogs and interview loops.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p, sec=True):
        p.add_argument("--company", required=True)
        p.add_argument("--companies", default=COMPANIES)
        p.add_argument("--loops", default=LOOPS)
        p.add_argument("--today", help="YYYY-MM-DD (default: today)")
        p.add_argument("--days", type=int, default=180, help="look back this many days for 8-Ks (posts: up to 60)")
        p.add_argument("--offline", metavar="DIR")
        p.add_argument("--json", action="store_true")
        if sec:
            p.add_argument("--ticker")
            p.add_argument("--cik")
            p.add_argument("--feed", action="append", default=[], help="extra RSS/Atom feed URL")
            p.add_argument("--user-agent", help="SEC User-Agent: \"Your Name you@example.com\"")
            p.add_argument("--profile", default="hunter/profile.json")
            p.add_argument("--uk", action="store_true", help="also look the company up on Companies House")
            p.add_argument("--ch-number", help="Companies House company number (skips the name search)")
            p.add_argument("--ch-key", help="Companies House API key (default: COMPANIES_HOUSE_API_KEY)")

    b = sub.add_parser("build", help="write hunter/companies/<company>.md")
    common(b)
    b.add_argument("--role")
    b.add_argument("--team")
    b.add_argument("--candidate", help="name on the brief (default: profile name)")
    b.add_argument("--out", help="default: hunter/companies/<company>.md")
    b.add_argument("--stdout", action="store_true", help="print instead of writing")
    b.add_argument("--force", action="store_true", help="overwrite an existing brief")
    lp = sub.add_parser("loop", help="the interview loop with sources")
    common(lp, sec=False)
    s = sub.add_parser("sec", help="SEC facts and filings only")
    common(s)
    f = sub.add_parser("feeds", help="recent posts from the company's own blogs")
    common(f)
    c = sub.add_parser("ch", help="Companies House facts, directors and filings only (UK)")
    common(c)

    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2

    loops = (load_json(args.loops) or {}).get("companies", {})
    if args.cmd == "loop":
        name, entry = find(loops, args.company)
        if args.json:
            print(json.dumps({"company": name, "loop": entry}, indent=2))
        else:
            print("%s interview loop (verified %s)" % (name, (load_json(args.loops) or {}).get("verified")))
            print("\n".join(loop_lines(entry)))
        return 0 if entry else 1

    if args.cmd == "sec":
        name, info, today, sec, _, notes = gather(args, need_feeds=False)
        if args.json:
            print(json.dumps({"sec": sec, "notes": notes}, indent=2))
        else:
            print("\n".join(business_lines(sec, info, notes) + news_lines(sec, [], args.days)[:-1]))
            for n in notes:
                print("note: %s" % n)
        return 0 if sec else 1

    if args.cmd == "ch":
        args.uk = True
        name, info, today, _, _, notes = gather(args, need_feeds=False)
        ch = args._ch
        if args.json:
            print(json.dumps({"companies_house": ch, "notes": notes}, indent=2))
        else:
            if ch:
                print("\n".join(ch_lines(ch)[:-1] + ch_news_lines(ch)))
            for n in notes:
                print("note: %s" % n)
        return 0 if ch else 1

    if args.cmd == "feeds":
        name, info, today, _, posts, notes = gather(args, need_sec=False)
        if args.json:
            print(json.dumps({"posts": [dict(p, date=p["date"] and p["date"].isoformat()) for p in posts],
                              "notes": notes}, indent=2))
        else:
            for p in posts:
                print("%-10s  %-24s %s\n            %s" % (day_text(p["date"]), p["feed"][:24], p["title"], p["link"]))
            for n in notes:
                print("note: %s" % n)
            if not posts:
                print("No posts in the window.")
        return 0 if posts else 1

    name, info, today, sec, posts, notes = gather(args)
    _, entry = find(loops, args.company)
    candidate = args.candidate or (load_json(args.profile) or {}).get("name")
    text = build(name, args.role, args.team, today, info, entry, sec, posts, notes, args.days, candidate,
                 ch=args._ch)
    if args.stdout:
        sys.stdout.write(text)
        return 0
    out = args.out or os.path.join("hunter", "companies", slug(name) + ".md")
    if os.path.exists(out) and not args.force:
        print("brief: %s already exists (it may hold your edits). Use --stdout to compare or --force to "
              "rebuild it." % out, file=sys.stderr)
        return 3
    folder = os.path.dirname(out)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)
    print("Wrote %s: %d filings, %d blog posts, %d loop notes. Fill the (fill in) lines from the sources." % (
        out, (1 if sec and sec["annual"] else 0) + (len(sec["events"]) if sec else 0)
        + (len(args._ch["filings"]) if args._ch else 0), len(posts),
        len((entry or {}).get("notes", []))))
    for n in notes:
        print("note: %s" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
