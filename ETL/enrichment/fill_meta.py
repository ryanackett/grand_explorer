"""Fill RealTitle/DOI/Abstract/Citation for every gracenet_candidates.csv row that
has a Link but no Abstract (the links the user confirmed), plus set PubID 44 to the
COFOARD2 record. Handles 4 link types: Ag Data Commons (Figshare), ADC DOI,
NAL GeoNetwork records, and journal-paper DOIs/PIIs. Preserves the user's Links;
only fills empty metadata fields."""
import urllib.request, json, re, html, time, csv
UA={"User-Agent":"fluxapp-enrich/1.0 (mailto:ryanackett@gmail.com)"}
CSV="/var/home/rackett/Web Projects/fluxapp/ETL/gracenet_candidates.csv"
CURATORS={"bruce vandenberg","erin antognoli","nancy sanders","adam martin"}
PII_DOI={"S0048969717323975":"10.1016/j.scitotenv.2017.09.054"}   # PubID 22 (no DOI in URL)

def getj(u):
    with urllib.request.urlopen(urllib.request.Request(u,headers=UA),timeout=45) as r: return json.load(r)
def gettext(u,accept="application/xml"):
    with urllib.request.urlopen(urllib.request.Request(u,headers={**UA,"Accept":accept}),timeout=45) as r:
        return r.read().decode("utf-8","replace")
def clean(s): return re.sub(r"\s+"," ",re.sub(r"<[^>]+>"," ",html.unescape(s or ""))).strip()
def strip_resources(ab): return re.split(r"Resources in this dataset:", ab)[0].strip()

def pick_author(persons, lead):
    persons=[p for p in persons if p and p.lower() not in CURATORS]
    la=(lead or "").strip().lower()
    return next((p for p in persons if la and la.split()[-1] in p.lower()), None) or (persons[0] if persons else (lead or "USDA Agricultural Research Service"))

import urllib.parse
def fig_article(aid): return getj(f"https://api.figshare.com/v2/articles/{aid}")
def fig_by_doi(doi):
    lst=getj("https://api.figshare.com/v2/articles?doi="+urllib.parse.quote(doi))
    return fig_article(lst[0]["id"]) if lst else None

def from_figshare(a, lead):
    rt=a.get("title",""); doi=a.get("doi","") or ""
    ab=strip_resources(clean(a.get("description","")))
    if ab and rt and ab.startswith(rt): ab=ab[len(rt):].strip()
    if len(ab)<30: ab=""
    yr=(a.get("published_date") or "")[:4] or "n.d."
    au=pick_author([x.get("full_name") for x in a.get("authors",[])], lead)
    link=f"https://doi.org/{doi}" if doi else a.get("url_public_html","")
    cite=f"{au} ({yr}). {rt} [Dataset]. USDA Agricultural Research Service, Ag Data Commons. {link}"
    return rt, doi, ab, cite

def from_geonetwork(uuid, lead):
    xml=gettext(f"https://geodata.nal.usda.gov/geonetwork/srv/api/records/{uuid}/formatters/xml")
    def first(p):
        m=re.search(p,xml,re.S); return clean(m.group(1)) if m else ""
    rt=first(r"<gmd:title>\s*<gco:CharacterString>(.*?)</gco:CharacterString>")
    ab=first(r"<gmd:abstract>\s*<gco:CharacterString>(.*?)</gco:CharacterString>")
    if ab and rt and ab.startswith(rt): ab=ab[len(rt):].strip()
    yr=(re.search(r"<gco:Date>(\d{4})",xml) or [None,""])[1] or "n.d."
    names=re.findall(r"<gmd:individualName>\s*<gco:CharacterString>(.*?)</gco:CharacterString>",xml)
    au=pick_author([clean(n) for n in dict.fromkeys(names)], lead)
    link=f"https://geodata.nal.usda.gov/geonetwork/srv/api/records/{uuid}"
    cite=f"{au} ({yr}). {rt} [Dataset]. USDA Agricultural Research Service. {link}"
    return rt, "", ab, cite

def from_doi_paper(doi, lead):
    m=getj(f"https://api.crossref.org/works/{urllib.parse.quote(doi)}?mailto=ryanackett@gmail.com")["message"]
    rt=" ".join(m.get("title") or [])
    auth=m.get("author",[])
    def fmt(a):
        g=a.get("given",""); return f"{a.get('family','')}, {g[0]}." if g else a.get("family","")
    al=[fmt(a) for a in auth]
    astr=", ".join(al[:-1])+(", & "+al[-1] if len(al)>1 else (al[0] if al else ""))
    yr=(m.get("issued") or {}).get("date-parts",[[None]])[0][0] or "n.d."
    jr=" ".join(m.get("container-title") or [])
    vol=m.get("volume",""); cite=f"{astr} ({yr}). {rt}. {jr}{', '+vol if vol else ''}. https://doi.org/{doi}"
    ab=clean(m.get("abstract","")) ; ab=re.sub(r"^Abstract\s*","",ab)
    if len(ab)<30:  # crossref has none -> try Semantic Scholar
        try: ab=clean(getj("https://api.semanticscholar.org/graph/v1/paper/DOI:"+urllib.parse.quote(doi)+"?fields=abstract").get("abstract") or "")
        except Exception: ab=""
    return rt, doi, ab, cite

def resolve(link, lead):
    l=link.strip()
    m=re.search(r"agdatacommons\.nal\.usda\.gov/articles/.*?/(\d+)\b", l)
    if m: return from_figshare(fig_article(m.group(1)), lead)
    m=re.search(r"(10\.15482/USDA\.ADC/\S+)", l)            # ADC DOI
    if m:
        a=fig_by_doi(m.group(1).rstrip("."));
        if a: return from_figshare(a, lead)
    m=re.search(r"geonetwork/srv/api/records/([0-9a-f-]{36})", l)
    if m: return from_geonetwork(m.group(1), lead)
    m=re.search(r"(10\.\d{4,9}/[^\s/]+(?:/[^\s?#]+)?)", l)   # journal DOI in URL
    if m: return from_doi_paper(m.group(1).rstrip("."), lead)
    m=re.search(r"/pii/(S\w+)", l)                           # ScienceDirect PII
    if m and m.group(1) in PII_DOI: return from_doi_paper(PII_DOI[m.group(1)], lead)
    return None

rows=list(csv.DictReader(open(CSV, encoding="utf-8-sig")))
# Set PubID 44 to the COFOARD2 record before filling.
for r in rows:
    if r["PubID"]=="44" and not r["Link"]:
        r["Link"]="https://doi.org/10.15482/USDA.ADC/1503997"

for r in rows:
    if not r.get("Link") or r.get("Abstract"):   # only fill rows with a link and no abstract yet
        continue
    try:
        res=resolve(r["Link"], r.get("LeadAuthor"))
    except Exception as e:
        print(f"PubID {r['PubID']}: ERR {e}"); continue
    if not res:
        print(f"PubID {r['PubID']}: unrecognized link type -> {r['Link'][:60]}"); continue
    rt,doi,ab,cite=res
    if not r.get("RealTitle"): r["RealTitle"]=rt
    if not r.get("DOI"): r["DOI"]=doi
    if not r.get("Abstract"): r["Abstract"]=ab
    if not r.get("Citation"): r["Citation"]=cite
    print(f"PubID {r['PubID']:>2}: {rt[:48]:<48} abs={len(ab)}c doi={doi or '-'}")
    time.sleep(0.3)

cols=list(rows[0].keys())
with open(CSV,"w",newline="",encoding="utf-8-sig") as fh:
    w=csv.DictWriter(fh,fieldnames=cols); w.writeheader(); w.writerows(rows)
filled=sum(1 for r in rows if r["Link"] and r["Abstract"])
print(f"\nrows with Link+Abstract now: {filled}; total rows: {len(rows)}")
