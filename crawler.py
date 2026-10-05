#!/usr/bin/env python3
"""jobwatch v2: prüft Karriereseiten (NICHT Jobportale) auf offene Cloud/Data/Infrastructure-Stellen,
meldet NEUE Stellen, zieht Ansprechpartner und erzeugt eine Anrufliste (HTML + CSV).

Start:   pip install requests beautifulsoup4 lxml && python crawler.py
Optional: python crawler.py --only "Fabasoft,Axians"   (nur diese Firmen)
Ausgabe: anrufliste_<Datum>.html, anrufliste_<Datum>.csv, status_<Datum>.csv, state.json
"""
import csv, json, re, sys, time, hashlib, datetime, html as H, urllib.robotparser
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

UA = "jobwatch/2.0 (Syncruit Recruiting; kontakt: stefspenger@gmail.com)"
DELAY = 1.5
MAX_PAGES = 4

# Filter: nur Cloud / Data / Infrastructure (SAP und Cyber Security bewusst NICHT enthalten)
INCLUDE = re.compile(
    r"cloud|devops|sre\b|site reliability|platform engineer|plattform|azure|aws\b|gcp|kubernetes|k8s|terraform|"
    r"infrastruktur|infrastructure|system ?(engineer|admin|techniker|betreuer)|netzwerk|network|linux|windows server|"
    r"vmware|virtualis|rechenzentrum|data ?center|backup|storage|"
    r"\bdata\b|daten|\bbi\b|business intelligence|analytics|etl|dwh|data ?warehouse|databricks|snowflake|"
    r"machine learning|\bml\b|\bai\b|\bki\b|dba|datenbank|database|integration", re.I)
EXCLUDE = re.compile(r"\bsap\b|cyber|security|ciso|penetration|soc analyst|praktik|lehrling|ferial|trainee|"
                     r"werkstudent|vertrieb|sales|marketing|buchhalt|HR ", re.I)

CAREER_PATHS = ["/karriere", "/jobs", "/karriere/jobs", "/de/karriere", "/career", "/careers", "/en/careers",
                "/de/jobs", "/unternehmen/karriere", "/karriere/offene-stellen", "/offene-stellen",
                "/stellenangebote", "/de/karriere/jobs", "/jobs-karriere", "/arbeiten-bei-uns"]
ATS_HINTS = {"personio": "Personio", "smartrecruiters": "SmartRecruiters", "join.com": "JOIN",
             "myworkdayjobs": "Workday", "successfactors": "SuccessFactors", "softgarden": "Softgarden",
             "rexx-systems": "rexx", "prospective.ch": "Prospective", "greenhouse.io": "Greenhouse",
             "lever.co": "Lever", "onlyfy": "Onlyfy", "umantis": "umantis", "d-vinci": "d.vinci",
             "karriere.at": "karriere.at-Widget", "bamboohr": "BambooHR", "recruitee": "Recruitee"}
PORTALS = re.compile(r"karriere\.at|stepstone|indeed|monster|xing\.com|linkedin\.com|jobs\.at|hokify|"
                     r"willhaben|jobswype|kununu", re.I)

S = requests.Session(); S.headers["User-Agent"] = UA
_robots = {}


def allowed(url):
    p = urlparse(url); key = p.netloc
    if key not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            rp.set_url(f"{p.scheme}://{p.netloc}/robots.txt"); rp.read()
        except Exception:
            rp = None
        _robots[key] = rp
    rp = _robots[key]
    return True if rp is None else rp.can_fetch(UA, url)


def get(url, raw=False):
    if not allowed(url):
        return None
    time.sleep(DELAY)
    try:
        r = S.get(url, timeout=20)
        return r if r.status_code == 200 else None
    except requests.RequestException:
        return None


def clean(t):
    return " ".join((t or "").split())


# ---------------------------------------------------------------- Adapter
def jsonld_jobs(soup, base):
    out = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list): stack.extend(d); continue
            if not isinstance(d, dict): continue
            if "@graph" in d: stack.extend(d["@graph"])
            if d.get("@type") == "JobPosting":
                loc = d.get("jobLocation"); loc = loc[0] if isinstance(loc, list) and loc else loc
                addr = (loc or {}).get("address", {}) if isinstance(loc, dict) else {}
                out.append({"title": clean(d.get("title")), "url": d.get("url") or base,
                            "location": addr.get("addressLocality", "") if isinstance(addr, dict) else "",
                            "date": d.get("datePosted", "")})
    return out


def softgarden_feed(html, base):
    """softgarden: https://<host>/jobs.feed.json bzw. /jobs.feed"""
    host = urlparse(base).netloc
    m = re.search(r"https?://([a-z0-9.-]+)/(?:de/)?job[s/]", html)
    cands = [f"https://{host}/jobs.feed.json"]
    if m: cands.append(f"https://{m.group(1)}/jobs.feed.json")
    for u in cands:
        r = get(u)
        if r:
            try:
                data = r.json()
            except Exception:
                continue
            items = data.get("jobs") or data.get("items") or (data if isinstance(data, list) else [])
            out = []
            for j in items:
                if not isinstance(j, dict): continue
                out.append({"title": clean(j.get("title") or j.get("name")),
                            "url": j.get("url") or j.get("link") or u,
                            "location": clean(str(j.get("location") or j.get("city") or "")),
                            "date": j.get("datePosted") or j.get("date") or ""})
            if out: return out
    return []


def personio_xml(html, base):
    m = re.search(r"https?://([a-z0-9-]+)\.jobs\.personio\.(?:de|com)", html)
    if not m: return []
    r = get(f"https://{m.group(1)}.jobs.personio.de/xml")
    if not r: return []
    s = BeautifulSoup(r.text, "xml")
    out = []
    for p in s.find_all("position"):
        g = lambda n: clean(p.find(n).text) if p.find(n) else ""
        out.append({"title": g("name"), "url": f"https://{m.group(1)}.jobs.personio.de/job/{g('id')}",
                    "location": g("office"), "date": g("createdAt")})
    return out


def link_jobs(soup, base):
    """Generischer Fallback (Drupal/WordPress/rexx/umantis/onlyfy): Links, die wie Stellentitel aussehen."""
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        t = clean(a.get_text(" ", strip=True))
        href = urljoin(base, a["href"])
        if not (8 < len(t) < 160) or PORTALS.search(href): continue
        looks_job = re.search(r"\(m/w/d\)|\(w/m/d\)|\(m/f/d\)|\(all genders\)|\(f/m/d\)|engineer|entwickl|manager|leiter|"
                              r"specialist|consultant|administrator|architekt|techniker|analyst|developer|admin|betreuer", t, re.I)
        looks_url = re.search(r"job|stelle|karriere|career|position|vacanc|/o/|/j/|ausschreibung|opening", href, re.I)
        if looks_job and looks_url and href not in seen:
            seen.add(href); out.append({"title": t, "url": href, "location": "", "date": ""})
    return out


def next_pages(soup, base, seen_urls):
    out = []
    for a in soup.find_all("a", href=True):
        t = clean(a.get_text())
        if re.fullmatch(r"[2-9]|weiter|next|›|»|nächste( seite)?", t, re.I) or \
           re.search(r"[?&](page|seite|p)=\d+", a["href"]):
            u = urljoin(base, a["href"])
            if u not in seen_urls and urlparse(u).netloc == urlparse(base).netloc:
                out.append(u)
    return out[:MAX_PAGES]


# ---------------------------------------------------------------- Kontakte
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?:\+43|0043|0)[\s/()-]*\d[\d\s/()-]{6,16}\d")
CONTACT_HDR = re.compile(r"ansprechperson|ansprechpartner|kontakt|dein kontakt|ihr kontakt|fragen\?|recruiting|hr[- ]team|"
                         r"contact person|your contact", re.I)


def extract_contact(url):
    r = get(url)
    if not r: return ""
    soup = BeautifulSoup(r.text, "html.parser")
    for t in soup(["script", "style", "nav", "footer"]): t.decompose()
    text = soup.get_text("\n", strip=True)
    lines = [clean(l) for l in text.split("\n") if clean(l)]
    for i, l in enumerate(lines):
        if CONTACT_HDR.search(l) and len(l) < 80:
            block = " | ".join(lines[i + 1:i + 6])
            em, ph = EMAIL.search(block), PHONE.search(block)
            name = next((x for x in lines[i + 1:i + 4] if re.fullmatch(r"[A-ZÄÖÜ][\wäöüß.-]+(?: [A-ZÄÖÜ][\wäöüß.-]+){1,3}", x)), "")
            parts = [p for p in (name, em.group(0) if em else "", clean(ph.group(0)) if ph else "") if p]
            if parts: return " · ".join(parts)
    em = EMAIL.search(text)
    return em.group(0) if em else ""


# ---------------------------------------------------------------- Crawl
def find_career(domain):
    root = f"https://www.{domain}" if not domain.startswith("www.") else f"https://{domain}"
    r = get(root)
    if r is None:
        root = f"https://{domain}"; r = get(root)
    if r is None: return None, None, None
    soup = BeautifulSoup(r.text, "html.parser")
    for a in soup.find_all("a", href=True):
        if re.search(r"karriere|career|jobs|stellen", a["href"] + a.get_text(), re.I) and not PORTALS.search(a["href"]):
            u = urljoin(root, a["href"]); rr = get(u)
            if rr: return u, rr, root
    for p in CAREER_PATHS:
        u = root + p; rr = get(u)
        if rr: return u, rr, root
    return None, None, root


def crawl(domain, career_override=""):
    if career_override:
        r = get(career_override); url, root = career_override, career_override
    else:
        url, r, root = find_career(domain)
    if not r:
        return {"status": "KEINE_KARRIERESEITE", "jobs": [], "url": root or domain, "ats": ""}
    html = r.text; soup = BeautifulSoup(html, "html.parser")
    ats = next((n for k, n in ATS_HINTS.items() if k in html.lower()), "")
    jobs = jsonld_jobs(soup, url) + softgarden_feed(html, url) + personio_xml(html, url) + link_jobs(soup, url)
    seen_pages = {url}
    for pu in next_pages(soup, url, seen_pages):
        seen_pages.add(pu); pr = get(pu)
        if pr:
            ps = BeautifulSoup(pr.text, "html.parser")
            jobs += jsonld_jobs(ps, pu) + link_jobs(ps, pu)
    uniq = {}
    for j in jobs: uniq.setdefault(j["url"], j)
    jobs = list(uniq.values())
    status = "OK" if jobs else ("JS/ATS_PRÜFEN" if ats or len(html) < 20000 else "KEINE_STELLEN_GEFUNDEN")
    return {"status": status, "jobs": jobs, "url": url, "ats": ats}


def relevant(j):
    return bool(INCLUDE.search(j["title"])) and not EXCLUDE.search(j["title"])


def hid(u): return hashlib.md5(u.encode()).hexdigest()


def write_html(rows, path, today):
    css = "body{font-family:system-ui;margin:24px;max-width:1100px}table{border-collapse:collapse;width:100%}" \
          "td,th{border-bottom:1px solid #ddd;padding:8px;text-align:left;vertical-align:top;font-size:14px}" \
          "th{background:#f3f3f3}.new{background:#e8f7e8}"
    h = [f"<meta charset=utf-8><style>{css}</style><h2>Anrufliste {today} – Cloud / Data / Infrastructure</h2>",
         "<table><tr><th>Firma</th><th>Stelle</th><th>Ort</th><th>Ansprechpartner</th><th>Neu</th><th>Angerufen / Notiz</th></tr>"]
    for r in rows:
        h.append(f"<tr class={'new' if r['neu'] else ''}><td>{H.escape(r['firma'])}</td>"
                 f"<td><a href='{H.escape(r['url'])}' target=_blank>{H.escape(r['titel'])}</a></td>"
                 f"<td>{H.escape(r['ort'])}</td><td>{H.escape(r['kontakt'])}</td>"
                 f"<td>{'NEU' if r['neu'] else ''}</td><td>☐</td></tr>")
    h.append("</table>")
    open(path, "w", encoding="utf-8").write("\n".join(h))


def main():
    only = None
    if "--only" in sys.argv:
        only = {x.strip().lower() for x in sys.argv[sys.argv.index("--only") + 1].split(",")}
    try: state = json.load(open("state.json"))
    except Exception: state = {}
    today = datetime.date.today().isoformat()
    rows, report = [], []
    for row in csv.DictReader(open("companies.csv", encoding="utf-8")):
        name, dom = row["name"].strip(), row["domain"].strip()
        if only and name.lower() not in only: continue
        if not dom: report.append((name, "KEINE_DOMAIN", "", "", 0)); continue
        res = crawl(dom, row.get("career_url", "").strip())
        rel = [j for j in res["jobs"] if relevant(j)]
        old = set(state.get(name, {}).get("seen", []))
        first_run = name not in state
        for j in rel:
            neu = hid(j["url"]) not in old
            contact = extract_contact(j["url"]) if j["url"] != res["url"] else ""
            rows.append({"firma": name, "titel": j["title"], "ort": j["location"], "url": j["url"],
                         "kontakt": contact, "neu": neu and not first_run, "datum": j["date"]})
        state[name] = {"seen": [hid(j["url"]) for j in rel], "last": today, "career_url": res["url"]}
        report.append((name, res["status"], res["ats"], res["url"], len(rel)))
        print(f"{name:28} {res['status']:22} {res['ats']:16} Treffer: {len(rel)}", flush=True)
    json.dump(state, open("state.json", "w"), ensure_ascii=False, indent=1)
    rows.sort(key=lambda r: (not r["neu"], r["firma"]))
    with open(f"anrufliste_{today}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["Firma", "Stelle", "Ort", "Ansprechpartner", "Neu", "Link"])
        w.writerows([(r["firma"], r["titel"], r["ort"], r["kontakt"], "NEU" if r["neu"] else "", r["url"]) for r in rows])
    write_html(rows, f"anrufliste_{today}.html", today)
    with open(f"status_{today}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["Firma", "Status", "ATS", "Karriereseite", "Treffer"]); w.writerows(report)
    print(f"\n{len(rows)} Cloud/Data/Infra-Stellen -> anrufliste_{today}.html / .csv")
    bad = [r for r in report if r[1] not in ("OK",)]
    print(f"{len(bad)} Firmen brauchen Nacharbeit (siehe status_{today}.csv: career_url in companies.csv eintragen)")


if __name__ == "__main__":
    main()
