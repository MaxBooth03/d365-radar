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
    q = (kw.get("params") or {}).get("what_phrase") or (kw.get("json") or {}).get("keywords") or ""
    DIAG["http"].append({"url": url.split("?")[0].replace(os.getenv("JOOBLE_API_KEY") or "\0", "KEY"),
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
        r"dynamics\s*365\s+(sales|customer\s+service|field\s+service|marketing|customer\s+insights|contact\s+center)", re.I)),
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
        tags = [name for name, rx in PRODUCT_RULES if name != "CE" and rx.search(desc)]
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
             country_code=None, default_country=None, contract_hint=None, direct=False):
    title = strip_html(title)
    company = strip_html(company) or "Unknown"
    desc = strip_html(description)
    if not title or not url:
        return None

    in_title = RELEVANT_CORE.search(title) or (CONFIG.get("include_power_platform", True) and RELEVANT_PP.search(title))
    in_desc = RELEVANT_CORE.search(desc) or (CONFIG.get("include_power_platform", True) and RELEVANT_PP.search(desc))
    family = role_family(title)
    # Description-only matches must at least look like a D365-type role (drops "Office Manager at a D365 partner")
    if not in_title and not (in_desc and family != "Other"):
        return _reject("not D365", title)

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
        "snippet": desc[:300],
    }


# ----------------------------------------------------------------- sources
def due(store, name, every_hours):
    if FORCE_ALL:
        return True
    last = parse_date(store.get("last_runs", {}).get(name))
    return not last or NOW - last >= timedelta(hours=every_hours) - timedelta(minutes=10)


def fetch_adzuna():
    aid, akey = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")
    if not (aid and akey):
        log("Adzuna: no API keys set, skipping")
        return None
    cfg, out = CONFIG["adzuna"], []
    for cc in CONFIG["countries"]:
        for q in cfg["queries"]:
            data = http_json("GET", f"https://api.adzuna.com/v1/api/jobs/{cc.lower()}/search/1", params={
                "app_id": aid, "app_key": akey, "what_phrase": q, "results_per_page": 50,
                "sort_by": "date", "max_days_old": cfg.get("max_days_old", 7),
                "content-type": "application/json"})
            for j in (data or {}).get("results", []):
                job = make_job(
                    "Adzuna", j.get("id"), j.get("title"), (j.get("company") or {}).get("display_name"),
                    j.get("redirect_url"), (j.get("location") or {}).get("display_name"), j.get("created"),
                    j.get("description"), country_code=cc, contract_hint=j.get("contract_type"))
                if job:
                    out.append(job)
            time.sleep(2.6)  # stay under 25 calls/minute
    return out


def fetch_jooble():
    key = os.getenv("JOOBLE_API_KEY")
    if not key:
        log("Jooble: no API key set, skipping")
        return None
    cfg, out = CONFIG["jooble"], []
    for cc in CONFIG["countries"]:
        for q in cfg["queries"]:
            data = http_json("POST", cfg["endpoint"] + key,
                             json={"keywords": q, "location": cfg["locations"].get(cc, ""), "page": "1"})
            for j in (data or {}).get("jobs", []):
                job = make_job("Jooble", j.get("id"), j.get("title"), j.get("company"), j.get("link"),
                               j.get("location"), j.get("updated"), j.get("snippet"),
                               default_country=cc, contract_hint=j.get("type"))
                if job:
                    out.append(job)
            time.sleep(1)
    return out


def fetch_ats(t):
    typ, slug = t.get("type", "").lower(), t.get("slug", "")
    comp, dflt = t.get("company") or slug, t.get("default_country")
    out = []
    if typ == "recruitee":
        data = http_json("GET", f"https://{slug}.recruitee.com/api/offers/")
        for j in (data or {}).get("offers", []):
            out.append(make_job(
                "Careers page", j.get("id"), j.get("title"), j.get("company_name") or comp, j.get("careers_url"),
                j.get("location") or j.get("city"), j.get("published_at") or j.get("created_at"),
                f"{j.get('description', '')} {j.get('requirements', '')}", country_code=j.get("country_code"),
                default_country=dflt, contract_hint=j.get("employment_type_code"), direct=True))
    elif typ == "greenhouse":
        data = http_json("GET", f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"})
        for j in (data or {}).get("jobs", []):
            out.append(make_job(
                "Careers page", j.get("id"), j.get("title"), comp, j.get("absolute_url"),
                (j.get("location") or {}).get("name"), j.get("first_published") or j.get("updated_at"),
                j.get("content"), default_country=dflt, direct=True))
    elif typ == "lever":
        host = "api.eu.lever.co" if t.get("region") == "eu" else "api.lever.co"
        data = http_json("GET", f"https://{host}/v0/postings/{slug}", params={"mode": "json"})
        for j in data or []:
            cat = j.get("categories") or {}
            out.append(make_job(
                "Careers page", j.get("id"), j.get("text"), comp, j.get("hostedUrl"), cat.get("location"),
                j.get("createdAt"), j.get("descriptionPlain"), default_country=dflt,
                contract_hint=cat.get("commitment"), direct=True))
    elif typ == "workable":
        data = http_json("GET", f"https://apply.workable.com/api/v1/widget/accounts/{slug}", params={"details": "true"})
        for j in (data or {}).get("jobs", []):
            out.append(make_job(
                "Careers page", j.get("shortcode"), j.get("title"), (data or {}).get("name") or comp,
                j.get("url") or j.get("application_url"), f"{j.get('city', '')}, {j.get('country', '')}",
                j.get("published_on") or j.get("created_at"), j.get("description"),
                country_code=j.get("country"), default_country=dflt,
                contract_hint=j.get("employment_type"), direct=True))
    else:
        log(f"  ! unknown ATS type '{typ}' for {comp}")
    return [j for j in out if j]


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
def load_store():
    try:
        s = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("jobs", [])
    s.setdefault("last_runs", {})
    s.setdefault("source_stats", {})
    return s


def merge(store, fresh):
    by_key = {j["key"]: j for j in store["jobs"]}
    new = []
    now = iso(NOW)
    for j in fresh:
        ex = by_key.get(j["key"])
        if ex:
            ex["last_seen"] = now
            for s in j["sources"]:
                if s not in ex["sources"]:
                    ex["sources"].append(s)
            if j["direct"] and not ex.get("direct"):
                ex["url"], ex["direct"] = j["url"], True
            if j["posted_at"] and (not ex.get("posted_at") or j["posted_at"] < ex["posted_at"]):
                ex["posted_at"] = j["posted_at"]
            if ex.get("contract") == "Unspecified" and j["contract"] != "Unspecified":
                ex["contract"] = j["contract"]
        else:
            j["first_seen"] = j["last_seen"] = now
            by_key[j["key"]] = j
            new.append(j)

    cutoff = NOW - timedelta(days=CONFIG.get("retention_days", 45))
    jobs = [j for j in by_key.values() if parse_date(j["last_seen"]) >= cutoff]
    jobs.sort(key=lambda j: j.get("posted_at") or j["first_seen"], reverse=True)
    store["jobs"] = jobs
    return new


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
def main():
    store = load_store()
    first_run = not store["jobs"]
    fresh = []

    def run(name, fn, every_hours=None):
        if every_hours and not due(store, name, every_hours):
            log(f"{name}: not due yet (runs every {every_hours}h)")
            return
        log(f"{name}: scanning")
        try:
            res = fn()
        except Exception as e:  # one broken source must never kill the run
            log(f"  ! {name} failed: {type(e).__name__}: {e}")
            return
        if res is None:
            return
        fresh.extend(res)
        store["last_runs"][name] = iso(NOW)
        store["source_stats"][name] = {"matched": len(res), "at": iso(NOW)}
        log(f"  {len(res)} relevant roles")

    if CONFIG["adzuna"].get("enabled", True):
        run("Adzuna", fetch_adzuna, CONFIG["adzuna"].get("every_hours", 4))
    if CONFIG["jooble"].get("enabled", True):
        run("Jooble", fetch_jooble, CONFIG["jooble"].get("every_hours", 4))
    for t in ats_targets():
        run(f"Careers: {t.get('company') or t['slug']}", lambda t=t: fetch_ats(t))

    new = merge(store, fresh)
    store["generated_at"] = iso(NOW)
    store["stale_after_days"] = CONFIG.get("stale_after_days", 3)
    store["diagnostics"] = {"at": iso(NOW), "raw_fetched": len(fresh), **DIAG}
    DATA_PATH.parent.mkdir(exist_ok=True)
    DATA_PATH.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"Done: {len(new)} new, {len(store['jobs'])} tracked")

    if first_run:
        log("First run: skipping alerts so you are not flooded with the backlog")
    else:
        send_alerts(new)


if __name__ == "__main__":
    sys.exit(main())
