from django.contrib import admin
from import_export.admin import ImportExportModelAdmin
from import_export import resources, fields
from import_export.widgets import ForeignKeyWidget
from .models import Publication, Site, Experiment, Treatment, RawMeasurementTreatment
from .models_upload import (
    UploadSubmission, ManuscriptSource, UploadedDataFile, LlmExtractionCall,
)


class ManuscriptSourceInline(admin.TabularInline):
    model = ManuscriptSource
    extra = 0
    fields = ("kind", "original_name", "extraction_status", "page_count",
              "char_count", "created_at")
    readonly_fields = fields
    show_change_link = True


class UploadedDataFileInline(admin.TabularInline):
    model = UploadedDataFile
    extra = 0
    fields = ("kind", "original_name", "parse_status", "sheet_count",
              "row_count", "created_at")
    readonly_fields = fields
    show_change_link = True


@admin.register(UploadSubmission)
class UploadSubmissionAdmin(admin.ModelAdmin):
    list_display = ("public_token", "status", "contributor_email",
                    "manuscript_extracted_at", "created_at")
    list_filter = ("status",)
    readonly_fields = ("public_token", "created_at", "updated_at",
                       "manuscript_extracted_at")
    inlines = [UploadedDataFileInline, ManuscriptSourceInline]

    def get_queryset(self, request):
        # The MB-scale parsed_content JSON must stay out of the changelist's
        # ORDER BY (Meta.ordering) or MySQL's filesort overflows (error 1038).
        return super().get_queryset(request).defer("parsed_content")


@admin.register(ManuscriptSource)
class ManuscriptSourceAdmin(admin.ModelAdmin):
    list_display = ("id", "submission", "kind", "original_name",
                    "extraction_status", "page_count", "char_count", "created_at")
    list_filter = ("kind", "extraction_status")
    # file_bytes is a binary blob — exclude it from the change form.
    exclude = ("file_bytes",)
    readonly_fields = ("sha256", "size_bytes", "extracted_text", "page_count",
                       "char_count", "extraction_status", "extraction_error",
                       "created_at")


@admin.register(UploadedDataFile)
class UploadedDataFileAdmin(admin.ModelAdmin):
    list_display = ("id", "submission", "kind", "original_name",
                    "parse_status", "sheet_count", "row_count", "created_at")
    list_filter = ("kind", "parse_status")
    exclude = ("file_bytes",)
    readonly_fields = ("sha256", "size_bytes", "sheet_count", "row_count",
                       "parse_status", "parse_error", "created_at")

@admin.register(LlmExtractionCall)
class LlmExtractionCallAdmin(admin.ModelAdmin):
    """Read-only audit log of billable manuscript-extraction calls (cost monitoring)."""
    list_display = ("id", "user", "submission", "status", "created_at")
    list_filter = ("status", "created_at")
    date_hierarchy = "created_at"
    readonly_fields = ("user", "submission", "status", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class PublicationResource(resources.ModelResource):
    
    class Meta:
        model = Publication
        
        # 1. Whitelist: Only allow these specific fields to be imported
        fields = ('PubID', 'PubTitle', 'LeadAuthor', 'PubYear', 'Citation', 'Link')
        
        # 2. Identification: This is CRITICAL.
        # This tells Django: "Look at the PubID column. If it matches an existing 
        # row in the database, UPDATE that row. If not, CREATE a new one."
        import_id_fields = ('PubID',)
        
        # 3. Validation: Skip rows that haven't changed to speed up large imports
        skip_unchanged = True
        report_skipped = True

    def before_import(self, dataset, **kwargs):
        """
        Smart Header Cleaning:
        Maps common CSV variations to your exact Model Field names.
        """
        aliases = {
            # Common variations for PubID
            'id': 'PubID',
            'publication_id': 'PubID',
            
            # Common variations for PubTitle
            'title': 'PubTitle',
            'publication_title': 'PubTitle',
            'pub_title': 'PubTitle',
            
            # Common variations for PubYear
            'year': 'PubYear',
            'date': 'PubYear',
            'pub_year': 'PubYear',
            
            # Common variations for LeadAuthor
            'author': 'LeadAuthor',
            'lead_author': 'LeadAuthor',
            'name': 'LeadAuthor',
            
            # Common variations for Link
            'url': 'Link',
            'website': 'Link',
            'doi': 'Link',
        }

        new_headers = []
        
        for header in dataset.headers:
            # Clean the header: remove extra spaces and make it lowercase
            clean_header = header.strip().lower()
            
            # Replace spaces/dashes with underscores for better matching
            clean_header = clean_header.replace(' ', '_').replace('-', '_')
            
            # If we recognize the alias, swap it for the Real Field Name
            if clean_header in aliases:
                new_headers.append(aliases[clean_header])
            else:
                # If we don't recognize it, keep the original (or normalized) version
                # Note: We must return the EXACT field name (e.g., 'PubTitle') 
                # if we want import-export to pick it up automatically.
                new_headers.append(header.strip())

        dataset.headers = new_headers

@admin.register(Publication)
class PublicationAdmin(ImportExportModelAdmin):
    resource_class = PublicationResource
    list_display = ('PubID', 'PubTitle', 'LeadAuthor', 'PubYear', 'Citation', 'Link')
    search_fields = ('PubTitle', 'LeadAuthor')


class SiteResource(resources.ModelResource):
    class Meta:
        model = Site
        fields = ('SiteID', 'SiteName', 'Latitude', 'Longitude', 'MAP', 'MAT', 'Gracenet')
        import_id_fields = ('SiteID',)
        skip_unchanged = True
        report_skipped = True

    def before_import(self, dataset, **kwargs):
        """
        Smart Header Cleaning:
        Maps common CSV variations to your exact Model Field names.
        """
        aliases = {
            'id': 'SiteID',
            'site_id': 'SiteID',
            'name': 'SiteName',
            'site_name': 'SiteName',
            'lat': 'Latitude',
            'latitude': 'Latitude',
            'long': 'Longitude',
            'longitude': 'Longitude',
            'map': 'MAP',
            'mat': 'MAT',
            'gracenet': 'Gracenet',
        }
        new_headers = []
        for header in dataset.headers:
            clean_header = header.strip().lower()
            clean_header = clean_header.replace(' ', '_').replace('-', '_')
            if clean_header in aliases:
                new_headers.append(aliases[clean_header])
            else:
                new_headers.append(header.strip())
        dataset.headers = new_headers

@admin.register(Site)
class SiteAdmin(ImportExportModelAdmin):
    resource_class = SiteResource
    list_display = ('SiteID', 'SiteName', 'Latitude', 'Longitude', 'MAP', 'MAT', 'Gracenet')
    search_fields = ('SiteName',)

class ExperimentResource(resources.ModelResource):
    class Meta:
        model = Experiment
        fields = ('ExperimentID', 'SiteID', 'ExperimentName', 'PubID')
        import_id_fields = ('ExperimentID',)
        skip_unchanged = True
        report_skipped = True

    def before_import(self, dataset, **kwargs):
        aliases = {
            'id': 'ExperimentID',
            'experiment_id': 'ExperimentID',
            'site_id': 'SiteID',
            'experiment_name': 'ExperimentName',
            'name': 'ExperimentName',
            'pub_id': 'PubID',
            'publication_id': 'PubID',
        }
        new_headers = []
        for header in dataset.headers:
            clean_header = header.strip().lower()
            clean_header = clean_header.replace(' ', '_').replace('-', '_')
            if clean_header in aliases:
                new_headers.append(aliases[clean_header])
            else:
                new_headers.append(header.strip())
        dataset.headers = new_headers

@admin.register(Experiment)
class ExperimentAdmin(ImportExportModelAdmin):
    resource_class = ExperimentResource
    list_display = ('ExperimentID', 'ExperimentName', 'SiteID', 'PubID')
    search_fields = ('ExperimentName',)

class TreatmentResource(resources.ModelResource):
    class Meta:
        model = Treatment
        fields = ('TreatmentID', 'ExperimentID', 'TreatmentDescription', 'SandMean', 
                  'SiltMean', 'ClayMean', 'pHMean', 'BulkDensityMean', 'SOMMean',
                  'PrimaryCrop', 'SecondaryCrop', 'CoverCrop', 'Management',
                  'FluxInstrument', 'TreatmentName')
        import_id_fields = ('TreatmentID',)
        skip_unchanged = True
        report_skipped = True

    def before_import(self, dataset, **kwargs):
        aliases = {
            'id': 'TreatmentID',
            'treatment_id': 'TreatmentID',
            'treatmentid': 'TreatmentID',
            'experiment_id': 'ExperimentID',
            'experimentid': 'ExperimentID',
            'exp_id': 'ExperimentID',
            'expid': 'ExperimentID',
            'treatment_description': 'TreatmentDescription',
            'description': 'TreatmentDescription',
            'treatmentdescription': 'TreatmentDescription',
            'sand_mean': 'SandMean',
            'sand': 'SandMean',
            'sandmean': 'SandMean',
            'silt_mean': 'SiltMean',
            'silt': 'SiltMean',
            'siltmean': 'SiltMean',
            'clay_mean': 'ClayMean',
            'clay': 'ClayMean',
            'claymean': 'ClayMean',
            'ph_mean': 'pHMean',
            'ph': 'pHMean',
            'phmean': 'pHMean',
            'bulk_density_mean': 'BulkDensityMean',
            'bulk_density': 'BulkDensityMean',
            'bd': 'BulkDensityMean',
            'bulkdensitymean': 'BulkDensityMean',
            'som_mean': 'SOMMean',
            'sommean': 'SOMMean',
            'som': 'SOMMean',
            'primarycrop': 'PrimaryCrop',
            'crop': 'PrimaryCrop',
            'secondarycrop': 'SecondaryCrop',
            'covercrop': 'CoverCrop',
            'management': 'Management',
            'mgmt': 'Management',
            'fluxinstrument': 'FluxInstrument',
            'treatment_name': 'TreatmentName',
            'name': 'TreatmentName',
            'treatmentname': 'TreatmentName',
        }
        new_headers = []
        for header in dataset.headers:
            clean_header = header.strip().lower()
            clean_header = clean_header.replace(' ', '_').replace('-', '_')
            if clean_header in aliases:
                new_headers.append(aliases[clean_header])
            else:
                new_headers.append(header.strip())
        dataset.headers = new_headers

@admin.register(Treatment)
class TreatmentAdmin(ImportExportModelAdmin):
    resource_class = TreatmentResource
    list_display = ('TreatmentID', 'ExperimentID', 'TreatmentDescription', 'PrimaryCrop', 'SecondaryCrop', 
                    'CoverCrop', 'Management', 'FluxInstrument', 'SandMean', 'SiltMean', 'ClayMean',
                    'pHMean', 'BulkDensityMean', 'SOMMean', 'TreatmentName')
    search_fields = ('TreatmentDescription',)


class RawMeasurementResource(resources.ModelResource):

    def before_import(self, dataset, **kwargs):
        # Map specific aliases if auto-matching fails
        aliases = {
            'rawtreatmentid': 'rawtreatmentid',
            'raw_treatment_id': 'rawtreatmentid',
            'TreatmentID': 'treatmentid',
            'TreatmentId': 'treatmentid',
            'treatment_id': 'treatmentid',
            'SoilNO3': 'soilno3',
            'SoilN03': 'soilno3', # Catching the typo N-Zero-3
            'NO3': 'soilno3',
            'N03': 'soilno3',     # Catching the typo again
            'SoilNH4': 'soilnh4',
            'NH4': 'soilnh4',
            'N2OFlux': 'n2oflux',
            'n2o_flux': 'n2oflux',
            'FluxStandardError': 'fluxstandarderror',
            'flux_se': 'fluxstandarderror',
            'FluxSE': 'fluxstandarderror',
            'Precip': 'precip',
            'Precipitation': 'precip',
            'SoilT': 'soilt',
            'SoilTemp': 'soilt',
            'SoilTemperature': 'soilt',
            'AirT': 'airt',
            'AirTemp': 'airt',
            'AirTemperature': 'airt',
            'PlantedCrop': 'plantedcrop',
            'Crop': 'plantedcrop',
            'Management': 'management',
            'Mgmt': 'management',
        }

        new_headers = []
        for header in dataset.headers:
            # 1. Strip whitespace
            clean_header = header.strip()
            
            # 2. Check aliases first
            if clean_header in aliases:
                new_headers.append(aliases[clean_header])
            # 3. Otherwise, try to match the verbose db_column style 
            # (The logic below simply passes the header through, relying on
            # Django's case-insensitive matching or your CSV matching db_columns)
            else:
                new_headers.append(clean_header)
        
        dataset.headers = new_headers

        lower_headers = [h.lower() for h in dataset.headers]
        dataset.headers = lower_headers

    class Meta:
        model = RawMeasurementTreatment
        
        # Use RawTreatmentID to identify rows (for updates).
        # Leave this column BLANK in your CSV to create new rows.
        import_id_fields = ('rawtreatmentid',)
        
        # Optional: Skip rows that haven't changed to speed up import
        skip_unchanged = True
        report_skipped = True
        
        # Ensure your CSV is recognized even if columns are missing (e.g. valid blank fields)
        clean_model_instances = True

@admin.register(RawMeasurementTreatment)
class RawMeasurementTreatmentAdmin(ImportExportModelAdmin):
    resource_class = RawMeasurementResource

    skip_import_confirm = True

    # 1. COLUMNS TO SHOW IN THE LIST (The most critical summary data)
    list_display = (
        'rawtreatmentid',
        'treatmentid', 
        'date', 
        'n2oflux', 
        'vwc', 
        'soilno3', 
        'plantedcrop',
        'hmtruth'  # Useful to see which rows are marked as "Truth" vs Outliers
    )

    # 2. SIDEBAR FILTERS (To quickly drill down)
    list_filter = (
        'date',
        'plantedcrop',
        'hmtruth', 
        'nitrogenform',
        # This adds a smart filter for the Foreign Key (Treatment)
        ('treatmentid', admin.RelatedOnlyFieldListFilter),
    )

    # 3. SEARCH BOX (Search by Crop, Management, or Nitrogen Type)
    search_fields = ('plantedcrop', 'management', 'nitrogentype', 'nitrogenform')

    # 4. ORGANIZE THE EDIT PAGE (Groups fields into sections)
    fieldsets = (
        ('Identifiers', {
            'fields': ('treatmentid', 'date', 'doy', 'management')
        }),
        ('Key Flux Measurements', {
            'fields': (
                ('n2oflux', 'fluxstandarderror', 'fluxconfidenceint'),
                ('vwc', 'wfps', 'vwccalculated', 'wfpscalculated'),
            )
        }),
        ('Environmental Conditions', {
            'fields': (
                ('airt', 'airtmax', 'airtmin'),
                ('soilt', 'precip', 'irrigationapplied'),
            )
        }),
        ('Soil Chemistry & Crop', {
            'fields': (
                ('soilnh4', 'soilno3'),
                ('nitrogenapplied', 'nitrogenform', 'nitrogentype'),
                ('plantedcrop', 'harvest', 'tillage'),
            )
        }),
        ('Outlier Analysis (Flags & Scores)', {
            'classes': ('collapse',),  # This section starts hidden (collapsible)
            'fields': (
                'hmtruth',
                ('hmmcd', 'mcdscores'),
                ('hmknn', 'knnscores'),
                ('hmmad', 'madscores'),
                ('hmlof', 'lofscores'),
                ('hmabod', 'abodscores'),
                ('hmiforest', 'iforestscores'),
                ('hmarima', 'arimascores'),
                ('hmhbos', 'hbosscores'),
                'hm75percentile',
                'hmiqr',
            )
        }),
    )
