"""Schreibt die Crawler-Ergebnisse in Supabase (REST/PostgREST, nur 'requests' nötig).

Nutzt die Umgebungsvariablen SUPABASE_URL und SUPABASE_SERVICE_KEY (GitHub-Secrets).
Fehlen sie, wird der Sync übersprungen – der Crawler läuft dann wie bisher.
Ein Sync-Fehler bricht den Crawler nie ab (CSV, HTML und state.json bleiben erhalten).
"""
import os, re, datetime
import requests

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
CHUNK = 200


class Supa:
    def __init__(self, url, key):
        self.base = url.rstrip("/") + "/rest/v1/"
        self.h = {"apikey": key, "Authorization": "Bearer " + key, "Content-Type": "application/json"}
        self.s = requests.Session()

    def _check(self, r, what):
        if r.status_code >= 300:
            raise RuntimeError(f"{what}: HTTP {r.status_code} {r.text[:300]}")
        return r

    def upsert(self, table, rows, conflict):
        for i in range(0, len(rows), CHUNK):
            part = rows[i:i + CHUNK]
            h = dict(self.h, Prefer="resolution=merge-duplicates,return=minimal")
            r = self.s.post(self.base + table, params={"on_conflict": conflict}, headers=h, json=part, timeout=60)
            self._check(r, f"upsert {table}")

    def select(self, table, params):
        r = self.s.get(self.base + table, params=params, headers=self.h, timeout=60)
        return self._check(r, f"select {table}").json()

    def patch(self, table, params, body):
        h = dict(self.h, Prefer="return=minimal")
        r = self.s.patch(self.base + table, params=params, headers=h, json=body, timeout=60)
        self._check(r, f"patch {table}")


def split_contact(s):
    """'Name · mail · Telefon' -> (name, email, telefon)"""
    parts = [p.strip() for p in (s or "").split("·") if p.strip()]
    email = next((p for p in parts if EMAIL.fullmatch(p)), "")
    rest = [p for p in parts if p != email]
    phone = next((p for p in rest if re.fullmatch(r"[+\d][\d\s/().-]{5,}", p)), "")
    name = next((p for p in rest if p != phone), "")
    return name, email, phone


def sync(companies, rows, report, today):
    """companies: Zeilen aus companies.csv (+ 'region'); rows: relevante Stellen; report: (name, status, ats, url, n)."""
    url, key = os.environ.get("SUPABASE_URL", "").strip(), os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        print("Supabase-Sync übersprungen (keine Zugangsdaten gesetzt).")
        return False
    try:
        db = Supa(url, key)
        rep = {r[0]: r for r in report}

        # 1) Firmen (Status/Notizen/Wiedervorlage werden NIE überschrieben – sie stehen nicht im Payload)
        full, basic = [], []
        for c in companies:
            nm = c["name"].strip(); r = rep.get(nm)
            d = {"name": nm, "domain": c.get("domain", "").strip() or None,
                 "ort": c.get("ort", "").strip() or None, "region": c.get("region") or None,
                 "size_est": c.get("groesse_schaetzung", "").strip() or None}
            if r:  # in diesem Lauf geprüft -> Crawl-Spalten mitschreiben
                d.update({"career_url": r[3] or c.get("career_url", "").strip() or None,
                          "crawl_status": r[1], "ats": r[2] or None, "last_crawled": today})
                full.append(d)
            else:  # nicht geprüft (z. B. --only): vorhandene Crawl-Daten nicht anfassen
                basic.append(d)
        if full: db.upsert("companies", full, "name")
        if basic: db.upsert("companies", basic, "name")
        comp = full + basic
        ids = {c["name"]: c["id"] for c in db.select("companies", {"select": "id,name"})}

        # 2) Stellen
        jobs, contacts = [], {}
        for r in rows:
            cid = ids.get(r["firma"])
            if not cid: continue
            first = (datetime.date.fromisoformat(today) - datetime.timedelta(days=r["tage"])).isoformat()
            jobs.append({"hash": r["hash"], "company_id": cid, "title": r["titel"], "area": r["typ"],
                         "ort": r["ort"] or None, "region": r["region"] or None, "url": r["url"],
                         "portal": r["portal"], "hook": r["hook"], "first_seen": first,
                         "last_seen": today, "active": True})
            if r["kontakt"]:
                n, e, p = split_contact(r["kontakt"])
                nm = n or e
                if nm: contacts[(cid, nm)] = {"company_id": cid, "name": nm, "email": e or None,
                                              "phone": p or None, "source": r["url"]}
        if jobs: db.upsert("jobs", jobs, "hash")
        if contacts: db.upsert("contacts", list(contacts.values()), "company_id,name")

        # 3) Stellen, die bei erfolgreich geprüften Firmen nicht mehr online sind -> inaktiv
        ok_ids = [ids[n] for n, st, *_ in report if st == "OK" and n in ids]
        if ok_ids:
            db.patch("jobs", {"company_id": "in.(" + ",".join(map(str, ok_ids)) + ")",
                              "last_seen": "lt." + today, "active": "eq.true"}, {"active": False})
        print(f"Supabase-Sync OK: {len(comp)} Firmen, {len(jobs)} Stellen, {len(contacts)} Kontakte.")
        return True
    except Exception as e:
        print(f"::warning::Supabase-Sync fehlgeschlagen: {e}")
        return False
