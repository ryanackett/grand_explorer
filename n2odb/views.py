from django.shortcuts import render
from django.db import models
from n2odb.models import Publication, Site, Experiment, Treatment, Replication, RawMeasurementTreatment, UserAction
from django.template.defaulttags import register
from django.template import loader
from django.template import Template, Context
from json import dumps
from django.http import JsonResponse
import json
from django.db.models import F
from collections import defaultdict
import datetime
from django.db.models import Count, Avg, Min, Max, Sum
from django.views.decorators.csrf import csrf_exempt
from .utils import Echo, _log_user_action
from django.views.decorators.http import require_POST  
import csv
import os
import zipstream, zipfile
from django.http import StreamingHttpResponse
from django.conf import settings
from django.core.mail import send_mail
from django.contrib import messages


def index(request): 
    return render(request, 'index.html')


def about(request):
    return render(request, 'about.html', {
    })

def query_filters(request):
    return render(request, 'query-filters.html', {
    })

def get_data(request):
    # -------------------------------------------------------------------------
    # PART 1: AJAX REQUEST (Diagnostic + Functional)
    # -------------------------------------------------------------------------
    if request.method == "POST" and request.headers.get('x-requested-with') == 'XMLHttpRequest':
        try:
            data = json.loads(request.body)
            
            # ### ANALYTICS ###
            if data.get('action') == 'fetch_graph':
                return get_graph_json(data.get('treatment_id'))
            
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)

        # 1. EXTRACT FILTERS
        selected_ins = data.get('instruments', [])
        selected_mgmt = data.get('management', [])
        selected_env = data.get('environment', [])
        selected_fert = data.get('fertilizer', [])
        selected_crops = data.get('crops', [])
        selected_soil = data.get('soil', [])
        print(selected_soil)

        print(f"\n=== DIAGNOSTIC START ===")
        print(f"Incoming Filters: {selected_env}")

        ENV_COLUMN_MAP = {
            'Soil Temperature': 'soilt',       
            'Soil VWC':         'vwc',
            'WFPS':             'wfps',
            'Mean Air Temperature': 'airt',
            'Max Air Temperature':  'airtmax',
            'Min Air Temperature':  'airtmin',
            'Precipitation':    'precip',
            'Soil Nitrate':     'soilno3',
            'Soil Ammonium':    'soilnh4',
        }

        SOIL_COLUMN_MAP = {
            'Soil pH':      'pHMean',       
            'Soil Bulk Density': 'BulkDensityMean',
            'Soil Organic Carbon':          'SOMMean',      
        }

        # ---------------------------------------------------------------------
        # 2. LOGIC: SET INTERSECTION (With Logging)
        # ---------------------------------------------------------------------
        # Start with ALL treatment IDs
        valid_treatment_ids = set(Treatment.objects.values_list('pk', flat=True))
        print(f"Base Total Treatments: {len(valid_treatment_ids)}")

        # A. Apply Environmental Filters
        if selected_env:
            for env_label in selected_env:
                if env_label in ENV_COLUMN_MAP:
                    column_name = ENV_COLUMN_MAP[env_label]
                    lookup_key = f"measurements__{column_name}__isnull"
                    
                    # 1. Find IDs that have THIS variable
                    ids_with_var = set(
                        Treatment.objects.filter(**{lookup_key: False})
                        .values_list('pk', flat=True)
                    )
                    
                    # 2. Perform Intersection
                    before_count = len(valid_treatment_ids)
                    valid_treatment_ids = valid_treatment_ids.intersection(ids_with_var)
                    after_count = len(valid_treatment_ids)
                    
                    print(f" -> Filter '{env_label}': Found {len(ids_with_var)} matches.")
                    print(f"    Intersection: {before_count} -> {after_count} remaining.")
                else:
                    print(f"WARNING: '{env_label}' not in map!")

        # B. Apply Soil Filters (Strict Intersection)
        if selected_soil:
            for soil_label in selected_soil:
                if soil_label in SOIL_COLUMN_MAP:
                    column_name = SOIL_COLUMN_MAP[soil_label]
                    
                    # CASE 1: If these fields are directly on the Treatment model:
                    lookup_key = f"{column_name}__isnull"
                    
                    # CASE 2: If they are in a related table (e.g., SoilData):
                    # lookup_key = f"soildata__{column_name}__isnull"

                    ids_with_soil = set(
                        Treatment.objects.filter(**{lookup_key: False})
                        .values_list('pk', flat=True)
                    )
                    valid_treatment_ids = valid_treatment_ids.intersection(ids_with_soil)

        # ---------------------------------------------------------------------
        # 3. BUILD FINAL QUERY
        # ---------------------------------------------------------------------
        treatment_qs = Treatment.objects.filter(pk__in=valid_treatment_ids)

        # B. Apply Standard Filters
        if selected_ins:
            treatment_qs = treatment_qs.filter(FluxInstrument__in=selected_ins)
        if selected_mgmt:
            treatment_qs = treatment_qs.filter(Management__in=selected_mgmt)
        if selected_crops:
            treatment_qs = treatment_qs.filter(PrimaryCrop__in=selected_crops)
        if selected_fert:
            # Note: This join can cause duplicates, so we distinct() later
            treatment_qs = treatment_qs.filter(measurements__nitrogentype__in=selected_fert)

        # C. Execute Data Fetch
        filtered_treatments = list(treatment_qs.distinct().values(
            'ExperimentID', 'TreatmentID', 'TreatmentName', 'PrimaryCrop', 
            'SecondaryCrop', 'CoverCrop', 'Management', 'FluxInstrument'
        ))
        
        print(f"Final Treatments Returned: {len(filtered_treatments)}")

        # ---------------------------------------------------------------------
        # 4. FETCH PARENTS (Valid JSON Packaging)
        # ---------------------------------------------------------------------
        valid_exp_ids = list(set([t['ExperimentID'] for t in filtered_treatments]))
        
        # Experiments
        exp_qs = Experiment.objects.filter(ExperimentID__in=valid_exp_ids)
        experiment_data = list(exp_qs.annotate(
            Lat=F('SiteID__Latitude'),
            Lon=F('SiteID__Longitude'),
            SiteName=F('SiteID__SiteName'), 
            MAP=F('SiteID__MAP'),
            MAT=F('SiteID__MAT'),
            PubTitle=F('PubID__PubTitle'),
            Citation=F('PubID__Citation'),
            Abstract=F('PubID__Abstract')
        ).values(
            'ExperimentID', 'ExperimentName', 
            'Lat', 'Lon', 'SiteName', 'MAP', 'MAT', 'PubTitle', 'Citation', 'Abstract'
        ))

        # Sites
        site_qs = Site.objects.filter(SiteID__in=exp_qs.values('SiteID'))
        filtered_sites = list(site_qs.values('SiteID', 'Latitude', 'Longitude', 'MAP', 'MAT').distinct())

        # Pubs
        pub_qs = Publication.objects.filter(PubID__in=exp_qs.values('PubID'))
        filtered_pubs = list(pub_qs.values('PubID', 'PubTitle', 'PubYear', 'LeadAuthor', 'Citation', 'Abstract').distinct())
        
        # --- NEW: FETCH MEASUREMENT DATES IN ONE BULK QUERY ---
        valid_treatment_ids = [t['TreatmentID'] for t in filtered_treatments]
        print(valid_treatment_ids)

        date_aggregates = RawMeasurementTreatment.objects.filter(
            treatmentid__in=valid_treatment_ids
        ).values('treatmentid').annotate(
            first_date=Min('date'), 
            last_date=Max('date')
        )
        print(f"Fetched date aggregates for {len(date_aggregates)} treatments.")

        # Create a fast lookup dictionary: {101: {'first': '2020-01-01', 'last': '2022-12-31'}}
        date_lookup = {
            item['treatmentid']: {
                # Converting to string so it serializes cleanly into JSON later
                'first_date': item['first_date'].strftime('%Y-%m-%d') if item['first_date'] else None,
                'last_date': item['last_date'].strftime('%Y-%m-%d') if item['last_date'] else None,
            } 
            for item in date_aggregates
        }

        # Attach treatments to experiments
        treatment_map = defaultdict(list)
        for t in filtered_treatments:
            t_dates = date_lookup.get(t['TreatmentID'], {})
            
            row = {
                'id': t['TreatmentID'],
                'name': t['TreatmentName'] if t['TreatmentName'] else "Unnamed Treatment",
                'crop': t['PrimaryCrop'],
                'mgmt': t['Management'],
                'ins': t.get('FluxInstrument'),
                'first_date': t_dates.get('first_date'),
                'last_date': t_dates.get('last_date'),
                
            }
            treatment_map[t['ExperimentID']].append(row)

        for exp in experiment_data:
            exp['TreatmentIDs'] = treatment_map.get(exp['ExperimentID'], [])

        print(f"=== DIAGNOSTIC END ===\n")

        # 5. RETURN VALID JSON
        update_data = {
            'json_treatment': json.dumps(filtered_treatments),
            'json_site': json.dumps(filtered_sites),
            'json_pub': json.dumps(filtered_pubs),
            'json_experiments': json.dumps(experiment_data)
        }
        return JsonResponse(update_data)

    # -------------------------------------------------------------------------
    # PART 2: INITIAL LOAD (GET/POST Request for Dropdowns)
    # -------------------------------------------------------------------------
    elif request.method == 'POST':
        selectedList = request.POST['selected']
        dropdown_dict = {}
        labels = []
        if 'ins' in selectedList:
            dropdown_dict['Flux Instrument'] = list(Treatment.objects.values_list('FluxInstrument', flat=True).distinct())
            dropdown_dict['ins'] = list(
                Treatment.objects.values_list('FluxInstrument', flat=True).distinct())
            labels.append('Flux Instrument')
        if 'env' in selectedList:
            env_options = [
                'Soil Temperature', 
                'Soil VWC', 
                'WFPS', 
                'Mean Air Temperature', 
                'Max Air Temperature', 
                'Min Air Temperature', 
                'Precipitation', 
                'Soil Nitrate', 
                'Soil Ammonium'
            ]
            
            dropdown_dict['Environmental Variables'] = env_options
            dropdown_dict['env'] = env_options
            labels.append('Environmental Variables')
        if 'mgmt' in selectedList:
            dropdown_dict['Management'] = list(Treatment.objects.values_list('Management', flat=True).distinct())
            dropdown_dict['mgmt'] = list(Treatment.objects.values_list('Management', flat=True).distinct())
            labels.append('Management')
        if 'fert' in selectedList:
            dropdown_dict['Fertilizer Form'] = list(
                RawMeasurementTreatment.objects
                .exclude(nitrogentype__isnull=True)   # Drops NULL values
                .exclude(nitrogentype__exact='')      # Drops empty strings ""
                .values_list('nitrogentype', flat=True)
                .distinct()
            )
            dropdown_dict['fert'] = list(
                RawMeasurementTreatment.objects
                .exclude(nitrogentype__isnull=True)   # Drops NULL values
                .exclude(nitrogentype__exact='')      # Drops empty strings ""
                .values_list('nitrogentype', flat=True)
                .distinct()
            )

            labels.append('Fertilizer Form')
        if 'crop' in selectedList:
            dropdown_dict['crop'] = list(Treatment.objects.values_list('PrimaryCrop', flat=True).distinct())
            labels.append('Crop')
        if 'cont' in selectedList:
            dropdown_dict['Continent'] = list(Site.objects.values_list('Continent', flat=True).distinct())
            labels.append('Continent')
        if 'soil' in selectedList:
            dropdown_dict['Soil Measurements'] = ['Soil pH', 'Soil Organic Carbon', 'Soil Bulk Density']
            dropdown_dict['soil'] = ['Soil pH', 'Soil Organic Carbon', 'Soil Bulk Density']
            labels.append('Soil Measurements')
        if 'texture' in selectedList:
            dropdown_dict['Soil Texture'] = ['Sand', 'Silt', 'Clay', 'Loam', 'Sandy Loam', 'Silt Loam', 'Clay Loam',
                                             'Sandy Clay Loam', 'Silty Clay Loam', 'Sandy Clay', 'Silty Clay',
                                             'Loamy Sand'
                                             ]
            dropdown_dict['texture'] = ['Sand', 'Silt', 'Clay', 'Loam', 'Sandy Loam', 'Silt Loam', 'Clay Loam',
                                                'Sandy Clay Loam', 'Silty Clay Loam', 'Sandy Clay', 'Silty Clay',
                                                'Loamy Sand']
            labels.append('Soil Texture')

        dropdown_dict['keys'] = selectedList.split(",")
        dropdown_dict['labels'] = labels
        dropdown_dict['dropdown_dict'] = dropdown_dict

        labels_dict = {}
        labels_dict['env'] = 'Env Covariates (all of these)'
        labels_dict['ins'] = 'Flux Instrument (any of these)'
        labels_dict['fert'] = 'Fertilizer Form (any of these)'
        labels_dict['mgmt'] = 'Management (any of these)'
        labels_dict['crop'] = 'Crop (any of these)'
        labels_dict['cont'] = 'Continent (any of these)'
        labels_dict['soil'] = 'Soil Measurements (all of these)'
        labels_dict['texture'] = 'Soil Texture (any of these)'

        dropdown_dict['labels_dict'] = labels_dict

        # Get all database data for table view
        pubdata = Publication.objects.all()
        dropdown_dict['Publication'] = pubdata
        sitedata = Site.objects.all()
        dropdown_dict['Site'] = sitedata
        treatmentdata = Treatment.objects.all()
        dropdown_dict['Treatment'] = treatmentdata
        replicationdata = Replication.objects.all()
        dropdown_dict['Replication'] = replicationdata

        # Also format same table data in JSON serializable manner for handling in javascript
        dropdown_dict['json_pub'] = dumps(list(pubdata.values('PubID', 'PubTitle', 'PubYear', 'LeadAuthor', 'Citation', 'Abstract')))
        dropdown_dict['json_site'] = dumps(
            list(sitedata.values('SiteID', 'Latitude', 'Longitude', 'MAP', 'MAT')))
        dropdown_dict['json_treatment'] = dumps(
            list(treatmentdata.values('TreatmentID', 'TreatmentName', 'PrimaryCrop', 'SecondaryCrop', 'CoverCrop', 'Management', 'FluxInstrument')))
        dropdown_dict['json_rep'] = dumps(
            list(replicationdata.values('RepID', 'RepName', 'Air_T_Meas', 'Precip_Meas', 'Soil_T_Meas', 'VWC_Meas', 'WFPS_Meas')))
        
        # 1. Query Experiments with all related Site and Publication data
        experiment_qs = Experiment.objects.values(
            'ExperimentID', 
            'ExperimentName',
            # Pull Site Data (via SiteID FK)
            Lat=F('SiteID__Latitude'),
            Lon=F('SiteID__Longitude'),
            SiteName=F('SiteID__SiteName'),
            MAP=F('SiteID__MAP'),
            MAT=F('SiteID__MAT'),
            # Pull Publication Data (via PubID FK)
            PubTitle=F('PubID__PubTitle'),
            Citation=F('PubID__Citation'),
            Abstract=F('PubID__Abstract')
        )

        # Convert to a list so we can modify it
        experiment_data = list(experiment_qs)

        # 2. Query ALL Treatments separately (fetching only what we need)
        treatments = Treatment.objects.values(
            'ExperimentID',
            'TreatmentID', 
            'TreatmentName', 
            'PrimaryCrop', 
            'Management'
        )

                # --- NEW: FETCH MEASUREMENT DATES IN ONE BULK QUERY ---
        valid_treatment_ids = [t['TreatmentID'] for t in treatments]
        print(valid_treatment_ids)

        date_aggregates = RawMeasurementTreatment.objects.filter(
            treatmentid__in=valid_treatment_ids
        ).values('treatmentid').annotate(
            first_date=Min('date'), 
            last_date=Max('date')
        )
        print(f"Fetched date aggregates for {len(date_aggregates)} treatments.")

        # Create a fast lookup dictionary: {101: {'first': '2020-01-01', 'last': '2022-12-31'}}
        date_lookup = {
            item['treatmentid']: {
                # Converting to string so it serializes cleanly into JSON later
                'first_date': item['first_date'].strftime('%Y-%m-%d') if item['first_date'] else None,
                'last_date': item['last_date'].strftime('%Y-%m-%d') if item['last_date'] else None,
            } 
            for item in date_aggregates
        }

        # 3. Group Treatments by ExperimentID using a Python dictionary
        treatment_map = defaultdict(list)
        for t in treatments:
            t_dates = date_lookup.get(t['TreatmentID'], {})
            row = {
                'id': t['TreatmentID'],
                'name': t['TreatmentName'] if t['TreatmentName'] else "Unnamed Treatment",
                'crop': t['PrimaryCrop'],
                'mgmt': t['Management'],
                'first_date': t_dates.get('first_date'),
                'last_date': t_dates.get('last_date'),
            }
            treatment_map[t['ExperimentID']].append(row)

        # 4. Attach the Treatment list to each Experiment object
        for exp in experiment_data:
            # Look up the list of treatments using the ID, default to empty list if none
            exp['TreatmentIDs'] = treatment_map.get(exp['ExperimentID'], [])

        # 5. Add to your dropdown_dict for the frontend
        dropdown_dict['json_experiments'] = dumps(experiment_data)        
        
        return render(request, 'get-data.html', dropdown_dict)


def get_graph_json(treatment_id):
    try:
        # 1. Verify Treatment
        treatment = Treatment.objects.filter(pk=treatment_id).first()
        if not treatment:
            return JsonResponse({'error': f'Treatment ID {treatment_id} not found.'})

        # 2. Define All Possible Columns
        fields_map = {
            'n2oflux': 'N2O Flux',
            'nitrogenapplied': 'Nitrogen Applied',
            'wfps': 'WFPS',
            'vwc': 'Volumetric Water Content',
            'soilt': 'Soil Temp',
            'airt': 'Air Temp',
            'precip': 'Precipitation'
        }

        # 3. Fetch Data
        columns_to_fetch = ['date'] + list(fields_map.keys())
        
        data_rows = RawMeasurementTreatment.objects.filter(
            treatmentid=treatment
        ).values(*columns_to_fetch).order_by('date')

        if not data_rows.exists():
             return JsonResponse({'error': 'No raw measurement data recorded for this treatment.'})

        # 4. Initialize Containers
        raw_series = {key: [] for key in fields_map.keys()}

        # 5. Process Rows
        for row in data_rows:
            date_val = row['date']
            if not date_val: continue
            
            # Combine Date + Midnight Time -> Timestamp
            dt = datetime.datetime.combine(date_val, datetime.time.min)
            timestamp = dt.timestamp() * 1000

            for field in fields_map.keys():
                val = row.get(field)
                if val is not None:
                    raw_series[field].append([timestamp, float(val)])
                else:
                    raw_series[field].append([timestamp, None])

        # 6. Apply Priority Logic & Build Series
        final_series = []

        # --- A. Top Graph (Flux + Nitrogen) ---
        if any(p[1] is not None for p in raw_series['n2oflux']):
            final_series.append({
                'name': 'N2O Flux',
                'data': raw_series['n2oflux'],
                'type': 'scatter',  # Already scatter
                'yAxis': 0,
                'tooltip': {'valueSuffix': ' g N/ha/d'} 
            })
        
        if any(p[1] is not None for p in raw_series['nitrogenapplied']):
            final_series.append({
                'name': 'Nitrogen Applied',
                'data': raw_series['nitrogenapplied'],
                'type': 'column',
                'yAxis': 1,
                'tooltip': {'valueSuffix': ' kg N/ha'}
            })

        # --- B. Bottom Graph (Moisture + Temp + Precip) ---
        
        # Priority: WFPS > VWC
        has_wfps = any(p[1] is not None for p in raw_series['wfps'])
        has_vwc = any(p[1] is not None for p in raw_series['vwc'])
        
        if has_wfps:
            final_series.append({
                'name': 'Water Filled Pore Space',
                'data': raw_series['wfps'],
                'type': 'scatter',  # CHANGED: line -> scatter
                'yAxis': 2,
                'tooltip': {'valueSuffix': '%'}
            })
        elif has_vwc:
            final_series.append({
                'name': 'Volumetric Water Content',
                'data': raw_series['vwc'],
                'type': 'scatter',  # CHANGED: line -> scatter
                'yAxis': 2,
                'tooltip': {'valueSuffix': ' v/v'}
            })

        # Priority: SoilT > AirT
        has_soilt = any(p[1] is not None for p in raw_series['soilt'])
        has_airt = any(p[1] is not None for p in raw_series['airt'])

        if has_soilt:
            final_series.append({
                'name': 'Soil Temp',
                'data': raw_series['soilt'],
                'type': 'scatter',  # CHANGED: line -> scatter
                'yAxis': 3,
                'tooltip': {'valueSuffix': ' °C'}
            })
        elif has_airt:
            final_series.append({
                'name': 'Air Temp',
                'data': raw_series['airt'],
                'type': 'scatter',  # CHANGED: line -> scatter
                'yAxis': 3,
                'tooltip': {'valueSuffix': ' °C'}
            })

        # Precip (Remains Column/Bar)
        if any(p[1] is not None for p in raw_series['precip']):
            final_series.append({
                'name': 'Precipitation',
                'data': raw_series['precip'],
                'type': 'column',
                'yAxis': 4,
                'tooltip': {'valueSuffix': ' mm'}
            })

        return JsonResponse({
            'title': f"Data for {treatment.TreatmentName}", 
            'series': final_series
        })

    except Exception as e:
        print(f"DEBUG ERROR: {e}") 
        return JsonResponse({'error': f"Server Error: {str(e)}"})

def submit_data(request):
    return render(request, 'submit_data.html', {
    })

def data_cart(request):
    return render(request, 'data-cart.html', {
    })

@csrf_exempt 
def get_cart_data(request):
    """
    API Endpoint: Receives IDs via POST, returns Analytics JSON.
    Mapped exactly to user provided models.py.
    """
    if request.method == 'POST':
        try:
            # 1. Parse IDs from Request Body
            body = json.loads(request.body)
            raw_ids = body.get('ids', [])
            treatment_ids = [int(x) for x in raw_ids if str(x).isdigit()]

            # 2. Fetch Treatments
            # We use select_related to traverse the ForeignKeys defined in your models:
            # Treatment.ExperimentID -> Experiment.SiteID / Experiment.PubID
            treatments = Treatment.objects.filter(pk__in=treatment_ids).select_related(
                'ExperimentID', 
                'ExperimentID__SiteID',
                'ExperimentID__PubID'
            )

            # 3. Fetch Measurements
            # Your model uses lowercase 'treatmentid' for the foreign key
            measurements = RawMeasurementTreatment.objects.filter(treatmentid__in=treatments)
            total_flux_rows = measurements.count()
            # Now get only the number of rows where N2OFlux is not null
            total_flux_measurements = measurements.filter(n2oflux__isnull=False).count()

            # --- BUILD WIDGET DATA ---

            # A. Sidebar List
            sidebar_list = []
            for t in treatments:
                # Access related objects via the attributes in your models
                exp = t.ExperimentID
                site = exp.SiteID if exp else None
                pub = exp.PubID if exp else None

                sidebar_list.append({
                    'id': t.pk,
                    'name': t.TreatmentName or f"Treatment {t.pk}", 
                    'crop': t.PrimaryCrop or "Unknown",
                    'mgmt': t.Management or "Unknown",
                    'location': site.SiteName if site else "Unknown Site", 
                    'year': pub.PubYear if pub else "N/A",
                    'publication': pub.PubTitle if pub else "N/A"
                })

            # B. Map Data (Grouped by Site)
            map_data = []
            # Get unique sites associated with these treatments
            unique_sites = set()
            for t in treatments:
                if t.ExperimentID and t.ExperimentID.SiteID:
                    unique_sites.add(t.ExperimentID.SiteID)
            
            for site in unique_sites:
                # Find which treatments in the cart belong to this specific site
                # We filter the 'treatments' list we already fetched
                site_treatments = [t for t in treatments if t.ExperimentID.SiteID == site]
                
                # Count measurements only for those treatments
                count = measurements.filter(treatmentid__in=site_treatments).count()
                
                if count > 0:
                    map_data.append({
                        'lat': site.Latitude,   # Model field: Latitude
                        'lon': site.Longitude,  # Model field: Longitude
                        'name': site.SiteName,
                        'z': count
                    })

            # C. HMMAD Stats (Methodology)
            hmmad_true = measurements.filter(hmmad=True).count()
            hmmad_false = total_flux_measurements - hmmad_true
            # Calculate the cumulative N2OFlux for each group
            hmmad_true_flux = measurements.filter(hmmad=True, n2oflux__isnull=False).aggregate(total_flux=Sum('n2oflux'))['total_flux'] or 0
            hmmad_false_flux = measurements.filter(hmmad=False, n2oflux__isnull=False).aggregate(total_flux=Sum('n2oflux'))['total_flux'] or 0
            
            # D. Crop Distribution
            # Model field: PrimaryCrop
            crop_counts = treatments.values('PrimaryCrop').annotate(count=Count('TreatmentID')).order_by('-count')
            crop_data = list(crop_counts) 

            # E. Soil Textures of Treatments
            # Create a list (sand, silt, clay) for each treatment
            soil_stats = []
            for t in treatments:
                sand = t.SandMean if t.SandMean is not None else 0
                silt = t.SiltMean if t.SiltMean is not None else 0
                clay = t.ClayMean if t.ClayMean is not None else 0
                soil_stats.append([sand, silt, clay])

            # Convert each inner list to dictionary for easier frontend use
            soil_stats = [
                {'Sand': s[0],
                 'Silt': s[1],
                 'Clay': s[2]}
                for s in soil_stats
            ]

            # F. Histogram Data (Raw Flux Values)
            # Model field: n2oflux (lowercase)
            # We filter out nulls to avoid errors
            flux_values = list(measurements.filter(n2oflux__isnull=False)
                               .values_list('n2oflux', flat=True)[:10000])

            # 4. Return JSON
            data = {
                'sidebar_list': sidebar_list,
                'total_rows': total_flux_measurements,
                'map_data': map_data,
                'hmmad_stats': {'HM': hmmad_true, 'BG': hmmad_false, 'HM_flux': hmmad_true_flux, 'BG_flux': hmmad_false_flux},
                'crop_data': crop_data,
                'soil_stats': soil_stats,
                'flux_values': flux_values
            }
            return JsonResponse(data)

        except Exception as e:
            # Print error to terminal for debugging
            print(f"API ERROR: {e}")
            return JsonResponse({'error': str(e)}, status=400)

    return JsonResponse({'error': 'POST method required'}, status=405)
    

class Echo:
    """An object that implements just the write method of the file-like
    interface.
    """
    def write(self, value):
        return value

# This view acts upon clicking Download on the Data Cart page.
# It streams a zip file containing CSVs of the selected treatments
# and a txt file readme.
@require_POST
def download_zip(request):
    parent_ids = request.POST.getlist('ids')
    # Cast to integers and filter out invalid entries
    parent_ids = [int(x) for x in parent_ids if str(x).isdigit()]
    email = request.POST.get('email_address')
    result_count = len(parent_ids)
    
    if not parent_ids:
        return StreamingHttpResponse("No data selected", status=400)
    
    # Log selections
    if parent_ids and not request.user_agent.is_bot:
        _log_user_action(
        request, 
        'DOWNLOAD_CART', 
        ids=parent_ids, 
        count=result_count,
        email=email,
        payload={}
    )
    
    # --- 1. Load the README from disk ---
    readme_path = os.path.join(settings.BASE_DIR, 'n2odb', 'static', 'files', 'README.txt')
    
    try:
        with open(readme_path, 'r', encoding='utf-8') as f:
            readme_content = f.read()
    except FileNotFoundError:
        # Fallback if file is missing so export doesn't crash
        readme_content = "Documentation file not found. Contact admin."

    # --- 2. Define the CSV Generator ---
    def csv_generator(ids_to_query):
        buffer = Echo()
        writer = csv.writer(buffer)

        column_map = {
            # Level 0: Up Front Info ---
            'Publication Title': 'treatmentid__ExperimentID__PubID__PubTitle',
            'Publication Year':  'treatmentid__ExperimentID__PubID__PubYear',

            # --- Level 1: The Measurement ---
            'Measurement ID':   'rawtreatmentid', 
            'Treatment ID':     'treatmentid',
            'Date':             'date',
            'N2O Flux (g N/ha/d)': 'n2oflux',
            'VWC (%)':          'vwc',
            'WFPS (%)':         'wfps',
            'Soil Temp (°C)':   'soilt',
            'Mean Daily Air Temp (°C)':  'airt',
            'Max Daily Air Temp (°C)':   'airtmax',
            'Min Daily Air Temp (°C)':   'airtmin',
            'Precipitation (mm)':        'precip',
            'Nitrogen Applied (kg N/ha)': 'nitrogenapplied',
            'Nitrogen Form':    'nitrogenform',
            'Nitrogen Type':    'nitrogentype',
            'Soil NH4 (mg/kg)': 'soilnh4',
            'Soil NO3 (mg/kg)': 'soilno3',
            'Planted Crop':     'plantedcrop',
            'Management':       'management',

            # --- Level 2: Treatment ---
            'Treatment Name':   'treatmentid__TreatmentName',
            'Primary Crop':     'treatmentid__PrimaryCrop',
            'Secondary Crop':   'treatmentid__SecondaryCrop',
            'Management Style': 'treatmentid__Management',
            'Flux Instrument':  'treatmentid__FluxInstrument',
            'Silt Content (%)': 'treatmentid__SiltMean',
            'Clay Content (%)': 'treatmentid__ClayMean',
            'Sand Content (%)': 'treatmentid__SandMean',
            'pH':               'treatmentid__pHMean',
            'Bulk Density (g/cm3)': 'treatmentid__BulkDensityMean',
            'Soil Organic Carbon (%)': 'treatmentid__SOMMean',
            'Treatment Description': 'treatmentid__TreatmentDescription',

            # --- Level 3: Experiment ---
            'Experiment Title': 'treatmentid__ExperimentID__ExperimentName',

            # --- Level 4 Branch A: Site ---
            'Site Name':        'treatmentid__ExperimentID__SiteID__SiteName',
            'Site Latitude':    'treatmentid__ExperimentID__SiteID__Latitude',
            'Site Longitude':   'treatmentid__ExperimentID__SiteID__Longitude',
            'Mean Annual Precipitation (mm)': 'treatmentid__ExperimentID__SiteID__MAP',
            'Mean Annual Temperature (°C)':   'treatmentid__ExperimentID__SiteID__MAT',

            # --- Level 5 Branch B: Publication ---
            'DOI':               'treatmentid__ExperimentID__PubID__Link',
            'Lead Author':       'treatmentid__ExperimentID__PubID__LeadAuthor',
            'Citation':          'treatmentid__ExperimentID__PubID__Citation'
        }

        qs_check = RawMeasurementTreatment.objects.filter(
            treatmentid__in=ids_to_query
        )
        
        count = qs_check.count()
        print(f"[DEBUG] 2. Query found {count} matching rows in DB.")
        
        if count == 0:
            print("[DEBUG] CRITICAL: No rows found. Check your filter syntax!")
            print(f"        Filter used: treatmentid__in={ids_to_query}")


        # Query Setup
        db_columns = column_map.values()
        
        # Execute Query
        queryset = RawMeasurementTreatment.objects.filter(
            treatmentid__in=ids_to_query
        ).values_list(
            *db_columns
        ).order_by(
            'pk' 
        ).iterator(chunk_size=2000)

        
        # Add Header
        yield writer.writerow(column_map.keys()).encode('utf-8')

        # Add Data
        for row in queryset:
            yield writer.writerow(row).encode('utf-8')

    # --- 3. Create the Zip Stream ---
    z = zipstream.ZipStream(
        compress_type=zipfile.ZIP_DEFLATED, 
        compress_level=5
    )

    # Add README
    z.add(readme_content.encode('utf-8'), 'README.txt')

    # Add CSV (Dynamic Stream)
    z.add(csv_generator(parent_ids), 'n2odb_data_export.csv')

    # --- 4. Return Response ---
    response = StreamingHttpResponse(z, content_type="application/zip")
    response['Content-Disposition'] = 'attachment; filename="n2odb_export.zip"'
    
    return response

def contact_view(request):
    if request.method == 'POST':
        # 1. Extract Data
        name = request.POST.get('name')
        email = request.POST.get('email')
        subject_type = request.POST.get('subject')
        message_content = request.POST.get('message')

        # 2. Format the Email Subject & Body
        # We put [GRAND] in the subject so you can filter these easily in your inbox
        email_subject = f"[GRAND Explorer] {subject_type}: {name}"
        
        email_body = f"""
        You have received a new message via the GRAND Explorer Contact Form.

        --------------------------------------------------
        SENDER INFO
        --------------------------------------------------
        Name:    {name}
        Email:   {email}
        Subject: {subject_type}

        --------------------------------------------------
        MESSAGE
        --------------------------------------------------
        {message_content}
        """

        try:
            # 3. Send Email
            send_mail(
                subject=email_subject,
                message=email_body,
                from_email=settings.DEFAULT_FROM_EMAIL, # Must match your settings
                recipient_list=[settings.CONTACT_EMAIL], # Where it goes (Admin)
                fail_silently=False,
            )
            
            # 4. Success Feedback
            messages.success(request, "Thank you! Your message has been sent successfully.")
            return redirect('contact')
            
        except Exception as e:
            # 5. Error Feedback
            print(f"Error sending email: {e}") # Log to console for debugging
            messages.error(request, "There was a technical error sending your message. Please try again later.")
            return redirect('contact')

    # If GET request, just show the form
    return render(request, 'contact.html')

def team_view (request):
    return render(request, 'team.html')
