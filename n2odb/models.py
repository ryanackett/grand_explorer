from django.db import models
from django.contrib.auth.models import AbstractUser

class User(AbstractUser):
    pass


class Publication(models.Model):
    PubID = models.AutoField(db_column='PubID', primary_key=True)  # Field name made lowercase.
    PubTitle = models.CharField(db_column='PubTitle', max_length=128)  # Field name made lowercase.
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
    class Meta:
        db_table = 'Site'


class Experiment(models.Model):
    ExperimentID = models.AutoField(db_column='ExperimentID', primary_key=True)  # Field name made lowercase.
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
    Tillage = models.CharField(db_column='Tillage', max_length=32, blank=True, null=True)  # Field name made lowercase.
    FluxInstrument = models.CharField(db_column='FluxInstrument', max_length=45, blank=False, null=True)  # Field name made lowercase.

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

    class Meta:
        db_table = 'Replication'


class LinearMeasurement(models.Model):
    RawID = models.AutoField(db_column='RawID', primary_key=True)  # Field name made lowercase.
    RepID = models.ForeignKey('Replication', models.DO_NOTHING, db_column='RepID')  # Field name made lowercase.
    Date = models.DateField(db_column='Date')  # Field name made lowercase.
    N2OFlux = models.FloatField(db_column='N2OFlux')  # Field name made lowercase.
    VWC = models.FloatField(db_column='VWC', blank=True, null=True)  # Field name made lowercase.
    WFPS = models.FloatField(db_column='WFPS', blank=True, null=True)  # Field name made lowercase.
    SoilT = models.FloatField(db_column='SoilT', blank=True, null=True)  # Field name made lowercase.
    AirT = models.FloatField(db_column='AirT', blank=True, null=True)  # Field name made lowercase.
    Precip = models.FloatField(db_column='Precip', blank=True, null=True)  # Field name made lowercase.
    NitrogenApplied = models.FloatField(db_column='NitrogenApplied')  # Field name made lowercase.
    NitrogenForm = models.CharField(db_column='NitrogenForm', max_length=32, blank=True, null=True)  # Field name made lowercase.
    PlantedCrop = models.CharField(db_column='PlantedCrop', max_length=32, blank=True, null=True)  # Field name made lowercase.
    Harvest = models.IntegerField(db_column='Harvest')  # Field name made lowercase.
    Tillage = models.IntegerField(db_column='Tillage')  # Field name made lowercase.
    IrrigationApplied = models.FloatField(db_column='IrrigationApplied')  # Field name made lowercase.

    class Meta:
        db_table = 'LinearMeasurement'


class MLMeasurement(models.Model):
    rawid = models.AutoField(db_column='RawID', primary_key=True)  # Field name made lowercase.
    repid = models.ForeignKey('Replication', models.DO_NOTHING, db_column='RepID')  # Field name made lowercase.
    date = models.DateField(db_column='Date')  # Field name made lowercase.
    n2oflux = models.FloatField(db_column='N2OFlux')  # Field name made lowercase.
    vwc = models.FloatField(db_column='VWC', blank=True, null=True)  # Field name made lowercase.
    wfps = models.FloatField(db_column='WFPS', blank=True, null=True)  # Field name made lowercase.
    soilt = models.FloatField(db_column='SoilT', blank=True, null=True)  # Field name made lowercase.
    airt = models.FloatField(db_column='AirT', blank=True, null=True)  # Field name made lowercase.
    precip = models.FloatField(db_column='Precip', blank=True, null=True)  # Field name made lowercase.
    nitrogenapplied = models.FloatField(db_column='NitrogenApplied')  # Field name made lowercase.
    nitrogenform = models.CharField(db_column='NitrogenForm', max_length=32)  # Field name made lowercase.
    plantedcrop = models.CharField(db_column='PlantedCrop', max_length=32)  # Field name made lowercase.
    harvest = models.IntegerField(db_column='Harvest')  # Field name made lowercase.
    tillage = models.IntegerField(db_column='Tillage')  # Field name made lowercase.
    irrigationapplied = models.FloatField(db_column='IrrigationApplied')  # Field name made lowercase.

    class Meta:
        db_table = 'MLMeasurement'


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
    nitrogenform = models.CharField(db_column='NitrogenForm', max_length=32)  # Field name made lowercase.
    plantedcrop = models.CharField(db_column='PlantedCrop', max_length=32)  # Field name made lowercase.
    harvest = models.IntegerField(db_column='Harvest')  # Field name made lowercase.
    tillage = models.IntegerField(db_column='Tillage')  # Field name made lowercase.
    irrigationapplied = models.FloatField(db_column='IrrigationApplied')  # Field name made lowercase.

    class Meta:
        db_table = 'RawMeasurement'
