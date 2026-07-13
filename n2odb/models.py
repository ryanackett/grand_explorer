import uuid

from django.db import models
from django.contrib.auth.models import AbstractUser

class User(AbstractUser):
    # Stable, non-enumerable account handle. The DB pk already survives an email change,
    # but it's sequential (leaks user counts) and not safe to expose; this UUID is the
    # identifier we surface/track a contributor by, unchanged across email edits.
    # first_name / last_name come from AbstractUser (captured at signup, editable in My Account).
    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)


class Publication(models.Model):
    PubID = models.AutoField(db_column='PubID', primary_key=True)  # Field name made lowercase.
    PubTitle = models.CharField(db_column='PubTitle', max_length=255, blank=True, null=True)  # Field name made lowercase.
    LeadAuthor = models.CharField(db_column='LeadAuthor', max_length=32, blank=True, null=True)  # Field name made lowercase.
    Citation = models.CharField(db_column='Citation', max_length=500, blank=True, null=True)  # Field name made lowercase.
    PubYear = models.IntegerField(db_column='PubYear', blank=True, null=True)  # Field name made lowercase.
    Link = models.CharField(db_column='Link', max_length=255, blank=True, null=True)  # Field name made lowercase.
    Abstract = models.TextField(db_column='Abstract', blank=True, null=True)
    # Persistent handle (User.public_id) of the workbench contributor who submitted this
    # dataset, stamped at promotion. Null for scraped/legacy publications and for
    # promotions with no owner. Stored as the UUID value (not a FK) so provenance
    # survives account deletion; indexed to list everything a given user contributed.
    Contributor = models.UUIDField(db_column='Contributor', null=True, blank=True, db_index=True)
    class Meta:
        db_table = 'Publication'


class Site(models.Model):
    SiteID = models.AutoField(db_column='SiteID', primary_key=True)  # Field name made lowercase.
    Latitude = models.FloatField(db_column='Latitude')  # Field name made lowercase.
    Longitude = models.FloatField(db_column='Longitude')  # Field name made lowercase.
    MAP = models.FloatField(db_column='MAP', blank=True, null=True)  # Field name made lowercase.
    MAT = models.FloatField(db_column='MAT', blank=True, null=True)  # Field name made lowercase.
    Gracenet = models.BooleanField(db_column='Gracenet', blank=False, null=True)
    # OLD Field
    #Name = models.CharField(db_column='Name', max_length=255, blank=True, null=True)  # Field name made lowercase.
    # New Field
    SiteName = models.CharField(db_column='SiteName', max_length=255, blank=True, null=True)  # Field name made lowercase.
    # Derived from Latitude/Longitude by the backfill_geography management
    # command (offline reverse-geocode). Re-run that command after adding sites.
    Country = models.CharField(db_column='Country', max_length=64, blank=True, null=True)
    Continent = models.CharField(db_column='Continent', max_length=32, blank=True, null=True)
    class Meta:
        db_table = 'Site'


class Experiment(models.Model):
    ExperimentID = models.AutoField(db_column='ExperimentID', primary_key=True)  # Field name made lowercase.
    ExperimentName = models.CharField(db_column='ExperimentName', max_length=255, blank=True, null=True)  # Field name made lowercase.
    SiteID = models.ForeignKey('Site', models.DO_NOTHING, db_column='SiteID')  # Field name made lowercase.
    PubID = models.ForeignKey('Publication', models.DO_NOTHING, db_column='PubID')  # Field name made lowercase.
    class Meta:
        db_table = 'Experiment'


class Treatment(models.Model):
    TreatmentID = models.AutoField(db_column='TreatmentID', primary_key=True)  # Field name made lowercase.
    ExperimentID = models.ForeignKey(Experiment, models.DO_NOTHING, db_column='ExperimentID')  # Field name made lowercase.
    PrimaryCrop = models.CharField(db_column='PrimaryCrop', max_length=128)  # Field name made lowercase.
    SecondaryCrop = models.CharField(db_column='SecondaryCrop', max_length=128, blank=True, null=True)  # Field name made lowercase.
    CoverCrop = models.CharField(db_column='CoverCrop', max_length=128, blank=True, null=True)  # Field name made lowercase.
    Management = models.CharField(db_column='Management', max_length=16, blank=True, null=True)
    Tillage = models.CharField(db_column='Tillage', blank=False, null=True, max_length=32)
    FluxInstrument = models.CharField(db_column='FluxInstrument', max_length=45, blank=False, null=True)  # Field name made lowercase.
    TreatmentName = models.CharField(db_column='TreatmentName', max_length=45, blank=True, null=True)  # Field name made lowercase.
    SandMean = models.FloatField(db_column='SandMean', blank=True, null=True)  # Field name made lowercase.
    SiltMean = models.FloatField(db_column='SiltMean', blank=True, null=True)  # Field name made lowercase.
    ClayMean = models.FloatField(db_column='ClayMean', blank=True, null=True)  # Field name made lowercase.
    TreatmentDescription = models.TextField(db_column='TreatmentDescription', blank=True, null=True)
    pHMean = models.FloatField(db_column='pH', blank=True, null=True)  # Field name made lowercase.
    BulkDensityMean = models.FloatField(db_column='BulkDensity', blank=True, null=True)
    SOMMean = models.FloatField(db_column='SOMMean', blank=True, null=True)   
    class Meta:
        db_table = 'Treatment'


class Replication(models.Model):
    RepID = models.AutoField(db_column='RepID', primary_key=True)  # Field name made lowercase.
    TreatmentID = models.ForeignKey('Treatment', models.DO_NOTHING, db_column='TreatmentID')  # Field name made lowercase.
    Sand = models.FloatField(db_column='Sand', blank=True, null=True)  # Field name made lowercase.
    Silt = models.FloatField(db_column='Silt', blank=True, null=True)  # Field name made lowercase.
    Clay = models.FloatField(db_column='Clay', blank=True, null=True)  # Field name made lowercase.
    SOM = models.FloatField(db_column='SOM', blank=True, null=True)  # Field name made lowercase.
    pH = models.FloatField(db_column='pH', blank=True, null=True)  # Field name made lowercase.
    BulkDensity = models.FloatField(db_column='BulkDensity', blank=True, null=True)  # Field name made lowercase.
    VWC_Meas = models.BooleanField(db_column='VWC_Meas', blank=False, null=True)
    WFPS_Meas = models.BooleanField(db_column='WFPS_Meas', blank=False, null=True)
    Soil_T_Meas = models.BooleanField(db_column='Soil_T_Meas', blank=False, null=True)
    Air_T_Meas = models.BooleanField(db_column='Air_T_Meas', blank=False, null=True)
    Precip_Meas = models.BooleanField(db_column='Precip_Meas', blank=False, null=True)
    RepName = models.CharField(db_column='RepName', max_length=45, blank=True, null=True)  # Field name made lowercase.
    OD_Paper = models.BooleanField(db_column='OD_Paper', blank=False, null=True)
    class Meta:
        db_table = 'Replication'

class RawMeasurement(models.Model):
    rawid = models.AutoField(db_column='RawID', primary_key=True)  # Field name made lowercase.
    repid = models.ForeignKey('Replication', models.DO_NOTHING, db_column='RepID')  # Field name made lowercase.
    date = models.DateField(db_column='Date')  # Field name made lowercase.
    n2oflux = models.FloatField(db_column='N2OFlux', blank=True, null=True)  # Field name made lowercase.
    vwc = models.FloatField(db_column='VWC', blank=True, null=True)  # Field name made lowercase.
    wfps = models.FloatField(db_column='WFPS', blank=True, null=True)  # Field name made lowercase.
    soilt = models.FloatField(db_column='SoilT', blank=True, null=True)  # Field name made lowercase.
    airt = models.FloatField(db_column='AirT', blank=True, null=True)  # Field name made lowercase.
    precip = models.FloatField(db_column='Precip', blank=True, null=True)  # Field name made lowercase.
    nitrogenapplied = models.FloatField(db_column='NitrogenApplied', blank=True, null=True)  # Field name made lowercase.
    nitrogenform = models.CharField(db_column='NitrogenForm', max_length=64)  # Field name made lowercase.
    nitrogentype = models.CharField(db_column='NitrogenType', max_length=45)  # Field name made lowercase.
    plantedcrop = models.CharField(db_column='PlantedCrop', max_length=32)  # Field name made lowercase.
    harvest = models.IntegerField(db_column='Harvest', blank=True, null=True)  # Field name made lowercase.
    tillage = models.IntegerField(db_column='Tillage', blank=True, null=True)  # Field name made lowercase.
    irrigationapplied = models.FloatField(db_column='IrrigationApplied', blank=True, null=True)  # Field name made lowercase.
    hmtruth = models.BooleanField(db_column='HMTruth', blank=False, null=True)
    airtmax = models.FloatField(db_column='AirTMax', blank=True, null=True)  # Field name made lowercase.
    airtmin = models.FloatField(db_column='AirTMin', blank=True, null=True)  # Field name made lowercase.
    soilnh4 = models.FloatField(db_column='SoilNH4', blank=True, null=True)  # Field name made lowercase.
    soilno3 = models.FloatField(db_column='SoilNO3', blank=True, null=True)  # Field name made lowercase.
    management = models.CharField(db_column='Management', max_length=45, blank=True, null=True)  # Field name made lowercase.
    doy = models.IntegerField(db_column='DOY')  # Field name made lowercase.
    vwccalculated = models.FloatField(db_column='VWCCalculated', blank=True, null=True)  # Field name made lowercase.
    wfpscalculated = models.FloatField(db_column='WFPSCalculated', blank=True, null=True)  # Field name made lowercase.
    hmmcd = models.BooleanField(db_column='HMMCD', blank=False, null=True)
    mcdscores = models.CharField(db_column='MCDscores', max_length=45, blank=True, null=True)  # Field name made lowercase.
    hmknn = models.BooleanField(db_column='HMKNN', blank=False, null=True)
    knnscores = models.CharField(db_column='KNNscores', max_length=45, blank=True, null=True)  # Field name made lowercase.
    class Meta:
        db_table = 'RawMeasurement'

class RawMeasurementTreatment(models.Model):
    rawtreatmentid = models.AutoField(db_column='RawTreatmentID', primary_key=True)  # Field name made lowercase.
    treatmentid = models.ForeignKey('Treatment', models.DO_NOTHING, db_column='TreatmentID', related_name='measurements')
    date = models.DateField(db_column='Date')  # Field name made lowercase.
    n2oflux = models.FloatField(db_column='N2OFlux', blank=True, null=True)  # Field name made lowercase.
    vwc = models.FloatField(db_column='VWC', blank=True, null=True)  # Field name made lowercase.
    wfps = models.FloatField(db_column='WFPS', blank=True, null=True)  # Field name made lowercase.
    soilt = models.FloatField(db_column='SoilT', blank=True, null=True)  # Field name made lowercase.
    airt = models.FloatField(db_column='AirT', blank=True, null=True)  # Field name made lowercase.
    precip = models.FloatField(db_column='Precip', blank=True, null=True)  # Field name made lowercase.
    nitrogenapplied = models.FloatField(db_column='NitrogenApplied', blank=True, null=True)  # Field name made lowercase.
    nitrogenform = models.CharField(db_column='NitrogenForm', blank=True, null=True, max_length=64)  # Field name made lowercase.
    nitrogentype = models.CharField(db_column='NitrogenType', blank=True, null=True, max_length=45)  # Field name made lowercase.
    plantedcrop = models.CharField(db_column='PlantedCrop', max_length=128, blank=True, null=True,)  # Field name made lowercase.
    harvest = models.IntegerField(db_column='Harvest', blank=True, null=True)  # Field name made lowercase.
    tillage = models.IntegerField(db_column='Tillage', blank=True, null=True)  # Field name made lowercase.
    irrigationapplied = models.FloatField(db_column='IrrigationApplied', blank=True, null=True)  # Field name made lowercase.
    hmtruth = models.BooleanField(db_column='HMTruth', blank=True, null=True)
    airtmax = models.FloatField(db_column='AirTMax', blank=True, null=True)  # Field name made lowercase.
    airtmin = models.FloatField(db_column='AirTMin', blank=True, null=True)  # Field name made lowercase.
    soilnh4 = models.FloatField(db_column='SoilNH4', blank=True, null=True)  # Field name made lowercase.
    soilno3 = models.FloatField(db_column='SoilNO3', blank=True, null=True)  # Field name made lowercase.
    management = models.CharField(db_column='Management', max_length=45, blank=True, null=True)  # Field name made lowercase.
    doy = models.IntegerField(db_column='DOY', blank=True, null=True)  # Field name made lowercase.
    vwccalculated = models.FloatField(db_column='VWCCalculated', blank=True, null=True)  # Field name made lowercase.
    wfpscalculated = models.FloatField(db_column='WFPSCalculated', blank=True, null=True)  # Field name made lowercase.
    hmmcd = models.BooleanField(db_column='HMMCD', blank=True, null=True)
    mcdscores = models.CharField(db_column='MCDscores', max_length=45, blank=True, null=True)  # Field name made lowercase.
    hmknn = models.BooleanField(db_column='HMKNN', blank=True, null=True)
    knnscores = models.CharField(db_column='KNNscores', max_length=45, blank=True, null=True)  # Field name made lowercase.
    fluxstandarderror = models.FloatField(db_column='FluxStandardError', blank=True, null=True)  # Field name made lowercase.
    fluxconfidenceint = models.FloatField(db_column='FluxConfidenceInt', blank=True, null=True)  # Field name made lowercase.
    hmiqr = models.BooleanField(db_column='HMIQR', blank=True, null=True)
    madscores = models.CharField(db_column='MADscores', max_length=45, blank=True, null=True)  # Field name made lowercase.
    hmmad = models.BooleanField(db_column='HMMAD', blank=True, null=True)
    hmlof = models.BooleanField(db_column='HMLOF', blank=True, null=True)
    lofscores = models.CharField(db_column='LOFscores', max_length=45, blank=True, null=True)  # Field name made lowercase.
    hmabod = models.BooleanField(db_column='HMABOD', blank=True, null=True)
    abodscores = models.CharField(db_column='ABODscores', max_length=45, blank=True, null=True)  # Field name made lowercase.
    hmiforest = models.BooleanField(db_column='HMIForest', blank=True, null=True)
    iforestscores = models.CharField(db_column='IForestscores', max_length=45, blank=True, null=True)  # Field name made lowercase.
    hmarima = models.BooleanField(db_column='HMARIMA', blank=True, null=True)
    arimascores = models.CharField(db_column='ARIMAscores', max_length=45, blank=True, null=True)  # Field name made lowercase.
    hm75percentile = models.BooleanField(db_column='HM75Percentile', blank=True, null=True)
    hmhbos = models.BooleanField(db_column='HMHBOS', blank=True, null=True)
    hbosscores = models.CharField(db_column='HBOSscores', max_length=45, blank=True, null=True)  # Field name made lowercase.
    class Meta:
        db_table = 'RawMeasurementTreatment'

class UserAction(models.Model):
    # Distinguish between "Browsing" (get-data) and "Downloading" (data-cart)
    ACTION_CHOICES = [
        ('EXPLORE', 'Map Exploration'),     # AJAX updates
        ('DOWNLOAD', 'Data Download'),      # CSV generation
    ]
    # 1. Who (Identity)
    session_key = models.CharField(max_length=40, null=True, blank=True)
    user_email = models.EmailField(null=True, blank=True)
    # 2. What (Intent)
    action_type = models.CharField(max_length=20, choices=ACTION_CHOICES)
    # stores the exact scientific query (e.g., {"crop": "Maize", "year": 2020})
    filters = models.JSONField(default=dict)
    # Create a list of downloaded IDs for reference
    download_ids = models.JSONField(null=True, blank=True)
    # Track impact (e.g., "User downloaded 500 rows")
    result_count = models.IntegerField(null=True, blank=True, help_text="Number of items returned/downloaded")
    # 3. When
    timestamp = models.DateTimeField(auto_now_add=True)
    # 4. Bot Detection
    is_bot = models.BooleanField(default=False)

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['action_type', 'timestamp']),
        ]

    def __str__(self):
        return f"{self.action_type} ({self.timestamp.strftime('%Y-%m-%d %H:%M')})"


# Assisted-upload workbench models live in their own module for clarity; import
# them here so Django's app-loader (and migrations) discover them.
from n2odb.models_upload import (  # noqa: E402,F401
    UploadSubmission,
    ManuscriptSource,
    UploadedDataFile,
    LlmExtractionCall,
)