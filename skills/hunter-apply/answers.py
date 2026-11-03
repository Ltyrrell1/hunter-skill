#!/usr/bin/env python3
"""Application answers for hunter-apply. Standard library only.

Matches the questions on an application form to the user's own answers in
hunter/profile.json, says which ones the user must answer themselves, keeps a
per-application checklist, and records the filled application as a draft in
hunter/pipeline.json. It never submits anything: Claude fills the form in the
user's browser and stops before Submit, and the user submits.

    python3 answers.py answer --question "Will you now or in the future require sponsorship?"
    python3 answers.py fill --questions form.json --out hunter/applications/<role-id>.json
    python3 answers.py checklist --role <role-id>
    python3 answers.py draft --role <role-id> --form-url https://... --answers hunter/applications/<role-id>.json
    python3 answers.py submitted --role <role-id> --confirm "I submitted it"

THE ANSWER BANK (hunter/answers.json): the user's approved answers to the questions that
come up again and again, so the cover letter, the form and the interview tell one story.
Company answers (why_company, why_role) are kept per company; the rest (why_sector,
why_leaving, strengths, weakness, tell_me_about_yourself, achievement) once for everyone.
Only words the user approved go in, and `bank check` lists any number or claim-like figure in
a draft that is in neither the bank nor the resume, before the draft goes anywhere.

    python3 answers.py bank set --key why_company --company Monzo --text "..." --approved "yes, save it"
    python3 answers.py bank get --key why_company --company Monzo
    python3 answers.py bank list
    python3 answers.py bank check hunter/covers/monzo-software-engineer.md --company Monzo \
        --resume hunter/resumes/base.md

form.json is a list of questions as they appear on the form: strings, or
{"label": "...", "options": ["Yes", "No"], "required": true}.
Statuses: filled (from the profile), ask (the user must answer), sensitive
(voluntary or legally sensitive: only the user's own choice), role-specific
(written for this role: cover letter, why this company).
"""

import argparse
import datetime as _dt
import json
import os
import re
import sys

# (key, pattern, kind). Order matters: the first match wins.
BANK = [
    ("self_id.gender", r"\bgender\b|\bsex\b", "sensitive"),
    ("self_id.race_ethnicity", r"\brace\b|ethnicit|hispanic|latino", "sensitive"),
    ("self_id.veteran_status", r"veteran|military service|armed forces", "sensitive"),
    ("self_id.disability_status", r"disabilit", "sensitive"),
    ("self_id.sexual_orientation", r"sexual orientation", "sensitive"),
    ("self_id.religion", r"religion|belief", "sensitive"),
    ("convictions", r"conviction|criminal record|\bdbs\b|disclosure (and|&) barring|basic disclosure", "sensitive"),
    ("ni_number", r"national insurance|\bni number\b|\bnino\b", "sensitive"),
    ("salary_history", r"(current|previous|past|prior)\s+(salary|compensation|pay)|salary history", "sensitive"),
    ("sponsorship", r"sponsor|(require|need)s?\b.{0,40}\bvisa\b|skilled worker", "fact"),
    ("work_authorization", r"(legally )?(authori[sz]ed|eligible|permitted) to work|work authori[sz]ation|right to work", "fact"),
    ("salary_expectation", r"(desired|expected|expectation|target)s?\b.*(salary|compensation|pay)|salary expectation|"
                           r"compensation expectation|(salary|compensation) requirement", "ask_if_empty"),
    ("notice_period", r"notice period|how much notice", "fact"),
    ("start_date", r"start date|when can you start|earliest (start|available)|availability to start", "fact"),
    ("relocation", r"relocat", "fact"),
    ("onsite", r"on-?site|in[- ]office|hybrid|commute|work from (the )?office", "fact"),
    ("how_did_you_hear", r"how did you (hear|find|learn)|where did you (hear|find|see)|source of (application|referral)",
     "fact"),
    ("referral", r"referr", "ask_if_empty"),
    ("previously_employed", r"(previously|ever|formerly) (been )?(employed|worked)|worked (for|at) .* before|"
                            r"former employee", "fact"),
    ("why_this_company", r"why (do you want|are you interested|this company|us\b|join)|what interests you|"
                         r"why .* (role|position|team)", "role"),
    ("cover_letter", r"cover letter|anything else you.*(share|tell)|additional information", "role"),
    ("why_leaving", r"why (are you )?(leaving|looking)|reason for leaving", "ask_if_empty"),
    ("pronouns", r"pronoun", "optional"),
    ("links.linkedin", r"linkedin", "fact"),
    ("links.github", r"github", "fact"),
    ("links.website", r"website|portfolio|personal site|blog", "fact"),
    ("years_experience", r"years of (professional |relevant |work )?experience|how many years", "fact"),
    ("current.company", r"current (company|employer)|(company|employer) name", "fact"),
    ("current.title", r"current (title|role|position)|job title", "fact"),
    ("education.classification", r"classification|predicted (grade|degree|class)|degree (result|grade)|\b2:1\b",
     "fact"),
    ("education.a_levels", r"a[- ]levels?|ucas|highers|leaving cert", "fact"),
    ("education.school", r"school|university|college|institution", "fact"),
    ("education.degree", r"degree|qualification|field of study|major", "fact"),
    ("education.year", r"graduation|year (of )?(graduat|complet)", "fact"),
    ("first_name", r"first name|given name|preferred name", "fact"),
    ("last_name", r"last name|family name|surname", "fact"),
    ("name", r"(full |legal )?name", "fact"),
    ("email", r"e-?mail", "fact"),
    ("phone", r"phone|mobile|cell", "fact"),
    ("location", r"location|city|town|postcode|zip|where are you (based|located)|current address|address", "fact"),
    ("resume", r"resume|résumé|\bcv\b", "file"),
]
COMPILED = [(k, re.compile(p, re.I), kind) for k, p, kind in BANK]
STAGES = ("saved", "applied", "screen", "interview", "onsite", "offer", "rejected", "withdrawn")


class ApplyError(Exception):
    pass


def load_json(path, default=None):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return default


def save_json(path, data):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def yes_no(value):
    return None if value is None else ("Yes" if value else "No")


def lookup(profile, key):
    """Value for a bank key from the profile, or None."""
    p = profile or {}
    a = p.get("answers") or {}
    auth = p.get("work_authorization") or {}
    edu = (p.get("education") or [{}])[0] if p.get("education") else {}
    links = p.get("links") or {}
    cur = p.get("current") or {}
    sid = p.get("self_identification") or {}
    name = p.get("name") or ""
    if key.startswith("self_id."):
        return sid.get(key.split(".", 1)[1])
    simple = {
        "sponsorship": yes_no(auth.get("needs_sponsorship")),
        "work_authorization": yes_no(auth.get("authorized")),
        "salary_expectation": a.get("salary_expectation"),
        "start_date": a.get("start_date") or a.get("notice_period"),
        "notice_period": a.get("notice_period") or a.get("start_date"),
        "education.classification": edu.get("classification"),
        "education.a_levels": edu.get("a_levels") or (p.get("school_qualifications") or None),
        "convictions": None,
        "ni_number": None,
        "relocation": a.get("relocation") if a.get("relocation") is not None else yes_no(p.get("relocate")),
        "onsite": a.get("onsite"),
        "how_did_you_hear": a.get("how_did_you_hear"),
        "referral": a.get("referral"),
        "previously_employed": a.get("previously_employed"),
        "why_leaving": a.get("why_leaving"),
        "pronouns": a.get("pronouns"),
        "links.linkedin": links.get("linkedin"),
        "links.github": links.get("github"),
        "links.website": links.get("website"),
        "years_experience": str(p["years_experience"]) if p.get("years_experience") is not None else None,
        "current.company": cur.get("company"),
        "current.title": cur.get("title"),
        "education.school": edu.get("school"),
        "education.degree": edu.get("degree"),
        "education.year": str(edu["year"]) if edu.get("year") else None,
        "first_name": p.get("first_name") or (name.split()[0] if name else None),
        "last_name": name.split()[-1] if len(name.split()) > 1 else None,
        "name": name or None,
        "email": p.get("email"),
        "phone": p.get("phone"),
        "location": p.get("location"),
        "resume": p.get("resume_path"),
        "salary_history": None,
        "why_this_company": a.get("why_this_company"),
        "cover_letter": None,
    }
    return simple.get(key)


def is_uk(profile):
    p = profile or {}
    c = str((p.get("work_authorization") or {}).get("country") or p.get("region") or "").strip().lower()
    return c in ("gb", "uk", "united kingdom", "great britain", "england", "scotland", "wales", "northern ireland")


# ---------------------------------------------------------------- the answer bank

BANK_FILE = "answers.json"
COMPANY_KEYS = ("why_company", "why_role", "what_you_bring")
GENERIC_KEYS = ("why_sector", "why_leaving", "strengths", "weakness", "tell_me_about_yourself", "achievement",
                "career_goals", "salary_expectation")


def bank_path(workspace="hunter"):
    return os.path.join(workspace, BANK_FILE)


def load_bank(workspace="hunter"):
    b = load_json(bank_path(workspace)) or {}
    b.setdefault("generic", {})
    b.setdefault("companies", {})
    return b


def bank_set(bank, key, text, approval, company=None, today=None):
    key = slug(key).replace("-", "_")
    if not (approval or "").strip():
        raise ApplyError("the bank keeps only answers the user approved: pass --approved with their words")
    if key in COMPANY_KEYS and not company:
        raise ApplyError("%s is per company: pass --company" % key)
    entry = {"text": text.strip(), "approved": approval.strip(),
             "updated": (today or _dt.date.today()).isoformat()}
    if company:
        c = bank["companies"].setdefault(slug(company), {"company": company})
        c[key] = entry
    else:
        bank["generic"][key] = entry
    return entry


def bank_get(bank, key, company=None):
    key = slug(key).replace("-", "_")
    if company:
        e = (bank["companies"].get(slug(company)) or {}).get(key)
        if e:
            return e
    return bank["generic"].get(key)


def bank_for_role(bank, role_id):
    """The company entry whose slug the role id starts with (roles are '<company>-<title>')."""
    if not role_id:
        return None
    best = None
    for cslug, entry in bank["companies"].items():
        if role_id == cslug or role_id.startswith(cslug + "-"):
            if best is None or len(cslug) > len(best[0]):
                best = (cslug, entry)
    return best[1] if best else None


FIGURE_RE = re.compile(r"(?<![\w.])[$\u00a3\u20ac]?\d[\d,.]*(?:\s?(?:%|k|m|bn|x|\+))?(?![\w])", re.I)


def figures(text):
    out = set()
    for m in FIGURE_RE.finditer(text or ""):
        f = m.group(0).strip().lower().rstrip(".,").replace(",", "").replace(" ", "")
        if re.fullmatch(r"(19|20)\d\d", f):  # years are dates, not claims
            continue
        out.add(f)
    return out


def bank_check(text, bank, company=None, resume_text=""):
    """Figures in a draft that appear in neither the bank (generic + this company) nor the resume."""
    known = set(figures(resume_text))
    for e in bank["generic"].values():
        known |= figures(e.get("text"))
    if company:
        for k, e in (bank["companies"].get(slug(company)) or {}).items():
            if isinstance(e, dict):
                known |= figures(e.get("text"))
    return sorted(f for f in figures(text) if f not in known)


def classify(label):
    for key, rx, kind in COMPILED:
        if rx.search(label or ""):
            return key, kind
    return None, "unknown"


def pick_option(value, options):
    """Map an answer to one of a select's options, or None."""
    if not options or value is None:
        return value
    v = str(value).strip().lower()
    for o in options:
        if str(o).strip().lower() == v:
            return o
    for o in options:
        ol = str(o).strip().lower()
        if v in ("yes", "no") and (ol.startswith(v + " ") or ol.startswith(v + ",") or ol == v):
            return o
    if v.startswith(("yes", "no")):
        head = "yes" if v.startswith("yes") else "no"
        for o in options:
            if str(o).strip().lower().startswith(head):
                return o
    return None


def answer(question, profile, role_id=None, workspace="hunter", options=None):
    """One form question -> {label, key, answer, status, source, note}."""
    label = question if isinstance(question, str) else question.get("label", "")
    options = options if options is not None else (question.get("options") if isinstance(question, dict) else None)
    required = bool(question.get("required")) if isinstance(question, dict) else False
    key, kind = classify(label)
    out = {"label": label, "key": key, "answer": None, "status": "ask", "source": None, "note": None,
           "required": required}
    if kind == "unknown":
        out["note"] = "not a question the profile answers: ask the user, never guess"
        return out
    value = lookup(profile, key)
    if kind == "sensitive":
        out["status"] = "sensitive"
        if key == "salary_history":
            if is_uk(profile):
                out["note"] = ("salary history: the user's call. UK employers may ask, and you may decline or give "
                               "your expectation instead; leave it blank if the form allows, never estimate it")
            else:
                out["note"] = ("salary history: the user's call. Several US states and cities bar employers from "
                               "asking; leave it blank if the form allows, never estimate it")
        elif key == "convictions":
            out["note"] = ("criminal record or DBS question: only the user answers this, on the form, in their own "
                           "words; it is never saved or filled")
        elif key == "ni_number":
            out["note"] = ("National Insurance number: never stored or filled; employers need it after an offer, "
                           "for payroll, and the user types it in themselves")
        elif value:
            out["answer"], out["source"] = pick_option(value, options) or value, "profile.self_identification"
            out["note"] = "voluntary: filled with the user's own saved choice"
        else:
            out["note"] = "voluntary self-identification: the user answers this themselves (or declines)"
        return out
    if kind == "role":
        cover = os.path.join(workspace, "covers", "%s.md" % role_id) if role_id else None
        out["status"] = "role-specific"
        saved = (bank_for_role(load_bank(workspace), role_id) or {}).get("why_company") if key == "why_this_company" else None
        if saved:
            out["answer"], out["source"] = saved["text"], "%s (approved %s)" % (bank_path(workspace), saved["updated"])
            out["note"] = "the user's approved answer for this company: the cover letter and interview use the same one"
        elif value:
            out["answer"], out["source"] = value, "profile.answers.%s" % key
        elif cover and os.path.isfile(cover):
            out["source"] = cover
            out["note"] = "use the approved cover letter for this role (or its first paragraph)"
        else:
            out["note"] = "write it for this role with /hunter-cover, from the research brief; never generic"
        return out
    if kind == "file":
        out["status"] = "filled" if value else "ask"
        out["answer"], out["source"] = value, "profile.resume_path"
        out["note"] = ("upload the tailored resume for this role if one exists (hunter/resumes/%s.md, exported "
                       "to PDF)" % role_id) if role_id else "the user picks the file in the browser's upload dialog"
        return out
    if value in (None, "") and key in GENERIC_KEYS:
        saved = bank_get(load_bank(workspace), key)
        if saved:
            value = saved["text"]
            out["answer"], out["status"] = value, "filled"
            out["source"] = "%s (approved %s)" % (bank_path(workspace), saved["updated"])
            return out
    if value in (None, ""):
        out["note"] = "missing from hunter/profile.json: ask the user, then save the answer there for next time"
        if key == "salary_expectation":
            out["note"] = ("ask the user: /hunter-salary benchmarks the role from cited sources; a range or "
                           "\"open to discussing\" is fine where the form allows")
        if key == "referral":
            out["note"] = "only a real referral: the name of someone who agreed to refer the user, else leave blank"
        if kind == "optional":
            out["status"] = "skip"
            out["note"] = "optional: leave blank unless the user wants to answer"
        return out
    chosen = pick_option(value, options)
    if options and chosen is None:
        out["note"] = "the saved answer %r matches none of the options %s: ask the user" % (value, options)
        return out
    out["answer"] = chosen if options else value
    out["status"] = "filled"
    out["source"] = "profile.%s" % key
    return out


def fill(questions, profile, role_id=None, workspace="hunter"):
    rows = [answer(q, profile, role_id, workspace) for q in questions]
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"role_id": role_id, "questions": rows, "counts": counts,
            "ready": not any(r["status"] == "ask" for r in rows)}


# ---------------------------------------------------------------- pipeline

def load_pipeline(path):
    data = load_json(path, {}) or {}
    for k in ("roles", "contacts", "threads"):
        data.setdefault(k, [])
    return data


def add_note(rec, when, text):
    """Notes are dated lines, "YYYY-MM-DD: text", the format /hunter-tracker reads."""
    line = "%s: %s" % (when.isoformat(), text)
    rec["notes"] = ((rec.get("notes") or "").rstrip() + "\n" + line).strip()


def find_role(pipe, rid):
    for r in pipe["roles"]:
        if r.get("id") == rid:
            return r
    raise ApplyError("no role %s in the pipeline (add it with /hunter-scout pick or /hunter-tracker)" % rid)


def checklist(pipe, rid, workspace="hunter", profile=None):
    r = find_role(pipe, rid)
    company = r.get("company") or ""
    plan = load_json(os.path.join(workspace, "applications", "%s.json" % rid))
    contacts = {c["id"] for c in pipe["contacts"] if (c.get("company") or "").lower() == company.lower()}
    threads = [t for t in pipe["threads"] if t.get("role_id") == rid or t.get("contact_id") in contacts]
    warm = any(t.get("status") in ("replied", "booked") for t in threads)
    auth = (profile or {}).get("work_authorization") or {}
    app = r.get("application") or {}
    items = [
        ("Resume tailored for this role", os.path.isfile(os.path.join(workspace, "resumes", "%s.md" % rid)),
         "/hunter-resume writes hunter/resumes/%s.md" % rid),
        ("Cover letter written", os.path.isfile(os.path.join(workspace, "covers", "%s.md" % rid)),
         "/hunter-cover writes hunter/covers/%s.md (skip if the form has no place for one)" % rid),
        ("Company researched", os.path.isfile(os.path.join(workspace, "companies", "%s.md" % slug(company))),
         "/hunter-research writes hunter/companies/%s.md" % slug(company)),
        ("Work authorization answered in the profile",
         auth.get("authorized") is not None and auth.get("needs_sponsorship") is not None,
         "set work_authorization in hunter/profile.json"),
        ("Form answers prepared, nothing left to ask", bool(plan) and not any(
            q["status"] == "ask" for q in plan.get("questions", [])),
         "answers.py fill --questions <form.json> --out hunter/applications/%s.json" % rid),
        ("Warm intro (optional): the hiring manager replied or a chat is booked", warm,
         "/hunter-connector"),
        ("Form filled in the browser and saved as a draft, not submitted", app.get("status") == "draft",
         "answers.py draft --role %s --form-url <url>" % rid),
        ("The user reviewed and submitted it themselves", r.get("stage") not in (None, "saved") or
         app.get("status") == "submitted", "answers.py submitted --role %s --confirm \"...\"" % rid),
    ]
    return [{"item": i, "done": bool(d), "how": h} for i, d, h in items]


def record_draft(pipe, rid, form_url, answers_path, today):
    r = find_role(pipe, rid)
    if r.get("stage") not in (None, "saved"):
        raise ApplyError("%s is already at stage %s" % (rid, r.get("stage")))
    r["application"] = {"status": "draft", "saved": today.isoformat(), "form_url": form_url,
                        "answers": answers_path}
    r["next_step"] = "Review the application draft and submit it yourself"
    r["next_date"] = today.isoformat()
    add_note(r, today, "Application filled and saved as a draft, not submitted (%s)" % form_url)
    return r


def record_submitted(pipe, rid, confirm, when):
    r = find_role(pipe, rid)
    if not re.search(r"\b(submitted|sent|applied|done|clicked submit)\b", confirm or "", re.I):
        raise ApplyError("record it only after the user says they submitted it themselves (for example "
                         "\"I submitted it\"); got %r" % confirm)
    app = r.setdefault("application", {})
    app["status"] = "submitted"
    app["confirmed"] = confirm
    r["stage"] = "applied"
    r["applied"] = when.isoformat()
    r["next_step"] = "Follow up if nothing in 7 days (/hunter-followup)"
    r["next_date"] = (when + _dt.timedelta(days=7)).isoformat()
    add_note(r, when, "Application submitted by the user (%s)" % confirm)
    return r


# ---------------------------------------------------------------- CLI

def day(value):
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})$", value or "")
    if not m:
        raise ApplyError("dates are YYYY-MM-DD")
    return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def render_fill(res):
    lines = []
    marks = {"filled": "fill", "ask": "ASK ", "sensitive": "USER", "role-specific": "ROLE", "skip": "skip"}
    for q in res["questions"]:
        ans = q["answer"] if q["answer"] is not None else ""
        lines.append("%s  %-55s %s" % (marks.get(q["status"], q["status"]), q["label"][:55], ans))
        if q["note"]:
            lines.append("      %s" % q["note"])
    c = res["counts"]
    lines.append("%d filled from the profile, %d to ask the user, %d for the user alone, %d written for this role." % (
        c.get("filled", 0), c.get("ask", 0), c.get("sensitive", 0), c.get("role-specific", 0)))
    lines.append("Fill only the 'fill' rows. Stop before Submit: the user reviews and submits.")
    return "\n".join(lines) + "\n"


def _read_text(path):
    if not os.path.isfile(path):
        raise ApplyError("no file at %s" % path)
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def bank_cmd(args):
    bank = load_bank(args.workspace)
    if args.bank_cmd == "set":
        text = args.text if args.text is not None else _read_text(args.text_file)
        e = bank_set(bank, args.key, text, args.approved, args.company, day(args.today) if args.today else None)
        save_json(bank_path(args.workspace), bank)
        print("Saved %s%s (%d words) to %s." % (args.key, " for %s" % args.company if args.company else "",
                                                len(e["text"].split()), bank_path(args.workspace)))
        return 0
    if args.bank_cmd == "get":
        e = bank_get(bank, args.key, args.company)
        if not e:
            print("No approved %s%s yet: draft one with the user, then `bank set`." % (
                args.key, " for %s" % args.company if args.company else ""))
            return 1
        print(e["text"])
        return 0
    if args.bank_cmd == "list":
        for k, e in sorted(bank["generic"].items()):
            print("%-24s %s  %s" % (k, e.get("updated"), " ".join(e.get("text", "").split()[:12])))
        for cslug, c in sorted(bank["companies"].items()):
            for k, e in sorted(c.items()):
                if isinstance(e, dict):
                    print("%-24s %s  %s" % ("%s.%s" % (c.get("company", cslug), k), e.get("updated"),
                                            " ".join(e.get("text", "").split()[:12])))
        if not bank["generic"] and not bank["companies"]:
            print("The answer bank is empty.")
        return 0
    if args.bank_cmd == "check":
        new = bank_check(_read_text(args.draft), bank, args.company,
                         _read_text(args.resume) if args.resume else "")
        if new:
            print("Figures in %s that are in neither the answer bank nor the resume: %s" % (args.draft, ", ".join(new)))
            print("Check each one with the user before the draft goes anywhere.")
            return 1
        print("Every figure in %s is in the answer bank or the resume." % args.draft)
        return 0
    print("bank: set, get, list or check")
    return 2


def main(argv=None):
    ap = argparse.ArgumentParser(description="Application answers, checklist and draft record. Never submits.")
    ap.add_argument("--profile", default="hunter/profile.json")
    ap.add_argument("--pipeline", default="hunter/pipeline.json")
    ap.add_argument("--workspace", default="hunter")
    sub = ap.add_subparsers(dest="cmd")
    a = sub.add_parser("answer", help="one question")
    a.add_argument("--question", required=True)
    a.add_argument("--options", nargs="*")
    a.add_argument("--role")
    f = sub.add_parser("fill", help="every question on a form")
    f.add_argument("--questions", required=True, help="JSON list of the form's questions")
    f.add_argument("--role")
    f.add_argument("--out")
    f.add_argument("--json", action="store_true")
    c = sub.add_parser("checklist")
    c.add_argument("--role", required=True)
    d = sub.add_parser("draft", help="record the filled, unsubmitted application")
    d.add_argument("--role", required=True)
    d.add_argument("--form-url", required=True)
    d.add_argument("--answers")
    d.add_argument("--today")
    s = sub.add_parser("submitted", help="record that the user submitted it themselves")
    s.add_argument("--role", required=True)
    s.add_argument("--confirm", required=True, help="the user's words, for example \"I submitted it\"")
    s.add_argument("--date")
    bk = sub.add_parser("bank", help="the user's approved answers, shared by cover letters, forms and interviews")
    bsub = bk.add_subparsers(dest="bank_cmd")
    b1 = bsub.add_parser("set")
    b1.add_argument("--key", required=True)
    b1.add_argument("--company")
    g = b1.add_mutually_exclusive_group(required=True)
    g.add_argument("--text")
    g.add_argument("--text-file")
    b1.add_argument("--approved", required=True, help="the user's words approving this answer")
    b1.add_argument("--today")
    b2 = bsub.add_parser("get")
    b2.add_argument("--key", required=True)
    b2.add_argument("--company")
    bsub.add_parser("list")
    b4 = bsub.add_parser("check", help="figures in a draft that are in neither the bank nor the resume")
    b4.add_argument("draft")
    b4.add_argument("--company")
    b4.add_argument("--resume")
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2
    profile = load_json(args.profile)
    try:
        if args.cmd == "bank":
            return bank_cmd(args)
        if args.cmd in ("answer", "fill") and profile is None:
            raise ApplyError("no %s: copy skills/hunter-apply/profile.example.json there and fill it in with the "
                             "user's own answers" % args.profile)
        if args.cmd == "answer":
            res = answer(args.question, profile, args.role, args.workspace, args.options)
            print(json.dumps(res, indent=2, ensure_ascii=False))
            return 0 if res["status"] == "filled" else 1
        if args.cmd == "fill":
            questions = load_json(args.questions)
            if not isinstance(questions, list):
                raise ApplyError("%s must be a JSON list of questions" % args.questions)
            res = fill(questions, profile, args.role, args.workspace)
            if args.out:
                save_json(args.out, res)
            if args.json:
                print(json.dumps(res, indent=2, ensure_ascii=False))
            else:
                sys.stdout.write(render_fill(res))
            return 0
        pipe = load_pipeline(args.pipeline)
        if args.cmd == "checklist":
            items = checklist(pipe, args.role, args.workspace, profile)
            for it in items:
                print("[%s] %s%s" % ("x" if it["done"] else " ", it["item"], "" if it["done"] else "  (%s)" % it["how"]))
            return 0 if all(it["done"] for it in items[:5]) else 1
        if args.cmd == "draft":
            today = day(args.today) if args.today else _dt.date.today()
            record_draft(pipe, args.role, args.form_url, args.answers, today)
            save_json(args.pipeline, pipe)
            print("Recorded as a draft, not submitted. The form is filled in the user's browser: they review it "
                  "and click Submit themselves.")
            return 0
        if args.cmd == "submitted":
            when = day(args.date) if args.date else _dt.date.today()
            record_submitted(pipe, args.role, args.confirm, when)
            save_json(args.pipeline, pipe)
            print("Stage: applied on %s. Next: a polite follow-up after 7 days if nothing comes back." % when.isoformat())
            return 0
    except ApplyError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
