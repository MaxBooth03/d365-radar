#!/usr/bin/env python3
"""
One-off discovery tool: tries each candidate employer against the public job-feed
APIs of common careers systems and records which ones exist and post D365 roles.
Writes data/probe.json. Not part of the regular scan.
"""
import json
import re
import concurrent.futures as cf
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "probe.json"
S = requests.Session()
S.headers["User-Agent"] = "D365-Radar/1.0 (job feed discovery)"

CANDIDATES = [
    # NL / Benelux Dynamics partners and ISVs
    "HSO", "Avanade", "Columbus", "Prodware", "9altitudes", "Fellowmind", "Mprise", "Wortell", "Macaw",
    "Motion10", "Exsolvi", "Kabalt", "Boltrics", "To-Increase", "Qbil", "4PS", "Arcus", "AXM 365", "Rubicon",
    "Acknowledge", "Sana Commerce", "Pulse", "Winvision", "Datamex", "InTWO", "WSB Solutions", "QBS Group",
    "Abecon", "4NG", "OfficeGrip", "Logic Vision", "Eswelt", "Indito", "Tempero", "Adfocom", "Lumen",
    "Innovadis", "MindsUnited", "ContinYou", "Uny", "Axenia", "Proximo", "SucceedIT", "Hitachi Solutions",
    "K3", "Tectura", "Cobalt", "Blis Digital", "Proactive", "Itility", "Conclusion", "Centric", "Ordina",
    "Sogeti", "Sopra Steria", "Capgemini", "CGI", "DXC", "adesso", "Valtech", "Inspark", "Dynamics Square",
    "Plaut", "ISM eCompany", "Wipro", "Assist Digital", "Antwerp Management School",
    # Belgium
    "Delaware", "Cegeka", "Inetum", "Realdolmen", "Ifacto", "Xylos", "Alistar", "Eptimum", "Ufirst", "Acrior",
    "NSI", "Scapta", "Codit", "Dynamate", "Thryve", "Spikes", "Elision", "NRB", "Ausy", "The Cloud Factory",
    "Kabisa", "Hecht", "Ordina Belgium", "Arxus", "Fluxys",
    # Advisory with Dynamics practices
    "BDO", "Grant Thornton", "Baker Tilly", "RSM", "KPMG", "PwC", "Deloitte", "EY", "Mazars", "Forvis Mazars",
    # End users seen hiring D365 roles
    "Renewi", "Kiwa", "Vanderlande", "Royal FloraHolland", "Coolblue", "bol", "Jumbo", "HEMA", "Eneco",
    "PostNL", "Signify", "Heineken", "Philips", "Redcare Pharmacy", "Strukton", "Netcompany", "E-mergo",
    "Kwery", "Alsena", "Everience", "WKL Consultancy", "Dynamic People", "Redkiwi",
]

D365 = re.compile(r"dynamics\s*365|\bd365\b|microsoft\s+dynamics|business\s+central|navision|"
                  r"dynamics\s+(crm|ax|nav)|power\s*platform|power\s*apps|dataverse", re.I)


def variants(name):
    base = name.lower().replace("&", "and")
    words = re.findall(r"[a-z0-9]+", base)
    joined, hyph = "".join(words), "-".join(words)
    out = [joined, hyph, f"{joined}nl", f"{joined}bv", f"{joined}groupbv", f"{joined}be", f"{joined}benelux"]
    seen, res = set(), []
    for v in out:
        if v and v not in seen:
            seen.add(v)
            res.append(v)
    return res


def get(url, **kw):
    try:
        r = S.get(url, timeout=15, allow_redirects=False, **kw)
        return r
    except requests.RequestException:
        return None


def summarise(titles, texts, locs):
    d = [t for t, x in zip(titles, texts) if D365.search(f"{t} {x}")]
    benelux = sum(bool(re.search(r"netherlands|nederland|belgi|amsterdam|utrecht|rotterdam|eindhoven|"
                                 r"brussel|antwerp|gent|ghent|leuven|mechelen|den haag|hague|zwolle|"
                                 r"arnhem|nijmegen|breda|tilburg|groningen|hasselt|kortrijk|\bnl\b|\bbe\b",
                                 l or "", re.I)) for l in locs)
    return {"jobs": len(titles), "d365_jobs": len(d), "benelux_jobs": benelux, "d365_samples": d[:5],
            "samples": titles[:4], "locations": sorted({l for l in locs if l})[:6]}


def try_recruitee(s):
    r = get(f"https://{s}.recruitee.com/api/offers/")
    if not r or r.status_code != 200:
        return None
    try:
        offers = r.json().get("offers", [])
    except ValueError:
        return None
    return summarise([o.get("title", "") for o in offers],
                     [f"{o.get('description', '')} {o.get('requirements', '')}" for o in offers],
                     [f"{o.get('location', '')} {o.get('country', '')}" for o in offers]) | {
        "company": offers[0].get("company_name") if offers else None}


def try_greenhouse(s, eu):
    host = "boards-api.eu.greenhouse.io" if eu else "boards-api.greenhouse.io"
    r = get(f"https://{host}/v1/boards/{s}/jobs", params={"content": "true"})
    if not r or r.status_code != 200:
        return None
    try:
        jobs = r.json().get("jobs", [])
    except ValueError:
        return None
    return summarise([j.get("title", "") for j in jobs], [j.get("content", "") for j in jobs],
                     [(j.get("location") or {}).get("name", "") for j in jobs])


def try_lever(s, eu):
    host = "api.eu.lever.co" if eu else "api.lever.co"
    r = get(f"https://{host}/v0/postings/{s}", params={"mode": "json"})
    if not r or r.status_code != 200:
        return None
    try:
        jobs = r.json()
    except ValueError:
        return None
    if not isinstance(jobs, list):
        return None
    return summarise([j.get("text", "") for j in jobs], [j.get("descriptionPlain", "") for j in jobs],
                     [(j.get("categories") or {}).get("location", "") for j in jobs])


def try_workable(s):
    r = get(f"https://apply.workable.com/api/v1/widget/accounts/{s}", params={"details": "true"})
    if not r or r.status_code != 200:
        return None
    try:
        d = r.json()
    except ValueError:
        return None
    jobs = d.get("jobs", [])
    return summarise([j.get("title", "") for j in jobs], [j.get("description", "") for j in jobs],
                     [f"{j.get('city', '')} {j.get('country', '')}" for j in jobs]) | {"company": d.get("name")}


def try_smartrecruiters(s):
    r = get(f"https://api.smartrecruiters.com/v1/companies/{s}/postings", params={"limit": 100})
    if not r or r.status_code != 200:
        return None
    try:
        d = r.json()
    except ValueError:
        return None
    jobs = d.get("content", [])
    if not jobs:
        return None
    return summarise([j.get("name", "") for j in jobs], ["" for _ in jobs],
                     [f"{(j.get('location') or {}).get('city', '')} {(j.get('location') or {}).get('country', '')}"
                      for j in jobs]) | {"company": (jobs[0].get("company") or {}).get("name"),
                                          "total": d.get("totalFound")}


def try_personio(s, tld):
    r = get(f"https://{s}.jobs.personio.{tld}/xml")
    if not r or r.status_code != 200 or "<position" not in r.text:
        return None
    titles = re.findall(r"<name>(.*?)</name>", r.text, re.S)
    offices = re.findall(r"<office>(.*?)</office>", r.text, re.S)
    return summarise(titles, ["" for _ in titles], offices)


def probe(name):
    found = []
    for s in variants(name):
        checks = [("recruitee", lambda: try_recruitee(s)),
                  ("greenhouse", lambda: try_greenhouse(s, False)),
                  ("greenhouse-eu", lambda: try_greenhouse(s, True)),
                  ("lever", lambda: try_lever(s, False)),
                  ("lever-eu", lambda: try_lever(s, True)),
                  ("workable", lambda: try_workable(s)),
                  ("smartrecruiters", lambda: try_smartrecruiters(s)),
                  ("personio", lambda: try_personio(s, "de")),
                  ("personio-com", lambda: try_personio(s, "com"))]
        for ats, fn in checks:
            try:
                res = fn()
            except Exception:  # noqa: BLE001 - discovery must never crash
                res = None
            if res and res.get("jobs"):
                found.append({"candidate": name, "ats": ats, "slug": s, **res})
    return found


def main():
    results = []
    with cf.ThreadPoolExecutor(max_workers=24) as ex:
        for found in ex.map(probe, CANDIDATES):
            results.extend(found)
    results.sort(key=lambda r: (-r["d365_jobs"], r["candidate"]))
    OUT.write_text(json.dumps({"candidates": len(CANDIDATES), "hits": results}, indent=1, ensure_ascii=False))
    print(f"{len(results)} feeds found for {len({r['candidate'] for r in results})} of {len(CANDIDATES)} candidates")
    for r in results:
        print(f"{r['candidate']:<22} {r['ats']:<15} {r['slug']:<22} jobs={r['jobs']:<4} d365={r['d365_jobs']:<3} "
              f"benelux={r['benelux_jobs']}")


if __name__ == "__main__":
    main()
