#!/usr/bin/env python3
"""
D365 Radar
Scans job sources for Microsoft Dynamics 365 roles in the Netherlands and Belgium,
de-duplicates them, tags them (product, role, contract/perm, agency) and writes
data/jobs.json for the dashboard. Optionally pushes new roles to Teams / Telegram.

Runs on GitHub Actions every 30 minutes. Aggregator APIs (Adzuna, Jooble) are
throttled to every few hours to stay inside their free tiers; direct company
career feeds (Recruitee, Greenhouse, Lever, Workable) run every time.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
DATA_PATH = ROOT / "data" / "jobs.json"
NOW = datetime.now(timezone.utc)
FORCE_ALL = os.getenv("FORCE_ALL", "").lower() in ("1", "true", "yes")

DIAG = {"http": [], "rejected": {}}
SESSION = requests.Session()
SESSION.headers["User-Agent"] = "D365-Radar/1.0 (job monitor)"


# ---------------------------------------------------------------- helpers
def log(*a):
    print(*a, flush=True)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def parse_date(v):
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        v = v / 1000 if v > 1e12 else v
        return datetime.fromtimestamp(v, tz=timezone.utc)
    s = str(v).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:  # RFC 2822, e.g. "Tue, 28 Oct 2026 08:00:00 GMT" (Careerjet)
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if dt:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def strip_html(s):
    if not s:
        return ""
    s = html.unescape(str(s))          # Greenhouse double-escapes its HTML
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _diag(url, kw, status, n=None, note=""):
    p = kw.get("params") or {}
    q = p.get("what_phrase") or p.get("keywords") or (kw.get("json") or {}).get("keywords") or ""
    if p.get("locale_code"):
        q += f" [{p['locale_code']}]"
    clean = url.split("?")[0]
    for k in ("JOOBLE_API_KEY", "JOOBLE_KEY_NL", "JOOBLE_KEY_BE"):
        v = (os.getenv(k) or "").strip()
        if v:
            clean = clean.replace(v, "KEY")
    DIAG["http"].append({"url": clean,
                         "query": q, "status": status, "results": n, "note": note[:160]})


def http_json(method, url, **kw):
    for attempt in range(3):
        try:
            r = SESSION.request(method, url, timeout=25, **kw)
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code >= 400:
                log(f"  ! HTTP {r.status_code} on {url.split('?')[0]}")
                _diag(url, kw, r.status_code, note=r.text)
                return None
            data = r.json()
            n = len(data.get("results", data.get("jobs", data.get("offers", []))) if isinstance(data, dict) else data)
            _diag(url, kw, r.status_code, n)
            return data
        except (requests.RequestException, ValueError) as e:
            log(f"  ! {type(e).__name__} on {url.split('?')[0]}")
            _diag(url, kw, "error", note=type(e).__name__)
            time.sleep(2)
    return None


# ---------------------------------------------------------- classification
RELEVANT_CORE = re.compile(
    r"dynamics\s*365|\bd365\b|microsoft\s+dynamics|dynamics\s+(crm|ax|nav|gp)\b|"
    r"business\s+central|navision|\bms\s?crm\b", re.I)
RELEVANT_PP = re.compile(
    r"power\s*platform|power\s*apps|power\s*automate|dataverse|power\s*pages|copilot\s+studio", re.I)

PRODUCT_RULES = [
    ("F&O", re.compile(
        r"finance\s*(&|and|en)\s*operations|\bf\s?&\s?o\b|\bfin\s?ops\b|\bd365\s?(fo|f&o|scm)\b|"
        r"dynamics\s+ax\b|\bax\s?20(09|12)\b|supply\s+chain\s+management|\bx\+\+|"
        r"(dynamics\s*365|\bd365)\s+(finance|supply\s+chain|scm|commerce|project\s+operations)\b", re.I)),
    ("CE", re.compile(
        r"customer\s+engagement|dynamics\s+crm|(dynamics\s*365|\bd365)\s*\(?ce\b|\bms\s?crm\b|\bcrm\b|"
        r"(dynamics\s*365|\bd365)\s+(sales|customer\s+service|field\s+service|marketing|customer\s+insights|contact\s+center)|\(ce\)", re.I)),
    ("BC", re.compile(r"business\s+central|navision|dynamics\s+nav\b|\bd365\s?bc\b|\bal\s+develop", re.I)),
    ("Power Platform", RELEVANT_PP),
]

ROLE_RULES = [
    ("Architect", re.compile(r"architect|solution\s+lead|solutions?\s+design", re.I)),
    ("Technical Consultant", re.compile(r"technical\s+consultant|technisch\s+consultant|consultant\s+technique", re.I)),
    ("Developer", re.compile(r"develop|ontwikkelaar|engineer|programmeur|d[ée]veloppeur|\bx\+\+", re.I)),
    ("Functional Consultant", re.compile(
        r"functional|functioneel|fonctionnel|consultant|analyst|analist|application\s+specialist|key\s*user", re.I)),
    ("Project / Delivery", re.compile(
        r"project|programme|program\s+manager|delivery|scrum|product\s+owner|engagement\s+manager|"
        r"practice\s+(lead|manager)|team\s*lead|head\s+of", re.I)),
    ("Support / Admin", re.compile(r"support|beheer|administrator|\badmin\b|application\s+manager", re.I)),
]

EXCLUDE_RX = re.compile(
    r"afstudeer|stagiair|\bstage\b|meewerkstage|internship|\bintern\b|werkstudent|working\s+student|"
    r"bijbaan|student\s+job|stagiaire|alternance|\bjobstudent", re.I)
PERM_RX = re.compile(
    r"vast\s+(contract|dienstverband)|vaste\s+baan|\bpermanent\b|onbepaalde\s+tijd|\bCDI\b|"
    r"durée\s+indéterminée|in\s+loondienst", re.I)
CONTRACT_RX = re.compile(
    r"freelan|\bzzp|\binterim\b|\bcontractor\b|\bcontract\s+(role|position|opportunity|assignment)\b|"
    r"\bopdracht\b|ind[ée]pendant|zelfstandige|day\s*rate|dagtarief|uurtarief|\bb2b\b", re.I)

NL_RX = re.compile(
    r"netherlands|nederland|holland|amsterdam|rotterdam|utrecht|den haag|the hague|'s-hertogenbosch|"
    r"den bosch|eindhoven|groningen|tilburg|breda|nijmegen|arnhem|zwolle|amersfoort|apeldoorn|haarlem|"
    r"leiden|enschede|maastricht|almere|delft|hilversum|veenendaal|zoetermeer|schiphol|hoofddorp|"
    r"nieuwegein|houten|woerden|gouda|ede\b|venlo|helmond|deventer|leeuwarden|alkmaar|amstelveen|"
    r"capelle|barneveld|drachten|dordrecht|\bnoord-|\bzuid-|gelderland|brabant|limburg \(nl\)", re.I)
BE_RX = re.compile(
    r"belgium|belgi[eë]|belgique|brussel|brussels|bruxelles|antwerpen|antwerp|anvers|\bgent\b|ghent|gand|"
    r"leuven|louvain|mechelen|li[eè]ge|luik|namur|namen|hasselt|brugge|bruges|kortrijk|charleroi|wavre|"
    r"zaventem|diegem|\bmons\b|aalst|genk|vlaanderen|flanders|wallonie|wallonia|roeselare|sint-niklaas", re.I)
OTHER_RX = re.compile(
    r"germany|deutschland|united kingdom|\buk\b|england|london|france|paris|spain|madrid|poland|warsaw|"
    r"india|united states|\busa\b|denmark|sweden|norway|finland|switzerland|austria|italy|portugal|"
    r"luxembourg|ireland|dublin|berlin|munich|frankfurt|copenhagen|stockholm|oslo|helsinki", re.I)

COUNTRY_NAMES = {
    "netherlands": "NL", "nederland": "NL", "the netherlands": "NL", "holland": "NL",
    "belgium": "BE", "belgië": "BE", "belgie": "BE", "belgique": "BE",
}


def detect_country(location, code=None, default=None):
    loc = location or ""
    if NL_RX.search(loc):
        return "NL"
    if BE_RX.search(loc):
        return "BE"
    if code:
        c = str(code).strip()
        c = COUNTRY_NAMES.get(c.lower(), c.upper())
        if len(c) == 2:
            return c
    if OTHER_RX.search(loc):
        return None
    return default


def tag_products(title, desc):
    tags = [name for name, rx in PRODUCT_RULES if rx.search(title)]
    if not tags:
        tags = [name for name, rx in PRODUCT_RULES if name in ("F&O", "BC") and rx.search(desc)]
        if not tags and re.search(r"customer\s+engagement|dynamics\s+crm|(dynamics\s*365|\bd365)\s*\(?ce\b", desc, re.I):
            tags = ["CE"]
    return tags or ["D365 general"]


def role_family(title):
    for name, rx in ROLE_RULES:
        if rx.search(title):
            return name
    return "Other"


def contract_type(title, text, hint=None):
    h = (hint or "").lower()
    if any(k in h for k in ("contract", "freelance", "temporary", "interim")):
        return "Contract"
    if "permanent" in h:
        return "Perm"
    if CONTRACT_RX.search(title):
        return "Contract"
    if PERM_RX.search(text):
        return "Perm"
    if CONTRACT_RX.search(text):
        return "Contract"
    return "Unspecified"


def is_agency(company):
    c = (company or "").lower()
    return any(k in c for k in CONFIG.get("agency_keywords", []))


def norm_key(company, title, country):
    c = company.lower()
    c = re.sub(r"\b(b\.?v\.?|n\.?v\.?|srl|bvba|sa|ltd|gmbh|inc|group|groep|nederland|netherlands|belgium|belgi[eë])\b", "", c)
    c = re.sub(r"[^a-z0-9]", "", c)
    t = re.sub(r"\(.*?\)|\[.*?\]", "", title.lower())
    t = re.sub(r"\b(m/v/x|m/v|v/m|m/f/x|m/f|h/f/x|h/f|f/h|m/w/d|f/m/x)\b", "", t)
    t = re.sub(r"[^a-z0-9]", "", t)
    return f"{c}|{t}|{country}"


def _reject(reason, sample):
    r = DIAG["rejected"].setdefault(reason, {"count": 0, "samples": []})
    r["count"] += 1
    if len(r["samples"]) < 5:
        r["samples"].append(sample[:120])
    return None


def make_job(source, source_id, title, company, url, location, posted, description,
             country_code=None, default_country=None, contract_hint=None, direct=False, origin=None):
    title = strip_html(title)
    company = strip_html(company) or "Unknown"
    desc = strip_html(description)
    if not title or not url:
        return None

    in_title = RELEVANT_CORE.search(title) or (CONFIG.get("include_power_platform", True) and RELEVANT_PP.search(title))
    in_desc = RELEVANT_CORE.search(desc) or (CONFIG.get("include_power_platform", True) and RELEVANT_PP.search(desc))
    family = role_family(title)
    # Job-board roles must name D365 in the title (descriptions are noisy).
    # Career-page roles can match on description if the title looks like a D365-type role.
    if not in_title and not (direct and in_desc and family != "Other"):
        return _reject("not D365", title)
    if EXCLUDE_RX.search(title):
        return _reject("internship / student", title)

    country = detect_country(location, country_code, default_country)
    if country not in CONFIG["countries"]:
        return _reject(f"country {country}", f"{title} ({location})")

    posted_dt = parse_date(posted)
    if posted_dt and posted_dt > NOW + timedelta(hours=2):
        posted_dt = None

    key = norm_key(company, title, country)
    return {
        "id": hashlib.sha1(key.encode()).hexdigest()[:12],
        "key": key,
        "company": company,
        "title": title,
        "url": url,
        "location": strip_html(location) or country,
        "country": country,
        "posted_at": iso(posted_dt),
        "products": tag_products(title, desc),
        "role": family,
        "contract": contract_type(title, f"{title} {desc}", contract_hint),
        "agency": is_agency(company),
        "direct": direct,
        "sources": [source],
        "origins": [origin or source],
        "snippet": desc[:300],
    }


# ------------------------------------------------------------ API budget
ADZ = CONFIG["adzuna"]


def usage(store):
    u = store.setdefault("api_usage", {})
    month, day = NOW.strftime("%Y-%m"), NOW.strftime("%Y-%m-%d")
    if u.get("month") != month:
        u.update(month=month, month_calls=0)
    if u.get("day") != day:
        u.update(day=day, day_calls=0)
    return u


def adzuna_budget_ok(store, n=1):
    u = usage(store)
    return (u["month_calls"] + n <= ADZ.get("monthly_budget", 2400)
            and u["day_calls"] + n <= ADZ.get("daily_budget", 240))


def adzuna_on_pace(store):
    """Incremental checks only run if this month's usage is on track for the budget."""
    u = usage(store)
    days_in_month = ((NOW.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)).day
    allowed = ADZ.get("monthly_budget", 2400) * NOW.day / days_in_month + 60
    return u["month_calls"] <= allowed


def adzuna_call(store, cc, page, query, days):
    if not adzuna_budget_ok(store):
        log("  ! Adzuna budget reached, stopping early")
        return None, False
    u = usage(store)
    u["month_calls"] += 1
    u["day_calls"] += 1
    time.sleep(2.6)  # stay under 25 calls/minute
    data = http_json("GET", f"https://api.adzuna.com/v1/api/jobs/{cc.lower()}/search/{page}", params={
        "app_id": (os.getenv("ADZUNA_APP_ID") or "").strip(), "app_key": (os.getenv("ADZUNA_APP_KEY") or "").strip(),
        "what_phrase": query, "results_per_page": 50, "sort_by": "date", "max_days_old": days,
        "content-type": "application/json"})
    return data, data is not None


def adzuna_jobs(data, cc):
    out = []
    for j in (data or {}).get("results", []):
        job = make_job(
            "Adzuna", j.get("id"), j.get("title"), (j.get("company") or {}).get("display_name"),
            j.get("redirect_url"), (j.get("location") or {}).get("display_name"), j.get("created"),
            j.get("description"), country_code=cc, contract_hint=j.get("contract_type"), origin="adzuna")
        if job:
            out.append(job)
    return out


def fetch_adzuna(store, full):
    """full=True: every page of the last N days (lets us detect removed roles).
    full=False: newest page only, for fast pickup of new roles between sweeps."""
    if not (os.getenv("ADZUNA_APP_ID") and os.getenv("ADZUNA_APP_KEY")):
        log("Adzuna: no API keys set, skipping")
        return None, False
    days = ADZ.get("lookback_days", 90) if full else ADZ.get("incremental_days", 3)
    out, complete, calls_before = [], True, usage(store)["month_calls"]
    queries = ADZ["queries"] if full else ADZ.get("incremental_queries", ADZ["queries"])
    for cc in CONFIG["countries"]:
        for q in queries:
            page = 1
            while True:
                data, ok = adzuna_call(store, cc, page, q, days)
                if not ok:
                    complete = False
                    break
                out += adzuna_jobs(data, cc)
                total, got = int(data.get("count") or 0), len(data.get("results") or [])
                if not full or got < 50 or page * 50 >= total:
                    break
                if page >= ADZ.get("max_pages", 20):
                    complete = False  # too many results to verify; don't close anything
                    break
                page += 1
    log(f"  Adzuna {'full sweep' if full else 'new-role check'}: {usage(store)['month_calls'] - calls_before} calls, "
        f"{usage(store)['month_calls']} used this month")
    return out, complete


def jooble_key(cc):
    k = os.getenv(f"JOOBLE_KEY_{cc}") or os.getenv("JOOBLE_API_KEY")   # one general key covers both countries
    return (k or "").strip()


def fetch_jooble():
    """Returns (jobs, {country: complete}). Pages through every result so missing roles can be detected."""
    cfg = CONFIG["jooble"]
    countries = [cc for cc in CONFIG["countries"] if jooble_key(cc)]
    if not countries:
        log("Jooble: no API keys set, skipping")
        return None, False
    out, done = [], {}
    log(f"  Jooble keys found for: {', '.join(countries)}")
    for cc in countries:
        key, complete = jooble_key(cc), True
        hosts = [cfg.get("hosts", {}).get(cc), "jooble.org"]
        host = next(h for h in hosts if h)
        for q in cfg["queries"]:
            page, fetched = 1, 0
            while True:
                # country sites search the whole country; the main site needs the country name
                loc = cfg["locations"].get(cc, "") if host == "jooble.org" else ""
                body = {"keywords": q, "location": loc, "page": str(page), "ResultOnPage": cfg.get("per_page", 100)}
                data = http_json("POST", f"https://{host}/api/{key}", json=body)
                if data is None and host != "jooble.org" and page == 1:
                    host = "jooble.org"   # some keys only work on the main endpoint
                    body["location"] = cfg["locations"].get(cc, "")
                    data = http_json("POST", f"https://{host}/api/{key}", json=body)
                if data is None:
                    complete = False
                    break
                jobs = data.get("jobs") or []
                fetched += len(jobs)
                if page == 1:
                    DIAG.setdefault("jooble_totals", {})[f"{cc} {q}"] = data.get("totalCount")
                for r in jobs:
                    job = make_job("Jooble", r.get("id"), r.get("title"), r.get("company"), r.get("link"),
                                   r.get("location"), r.get("updated"), r.get("snippet"),
                                   default_country=cc, contract_hint=r.get("type"), origin=f"jooble:{cc}")
                    if job:
                        out.append(job)
                if not jobs or fetched >= int(data.get("totalCount") or 0):
                    break
                if page >= cfg.get("max_pages", 10):
                    break   # Jooble matches loosely; relevant roles rank in the top pages
                page += 1
                time.sleep(0.5)
        done[cc] = complete
        if not complete:
            log(f"  Jooble {cc}: incomplete (key may not be valid for this country)")
    return out, done


# ------------------------------------------------------------- Careerjet
_PUBLIC_IP = None


def public_ip():
    """Careerjet requires the caller's IP; use the runner's public address."""
    global _PUBLIC_IP
    if _PUBLIC_IP is None:
        try:
            _PUBLIC_IP = SESSION.get("https://api.ipify.org", timeout=10).text.strip()
        except requests.RequestException:
            _PUBLIC_IP = "127.0.0.1"
    return _PUBLIC_IP


def fetch_careerjet():
    """Returns (jobs, complete)."""
    key = (os.getenv("CAREERJET_API_KEY") or "").strip()
    if not key:
        log("Careerjet: no API key set, skipping")
        return None, False
    cfg, out, complete = CONFIG["careerjet"], [], True
    for cc in CONFIG["countries"]:
        locales = list(cfg["locales"].get(cc, []))
        for q in cfg["queries"]:
            page = 1
            while True:
                data = http_json("GET", "https://search.api.careerjet.net/v4/query", auth=(key, ""), params={
                    "locale_code": locales[0], "keywords": q, "page": page, "page_size": 100, "sort": "date",
                    "user_ip": public_ip(), "user_agent": SESSION.headers["User-Agent"]})
                if data is None and not out and cc == CONFIG["countries"][0] and q == cfg["queries"][0] and page == 1:
                    log("  Careerjet rejected the first request (key or IP not authorised), skipping")
                    return None, False
                if data is None and page == 1 and len(locales) > 1:
                    locales.pop(0)   # e.g. nl_BE not supported, fall back to fr_BE
                    continue
                if data is None:
                    complete = False
                    break
                if data.get("type") not in (None, "JOBS"):   # e.g. ambiguous location: nothing to page
                    break
                for r in data.get("jobs") or []:
                    job = make_job("Careerjet", r.get("url"), r.get("title"), r.get("company"), r.get("url"),
                                   r.get("locations"), r.get("date"), r.get("description"),
                                   default_country=cc, origin="careerjet")
                    if job:
                        out.append(job)
                if page >= int(data.get("pages") or 1):
                    break
                if page >= 10:
                    complete = False
                    break
                page += 1
                time.sleep(0.5)
    return out, complete


def fetch_ats(t):
    """Returns (jobs, ok). A careers feed lists every open role, so each successful fetch is a full sweep."""
    typ, slug = t.get("type", "").lower(), t.get("slug", "")
    comp, dflt = t.get("company") or slug, t.get("default_country")
    origin = f"ats:{typ}:{slug}"
    out, data = [], None
    kw = dict(default_country=dflt, direct=True, origin=origin)
    if typ == "recruitee":
        data = http_json("GET", f"https://{slug}.recruitee.com/api/offers/")
        for j in (data or {}).get("offers", []):
            out.append(make_job(
                "Careers page", j.get("id"), j.get("title"), j.get("company_name") or comp, j.get("careers_url"),
                j.get("location") or j.get("city"), j.get("published_at") or j.get("created_at"),
                f"{j.get('description', '')} {j.get('requirements', '')}", country_code=j.get("country_code"),
                contract_hint=j.get("employment_type_code"), **kw))
    elif typ == "greenhouse":
        host = "boards-api.eu.greenhouse.io" if t.get("region") == "eu" else "boards-api.greenhouse.io"
        data = http_json("GET", f"https://{host}/v1/boards/{slug}/jobs", params={"content": "true"})
        for j in (data or {}).get("jobs", []):
            out.append(make_job(
                "Careers page", j.get("id"), j.get("title"), comp, j.get("absolute_url"),
                (j.get("location") or {}).get("name"), j.get("first_published") or j.get("updated_at"),
                j.get("content"), **kw))
    elif typ == "lever":
        host = "api.eu.lever.co" if t.get("region") == "eu" else "api.lever.co"
        data = http_json("GET", f"https://{host}/v0/postings/{slug}", params={"mode": "json"})
        for j in data or []:
            cat = j.get("categories") or {}
            out.append(make_job(
                "Careers page", j.get("id"), j.get("text"), comp, j.get("hostedUrl"), cat.get("location"),
                j.get("createdAt"), j.get("descriptionPlain"), contract_hint=cat.get("commitment"), **kw))
    elif typ == "workable":
        data = http_json("GET", f"https://apply.workable.com/api/v1/widget/accounts/{slug}", params={"details": "true"})
        for j in (data or {}).get("jobs", []):
            out.append(make_job(
                "Careers page", j.get("shortcode"), j.get("title"), (data or {}).get("name") or comp,
                j.get("url") or j.get("application_url"), f"{j.get('city', '')}, {j.get('country', '')}",
                j.get("published_on") or j.get("created_at"), j.get("description"),
                country_code=j.get("country"), contract_hint=j.get("employment_type"), **kw))
    elif typ == "smartrecruiters":
        data = http_json("GET", f"https://api.smartrecruiters.com/v1/companies/{slug}/postings", params={"limit": 100})
        for j in (data or {}).get("content", []):
            loc = j.get("location") or {}
            out.append(make_job(
                "Careers page", j.get("id"), j.get("name"), (j.get("company") or {}).get("name") or comp,
                f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}", f"{loc.get('city', '')}, {loc.get('country', '')}",
                j.get("releasedDate"), (j.get("function") or {}).get("label", ""),
                country_code=(loc.get("country") or "").upper() or None,
                contract_hint=(j.get("typeOfEmployment") or {}).get("label"), **kw))
    elif typ == "personio":
        import xml.etree.ElementTree as ET
        tld = t.get("tld", "de")
        try:
            r = SESSION.get(f"https://{slug}.jobs.personio.{tld}/xml", timeout=25)
            root = ET.fromstring(r.content) if r.status_code == 200 else None
        except (requests.RequestException, ET.ParseError):
            root = None
        _diag(f"https://{slug}.jobs.personio.{tld}/xml", {}, 200 if root is not None else "error",
              len(root.findall("position")) if root is not None else None)
        data = root
        for pos in (root.findall("position") if root is not None else []):
            txt = lambda tag: (pos.findtext(tag) or "").strip()
            desc = " ".join((d.findtext("value") or "") for d in pos.iter("jobDescription"))
            out.append(make_job(
                "Careers page", txt("id"), txt("name"), txt("subcompany") or comp,
                f"https://{slug}.jobs.personio.{tld}/job/{txt('id')}", txt("office"), txt("createdAt"), desc,
                contract_hint=txt("employmentType"), **kw))
    else:
        log(f"  ! unknown ATS type '{typ}' for {comp}")
    return [j for j in out if j], data is not None


def ats_targets():
    targets = [t for t in CONFIG.get("ats_targets", []) if t.get("enabled", True) and t.get("slug")]
    extra = os.getenv("ATS_TARGETS")  # optional secret, keeps your target-account list private
    if extra:
        try:
            targets += [t for t in json.loads(extra) if t.get("enabled", True)]
        except json.JSONDecodeError:
            log("  ! ATS_TARGETS secret is not valid JSON, ignoring it")
    return targets


# ------------------------------------------------------------------ store
SWEEPABLE = ("adzuna", "ats:", "jooble", "careerjet")   # origins whose full listings we can re-check


def load_store():
    try:
        s = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    for k, v in (("jobs", []), ("last_runs", {}), ("source_stats", {})):
        s.setdefault(k, v)
    for j in s["jobs"]:  # upgrade records from older versions
        if "jooble" in j.get("origins", []):
            new = f"jooble:{j['country']}"
            j["origins"] = [new if o == "jooble" else o for o in j["origins"]]
            if "jooble" in j.get("miss", {}):
                j["miss"][new] = j["miss"].pop("jooble")
        j.setdefault("status", "open")
        j.setdefault("origins", ["adzuna" if "Adzuna" in j.get("sources", []) else "jooble"])
        j.setdefault("miss", {})
    return s


def merge(store, fresh):
    by_key = {j["key"]: j for j in store["jobs"]}
    new, now = [], iso(NOW)
    for j in fresh:
        ex = by_key.get(j["key"])
        if ex:
            if ex["status"] == "closed":          # re-listed
                ex["status"], ex["closed_at"] = "open", None
                ex["relisted"] = True
            ex["last_seen"] = now
            for s in j["sources"]:
                if s not in ex["sources"]:
                    ex["sources"].append(s)
            for o in j["origins"]:
                if o not in ex["origins"]:
                    ex["origins"].append(o)
                ex["miss"][o] = 0
            if j["direct"] and not ex.get("direct"):
                ex["url"], ex["direct"] = j["url"], True
            if j["posted_at"] and (not ex.get("posted_at") or j["posted_at"] < ex["posted_at"]):
                ex["posted_at"] = j["posted_at"]
            if ex.get("contract") == "Unspecified" and j["contract"] != "Unspecified":
                ex["contract"] = j["contract"]
        else:
            j.update(first_seen=now, last_seen=now, status="open", miss={o: 0 for o in j["origins"]})
            by_key[j["key"]] = j
            new.append(j)
    store["jobs"] = list(by_key.values())
    return new


def apply_sweeps(store, sweeps):
    """sweeps: {origin: set(keys seen in a complete listing)}. A role missing from a complete
    listing gets a strike; after N strikes on every sweepable source it is marked closed."""
    default_need = CONFIG.get("close_after_missed_sweeps", 2)
    per_source = CONFIG.get("close_after_missed_sweeps_by_source", {})
    need_for = lambda o: next((n for prefix, n in per_source.items() if o.startswith(prefix)), default_need)
    closed = 0
    for j in store["jobs"]:
        if j["status"] != "open":
            continue
        for origin, seen in sweeps.items():
            if origin in j["origins"] and j["key"] not in seen:
                j["miss"][origin] = j["miss"].get(origin, 0) + 1
        checkable = [o for o in j["origins"] if o.startswith(SWEEPABLE)]
        if checkable and all(j["miss"].get(o, 0) >= need_for(o) for o in checkable):
            j["status"], j["closed_at"] = "closed", iso(NOW)
            closed += 1
    return closed


def prune(store):
    """Drop roles older than the lookback window, and closed roles after a while."""
    max_age = timedelta(days=ADZ.get("lookback_days", 90))
    keep_closed = timedelta(days=CONFIG.get("closed_keep_days", 30))
    kept = []
    for j in store["jobs"]:
        born = parse_date(j.get("posted_at")) or parse_date(j["first_seen"])
        # Old roles still live on an employer's own careers feed are kept: they are hard-to-fill leads.
        live_direct = j["status"] == "open" and any(o.startswith("ats:") for o in j.get("origins", []))
        if NOW - born > max_age + timedelta(days=1) and not live_direct:
            continue
        if j["status"] == "closed" and NOW - parse_date(j["closed_at"]) > keep_closed:
            continue
        kept.append(j)
    kept.sort(key=lambda j: j.get("posted_at") or j["first_seen"], reverse=True)
    store["jobs"] = kept


# ----------------------------------------------------------------- alerts
def send_alerts(new_jobs):
    cfg = CONFIG.get("alerts", {})
    window = NOW - timedelta(days=cfg.get("only_posted_within_days", 3))
    items = [j for j in new_jobs
             if (cfg.get("include_agencies") or not j["agency"])
             and (parse_date(j.get("posted_at")) or NOW) >= window]
    if not items:
        return
    items = items[: cfg.get("max_items", 15)]
    dash = os.getenv("DASHBOARD_URL", "")
    head = f"{len(items)} new D365 role{'s' if len(items) != 1 else ''} in NL/BE"

    teams = os.getenv("TEAMS_WEBHOOK_URL")
    if teams:
        body = [{"type": "TextBlock", "text": head, "weight": "Bolder", "size": "Medium", "wrap": True}]
        for j in items:
            body.append({"type": "TextBlock", "wrap": True, "spacing": "Small",
                         "text": f"**[{j['title']}]({j['url']})**  \n{j['company']} · {j['location']} · "
                                 f"{', '.join(j['products'])} · {j['contract']}"})
        card = {"type": "AdaptiveCard", "version": "1.4", "body": body,
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json"}
        if dash:
            card["actions"] = [{"type": "Action.OpenUrl", "title": "Open dashboard", "url": dash}]
        try:
            r = SESSION.post(teams, json={"type": "message", "attachments": [
                {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]}, timeout=20)
            log(f"Teams alert: HTTP {r.status_code}")
        except requests.RequestException as e:
            log(f"  ! Teams alert failed: {type(e).__name__}")

    tg_token, tg_chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        esc = lambda s: html.escape(str(s))
        lines = [f"<b>{esc(head)}</b>"]
        for j in items:
            lines.append(f'• <a href="{esc(j["url"])}">{esc(j["title"])}</a>\n  {esc(j["company"])} · '
                         f'{esc(j["location"])} · {esc(", ".join(j["products"]))} · {esc(j["contract"])}')
        if dash:
            lines.append(f'<a href="{esc(dash)}">Open dashboard</a>')
        try:
            r = SESSION.post(f"https://api.telegram.org/bot{tg_token}/sendMessage", timeout=20, json={
                "chat_id": tg_chat, "text": "\n".join(lines)[:4000], "parse_mode": "HTML",
                "disable_web_page_preview": True})
            log(f"Telegram alert: HTTP {r.status_code}")
        except requests.RequestException as e:
            log(f"  ! Telegram alert failed: {type(e).__name__}")


# ------------------------------------------------------------------- main
def due(store, name, every_hours):
    last = parse_date(store.get("last_runs", {}).get(name))
    return not last or NOW - last >= timedelta(hours=every_hours) - timedelta(minutes=10)


def main():
    store = load_store()
    first_run = not store["jobs"]
    fresh, sweeps = [], {}

    def record(name, res, complete=None):
        store["last_runs"][name] = iso(NOW)
        store["source_stats"][name] = {"matched": len(res), "at": iso(NOW),
                                       **({"complete": complete} if complete is not None else {})}
        log(f"  {len(res)} relevant roles")

    # Adzuna: one full sweep a day (backfill + removal check), quick new-role checks in between
    if ADZ.get("enabled", True):
        full_due = due(store, "Adzuna full sweep", ADZ.get("sweep_every_hours", 24))
        inc_due = FORCE_ALL or due(store, "Adzuna new-role check", ADZ.get("incremental_every_hours", 6))
        mode = "full" if full_due else ("new" if inc_due and adzuna_on_pace(store) else None)
        if mode:
            log(f"Adzuna: {'full 3-month sweep' if mode == 'full' else 'new-role check'}")
            try:
                res, complete = fetch_adzuna(store, mode == "full")
            except Exception as e:  # one broken source must never kill the run
                log(f"  ! Adzuna failed: {type(e).__name__}: {e}")
                res, complete = None, False
            if res is not None:
                fresh.extend(res)
                name = "Adzuna full sweep" if mode == "full" else "Adzuna new-role check"
                if mode == "full" and complete:
                    sweeps["adzuna"] = {j["key"] for j in res}
                    record(name, res, True)
                elif mode == "full":
                    log("  sweep incomplete: no roles will be closed this time")
                    record(name, res, False)  # still wait a day before the next sweep
                else:
                    record(name, res)
        else:
            log("Adzuna: not due yet")

    for name, fn, cfg_key in (("Jooble", fetch_jooble, "jooble"), ("Careerjet", fetch_careerjet, "careerjet")):
        cfg = CONFIG.get(cfg_key, {})
        if not cfg.get("enabled", True) or not (FORCE_ALL or due(store, name, cfg.get("every_hours", 6))):
            continue
        log(f"{name}: scanning")
        try:
            res, complete = fn()
        except Exception as e:  # one broken source must never kill the run
            log(f"  ! {name} failed: {type(e).__name__}: {e}")
            continue
        if res is None:
            continue
        fresh.extend(res)
        if isinstance(complete, dict):   # per-country completeness (Jooble)
            for cc, ok in complete.items():
                if ok:
                    sweeps[f"{cfg_key}:{cc}"] = {j["key"] for j in res if j["country"] == cc}
            complete = all(complete.values()) if complete else False
        elif complete:
            sweeps[cfg_key] = {j["key"] for j in res}
        record(name, res, complete)

    for t in ats_targets():
        name = f"Careers: {t.get('company') or t['slug']}"
        log(f"{name}: scanning")
        try:
            res, ok = fetch_ats(t)
        except Exception as e:
            log(f"  ! {name} failed: {type(e).__name__}: {e}")
            continue
        if ok:
            fresh.extend(res)
            sweeps[f"ats:{t.get('type', '').lower()}:{t['slug']}"] = {j["key"] for j in res}
            record(name, res, True)

    new = merge(store, fresh)
    closed = apply_sweeps(store, sweeps)
    prune(store)
    for j in store["jobs"]:  # pick up edits to the agency list for roles already tracked
        j["agency"] = is_agency(j["company"])

    store["generated_at"] = iso(NOW)
    store["close_after_missed_sweeps"] = CONFIG.get("close_after_missed_sweeps", 2)
    store["diagnostics"] = {"at": iso(NOW), "raw_fetched": len(fresh), "swept": sorted(sweeps), **DIAG}
    DATA_PATH.parent.mkdir(exist_ok=True)
    DATA_PATH.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
    open_n = sum(j["status"] == "open" for j in store["jobs"])
    log(f"Done: {len(new)} new, {closed} closed, {open_n} open roles tracked")

    if first_run:
        log("First run: skipping alerts so you are not flooded with the backlog")
    else:
        send_alerts(new)


if __name__ == "__main__":
    sys.exit(main())
