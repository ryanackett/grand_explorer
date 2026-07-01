#!/usr/bin/env python3
"""
Generate the soil & climate enrichment worksheets for the n2odb Treatment and
Site tables.

PRIMARY (authoritative) source: the Dorich export `Summary_V1.csv`, which already
carries per-(Reference, Treatment) soil physics/chemistry and site climate:

    Sand / Silt / Clay -> Treatment.SandMean / SiltMean / ClayMean   (%)
    pH                 -> Treatment.pHMean
    BD                 -> Treatment.BulkDensityMean                  (g/cm3)
    soilC_perc         -> Treatment.SOMMean   (stored AS-IS: organic C %)
    MeanPrec_mm        -> Site.MAP            (mm/yr)
    MeanTemp_c         -> Site.MAT            (degC)

The live Treatment row joins Summary on (Experiment.ExperimentName == Reference,
Treatment.TreatmentName == Treatment); values are site-level. Legacy rows (no
ExperimentName/TreatmentName) have no Summary match.

FALLBACK (gridded, modelled) source, used only where Summary has nothing AND the
field is still missing in the DB, flagged in the Source column:
    soil  -> SoilGrids v2.0 (ISRIC), 0-30 cm depth-weighted mean
    clim  -> NASA POWER climatology, T2M + PRECTOTCORR

Inputs (dumped from the live DB by the caller, see ETL/README.md):
    /tmp/treatments_dump.tsv   TreatmentID, ExperimentName, TreatmentName, SiteID,
                               Latitude, Longitude, + current Sand/Silt/Clay/pH/BD/SOM
    /tmp/sites_dump.tsv        SiteID, Latitude, Longitude, MAP, MAT
    Summary_V1.csv  (read latin-1)

Outputs (reviewed, then applied with the reversible mgmt commands):
    ETL/soil_enrichment.csv    TreatmentID, SandMean, SiltMean, ClayMean, pHMean,
                               BulkDensityMean, SOMMean, Source
    ETL/site_climate.csv       SiteID, MAP, MAT, Source

Only fields currently MISSING in the DB are emitted (NULL for Treatment; NULL or 0
placeholder for Site MAP/MAT), so nothing authoritative is ever overwritten.

Network: SoilGrids + NASA POWER are queried once per Site (lat/lon) and cached to
ETL/_grid_cache.json so re-runs are cheap and offline-resumable (--offline uses
only the cache + Summary).
"""

import csv
import json
import os
import sys
import time
import urllib.request
import urllib.error

ETL = os.path.dirname(os.path.abspath(__file__))
SUMMARY = os.environ.get(
    "SUMMARY_CSV",
    "/var/home/rackett/Development/DorichScraper/DorichData/Summary_V1.csv")
TREAT_TSV = os.environ.get("TREAT_TSV", "/tmp/treatments_dump.tsv")
SITE_TSV = os.environ.get("SITE_TSV", "/tmp/sites_dump.tsv")
CACHE = os.path.join(ETL, "_grid_cache.json")

NA = {"", "na", "nan", "n/a", "null", "none"}


def num(v):
    """Parse a numeric cell, treating NA-ish strings as missing."""
    if v is None:
        return None
    s = str(v).strip()
    if s.lower() in NA:
        return None
    try:
        return float(s)
    except ValueError:
        return None


# --------------------------------------------------------------- Summary index #

def _norm(s):
    """Normalize a Reference/Treatment for case-insensitive joining."""
    return (s or "").strip().lower()


def summary_soil(by_trt, ref, trt):
    """Look up Summary soil for a live (ExperimentName, TreatmentName), tolerantly."""
    for r in (ref, ref.rsplit(".", 1)[0]):   # also try with a trailing .auto/.manual stripped
        hit = by_trt.get((_norm(r), _norm(trt)))
        if hit:
            return hit
    return {}


def load_summary():
    """(ref_lower, trt_lower) -> soil dict;  ref_lower -> {prec:[], temp:[]}."""
    by_trt = {}
    clim = {}
    with open(SUMMARY, encoding="latin-1") as fh:
        for r in csv.DictReader(fh):
            ref = _norm(r.get("Reference"))
            trt = _norm(r.get("Treatment"))
            sand, silt, clay = num(r.get("Sand")), num(r.get("Silt")), num(r.get("Clay"))
            # derive the missing third component when the other two are present
            if silt is None and sand is not None and clay is not None:
                silt = round(100 - sand - clay, 2)
            elif sand is None and silt is not None and clay is not None:
                sand = round(100 - silt - clay, 2)
            elif clay is None and sand is not None and silt is not None:
                clay = round(100 - sand - silt, 2)
            by_trt[(ref, trt)] = {
                "SandMean": sand, "SiltMean": silt, "ClayMean": clay,
                "pHMean": num(r.get("pH")),
                "BulkDensityMean": num(r.get("BD")),
                "SOMMean": num(r.get("soilC_perc")),  # stored as-is (organic C %)
            }
            c = clim.setdefault(ref, {"prec": [], "temp": []})
            p, t = num(r.get("MeanPrec_mm")), num(r.get("MeanTemp_c"))
            if p is not None:
                c["prec"].append(p)
            if t is not None:
                c["temp"].append(t)
    return by_trt, clim


# ------------------------------------------------------------------- gridded #

def _get_json(url, tries=3):
    req = urllib.request.Request(url, headers={"User-Agent": "fluxapp-etl/1.0"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            if i == tries - 1:
                raise
            time.sleep(3 * (i + 1))


SG_DEPTHS = [("0-5cm", 5), ("5-15cm", 10), ("15-30cm", 15)]
SG_PROPS = {"sand": "SandMean", "silt": "SiltMean", "clay": "ClayMean",
            "phh2o": "pHMean", "bdod": "BulkDensityMean", "soc": "SOMMean"}


def soilgrids(lat, lon):
    """0-30 cm depth-weighted soil properties, scaled to natural units."""
    props = "&".join(f"property={p}" for p in SG_PROPS)
    depths = "&".join(f"depth={d}" for d, _ in SG_DEPTHS)
    url = (f"https://rest.isric.org/soilgrids/v2.0/properties/query?"
           f"lat={lat}&lon={lon}&{props}&{depths}&value=mean")
    d = _get_json(url)
    out = {}
    for layer in d.get("properties", {}).get("layers", []):
        name = layer["name"]
        dfac = layer["unit_measure"]["d_factor"]
        wsum = vsum = 0.0
        for depth in layer["depths"]:
            w = next((wt for lbl, wt in SG_DEPTHS if lbl == depth["label"]), 0)
            v = depth["values"].get("mean")
            if v is not None and w:
                vsum += (v / dfac) * w
                wsum += w
        if not wsum:
            continue
        val = vsum / wsum
        if name in ("sand", "silt", "clay"):      # d_factor 10 -> already %
            out[SG_PROPS[name]] = round(val, 1)
        elif name == "phh2o":                      # d_factor 10 -> pH
            out[SG_PROPS[name]] = round(val, 1)
        elif name == "bdod":                       # d_factor 100 -> kg/dm3 = g/cm3
            out[SG_PROPS[name]] = round(val, 2)
        elif name == "soc":                        # d_factor 10 -> g/kg; /10 -> %
            out[SG_PROPS[name]] = round(val / 10.0, 2)
    return out


def nasa_power(lat, lon):
    """Annual MAT (degC) and MAP (mm/yr) from POWER climatology."""
    url = ("https://power.larc.nasa.gov/api/temporal/climatology/point?"
           f"parameters=T2M,PRECTOTCORR&community=AG&longitude={lon}&latitude={lat}&format=JSON")
    d = _get_json(url)
    par = d["properties"]["parameter"]
    return {"MAP": round(par["PRECTOTCORR"]["ANN"] * 365.25, 1),  # mm/day -> mm/yr
            "MAT": round(par["T2M"]["ANN"], 1)}


# ----------------------------------------------------------------- cache I/O #

def load_cache():
    if os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as fh:
            return json.load(fh)
    return {"soil": {}, "clim": {}}


def save_cache(c):
    with open(CACHE, "w", encoding="utf-8") as fh:
        json.dump(c, fh, indent=2)


# --------------------------------------------------------------------- main #

def read_tsv(path):
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def valid_coord(lat, lon):
    """Reject the (0,0) null-island placeholder and out-of-range coordinates."""
    la, lo = num(lat), num(lon)
    if la is None or lo is None:
        return False
    if la == 0 and lo == 0:
        return False
    return -90 <= la <= 90 and -180 <= lo <= 180


SOIL_FIELDS = ["SandMean", "SiltMean", "ClayMean", "pHMean", "BulkDensityMean", "SOMMean"]
DUMP_COL = {"SandMean": "SandMean", "SiltMean": "SiltMean", "ClayMean": "ClayMean",
            "pHMean": "pH", "BulkDensityMean": "BulkDensity", "SOMMean": "SOMMean"}


def main():
    offline = "--offline" in sys.argv
    by_trt, clim = load_summary()
    cache = load_cache()
    treats = read_tsv(TREAT_TSV)
    sites = read_tsv(SITE_TSV)

    bad_coords = set()

    def site_soil(siteid, lat, lon):
        key = str(siteid)
        if not valid_coord(lat, lon):
            bad_coords.add(key)
            return {}
        if key not in cache["soil"]:
            if offline:
                return {}
            print(f"  SoilGrids site {siteid} ({lat},{lon}) ...", file=sys.stderr)
            cache["soil"][key] = soilgrids(lat, lon)
            save_cache(cache)
            time.sleep(1.0)
        return cache["soil"][key]

    def site_clim(siteid, lat, lon):
        key = str(siteid)
        if not valid_coord(lat, lon):
            bad_coords.add(key)
            return {}
        if key not in cache["clim"]:
            if offline:
                return {}
            print(f"  NASA POWER site {siteid} ({lat},{lon}) ...", file=sys.stderr)
            cache["clim"][key] = nasa_power(lat, lon)
            save_cache(cache)
            time.sleep(0.5)
        return cache["clim"][key]

    # ---- treatment soil ----
    TEXTURE = ["SandMean", "SiltMean", "ClayMean"]   # atomic triplet (one source)
    SCALAR = ["pHMean", "BulkDensityMean", "SOMMean"]  # independent

    soil_rows = []
    stat = {"summary": 0, "soilgrids": 0, "none_rows": 0}
    for r in treats:
        missing = [f for f in SOIL_FIELDS if (r.get(DUMP_COL[f]) or "NULL") == "NULL"]
        if not missing:
            continue
        ref, trt = r["ExperimentName"].strip(), r["TreatmentName"].strip()
        sm = summary_soil(by_trt, ref, trt)
        sg = None  # lazily fetched SoilGrids triplet for this site

        def grids():
            nonlocal sg
            if sg is None:
                sg = site_soil(r["SiteID"], r["Latitude"], r["Longitude"])
            return sg

        out = {"TreatmentID": r["TreatmentID"]}
        srcs = {}

        # texture: keep Sand/Silt/Clay coherent (sum 100). Preserve every component
        # Summary measured; fill the rest from SoilGrids rescaled to the remaining
        # (100 - measured) fraction. With no Summary texture, use a full SoilGrids
        # triplet; with a complete Summary triplet, use it as-is.
        miss_tex = [f for f in TEXTURE if f in missing]
        if miss_tex:
            measured = {f: sm[f] for f in TEXTURE if sm.get(f) is not None}
            if len(measured) == 3:                      # authoritative triplet
                for f in miss_tex:
                    out[f], srcs[f] = measured[f], "summary"
                    stat["summary"] += 1
            elif measured:                              # hybrid: measured + rescaled grid
                need = [f for f in TEXTURE if f not in measured]
                remaining = 100.0 - sum(measured.values())
                sg = grids()
                denom = sum(sg[f] for f in need if sg.get(f) is not None)
                for f in miss_tex:
                    if f in measured:
                        out[f], srcs[f] = measured[f], "summary"
                        stat["summary"] += 1
                    elif sg.get(f) is not None and denom > 0 and remaining > 0:
                        out[f] = round(remaining * sg[f] / denom, 1)
                        srcs[f] = "soilgrids_scaled"
                        stat["soilgrids"] += 1
                    elif sg.get(f) is not None:         # degenerate (clay>=100): use raw
                        out[f], srcs[f] = sg[f], "soilgrids"
                        stat["soilgrids"] += 1
            else:                                       # no Summary texture: grid triplet
                sg = grids()
                for f in miss_tex:
                    if sg.get(f) is not None:
                        out[f], srcs[f] = sg[f], "soilgrids"
                        stat["soilgrids"] += 1

        # scalars: independent, per-field Summary-else-SoilGrids
        for f in (c for c in SCALAR if c in missing):
            v = sm.get(f)
            if v is not None:
                out[f], srcs[f] = v, "summary"
                stat["summary"] += 1
                continue
            v = grids().get(f)
            if v is not None:
                out[f], srcs[f] = v, "soilgrids"
                stat["soilgrids"] += 1

        if len(out) == 1:
            stat["none_rows"] += 1
            continue
        out["Source"] = ";".join(f"{f}:{s}" for f, s in srcs.items())
        soil_rows.append(out)

    # ---- site climate ----
    refs_by_site = {}
    for r in treats:
        if r["ExperimentName"].strip():
            refs_by_site.setdefault(r["SiteID"], set()).add(r["ExperimentName"].strip())

    clim_rows = []
    cstat = {"summary": 0, "nasa_power": 0, "none_sites": 0}
    for r in sites:
        sid = r["SiteID"]
        cur_map, cur_mat = num(r.get("MAP")), num(r.get("MAT"))
        need_map = cur_map is None or cur_map == 0          # 0 = legacy placeholder
        need_mat = cur_mat is None or (cur_map == 0 and cur_mat == 0)
        if not (need_map or need_mat):
            continue
        precs, temps = [], []
        for ref in refs_by_site.get(sid, ()):
            c = clim.get(_norm(ref)) or clim.get(_norm(ref.rsplit(".", 1)[0]))
            if c:
                precs += c["prec"]
                temps += c["temp"]
        out = {"SiteID": sid}
        srcs = {}
        if need_map:
            if precs:
                out["MAP"], srcs["MAP"] = round(sum(precs) / len(precs), 1), "summary"
                cstat["summary"] += 1
            else:
                np_ = site_clim(sid, r["Latitude"], r["Longitude"])
                if np_.get("MAP") is not None:
                    out["MAP"], srcs["MAP"] = np_["MAP"], "nasa_power"
                    cstat["nasa_power"] += 1
        if need_mat:
            if temps:
                out["MAT"], srcs["MAT"] = round(sum(temps) / len(temps), 1), "summary"
                cstat["summary"] += 1
            else:
                np_ = site_clim(sid, r["Latitude"], r["Longitude"])
                if np_.get("MAT") is not None:
                    out["MAT"], srcs["MAT"] = np_["MAT"], "nasa_power"
                    cstat["nasa_power"] += 1
        if len(out) == 1:
            cstat["none_sites"] += 1
            continue
        out["Source"] = ";".join(f"{f}:{s}" for f, s in srcs.items())
        clim_rows.append(out)

    save_cache(cache)

    # ---- write ----
    with open(os.path.join(ETL, "soil_enrichment.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["TreatmentID"] + SOIL_FIELDS + ["Source"])
        w.writeheader()
        w.writerows(soil_rows)
    with open(os.path.join(ETL, "site_climate.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["SiteID", "MAP", "MAT", "Source"])
        w.writeheader()
        w.writerows(clim_rows)

    print(f"\nsoil_enrichment.csv: {len(soil_rows)} treatment rows  "
          f"(field fills - summary={stat['summary']}, soilgrids={stat['soilgrids']}; "
          f"{stat['none_rows']} rows had no source)")
    print(f"site_climate.csv:    {len(clim_rows)} site rows  "
          f"(field fills - summary={cstat['summary']}, nasa_power={cstat['nasa_power']}; "
          f"{cstat['none_sites']} sites had no source)")
    if bad_coords:
        print(f"\nWARNING: {len(bad_coords)} site(s) skipped for invalid coordinates "
              f"(e.g. (0,0) null-island): SiteID {sorted(bad_coords, key=int)} — "
              f"fix lat/lon, then re-run.")


if __name__ == "__main__":
    main()
