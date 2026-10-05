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


# ---------------------------------------------------------------- Karriereseite finden
CAREER_WORDS = re.compile(r"karriere|career|jobs?\b|stellen|offene stellen|join us|work with us|arbeiten bei|"
                          r"jobportal|stellenmarkt|bewerb|recruiting|werde teil|team werden|wir suchen|vacanc|openings", re.I)
SUBDOMAINS = ["karriere", "jobs", "career", "careers", "job", "bewerbung", "recruiting"]
DEEPER = re.compile(r"alle (offenen )?(stellen|jobs)|offene stellen|zu den (jobs|stellen)|jobs? ansehen|"
                    r"stellenmarkt|jobportal|aktuelle (stellen|jobs)|all (open )?(jobs|positions)|open positions|"
                    r"view all|zur jobsuche|jobsuche|stellenangebote", re.I)
USE_JS = bool(__import__("os").environ.get("JOBWATCH_JS"))


def render_js(url):
    """Seite mit echtem Browser laden (nur wenn playwright installiert und JOBWATCH_JS=1)."""
    if not USE_JS: return None
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(); pg = b.new_page(user_agent=UA)
            pg.goto(url, timeout=30000, wait_until="networkidle")
            for sel in ("button:has-text('Akzeptieren')", "button:has-text('Alle akzeptieren')",
                        "button:has-text('Accept')", "button:has-text('Zustimmen')"):
                try: pg.click(sel, timeout=1500); break
                except Exception: pass
            pg.wait_for_timeout(1500)
            h = pg.content(); b.close(); return h
    except Exception:
        return None


def score(url, text=""):
    u = urlparse(url); s = 0
    host, path = u.netloc.lower(), u.path.lower()
    if re.search(r"^(karriere|jobs|career|careers)\.", host): s += 6
    if any(k in host for k in ("softgarden", "personio", "join.com", "rexx", "umantis", "onlyfy", "smartrecruiters",
                              "recruitee", "workable", "greenhouse", "lever.co", "bamboohr", "myworkdayjobs")): s += 5
    if re.search(r"karriere|career", path): s += 4
    if re.search(r"job|stelle", path): s += 3
    if re.search(r"offene|open|alle", path + text.lower()): s += 1
    s -= min(len(path.strip("/").split("/")), 4) * 0.3
    return s


def candidates(domain):
    """Geordnete Liste möglicher Karriere-URLs + die geladene Startseite."""
    bare = re.sub(r"^www\.", "", domain)
    home_r, root = None, None
    for h in (f"https://www.{bare}", f"https://{bare}"):
        home_r = get(h)
        if home_r: root = h; break
    cands = {}
    if home_r is not None:
        base = home_r.url
        soup = BeautifulSoup(home_r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            t = clean(a.get_text(" ", strip=True)); href = urljoin(base, a["href"]).split("#")[0]
            if PORTALS.search(href) or href.startswith(("mailto:", "tel:")): continue
            if CAREER_WORDS.search(t) or CAREER_WORDS.search(urlparse(href).path) or \
               re.match(r"(karriere|jobs|career)\.", urlparse(href).netloc, re.I):
                cands[href] = max(cands.get(href, 0), score(href, t) + 2)
        # Sitemap
        for sm in (f"{root}/sitemap.xml", f"{root}/sitemap_index.xml"):
            r = get(sm)
            if r:
                for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text)[:3000]:
                    if re.search(r"karriere|career|/jobs?(/|$)|stellen", loc, re.I) and not PORTALS.search(loc):
                        cands.setdefault(loc, score(loc))
                break
    base_root = root or f"https://www.{bare}"
    for sub in SUBDOMAINS:
        cands.setdefault(f"https://{sub}.{bare}", score(f"https://{sub}.{bare}") - 1)
    for p in CAREER_PATHS:
        cands.setdefault(base_root + p, score(base_root + p) - 2)
    ordered = [u for u, _ in sorted(cands.items(), key=lambda kv: -kv[1])]
    return ordered[:14], root


def extract_jobs(html, url):
    soup = BeautifulSoup(html, "html.parser")
    jobs = jsonld_jobs(soup, url) + softgarden_feed(html, url) + personio_xml(html, url) + link_jobs(soup, url)
    seen_pages = {url}
    for pu in next_pages(soup, url, seen_pages):
        seen_pages.add(pu); pr = get(pu)
        if pr:
            ps = BeautifulSoup(pr.text, "html.parser")
            jobs += jsonld_jobs(ps, pu) + link_jobs(ps, pu)
    return jobs, soup


def crawl(domain, career_override=""):
    cands, root = ([career_override], None) if career_override else candidates(domain)
    if not cands and not root:
        return {"status": "WEBSITE_NICHT_ERREICHBAR", "jobs": [], "url": domain, "ats": ""}
    fallback = None
    tried = 0
    for url in cands:
        r = get(url)
        if not r: continue
        tried += 1
        html = r.text; final = r.url
        ats = next((n for k, n in ATS_HINTS.items() if k in html.lower()), "")
        jobs, soup = extract_jobs(html, final)
        # eine Ebene tiefer: "Alle offenen Stellen"
        if not jobs:
            for a in soup.find_all("a", href=True)[:400]:
                if DEEPER.search(clean(a.get_text(" ", strip=True))):
                    du = urljoin(final, a["href"])
                    if du != final and not PORTALS.search(du):
                        dr = get(du)
                        if dr:
                            dj, _ = extract_jobs(dr.text, dr.url)
                            if dj: jobs, final, html = dj, dr.url, dr.text; break
        # JavaScript-Seiten mit Browser nachladen
        if not jobs and USE_JS:
            rh = render_js(final)
            if rh:
                jobs, _ = extract_jobs(rh, final)
                if jobs: html = rh
        if fallback is None and (CAREER_WORDS.search(html[:200000]) or ats):
            fallback = {"url": final, "ats": ats, "len": len(html)}
        if jobs:
            uniq = {}
            for j in jobs: uniq.setdefault(j["url"], j)
            return {"status": "OK", "jobs": list(uniq.values()), "url": final, "ats": ats}
        if tried >= 8: break
    if fallback:
        st = "JS/ATS_PRÜFEN" if fallback["ats"] or fallback["len"] < 30000 else "KEINE_STELLEN_GEFUNDEN"
        return {"status": st, "jobs": [], "url": fallback["url"], "ats": fallback["ats"]}
    return {"status": "KEINE_KARRIERESEITE", "jobs": [], "url": root or domain, "ats": ""}


def relevant(j):
    return bool(INCLUDE.search(j["title"])) and not EXCLUDE.search(j["title"])


def region_of(ort):
    o = (ort or "").lower()
    if any(k in o for k in ("linz", "wels", "leonding", "hörsching", "kremsmünster", "grieskirchen", "lenzing", "steinhaus", "marchtrenk", "oö")): r = "OÖ"
    elif any(k in o for k in ("pölten", "neudorf", "amstetten", "berndorf", "enzersdorf", "schrems", "siegharts", "waidhofen", "gumpoldskirchen", "nö")): r = "NÖ"
    elif "wien" in o: r = "Wien"
    else: r = ""
    if "wien" in o and r != "Wien": r = (r + " / Wien").strip(" /")
    return r or (ort or "")


def hid(u): return hashlib.md5(u.encode()).hexdigest()


# ---------------------------------------------------------------- Aufhänger, Typ, Portal-Check
def job_type(title):
    t = title.lower()
    if re.search(r"cloud|devops|sre\b|site reliability|platform|azure|aws|gcp|kubernetes|terraform", t): return "Cloud"
    if re.search(r"\bdata\b|daten|\bbi\b|analytics|etl|dwh|warehouse|databricks|snowflake|machine learning|\bml\b|\bai\b|\bki\b|dba|datenbank|database|integration", t): return "Data"
    return "Infrastructure"


def title_clean(t):
    return clean(re.sub(r"\((?:m|w|f|d|x|all|genders|/|\s|\*|:)+\)|\b(?:m/w/d|w/m/d|m/f/d|f/m/d)\b", "", t, flags=re.I)).strip(" -–|")


HOOKS = {
    "Cloud": "Ich habe gesehen, dass Sie bei {firma} einen {titel} suchen. Wie ist Ihr Cloud-Team aktuell aufgestellt – und was macht die Besetzung in dem Bereich gerade schwierig?",
    "Data": "Ich habe gesehen, dass Sie bei {firma} einen {titel} suchen. Was soll in dem Datenbereich als Nächstes entstehen, und wie sind die bisherigen Bewerbungen für die Rolle?",
    "Infrastructure": "Ich habe gesehen, dass Sie bei {firma} einen {titel} suchen. Wie läuft die Suche, und wie ist die Qualität der Bewerbungen bisher?",
}


def hook(firma, titel, tage):
    h = HOOKS[job_type(titel)].format(firma=firma, titel=title_clean(titel))
    if tage >= 28:
        h += f" Die Stelle steht schon einige Wochen offen – was hat bisher gefehlt?"
    return h


def portal_check(firma, titel, budget):
    """Prüft, ob die Stelle auch auf karriere.at gefunden wird. Gibt (status, budget) zurück."""
    if budget[0] <= 0: return "nicht geprüft"
    budget[0] -= 1
    from urllib.parse import quote_plus
    r = get("https://www.karriere.at/jobs?keywords=" + quote_plus(title_clean(titel) + " " + firma.split()[0]))
    if r is None: return "nicht prüfbar"
    txt = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True).lower()
    tk = [w for w in re.findall(r"[a-zäöüß]{4,}", title_clean(titel).lower())][:3]
    return "auch auf karriere.at" if (firma.split()[0].lower() in txt and tk and all(w in txt for w in tk)) \
        else "nicht auf karriere.at gefunden"


# ---------------------------------------------------------------- Ausgabe
PAGE = r"""<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jobwatch – Anrufliste</title><style>
:root{--bg:#fafaf8;--fg:#1d1d1b;--mut:#6b6b66;--line:#e2e1dc;--card:#fff;--acc:#1d5fd1;--new:#e6f4e6;--due:#fff1d6}
@media(prefers-color-scheme:dark){:root{--bg:#161614;--fg:#eceae4;--mut:#9a988f;--line:#33322e;--card:#1e1e1b;--acc:#7fb0ff;--new:#1d3320;--due:#3a2f12}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
header{padding:16px;border-bottom:1px solid var(--line)}h1{font-size:19px;margin:0 0 4px}.sub{color:var(--mut);font-size:13px}
.bar{display:flex;flex-wrap:wrap;gap:8px;padding:12px 16px;border-bottom:1px solid var(--line);align-items:center}
.bar input[type=search],.bar select{padding:7px 9px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);font:inherit}
.bar input[type=search]{min-width:200px;flex:1}label.c{font-size:13px;color:var(--mut);display:flex;gap:5px;align-items:center}
main{padding:0 16px 40px;max-width:1100px;margin:auto}#due{margin:14px 0;padding:10px 12px;border-radius:8px;background:var(--due);display:none}
.firm{margin-top:18px;border:1px solid var(--line);border-radius:10px;background:var(--card);overflow:hidden}
.fh{display:flex;justify-content:space-between;gap:8px;padding:10px 12px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.fh b{font-size:16px}.fh span{color:var(--mut);font-size:13px}.fh a{color:var(--acc)}
.job{padding:10px 12px;border-top:1px solid var(--line)}.job:first-of-type{border-top:0}.job.new{background:var(--new)}.job.done{opacity:.55}
.t{display:flex;gap:8px;flex-wrap:wrap;align-items:baseline}.t a{color:var(--acc);font-weight:600}
.tag{font-size:12px;border:1px solid var(--line);border-radius:99px;padding:1px 8px;color:var(--mut)}
.hk{margin:6px 0;color:var(--fg);font-size:14px}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:6px}
.row input[type=text],.row input[type=date]{padding:5px 7px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit;font-size:13px}
.row input[type=text]{flex:1;min-width:160px}button{padding:4px 9px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);cursor:pointer;font:inherit;font-size:13px}
.empty{padding:30px;text-align:center;color:var(--mut)}
</style></head><body>
<header><h1>Anrufliste – Cloud / Data / Infrastructure</h1><div class="sub" id="meta"></div></header>
<div class="bar"><input type="search" id="q" placeholder="Suche: Firma, Stelle, Ansprechpartner …">
<select id="reg"><option value="">Alle Regionen</option><option>Wien</option><option>NÖ</option><option>OÖ</option></select>
<select id="typ"><option value="">Alle Bereiche</option><option>Cloud</option><option>Data</option><option>Infrastructure</option></select>
<label class="c"><input type="checkbox" id="fn"> nur NEU</label>
<label class="c"><input type="checkbox" id="fp"> nicht auf karriere.at</label>
<label class="c"><input type="checkbox" id="fd"> Angerufene ausblenden</label></div>
<main><div id="due"></div><div id="list"></div></main>
<script>
const DATA=__DATA__;
const LS={get(k){try{return JSON.parse(localStorage.getItem(k))||{}}catch(e){return {}}},set(k,v){try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}}};
const st=LS.get('jobwatch_state');const today=new Date().toISOString().slice(0,10);
const $=id=>document.getElementById(id);const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
$('meta').textContent='Stand '+DATA.date+' · '+DATA.jobs.length+' Stellen bei '+new Set(DATA.jobs.map(j=>j.firma)).size+' Firmen · '+DATA.jobs.filter(j=>j.neu).length+' neu';
function save(){LS.set('jobwatch_state',st)}
function render(){
 const q=$('q').value.toLowerCase(),reg=$('reg').value,typ=$('typ').value;
 let jobs=DATA.jobs.filter(j=>{const s=st[j.url]||{};
  if(q&&!(j.firma+' '+j.titel+' '+j.kontakt+' '+j.ort).toLowerCase().includes(q))return false;
  if(reg&&!(j.region||'').includes(reg))return false;
  if(typ&&j.typ!==typ)return false;
  if($('fn').checked&&!j.neu)return false;
  if($('fp').checked&&j.portal!=='nicht auf karriere.at gefunden')return false;
  if($('fd').checked&&s.called)return false;return true});
 const due=DATA.jobs.filter(j=>{const s=st[j.url]||{};return s.follow&&s.follow<=today});
 const d=$('due');if(due.length){d.style.display='block';d.innerHTML='<b>Wiedervorlage fällig ('+due.length+'):</b> '+[...new Set(due.map(j=>esc(j.firma)))].join(', ')}else d.style.display='none';
 const by={};jobs.forEach(j=>(by[j.firma]=by[j.firma]||[]).push(j));
 const firms=Object.keys(by).sort((a,b)=>(by[b].some(j=>j.neu)-by[a].some(j=>j.neu))||(by[b].length-by[a].length)||a.localeCompare(b));
 if(!firms.length){$('list').innerHTML='<div class="empty">Keine Treffer für diese Filter.</div>';return}
 $('list').innerHTML=firms.map(f=>{const js=by[f].sort((a,b)=>b.neu-a.neu||a.tage-b.tage);const first=js[0];
  return '<section class="firm"><div class="fh"><div><b>'+esc(f)+'</b> <span>'+js.length+(js.length>1?' Stellen':' Stelle')+' · '+esc(first.region||'')+'</span></div><span><a href="'+esc(first.karriere)+'" target="_blank" rel="noopener">Karriereseite</a></span></div>'+
  js.map(j=>{const s=st[j.url]||{};
   return '<div class="job'+(j.neu?' new':'')+(s.called?' done':'')+'" data-u="'+esc(j.url)+'"><div class="t"><a href="'+esc(j.url)+'" target="_blank" rel="noopener">'+esc(j.titel)+'</a>'+
   '<span class="tag">'+j.typ+'</span>'+(j.neu?'<span class="tag">NEU</span>':'')+'<span class="tag">seit '+j.tage+' Tg. gesehen</span><span class="tag">'+esc(j.portal)+'</span>'+(j.ort?'<span class="tag">'+esc(j.ort)+'</span>':'')+'</div>'+
   (j.kontakt?'<div class="sub">Ansprechpartner: '+esc(j.kontakt)+'</div>':'')+
   '<div class="hk">„'+esc(j.hook)+'“ <button data-c="'+esc(j.hook)+'">Kopieren</button></div>'+
   '<div class="row"><label class="c"><input type="checkbox" data-k="called"'+(s.called?' checked':'')+'> angerufen</label>'+
   '<input type="text" data-k="note" placeholder="Notiz" value="'+esc(s.note||'')+'"><label class="c">Wiedervorlage <input type="date" data-k="follow" value="'+esc(s.follow||'')+'"></label></div></div>'}).join('')+'</section>'}).join('')}
document.addEventListener('input',e=>{const el=e.target;if(!el.dataset.k)return;const u=el.closest('.job').dataset.u;const s=st[u]=st[u]||{};
 s[el.dataset.k]=el.type==='checkbox'?el.checked:el.value;save();if(el.dataset.k!=='note')render()});
document.addEventListener('click',e=>{const c=e.target.dataset&&e.target.dataset.c;if(c){navigator.clipboard.writeText(c);e.target.textContent='Kopiert ✓'}});
['q','reg','typ','fn','fp','fd'].forEach(i=>$(i).addEventListener('input',render));render();
</script></body></html>"""


def write_html(rows, path, today):
    data = json.dumps({"date": today, "jobs": rows}, ensure_ascii=False).replace("</", "<\\/")
    open(path, "w", encoding="utf-8").write(PAGE.replace("__DATA__", data))


def main():
    only = None
    if "--only" in sys.argv:
        only = {x.strip().lower() for x in sys.argv[sys.argv.index("--only") + 1].split(",")}
    try: state = json.load(open("state.json"))
    except Exception: state = {}
    today = datetime.date.today().isoformat()
    todayd = datetime.date.today()
    rows, report, all_companies = [], [], []
    budget = [60]  # max. karriere.at-Prüfungen pro Lauf
    for row in csv.DictReader(open("companies.csv", encoding="utf-8")):
        name, dom = row["name"].strip(), row["domain"].strip()
        row["region"] = region_of(row.get("ort", ""))
        all_companies.append(row)
        if only and name.lower() not in only: continue
        if not dom: report.append((name, "KEINE_DOMAIN", "", "", 0)); continue
        res = crawl(dom, row.get("career_url", "").strip())
        rel = [j for j in res["jobs"] if relevant(j)]
        prev = state.get(name, {})
        old = prev.get("seen", {})
        if isinstance(old, list):  # alter Stand: Liste -> Datum
            old = {h: prev.get("last", today) for h in old}
        portal_old = prev.get("portal", {})
        first_run = name not in state
        seen_new, portal_new = {}, {}
        for j in rel:
            h = hid(j["url"])
            first = old.get(h, today); seen_new[h] = first
            neu = h not in old and not first_run
            tage = (todayd - datetime.date.fromisoformat(first)).days
            pstat = portal_old.get(h)
            if pstat not in ("auch auf karriere.at", "nicht auf karriere.at gefunden"):
                pstat = portal_check(name, j["title"], budget)
            if pstat in ("nicht geprüft",): pass
            portal_new[h] = pstat
            contact = extract_contact(j["url"]) if (j["url"] != res["url"] and h not in old) else ""
            rows.append({"firma": name, "hash": h, "titel": j["title"], "ort": j["location"], "url": j["url"],
                         "kontakt": contact, "neu": neu, "tage": tage, "typ": job_type(j["title"]),
                         "portal": pstat, "region": region_of(row.get("ort", "")), "karriere": res["url"],
                         "hook": hook(name, j["title"], tage)})
        state[name] = {"seen": seen_new, "portal": portal_new, "last": today, "career_url": res["url"]}
        report.append((name, res["status"], res["ats"], res["url"], len(rel)))
        print(f"{name:28} {res['status']:24} {res['ats']:16} Treffer: {len(rel)}", flush=True)
    json.dump(state, open("state.json", "w"), ensure_ascii=False, indent=1)
    with open(f"anrufliste_{today}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Firma", "Stelle", "Bereich", "Ort", "Region", "Ansprechpartner", "Neu", "Tage gesehen", "Portal", "Aufhänger", "Link"])
        w.writerows([(r["firma"], r["titel"], r["typ"], r["ort"], r["region"], r["kontakt"], "NEU" if r["neu"] else "",
                      r["tage"], r["portal"], r["hook"], r["url"]) for r in rows])
    write_html(rows, f"anrufliste_{today}.html", today)
    with open(f"status_{today}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["Firma", "Status", "ATS", "Karriereseite", "Treffer"]); w.writerows(report)
    print(f"\n{len(rows)} Cloud/Data/Infra-Stellen -> anrufliste_{today}.html / .csv")
    bad = [r for r in report if r[1] != "OK"]
    print(f"{len(bad)} Firmen brauchen Nacharbeit (siehe status_{today}.csv)")
    try:
        import sync_supabase
        sync_supabase.sync(all_companies, rows, report, today)
    except Exception as e:  # Sync darf den Lauf nie zum Absturz bringen
        print(f"::warning::Supabase-Sync nicht ausgeführt: {e}")


if __name__ == "__main__":
    main()
