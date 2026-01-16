from django.shortcuts import render
from django.db import models
from n2odb.models import Publication, Site, Experiment, Treatment, Replication, RawMeasurementTreatment
from django.template.defaulttags import register
from django.template import loader
from django.template import Template, Context
from json import dumps
from django.http import JsonResponse
import json
from django.db.models import F
from collections import defaultdict
import datetime
from django.db.models import Count, Avg
from django.views.decorators.csrf import csrf_exempt


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
    # PART 1: AJAX REQUEST (Filtering Map Data)
    # -------------------------------------------------------------------------
    if request.method == "POST" and request.headers.get('x-requested-with') == 'XMLHttpRequest':
        try:
            data = json.loads(request.body)
        
            # =========================================================
            # A. GRAPH REQUEST INTERCEPTOR
            # Logic: If 'action' is 'fetch_graph', we ignore map filters
            # =========================================================
            if data.get('action') == 'fetch_graph':
                treatment_id = data.get('treatment_id')
                return get_graph_json(treatment_id)
            
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)

        # 1. Extract Filters
        selected_ins = data.get('instruments', [])
        selected_mgmt = data.get('management', [])
        selected_env = data.get('environment', [])
        selected_fert = data.get('fertilizer', [])
        selected_crops = data.get('crops', [])

        # 2. Base Query (Replication)
        # We assume Replication links to Treatment, but NOT directly to Site.
        rep_qs = Replication.objects.select_related('TreatmentID').all()

        # --- A. Environmental Filters (Replication) ---
        env_mapping = {
            'Soil VWC': 'VWC_Meas', 'Soil WFPS': 'WFPS_Meas',
            'Soil Temp': 'Soil_T_Meas', 'Air Temp':  'Air_T_Meas',
            'Precipitation': 'Precip_Meas'
        }
        for env_label in selected_env:
            if env_label in env_mapping:
                rep_qs = rep_qs.filter(**{env_mapping[env_label]: 1})

        # --- B. Treatment Filters ---
        if selected_ins:
            rep_qs = rep_qs.filter(TreatmentID__FluxInstrument__in=selected_ins)
        if selected_mgmt:
            rep_qs = rep_qs.filter(TreatmentID__Management__in=selected_mgmt)
        if selected_crops:
            rep_qs = rep_qs.filter(TreatmentID__PrimaryCrop__in=selected_crops)

        # --- C. Fertilizer Filter ---
        if selected_fert:
            rep_qs = rep_qs.filter(
                TreatmentID__measurements__nitrogentype__in=selected_fert
            )

        # 3. Retrieve Data 
        
        # -- Replications --
        filtered_reps = list(rep_qs.values(
            'RepID', 'Air_T_Meas', 'Precip_Meas', 'Soil_T_Meas', 'VWC_Meas', 'WFPS_Meas'
        ).distinct())

        # -- Treatments --
        treatment_qs = Treatment.objects.filter(TreatmentID__in=rep_qs.values('TreatmentID'))
        filtered_treatments = list(treatment_qs.values(
            'ExperimentID', 'TreatmentID', 'TreatmentName', 'PrimaryCrop', 
            'SecondaryCrop', 'CoverCrop', 'Management', 'FluxInstrument'
        ).distinct())

        # -- Sites --
        # Path: Replication -> Treatment -> Experiment -> Site
        site_qs = Site.objects.filter(
            SiteID__in=rep_qs.values('TreatmentID__ExperimentID__SiteID')
        )
        filtered_sites = list(site_qs.values(
            'SiteID', 'Latitude', 'Longitude', 'MAP', 'MAT'
        ).distinct())

        # -- Publications --
        # Path: Replication -> Treatment -> Experiment -> Publication
        pub_qs = Publication.objects.filter(
            PubID__in=rep_qs.values('TreatmentID__ExperimentID__PubID')
        )
        filtered_pubs = list(pub_qs.values(
            'PubID', 'PubTitle', 'PubYear', 'LeadAuthor', 'Citation'
        ).distinct())

        # 4. Construct the "Experiment Package"
        exp_qs = Experiment.objects.filter(
            ExperimentID__in=treatment_qs.values('ExperimentID')
        )
        
        experiment_data = list(exp_qs.annotate(
            Lat=F('SiteID__Latitude'),
            Lon=F('SiteID__Longitude'),
            SiteName=F('SiteID__Name'), 
            MAP=F('SiteID__MAP'),
            MAT=F('SiteID__MAT'),
            PubTitle=F('PubID__PubTitle'),
            Citation=F('PubID__Citation')
        ).values(
            'ExperimentID', 'ExperimentName', 
            'Lat', 'Lon', 'SiteName', 'MAP', 'MAT', 'PubTitle', 'Citation'
        ))

        # Group Treatments by ExperimentID
        treatment_map = defaultdict(list)
        for t in filtered_treatments:
            row = {
                'id': t['TreatmentID'],
                'name': t['TreatmentName'] if t['TreatmentName'] else "Unnamed Treatment",
                'crop': t['PrimaryCrop'],
                'mgmt': t['Management'],
                'ins': t.get('FluxInstrument')
            }
            treatment_map[t['ExperimentID']].append(row)

        for exp in experiment_data:
            exp['TreatmentIDs'] = treatment_map.get(exp['ExperimentID'], [])

        # 5. Return JSON (Using consistent keys)
        update_data = {
            'json_rep': json.dumps(filtered_reps),
            'json_treatment': json.dumps(filtered_treatments),
            'json_site': json.dumps(filtered_sites),
            'json_pub': json.dumps(filtered_pubs),
            'json_experiments': json.dumps(experiment_data)
        }
        print("No errors")
        print(update_data['json_experiments'])
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
            dropdown_dict['Environmental Variables'] = ['Soil VWC', 'Soil Temp', 'Air Temp', 'Precipitation']
            dropdown_dict['env'] = ['Soil VWC', 'Soil Temp', 'Air Temp', 'Precipitation']
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
            print(list(
                RawMeasurementTreatment.objects
                .exclude(nitrogentype__isnull=True)   # Drops NULL values
                .exclude(nitrogentype__exact='')      # Drops empty strings ""
                .values_list('nitrogentype', flat=True)
                .distinct()
            ))
            labels.append('Fertilizer Form')
        if 'crop' in selectedList:
            dropdown_dict['crop'] = list(Treatment.objects.values_list('PrimaryCrop', flat=True).distinct())
            labels.append('Crop')
        if 'cont' in selectedList:
            dropdown_dict['Continent'] = list(Site.objects.values_list('Continent', flat=True).distinct())
            labels.append('Continent')

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
        dropdown_dict['json_pub'] = dumps(list(pubdata.values('PubID', 'PubTitle', 'PubYear', 'LeadAuthor', 'Citation')))
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
            Citation=F('PubID__Citation')
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

        # 3. Group Treatments by ExperimentID using a Python dictionary
        # This is much faster than running a sub-query for every single experiment
        treatment_map = defaultdict(list)
        for t in treatments:
            row = {
                'id': t['TreatmentID'],
                'name': t['TreatmentName'] if t['TreatmentName'] else "Unnamed Treatment",
                'crop': t['PrimaryCrop'],
                'mgmt': t['Management']
            }
            treatment_map[t['ExperimentID']].append(row)

        # 4. Attach the Treatment list to each Experiment object
        for exp in experiment_data:
            # Look up the list of treatments using the ID, default to empty list if none
            exp['TreatmentIDs'] = treatment_map.get(exp['ExperimentID'], [])

        # 5. Add to your dropdown_dict for the frontend
        dropdown_dict['json_experiments'] = dumps(experiment_data)
        print(dropdown_dict['json_experiments'])
        
        
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
                'hmmad_stats': {'Auto': hmmad_true, 'Manual': hmmad_false},
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
    """API Endpoint: Receives IDs via POST, returns Analytics JSON."""
    if request.method == 'POST':
        try:
            # 1. Parse IDs from Request Body
            body = json.loads(request.body)
            raw_ids = body.get('ids', [])
            # Validate IDs are integers
            treatment_ids = [int(x) for x in raw_ids if str(x).isdigit()]

            # 2. Fetch Objects
            # Use select_related to grab parent Experiment data efficiently
            treatments = Treatment.objects.filter(pk__in=treatment_ids).select_related('ExperimentID')
            
            # 3. Filter Measurements
            measurements = RawMeasurementTreatment.objects.filter(treatmentid__in=treatments)
            total_flux_rows = measurements.count()

            # --- BUILD WIDGET DATA ---

            # A. Sidebar List
            sidebar_list = []
            for t in treatments:
                sidebar_list.append({
                    'id': t.pk,
                    'name': getattr(t, 'Name', f"Treatment {t.pk}"), 
                    'crop': getattr(t, 'PrimaryCrop', "Unknown"),
                    'location': t.ExperimentID.SiteName, 
                    'year': t.ExperimentID.Year,
                })

            # B. Map Data (Grouped by Experiment Site)
            map_data = []
            experiments = set(t.ExperimentID for t in treatments)
            for exp in experiments:
                # Count rows just for this experiment's selected treatments
                count = measurements.filter(treatment__experiment=exp).count()
                if count > 0:
                    map_data.append({
                        'lat': float(exp.Lat),
                        'lon': float(exp.Lon),
                        'name': exp.SiteName,
                        'z': count # Bubble size
                    })

            # C. HMMAD (Methodology)
            # Assuming 1 = Auto, 0 = Manual. Adjust field name if needed.
            hmmad_auto = measurements.filter(HMMAD=1).count()
            hmmad_manual = total_flux_rows - hmmad_auto
            
            # D. Crop Distribution
            crop_counts = treatments.values('PrimaryCrop').annotate(count=Count('id')).order_by('-count')
            crop_data = list(crop_counts) 

            # E. Soil Texture (Average)
            # Assumes Experiment has Sand/Silt/Clay fields.
            soil_stats = treatments.aggregate(
                Sand=Avg('experiment__Sand'),
                Silt=Avg('experiment__Silt'),
                Clay=Avg('experiment__Clay')
            )

            # F. Histogram Data (Raw Flux Values)
            # Limit to 10,000 points to prevent browser crash if data is huge
            flux_values = list(measurements.filter(N2OFlux__isnull=False)
                               .values_list('N2OFlux', flat=True)[:10000])

            # 4. Return JSON
            data = {
                'sidebar_list': sidebar_list,
                'total_rows': total_flux_rows,
                'map_data': map_data,
                'hmmad_stats': {'Auto': hmmad_auto, 'Manual': hmmad_manual},
                'crop_data': crop_data,
                'soil_stats': soil_stats,
                'flux_values': flux_values
            }
            return JsonResponse(data)

        except Exception as e:
            # --- ADD THIS PRINT STATEMENT ---
            print("!!!!!!! API ERROR !!!!!!!")
            print(e)
            print("!!!!!!!!!!!!!!!!!!!!!!!!!")
            # -------------------------------
            return JsonResponse({'error': str(e)}, status=400)

    return JsonResponse({'error': 'POST method required'}, status=405)