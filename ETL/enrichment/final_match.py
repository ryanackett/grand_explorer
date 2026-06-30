import os
DB_PW=os.environ.get("MYSQL_PASSWORD") or os.environ.get("DB_PW","")  # set before running
"""Match the 32 GRACEnet pubs against the complete Ag Data Commons GRACEnet
collection (adc_studies.json) locally: city (required) + study-name/acronym with
aliases. For each match, fetch DOI/abstract and compose a proper citation.
Writes ETL/gracenet_candidates.csv."""
import json, re, html, time, csv, urllib.request
import dock
UA={"User-Agent":"fluxapp-enrich/1.0 (mailto:ryanackett@gmail.com)"}
OUT="/var/home/rackett/Web Projects/fluxapp/ETL/gracenet_candidates.csv"
recs=json.load(open("/tmp/adc_studies.json"))

def norm(s): return re.sub(r"[^a-z0-9]+"," ",(s or "").lower()).strip()
def getj(u):
    with urllib.request.urlopen(urllib.request.Request(u,headers=UA),timeout=45) as r: return json.load(r)
def clean(s): return re.sub(r"\s+"," ",re.sub(r"<[^>]+>"," ",html.unescape(s or ""))).strip()
BOIL=set("greenhouse gas reduction through agricultural carbon enhancement network study for in and the of to from data resilient economic practices reap nutrient use outcomes dataset co".split())
def stoks(s): return set(t for t in norm(s).split() if t not in BOIL and len(t)>1)

# Precompute each ADC record: city(norm), study-name tokens, url, doi, id, title.
adc=[]
for r in recs:
    t=r["title"]
    m=re.search(r"\bin (.+)$", t.strip())
    loc=norm(m.group(1).split(",")[0]) if m else ""
    name=t.split(" Study for Greenhouse")[0]
    adc.append(dict(city=loc, nametoks=stoks(name), rawname=set(norm(name).split()), title=t, url=r["url"], doi=r["doi"],
                    id=re.search(r"/(\d+)$", r["url"]).group(1) if re.search(r"/(\d+)$", r["url"]) else None))

# alias: pub-side acronym -> expansion words to align with ADC study names
ALIAS={"gwp":"global warming potential","ferec":"fort ellis research and extension center",
       "smt":"smt","phace":"phace","nvnd":"nvnd","reap":""}
CITYPREFIX={"MNMO":"morris","MNSP":"st paul","MNRS":"st paul","NDMA":"mandan","COFO":"fort collins",
            "CONU":"nunn","INWL":"west lafayette","MTSI":"sidney","SDBR":"brookings","SDAL":"brookings",
            "NEME":"lincoln","WYPH":"cheyenne","KYBG":"bowling green","IAAM":"ames","IDKM":"kimberly"}
CITY_TOKENS=set("morris st paul mandan fort collins nunn west lafayette sidney brookings lincoln cheyenne bowling green ames kimberly university park watkinsville lubbock pendleton".split())
def cityname(s):
    n=norm(s)
    for c in ["bowling green","west lafayette","fort collins","st paul","university park",
              "mandan","sidney","brookings","lincoln","cheyenne","ames","kimberly","morris",
              "nunn","watkinsville","lubbock","pendleton"]:
        if c in n: return c
def city_from_site(sn):
    for p,c in CITYPREFIX.items():
        if (sn or "").upper().startswith(p): return c
CURATORS={"bruce vandenberg","erin antognoli","nancy sanders","adam martin"}

def q(sql):
    o=dock.exec_run(["mysql","-urackett",f"-p{DB_PW}","fluxdb","-N","--default-character-set=utf8mb4","-e",sql])
    return "\n".join(l for l in o.splitlines() if "Using a password" not in l)
info={}
for line in q("SELECT p.PubID, COALESCE(p.LeadAuthor,''), COALESCE(GROUP_CONCAT(DISTINCT NULLIF(s.SiteName,'') SEPARATOR ' | '),'') "
              "FROM Publication p LEFT JOIN Experiment e ON e.PubID=p.PubID LEFT JOIN Site s ON s.SiteID=e.SiteID "
              "GROUP BY p.PubID,p.LeadAuthor;").split("\n"):
    pr=(line.split("\t")+["",""])[:3]; info[pr[0]]=(pr[1],pr[2])
rows=q("SELECT PubID, REPLACE(REPLACE(PubTitle,'\\n',' '),'\\t',' '), REPLACE(REPLACE(Citation,'\\n',' '),'\\t',' ') "
       "FROM Publication WHERE Citation LIKE '%(dataset)%' ORDER BY PubID;").split("\n")
def sn_cite(c):
    m=re.search(r"\)\.\s*(.*?)\s+Study for Greenhouse",c,re.I); return m.group(1) if m else None

def fetch_meta(aid, lead, url, doi):
    art=getj(f"https://api.figshare.com/v2/articles/{aid}")
    persons=[clean(a.get("full_name")) for a in art.get("authors",[]) if clean(a.get("full_name")).lower() not in CURATORS]
    la=(lead or "").strip().lower()
    pi=next((p for p in persons if la and la.split()[-1] in p.lower()), None) or (persons[0] if persons else lead or "USDA ARS")
    yr=(art.get("published_date") or "")[:4] or "n.d."
    ab=clean(art.get("description",""))
    if ab and art.get("title") and ab.startswith(art["title"]): ab=ab[len(art["title"]):].strip()
    ab=re.split(r"Resources in this dataset:", ab, 1)[0].strip()   # drop Figshare file-listing block
    if len(ab)<30: ab=""                                          # no real abstract in the record
    link=f"https://doi.org/{doi}" if doi else url
    cite=f"{pi} ({yr}). {art.get('title')} [Dataset]. USDA Agricultural Research Service, Ag Data Commons. {link}"
    return art.get("title",""), ab, cite, link, (doi or "")

out=[]
for r in rows:
    pid,title,cit=r.split("\t",2)
    la,site=info.get(pid,("",""))
    city=cityname(title) or cityname(cit) or city_from_site(site)
    sname=sn_cite(cit)
    ident=sname if (sname and stoks(sname)) else title
    ptoks=stoks(ident)-CITY_TOKENS
    for a,exp in ALIAS.items():                            # acronym -> expansion words
        if a in ptoks and exp: ptoks=(ptoks-{a})|set(exp.split())
    # The SiteName code embeds the study acronym (MTSINVND->NVND, WYPHACE->PHACE),
    # so keep a spaceless blob of identifier+sitename for substring corroboration.
    blob=norm(ident+" "+site).replace(" ","")
    pub_rawname=set(norm(ident).split())-CITY_TOKENS      # the pub's own study name
    def corrob(a):                                         # name-corroboration of a record
        if ptoks & a["nametoks"]: return True
        if pub_rawname & a["rawname"]: return True         # direct study-name overlap
        return any(len(nt)>=3 and nt not in BOIL and nt in blob for nt in a["nametoks"])
    def score(a): return (1 if corrob(a) else 0, len(ptoks & a["nametoks"]))

    distinctive=pub_rawname-BOIL                          # pub's own non-boilerplate name words
    atcity=[a for a in adc if city and a["city"]==city]
    pick=None; status="REVIEW"
    if city and atcity:
        scored=sorted(atcity, key=score, reverse=True); pick=scored[0]
        if corrob(pick) and (len(scored)==1 or score(pick)>score(scored[1])):
            status="confident"                            # city + name corroboration
        elif len(atcity)==1 and not distinctive:
            status="confident"                            # all-boilerplate name -> trust unique city
        # else: distinctive name with no record match -> REVIEW (could be absent)
    elif city and not atcity:
        status="REVIEW"                                   # city known but not in collection
    else:
        # City unknown: accept only a unique, distinctively-named study (e.g. PAHAW).
        named=[a for a in adc if distinctive and corrob(a)]
        if len(named)==1: pick=named[0]; status="confident"
        elif named: pick=named[0]
    rec=dict(PubID=pid,DBTitle=title[:48],LeadAuthor=la,SiteName=site,City=city or "",Status=status,
             Link="",DOI="",RealTitle="",Abstract="",Citation="",
             Guess=pick["title"][:60] if pick else "NO CITY MATCH", GuessURL=pick["url"] if pick else "")
    if status=="confident" and pick:
        try:
            rt,ab,cite,link,doi=fetch_meta(pick["id"], la, pick["url"], pick["doi"])
            rec.update(Link=link,DOI=doi,RealTitle=rt,Abstract=ab,Citation=cite); time.sleep(0.25)
        except Exception as e:
            rec["Status"]="REVIEW"; print(f"PubID {pid} meta-err {e}")
    out.append(rec)
    print(f"PubID {pid:>2} [{rec['Status']:<9}] city={city or '?':<14} -> {rec['RealTitle'][:46] or rec['Guess'][:46]}")

cols=["PubID","DBTitle","LeadAuthor","SiteName","City","Status","Link","DOI","RealTitle","Abstract","Citation","Guess","GuessURL"]
with open(OUT,"w",newline="",encoding="utf-8") as fh:
    w=csv.DictWriter(fh,fieldnames=cols); w.writeheader()
    for r in out: w.writerow({c:r.get(c,"") for c in cols})
conf=sum(1 for r in out if r["Status"]=="confident")
print(f"\nconfident={conf}/32, review={32-conf}")
