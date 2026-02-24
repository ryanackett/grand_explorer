================================================================================
                            *GRAND* DATA EXPORT README
================================================================================

SOURCE:       GRAND Explorer (url.com)
FILE:         n2o_data_export.csv

--------------------------------------------------------------------------------
1. OVERVIEW
--------------------------------------------------------------------------------
This archive contains harmonized N2O emission data selected from the N2Odb 
interface. The data is provided in a "flat" CSV format suitable for process-based 
modeling, ML modeling, statistical analysis, and meta-analysis.

The export includes:
1. Experimental metadata (Site, treatment, and management details)
2. Environmental drivers (Soil properties, climate context)
3. Flux measurements (N2O emissions and related nitrogen species)

--------------------------------------------------------------------------------
2. CITATION POLICY
--------------------------------------------------------------------------------
Using this data for publication requires citing both the GRAND database AND 
the original primary sources included in your download.

Suggested Citation for GRAND Explorer:
[Insert your Lab/Project Citation Here, e.g.: Smith et al. (2025). Global N2O Database...]

A full list of original citations for all rows in this export are provided in
the "Citation" column (or, if empty, the "Link" column)

--------------------------------------------------------------------------------
3. DATA STRUCTURE & UNITS
--------------------------------------------------------------------------------
The CSV file is structured with one row per measurement/aggregation period. 
Missing values are denoted by blank cells or "NA" depending on the source aggregation.

KEY COLUMNS:

-- IDENTIFIERS --
* measurement_id    : Unique database ID for the specific observation
* treatment_id      : Unique ID linking measurements to a specific experimental treatment
* site_name         : Name of the experimental site or research station
* reference_source  : Citation/DOI for the original study

-- TEMPORAL --
* date              : Date of measurement (ISO 8601: YYYY-MM-DD)

-- FLUX DATA (Response Variables) --
* n2o_flux          : Daily N2O emission rate
                      [Units: g N ha-1 d-1] (grams Nitrogen per hectare per day)

-- EXPERIMENTAL DRIVERS (Management) --
* fertilizer_type   : Class of fertilizer applied (e.g., Synthetic, Manure, Control)
* n_rate            : Total Nitrogen application rate
                      [Units: kg N ha-1]
* crop_type         : Main crop species grown during the measurement period
* tillage           : Tillage practice (e.g., Conventional, No-till)

-- ENVIRONMENTAL CONTEXT (Soil & Climate) --
* soil_texture      : USDA Soil Texture Class (e.g., Silty Loam)
* clay_percent      : Percentage of clay in topsoil (0-20cm) [%]
* soc               : Soil Organic Carbon content [% or g kg-1]
* ph                : Soil pH (method varies, typically H2O or CaCl2)
* bulk_density      : Soil bulk density [g cm-3]
* precipitation     : Daily precipitation (or interpolated) [mm]
* air_temp          : Mean daily air temperature [°C]

--------------------------------------------------------------------------------
4. USAGE NOTES FOR MODELERS
--------------------------------------------------------------------------------
* SPATIAL AGGREGATION: 
  Site coordinates (Lat/Lon) are provided at the experiment level. Be aware that 
  some multi-year studies may have slight plot variations not captured in global 
  coordinates.

* ZERO vs. NULL: 
  Please distinguish between '0' (a measured zero flux) and '' (null/missing data). 
  We strive to preserve the author's original reporting distinction.

* N-FERTILIZER TIMING: 
  Where available, fertilization events are flagged. 

--------------------------------------------------------------------------------
CONTACT & SUPPORT
--------------------------------------------------------------------------------
For data corrections or technical issues, please contact:
admin@n2odb.org (or your specific contact email)
