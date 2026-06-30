import os
DB_PW=os.environ.get("MYSQL_PASSWORD") or os.environ.get("DB_PW","")  # set before running
import dock, json, urllib.parse, urllib.request, re, html, time, csv
MAILTO="ryanackett@gmail.com"
UA={"User-Agent":f"fluxapp-enrich/1.0 (mailto:{MAILTO})"}
GOOD={"journal-article","proceedings-article","book-chapter","posted-content","report","monograph","reference-entry"}

def q(sql):
    o=dock.exec_run(["mysql","-urackett",f"-p{DB_PW}","fluxdb","-N","--default-character-set=utf8mb4","-e",sql])
    return "\n".join(l for l in o.splitlines() if "Using a password" not in l)
def getj(url,t=30):
    with urllib.request.urlopen(urllib.request.Request(url,headers=UA),timeout=t) as r: return json.load(r)
def strip_tags(s): return re.sub(r"<[^>]+>"," ",html.unescape(s or ""))
def norm(s): return re.sub(r"[^a-z0-9]+"," ",(s or "").lower()).strip()
def tscore(a,b):
    A=set(norm(strip_tags(a)).split()); B=set(norm(strip_tags(b)).split())
    return len(A&B)/max(len(A|B),1)
def clean_abs(a):
    if not a: return None
    a=re.sub(r"<jats:title>.*?</jats:title>"," ",a,flags=re.I|re.S)
    a=re.sub(r"<[^>]+>"," ",html.unescape(a)); a=re.sub(r"\s+"," ",a).strip()
    a=re.sub(r"^abstract[:\s]+","",a,flags=re.I).strip()
    return a or None
def doi_in(text):
    m=re.search(r"10\.\d{4,9}/[^\s\"'<>)\]]+", text or "")
    return m.group(0).rstrip(".,;)]") if m else None
def cite_title(cit):
    m=re.search(r"[“\"](.+?)[”\"]", cit or "")
    return m.group(1).strip().rstrip(".") if m else None
def cr_by_doi(doi):
    try: return getj(f"https://api.crossref.org/works/{urllib.parse.quote(doi)}?mailto={MAILTO}")["message"]
    except Exception: return None
def cr_search(title):
    try:
        items=getj("https://api.crossref.org/works?rows=5&mailto="+MAILTO+"&query.bibliographic="+urllib.parse.quote(title))["message"]["items"]
    except Exception: return None,0.0
    best,bs=None,0.0
    for it in items:
        ct=" ".join(it.get("title") or [])
        if not ct or (it.get("type") or "") not in GOOD: continue
        if (it.get("DOI") or "").startswith("10.3410"): continue
        sc=tscore(title,ct)
        if sc>bs: best,bs=it,sc
    return best,round(bs,3)
def s2(doi):
    try: return clean_abs(getj("https://api.semanticscholar.org/graph/v1/paper/DOI:"+urllib.parse.quote(doi)+"?fields=abstract").get("abstract"))
    except Exception: return None

# LeadAuthor + SiteName per pub
info={}
for line in q("SELECT p.PubID, COALESCE(p.LeadAuthor,''), "
              "COALESCE(GROUP_CONCAT(DISTINCT NULLIF(s.SiteName,'') SEPARATOR ' | '),'') "
              "FROM Publication p LEFT JOIN Experiment e ON e.PubID=p.PubID "
              "LEFT JOIN Site s ON s.SiteID=e.SiteID GROUP BY p.PubID,p.LeadAuthor ORDER BY p.PubID;").split("\n"):
    parts=(line.split("\t")+["",""])[:3]; info[parts[0]]=(parts[1],parts[2])

rows=q("SELECT PubID, COALESCE(PubYear,''), COALESCE(Link,''), "
       "CASE WHEN Abstract IS NULL OR Abstract='' THEN 0 ELSE 1 END, "
       "REPLACE(REPLACE(Citation,'\\n',' '),'\\t',' '), PubTitle FROM Publication ORDER BY PubID;").split("\n")

out=[]
for i,r in enumerate(rows):
    pid,year,link,hasabs,cit,title=r.split("\t",5)
    hasabs=hasabs=="1"; is_ds="(dataset)" in cit.lower()
    cdoi=doi_in(cit); ldoi=doi_in(link); doi=cdoi or ldoi
    rec=dict(PubID=pid,PubYear=year,Verdict="",DOISource="",MatchDOI=doi or "",
             Link="",AbsSource="",Abstract="",PubTitle=title,RealTitle="",
             LeadAuthor=info.get(pid,("",""))[0],SiteName=info.get(pid,("",""))[1],Note="")
    cr=None
    if doi:
        rec["DOISource"]="citation" if cdoi else "existing-link"
        rec["Verdict"]="citation-doi" if cdoi else "link-doi"
        cr=cr_by_doi(doi)
        rec["RealTitle"]=(" ".join((cr or {}).get("title") or []) and strip_tags(" ".join(cr["title"]))) or (cite_title(cit) or "")
        if not link: rec["Link"]="https://doi.org/"+doi
        if not hasabs:
            ab=clean_abs((cr or {}).get("abstract")) or s2(doi)
            if ab: rec["Abstract"]=ab; rec["AbsSource"]="crossref/s2"
    elif is_ds:
        rec["Verdict"]="dataset-skip"; rec["Note"]="GRACEnet — see gracenet_candidates.csv"
    else:
        cr,sc=cr_search(title); rec["MatchDOI"]=(cr or {}).get("DOI","") if cr else ""
        rec["RealTitle"]=strip_tags(" ".join((cr or {}).get("title") or [])) if cr else ""
        if cr and sc>=0.80:
            rec["Verdict"]="title-search"; rec["DOISource"]="crossref-search"
            d=cr.get("DOI")
            if d and not link: rec["Link"]="https://doi.org/"+d
            if d and not hasabs:
                ab=clean_abs(cr.get("abstract")) or s2(d)
                if ab: rec["Abstract"]=ab; rec["AbsSource"]="crossref/s2"
        else:
            rec["Verdict"]="no-doi"; rec["Note"]=f"best title-search score {sc}"
    # title-match flag for the user
    if rec["RealTitle"]:
        rec["Note"]=(rec["Note"]+f" titlematch={tscore(title,rec['RealTitle']):.2f}").strip()
    out.append(rec)
    print(f"[{i+1}/95] PubID {pid}: {rec['Verdict']} link={'Y' if rec['Link'] else '-'} abs={'Y' if rec['Abstract'] else '-'} rt={'Y' if rec['RealTitle'] else '-'}")
    time.sleep(0.25)

cols=["PubID","PubYear","Verdict","DOISource","MatchDOI","Link","AbsSource","Abstract",
      "PubTitle","RealTitle","LeadAuthor","SiteName","Note"]
with open("/var/home/rackett/Web Projects/fluxapp/ETL/pub_enrichment.csv","w",newline="",encoding="utf-8") as fh:
    w=csv.DictWriter(fh,fieldnames=cols); w.writeheader()
    for rec in out: w.writerow({c:rec.get(c,"") for c in cols})
from collections import Counter
print("\nverdicts:",dict(Counter(r["Verdict"] for r in out)))
print("Links to set:",sum(1 for r in out if r["Link"]),"| Abstracts to set:",sum(1 for r in out if r["Abstract"]),
      "| RealTitle filled:",sum(1 for r in out if r["RealTitle"]))
# flag suspicious title mismatches
print("\nLOW title match (stored PubTitle vs authoritative RealTitle):")
for r in out:
    if r["RealTitle"]:
        ms=tscore(r["PubTitle"],r["RealTitle"])
        if ms<0.6: print(f"  PubID {r['PubID']} ms={ms:.2f}: '{r['PubTitle'][:50]}' vs '{r['RealTitle'][:50]}'")
