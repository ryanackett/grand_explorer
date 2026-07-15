#!/usr/bin/env python3
"""Generate two realistic test CSVs for the assisted-upload workbench, packed with the
edge cases the pipeline now handles. Deterministic (seeded).

Outputs (in this directory):
  * workbench_temporal_test.csv  — the time-series measurement file (the "data file")
  * workbench_soil_site_test.csv — per-plot soil + site data (reference / leftover-fill)

Edge cases embedded in the temporal file (see EDGE_CASES.md for the full list):
  - 4 treatments as codes (CT-U/NT-U/CT-C/NT-C) -> readable-name feature
  - 4 replicate blocks -> replicate averaging + N2O flux SE
  - Missing replicates on some dates -> "average what's present", no SE for single-rep
  - Sub-daily datetimes on some dates (auto-chamber, 4x/day) -> time-weighted daily mean
  - US datetime format MM/DD/YYYY HH:MM (day>12 present) -> format guess + datetime parse
  - Blank-treatment rows -> flagged/dropped surfacing
  - A few unparseable date values -> bad-date flag
  - Several measurement columns (flux/VWC/soil T/air T/precip/NH4/NO3) -> multi-pane graph
  - N_applied column populated only on fertiliser dates -> column-provided fertilisation
  - Non-canonical flux unit (µg N/m²/h) -> mandatory unit selection + conversion
  - Irregular sampling with gaps -> daily fill
  - A DOY column + a Notes column -> non-mappable/derived + leftover columns
"""
import csv
import math
import random
from datetime import date, datetime, timedelta

random.seed(20260711)

SEASON_START = date(2023, 4, 1)
SEASON_END = date(2023, 9, 30)
FERT_DATES = [date(2023, 4, 15), date(2023, 6, 20)]  # urea applications, 75 kg N/ha

TREATMENTS = {
    # code: (tillage, N source, background flux µg N/m²/h, is_urea)
    "CT-U": ("conventional", "urea", 30.0, True),
    "NT-U": ("no-till", "urea", 42.0, True),
    "CT-C": ("conventional", "control", 12.0, False),
    "NT-C": ("no-till", "control", 15.0, False),
}
BLOCKS = [1, 2, 3, 4]

# --- shared daily environment (same across plots on a given day) ----------------

def doy(d):
    return d.timetuple().tm_yday


def soil_temp(d):
    # Seasonal: ~8 °C at the shoulders, ~24 °C mid-summer.
    frac = max(0.0, (doy(d) - 91)) / 183.0
    return round(8 + 16 * math.sin(min(frac, 1.0) * math.pi) + random.uniform(-1.2, 1.2), 1)


# Pre-roll a precip series so "recent rain" can drive moisture/flux pulses.
_PRECIP = {}
def precip_mm(d):
    if d not in _PRECIP:
        _PRECIP[d] = round(random.expovariate(1 / 7.0), 1) if random.random() < 0.30 else 0.0
        _PRECIP[d] = min(_PRECIP[d], 46.0)
    return _PRECIP[d]


def recent_rain(d, days=3):
    return sum(precip_mm(d - timedelta(days=k)) for k in range(days))


def air_temp(d, hour):
    diel = 5.0 * math.sin((hour - 6) / 24.0 * 2 * math.pi)
    return round(soil_temp(d) + 2.5 + diel + random.uniform(-1, 1), 1)


def vwc(d):
    base = 30 - 0.45 * (soil_temp(d) - 16) + 0.6 * recent_rain(d)
    return round(max(9.0, min(43.0, base + random.uniform(-2.5, 2.5))), 1)


def days_since_fert(d):
    past = [(d - f).days for f in FERT_DATES if (d - f).days >= 0]
    return min(past) if past else None


def flux(d, hour, base, is_urea):
    seasonal = 1 + 0.30 * math.sin((doy(d) - 100) / 183.0 * math.pi)
    pulse = 0.0
    dsf = days_since_fert(d)
    if is_urea and dsf is not None and dsf <= 16:
        pulse = base * 4.0 * math.exp(-dsf / 4.5)
    rain_pulse = 1 + 0.05 * recent_rain(d)
    diel = 1 + 0.22 * math.sin((hour - 8) / 24.0 * 2 * math.pi)   # higher midday
    rep_noise = math.exp(random.gauss(0, 0.26))                   # lognormal between-rep spread
    return round(max(0.3, (base * seasonal + pulse) * rain_pulse * diel * rep_noise), 1)


def soil_nh4(d, is_urea):
    dsf = days_since_fert(d)
    spike = 55 * math.exp(-dsf / 6.0) if (is_urea and dsf is not None and dsf <= 20) else 0
    return round(max(0.5, 3 + spike + random.uniform(-1, 1)), 1)


def soil_no3(d, is_urea):
    dsf = days_since_fert(d)
    spike = 35 * math.exp(-dsf / 9.0) if (is_urea and dsf is not None and dsf <= 30) else 0
    return round(max(1.0, 8 + spike + random.uniform(-2, 2)), 1)


# --- sampling schedule ----------------------------------------------------------

def sampling_datetimes():
    """List of (date, [hours]) sampling events across the season."""
    events = {}
    # Baseline: every 3 days at 10:00.
    d = SEASON_START
    while d <= SEASON_END:
        events[d] = [10]
        d += timedelta(days=3)
    # Intensive daily sampling for 12 days after each fertilisation.
    for f in FERT_DATES:
        for k in range(12):
            events[f + timedelta(days=k)] = [10]
    # Sub-daily auto-chamber (4×/day) on a handful of dates in the intensive windows.
    for sd in [date(2023, 4, 16), date(2023, 4, 17), date(2023, 4, 18),
               date(2023, 6, 21), date(2023, 6, 22), date(2023, 6, 23)]:
        events[sd] = [0, 6, 12, 18]
    return dict(sorted(events.items()))


def n_applied(d, is_urea):
    return 75.0 if (is_urea and d in FERT_DATES) else ""


# --- write temporal file --------------------------------------------------------

TEMPORAL_HEADERS = [
    "DateTime", "Treatment", "Block", "N2O_flux_ugN_m2_h", "SoilVWC_pct",
    "SoilTemp_C", "AirTemp_C", "Precip_mm", "SoilNH4_mgN_kg", "SoilNO3_mgN_kg",
    "N_applied_kgN_ha", "DOY", "Notes",
]


def build_temporal_rows():
    rows = []
    schedule = sampling_datetimes()
    # Deterministic "missing replicate" events: (date, treatment) -> block to drop.
    missing = set()
    all_dates = list(schedule.keys())
    for _ in range(10):
        md = random.choice(all_dates)
        mt = random.choice(list(TREATMENTS))
        missing.add((md, mt, random.choice(BLOCKS)))

    for d, hours in schedule.items():
        st = soil_temp(d)
        pr = precip_mm(d)
        mo = vwc(d)
        for code, (till, nsrc, base, is_urea) in TREATMENTS.items():
            for blk in BLOCKS:
                if (d, code, blk) in missing:
                    continue  # experimental error — this rep has no data this date
                for h in hours:
                    dt = datetime(d.year, d.month, d.day, h, 0)
                    note = ""
                    if len(hours) > 1:
                        note = "auto-chamber"
                    elif d in FERT_DATES and is_urea:
                        note = "fertilised"
                    rows.append([
                        dt.strftime("%m/%d/%Y %H:%M"),          # US datetime format
                        code, blk,
                        flux(d, h, base, is_urea),
                        mo, st, air_temp(d, h), pr,
                        soil_nh4(d, is_urea), soil_no3(d, is_urea),
                        n_applied(d, is_urea), doy(d), note,
                    ])

    # --- deliberate malformed rows (scattered near the front) ---
    # Blank-treatment rows (valid data, empty Treatment cell).
    for k in range(8):
        d = all_dates[5 + k * 3]
        rows.append([
            datetime(d.year, d.month, d.day, 10, 0).strftime("%m/%d/%Y %H:%M"),
            "", random.choice(BLOCKS), flux(d, 10, 20, False),
            vwc(d), soil_temp(d), air_temp(d, 10), precip_mm(d),
            soil_nh4(d, False), soil_no3(d, False), "", doy(d), "MISSING TREATMENT",
        ])
    # A few unparseable dates.
    for bad in ["late June 2023", "2023 growing season", "06/31/2023 10:00"]:  # 06/31 invalid too
        rows.append([bad, "CT-U", 1, 21.5, 27.0, 18.0, 20.5, 0.0, 4.0, 9.0, "", "", "BAD DATE"])

    random.shuffle(rows)   # realistic: not perfectly ordered
    return rows


def build_soil_site_rows():
    """One row per plot: soil texture/chemistry varies by plot (heterogeneity); site-level
    fields are constant across rows."""
    rows = []
    # Base soil per treatment, with per-plot jitter (this is the between-plot variation the
    # deferred 'treatment soil average from CSV' feature would collapse).
    base_soil = {
        "CT-U": (42, 38, 20, 6.3, 1.38, 3.1),
        "NT-U": (40, 39, 21, 6.5, 1.29, 4.0),   # no-till: lower BD, higher SOM
        "CT-C": (43, 37, 20, 6.2, 1.40, 2.9),
        "NT-C": (41, 38, 21, 6.6, 1.28, 4.2),
    }
    for code, (sand, silt, clay, ph, bd, som) in base_soil.items():
        for blk in BLOCKS:
            s = round(sand + random.uniform(-3, 3), 1)
            si = round(silt + random.uniform(-3, 3), 1)
            c = round(max(5.0, 100 - s - si), 1)
            rows.append([
                code, blk, s, si, c,
                round(ph + random.uniform(-0.25, 0.25), 2),
                round(bd + random.uniform(-0.05, 0.05), 3),
                round(som + random.uniform(-0.4, 0.4), 2),
                40.4661, -86.9948, 985, 10.7, "ACRE Research Farm", "USA",
            ])
    return rows


def main():
    import os
    here = os.path.dirname(os.path.abspath(__file__))

    temporal = build_temporal_rows()
    with open(os.path.join(here, "workbench_temporal_test.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(TEMPORAL_HEADERS)
        w.writerows(temporal)

    soil = build_soil_site_rows()
    with open(os.path.join(here, "workbench_soil_site_test.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Treatment", "Block", "Sand_pct", "Silt_pct", "Clay_pct", "pH_H2O",
                    "BulkDensity_g_cm3", "SOM_pct", "Latitude", "Longitude",
                    "MAP_mm", "MAT_C", "SiteName", "Country"])
        w.writerows(soil)

    print(f"temporal rows: {len(temporal)}  |  soil/site rows: {len(soil)}")


if __name__ == "__main__":
    main()
