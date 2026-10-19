"""Denní kontrola indexace článků z vybraných zdrojů na centrum.cz.

1. Stáhne nové články zdrojů z API centrum.cz (od START) a přidá je k sledovaným.
2. Všechny sledované URL ověří v Search Console (URL Inspection API).
3. Uloží data/vysledky/<datum>.csv, doplní data/historie.csv a přegeneruje data/souhrn.md.

Env: GSC_SERVICE_ACCOUNT_JSON - klíč service accountu (obsah JSON nebo cesta k souboru)
     GSC_SITE_URL             - property, výchozí "sc-domain:centrum.cz"
Použití: python indexace.py [--date YYYY-MM-DD] [--import-csv soubor]
"""
import argparse, csv, json, os, time, urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

FEEDS = {28: "SportyŽivě", 84: "VědaŽivě.cz", 163: "Mobify.cz", 82: "Chalupáři-Zahrádkáři", 74: "MMAMAG.cz", 117: "VařímeDobroty.cz"}
START = "2026-10-15T22:00:00Z"  # 16. 10. 00:00 CEST
SITE = os.environ.get("GSC_SITE_URL", "sc-domain:centrum.cz")
API = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
ARTICLES = os.path.join(DATA, "clanky.json")
HISTORY = os.path.join(DATA, "historie.csv")
HIST_FIELDS = ["datum_kontroly", "zdroj", "publikovano", "url", "verdikt", "stav", "kanonicka_dle_google", "posledni_prochazeni", "chyba"]
CEST = timezone(timedelta(hours=2))


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.5.0"})
    return json.load(urllib.request.urlopen(req, timeout=60))


def fetch_articles(known):
    for fid, name in FEEDS.items():
        page = None
        while True:
            d = get_json(f"https://www.centrum.cz/api/hp/articles?feedId={fid}" + (f"&pageItem={page}" if page else ""))
            for a in d["items"]:
                url = f"https://www.centrum.cz/clanek/{a['slug']}"
                if a["feedId"] == fid and a["publishedAt"] >= START and url not in known:
                    known[url] = {"source": name, "title": a["title"], "publishedAt": a["publishedAt"],
                                  "url": url, "sourceUrl": a.get("url")}
            page = d.get("pageItem")
            if not d["items"] or not page or min(a["publishedAt"] for a in d["items"]) < START:
                break
    return known


def gsc_session():
    from google.oauth2 import service_account
    from google.auth.transport.requests import AuthorizedSession
    key = os.environ["GSC_SERVICE_ACCOUNT_JSON"].strip().strip("'")
    info = json.loads(key) if key.startswith("{") else json.load(open(key))
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/webmasters.readonly"])
    return AuthorizedSession(creds)


def inspect(session, url):
    for attempt in range(3):
        r = session.post(API, json={"inspectionUrl": url, "siteUrl": SITE, "languageCode": "cs"}, timeout=120)
        if r.ok:
            st = r.json()["inspectionResult"]["indexStatusResult"]
            return {"verdict": st.get("verdict", ""), "state": st.get("coverageState", "").replace("\xa0", " "),
                    "googleCanonical": st.get("googleCanonical", ""), "lastCrawl": st.get("lastCrawlTime", ""), "error": ""}
        if r.status_code not in (429, 500, 503):
            break
        time.sleep(10 * (attempt + 1))
    return {"verdict": "", "state": "", "googleCanonical": "", "lastCrawl": "", "error": f"{r.status_code} {r.text[:200]}"}


def canon_kind(a, g):
    return "" if not g else "centrum.cz" if g == a["url"] else "zdroj" if g == a["sourceUrl"] else "jina: " + g


def cest(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(CEST).strftime("%Y-%m-%d %H:%M")


def write_run(day, arts, results):
    rows = []
    for a in sorted(arts.values(), key=lambda a: (a["source"], a["publishedAt"])):
        x = results[a["url"]]
        rows.append({"datum_kontroly": day, "zdroj": a["source"], "publikovano": cest(a["publishedAt"]), "url": a["url"],
                     "verdikt": x["verdict"], "stav": x["state"], "kanonicka_dle_google": canon_kind(a, x["googleCanonical"]),
                     "posledni_prochazeni": x["lastCrawl"], "chyba": x["error"]})
    with open(os.path.join(DATA, "vysledky", f"{day}.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=HIST_FIELDS + ["titulek"])
        w.writeheader()
        for r in rows:
            w.writerow({**r, "titulek": arts[r["url"]]["title"]})
    old = [r for r in read_history() if r["datum_kontroly"] != day]
    with open(HISTORY, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=HIST_FIELDS)
        w.writeheader()
        w.writerows(old + rows)


def read_history():
    if not os.path.exists(HISTORY):
        return []
    return list(csv.DictReader(open(HISTORY, encoding="utf-8-sig")))


def write_summary(arts):
    hist = read_history()
    days = sorted({r["datum_kontroly"] for r in hist})
    by_day = defaultdict(dict)
    for r in hist:
        by_day[r["datum_kontroly"]][r["url"]] = r
    out = ["# Indexace článků zdrojů na centrum.cz", "",
           "Kanonická URL podle Googlu: **centrum.cz** = Google indexuje kopii na centrum.cz, "
           "**zdroj** = Google přijal canonical na web zdroje.", "", "## Vývoj po dnech kontroly", "",
           "| Datum kontroly | Zdroj | Článků | centrum.cz | zdroj | jiné / chyba |", "|---|---|---|---|---|---|"]
    for d in days:
        per = defaultdict(Counter)
        for r in by_day[d].values():
            k = r["kanonicka_dle_google"]
            per[r["zdroj"]]["centrum.cz" if k == "centrum.cz" else "zdroj" if k == "zdroj" else "jine"] += 1
        tot = Counter()
        for s in sorted(per):
            c = per[s]; tot.update(c)
            out.append(f"| {d} | {s} | {sum(c.values())} | {c['centrum.cz']} | {c['zdroj']} | {c['jine']} |")
        out.append(f"| **{d}** | **celkem** | **{sum(tot.values())}** | **{tot['centrum.cz']}** | **{tot['zdroj']}** | **{tot['jine']}** |")
    out += ["", "## Změny kanonické URL mezi kontrolami", ""]
    changes = []
    for prev, cur in zip(days, days[1:]):
        for u, r in by_day[cur].items():
            p = by_day[prev].get(u)
            if p and p["kanonicka_dle_google"] != r["kanonicka_dle_google"]:
                changes.append(f"| {cur} | {r['zdroj']} | {r['publikovano']} | {p['kanonicka_dle_google'] or '–'} → {r['kanonicka_dle_google'] or '–'} | [{arts[u]['title'][:70]}]({u}) |")
    out += (["| Datum kontroly | Zdroj | Publikováno | Změna | Článek |", "|---|---|---|---|---|"] + changes) if changes else ["Zatím žádné změny."]
    open(os.path.join(DATA, "souhrn.md"), "w", encoding="utf-8").write("\n".join(out) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now(CEST).strftime("%Y-%m-%d"))
    ap.add_argument("--import-csv", help="výsledky gsc_inspect.py k uložení jako běh pro --date (bez volání API)")
    args = ap.parse_args()
    arts = json.load(open(ARTICLES, encoding="utf-8")) if os.path.exists(ARTICLES) else {}
    if args.import_csv:
        results = {r["url"]: {"verdict": r["verdict"], "state": r["coverageState"].replace("\xa0", " "),
                              "googleCanonical": r["googleCanonical"], "lastCrawl": r["lastCrawlTime"], "error": r["error"]}
                   for r in csv.DictReader(open(args.import_csv, encoding="utf-8"))}
    else:
        n = len(arts)
        arts = fetch_articles(arts)
        json.dump(arts, open(ARTICLES, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"sledovaných článků: {len(arts)} (nových {len(arts) - n})")
        s = gsc_session()
        urls = list(arts)
        with ThreadPoolExecutor(4) as ex:
            results = dict(zip(urls, ex.map(lambda u: inspect(s, u), urls)))
    write_run(args.date, arts, results)
    write_summary(arts)
    c = Counter(canon_kind(arts[u], r["googleCanonical"]) or "chyba" for u, r in results.items())
    print(args.date, dict(c))


if __name__ == "__main__":
    main()
