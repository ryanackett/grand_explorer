================================================================================
                            GRAND DATA EXPORT README
================================================================================

SOURCE:    GRAND Explorer — Global Repository for Agricultural N2O Data
           (public site URL: TBD — pre-launch)

THIS ARCHIVE CONTAINS:
  * README.txt             — this file
  * n2odb_data_export.csv  — your selected data, one row per measurement
  * MODELED_VALUES.md      — which sites/treatments carry modeled (estimated)
                             soil/climate covariates (see section 4)

--------------------------------------------------------------------------------
1. OVERVIEW
--------------------------------------------------------------------------------
This archive contains harmonized soil N2O emission data selected through the
GRAND Explorer interface. The data is a "flat" CSV suitable for process-based
modeling, machine learning, statistical analysis, and meta-analysis.

Each row is one measurement for one treatment on one date. A record represents
a treatment's mean flux (replicate plots are averaged), joined to its treatment,
experiment, site, and publication context.

--------------------------------------------------------------------------------
2. CITATION POLICY
--------------------------------------------------------------------------------
Using this data in a publication requires citing BOTH the GRAND database AND the
original primary sources for the rows you use.

Suggested citation for GRAND:
    Ackett, R., Saha, D. (2026). GRAND: The Global Repository for
    Agricultural N2O Data.

Every row carries its original source in the "Citation" column (and a link in
the "DOI" column where available). Please cite the primary studies accordingly.

--------------------------------------------------------------------------------
3. COLUMNS
--------------------------------------------------------------------------------
Columns appear in the CSV in the order listed below. Missing values are blank.

-- PUBLICATION --
  Publication Title            Title of the source publication
  Publication Year             Year of publication
  DOI                          Link to the source (from the publication's Link
                               field; usually a DOI, not guaranteed to be one)
  Lead Author                  Lead author of the source publication
  Citation                     Full citation string for the source

-- IDENTIFIERS --
  Measurement ID               Unique ID for this observation (row)
  Treatment ID                 ID of the experimental treatment this row belongs to
  Experiment Title             Name of the experiment
  Treatment Name               Name of the treatment
  Treatment Description        Free-text description of the treatment

-- TEMPORAL --
  Date                         Measurement date (ISO 8601: YYYY-MM-DD)

-- FLUX & NITROGEN --
  N2O Flux (g N/ha/d)          Daily N2O emission rate (grams N per hectare per day)
  Nitrogen Applied (kg N/ha)   N application rate on this date
  Nitrogen Form                Form of N applied (e.g. urea, UAN, manure)
  Nitrogen Type                Class of N applied (e.g. synthetic, organic)
  Soil NH4 (mg/kg)             Soil ammonium
  Soil NO3 (mg/kg)             Soil nitrate

-- MICROMETEOROLOGY / SOIL STATE --
  VWC (%)                      Volumetric water content
  WFPS (%)                     Water-filled pore space
  Soil Temp (°C)               Soil temperature
  Mean Daily Air Temp (°C)     Mean daily air temperature
  Max Daily Air Temp (°C)      Maximum daily air temperature
  Min Daily Air Temp (°C)      Minimum daily air temperature
  Precipitation (mm)           Daily precipitation

-- TREATMENT / MANAGEMENT --
  Primary Crop                 Main crop in the treatment
  Secondary Crop               Secondary crop, if any
  Planted Crop                 Crop planted during this measurement period
  Management                   Management recorded at the measurement level
  Management Style             Management recorded at the treatment level
  Flux Instrument              Measurement method (Static Chamber /
                               Automatic Chamber / Eddy Covariance)

-- SOIL PHYSICAL / CHEMICAL (treatment level) --
  Sand Content (%)             Topsoil sand fraction
  Silt Content (%)             Topsoil silt fraction
  Clay Content (%)             Topsoil clay fraction
  pH                           Soil pH (method varies; typically H2O or CaCl2)
  Bulk Density (g/cm3)         Soil bulk density
  Soil Organic Carbon (%)      Soil organic carbon (see note in section 5)

-- SITE --
  Site Name                    Name of the experimental site
  Site Latitude                Decimal degrees
  Site Longitude               Decimal degrees
  Country                      Country of the site (derived from coordinates)
  Continent                    Continent of the site (derived from coordinates)
  Mean Annual Precipitation (mm)   Site MAP
  Mean Annual Temperature (°C)     Site MAT

--------------------------------------------------------------------------------
4. MEASURED vs MODELED DATA  (IMPORTANT)
--------------------------------------------------------------------------------
Some covariates are MODELED (gridded estimates) rather than reported by the
source study. There is NO per-cell flag in the CSV — instead, the sites and
treatments that carry estimated values are listed in the bundled file
MODELED_VALUES.md. Cross-reference it by "Site Name" / "Treatment ID".

  * Soil pH, Bulk Density, and Soil Organic Carbon may be filled from
    SoilGrids v2.0 (ISRIC, 0-30 cm depth-weighted) where the source study did
    not report them.
  * Mean Annual Precipitation / Temperature (MAP/MAT) may be filled from
    NASA POWER climatology where not reported.
  * Soil texture (Sand/Silt/Clay) is measured at most sites, but IS
    SoilGrids-filled at some. This happens two ways: (a) FULLY estimated where
    the source study reported no soil fractions, or (b) PARTIALLY — a measured
    component (e.g. clay) is kept and the remaining fractions rescaled from
    SoilGrids around it. In MODELED_VALUES.md these show up as "Sand", "Silt",
    and/or "Clay" in the "Estimated soil" column (a subset of a site's three
    fractions means only those were filled).
  * Country and Continent are derived from site coordinates (offline
    reverse-geocoding), not reported by the study.

See MODELED_VALUES.md for the exact per-site breakdown, including the legacy-row
caveat (older treatments have measured texture but SoilGrids-estimated
pH/BD/SOM).

--------------------------------------------------------------------------------
5. USAGE NOTES FOR MODELERS
--------------------------------------------------------------------------------
  * TREATMENT-AVERAGED: rows are treatment means, not raw replicate-level data.

  * ZERO vs. NULL: a measured 0 (e.g. 0 flux) is distinct from a blank/missing
    value. We preserve the author's original reporting distinction.

  * MEASUREMENT GAPS: most data comes from static chambers sampled every 1-2
    weeks. GRAND does NOT gap-fill flux values — absent dates are simply absent.

  * FIGURE-DIGITIZED VALUES: where data was published only inside a figure, it
    was digitized point-by-point and carries the small reading uncertainty
    inherent to that process.

  * SOM vs SOC: the "Soil Organic Carbon (%)" column is sourced from the soil
    organic matter field; treat the organic-matter/organic-carbon distinction
    with care for your application.

--------------------------------------------------------------------------------
CONTACT & SUPPORT
--------------------------------------------------------------------------------
For data corrections or technical issues, please use the Contact page on the
GRAND Explorer site (/contact).
