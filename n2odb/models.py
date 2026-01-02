from django.db import models
from django.contrib.auth.models import AbstractUser

class User(AbstractUser):
    pass


class Publication(models.Model):
    PubID = models.AutoField(db_column='PubID', primary_key=True)  # Field name made lowercase.
    PubTitle = models.CharField(db_column='PubTitle', max_length=255)  # Field name made lowercase.
    LeadAuthor = models.CharField(db_column='LeadAuthor', max_length=32)  # Field name made lowercase.
    Citation = models.CharField(db_column='Citation', max_length=500)  # Field name made lowercase.
    PubYear = models.IntegerField(db_column='PubYear')  # Field name made lowercase.
    class Meta:
        db_table = 'Publication'


class Site(models.Model):
    SiteID = models.AutoField(db_column='SiteID', primary_key=True)  # Field name made lowercase.
    Latitude = models.FloatField(db_column='Latitude')  # Field name made lowercase.
    Longitude = models.FloatField(db_column='Longitude')  # Field name made lowercase.
    MAP = models.FloatField(db_column='MAP', blank=True, null=True)  # Field name made lowercase.
    MAT = models.FloatField(db_column='MAT', blank=True, null=True)  # Field name made lowercase.
    Gracenet = models.BooleanField(db_column='Gracenet', blank=False, null=True)
    Name = models.CharField(db_column='Name', max_length=255, blank=True, null=True)  # Field name made lowercase.
    class Meta:
        db_table = 'Site'


class Experiment(models.Model):
    ExperimentID = models.AutoField(db_column='ExperimentID', primary_key=True)  # Field name made lowercase.
    ExperimentName = models.CharField(db_column='ExperimentName', max_length=255)  # Field name made lowercase.
    SiteID = models.ForeignKey('Site', models.DO_NOTHING, db_column='SiteID')  # Field name made lowercase.
    PubID = models.ForeignKey('Publication', models.DO_NOTHING, db_column='PubID')  # Field name made lowercase.
    class Meta:
        db_table = 'Experiment'


class Treatment(models.Model):
    TreatmentID = models.AutoField(db_column='TreatmentID', primary_key=True)  # Field name made lowercase.
    ExperimentID = models.ForeignKey(Experiment, models.DO_NOTHING, db_column='ExperimentID')  # Field name made lowercase.
    PrimaryCrop = models.CharField(db_column='PrimaryCrop', max_length=32)  # Field name made lowercase.
    SecondaryCrop = models.CharField(db_column='SecondaryCrop', max_length=32, blank=True, null=True)  # Field name made lowercase.
    CoverCrop = models.CharField(db_column='CoverCrop', max_length=32, blank=True, null=True)  # Field name made lowercase.
    Management = models.CharField(db_column='Management', max_length=16, blank=True, null=True)  # Field name made lowercase.
    FluxInstrument = models.CharField(db_column='FluxInstrument', max_length=45, blank=False, null=True)  # Field name made lowercase.
    TreatmentName = models.CharField(db_column='TreatmentName', max_length=45, blank=True, null=True)  # Field name made lowercase.
    SandMean = models.FloatField(db_column='SandMean', blank=True, null=True)  # Field name made lowercase.
    SiltMean = models.FloatField(db_column='SiltMean', blank=True, null=True)  # Field name made lowercase.
    ClayMean = models.FloatField(db_column='ClayMean', blank=True, null=True)  # Field name made lowercase.
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
    nitrogenapplied = models.FloatField(db_column='NitrogenApplied')  # Field name made lowercase.
    nitrogenform = models.CharField(db_column='NitrogenForm', max_length=64)  # Field name made lowercase.
    nitrogentype = models.CharField(db_column='NitrogenType', max_length=45)  # Field name made lowercase.
    plantedcrop = models.CharField(db_column='PlantedCrop', max_length=32)  # Field name made lowercase.
    harvest = models.IntegerField(db_column='Harvest')  # Field name made lowercase.
    tillage = models.IntegerField(db_column='Tillage')  # Field name made lowercase.
    irrigationapplied = models.FloatField(db_column='IrrigationApplied')  # Field name made lowercase.
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
    nitrogenapplied = models.FloatField(db_column='NitrogenApplied')  # Field name made lowercase.
    nitrogenform = models.CharField(db_column='NitrogenForm', max_length=64)  # Field name made lowercase.
    nitrogentype = models.CharField(db_column='NitrogenType', max_length=45)  # Field name made lowercase.
    plantedcrop = models.CharField(db_column='PlantedCrop', max_length=32)  # Field name made lowercase.
    harvest = models.IntegerField(db_column='Harvest')  # Field name made lowercase.
    tillage = models.IntegerField(db_column='Tillage')  # Field name made lowercase.
    irrigationapplied = models.FloatField(db_column='IrrigationApplied')  # Field name made lowercase.
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
    fluxstandarderror = models.FloatField(db_column='FluxStandardError', blank=True, null=True)  # Field name made lowercase.
    fluxconfidenceint = models.FloatField(db_column='FluxConfidenceInt', blank=True, null=True)  # Field name made lowercase.
    hmiqr = models.BooleanField(db_column='HMIQR', blank=False, null=True)
    madscores = models.CharField(db_column='MADscores', max_length=45, blank=True, null=True)  # Field name made lowercase.
    hmmad = models.BooleanField(db_column='HMMAD', blank=False, null=True)
    hmlof = models.BooleanField(db_column='HMLOF', blank=False, null=True)
    lofscores = models.CharField(db_column='LOFscores', max_length=45, blank=True, null=True)  # Field name made lowercase.
    hmabod = models.BooleanField(db_column='HMABOD', blank=False, null=True)
    abodscores = models.CharField(db_column='ABODscores', max_length=45, blank=True, null=True)  # Field name made lowercase.
    hmiforest = models.BooleanField(db_column='HMIForest', blank=False, null=True)
    iforestscores = models.CharField(db_column='IForestscores', max_length=45, blank=True, null=True)  # Field name made lowercase.
    hmarima = models.BooleanField(db_column='HMARIMA', blank=False, null=True)
    arimascores = models.CharField(db_column='ARIMAscores', max_length=45, blank=True, null=True)  # Field name made lowercase.
    hm75percentile = models.BooleanField(db_column='HM75Percentile', blank=False, null=True)
    hmhbos = models.BooleanField(db_column='HMHBOS', blank=False, null=True)
    hbosscores = models.CharField(db_column='HBOSscores', max_length=45, blank=True, null=True)  # Field name made lowercase.
    class Meta:
        db_table = 'RawMeasurementTreatment'