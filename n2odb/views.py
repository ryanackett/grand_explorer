from django.shortcuts import render, redirect
from django.db import models
from n2odb.models import Publication, Site, Experiment, Treatment, Replication, RawMeasurementTreatment, UserAction
from django.template.defaulttags import register
from django.template import loader
from django.template import Template, Context
from json import dumps
from django.http import JsonResponse, HttpResponse
import json
from django.db.models import F
from collections import defaultdict
import datetime
from django.db.models import Count, Avg, Min, Max, Sum, Q
from django.views.decorators.csrf import csrf_exempt
from .utils import Echo, _log_user_action
from django.views.decorators.http import require_POST  
import csv
import os
import zipstream, zipfile
from django.http import StreamingHttpResponse
from django.conf import settings
from django.core.mail import EmailMessage
from django.contrib import messages
import logging

logger = logging.getLogger(__name__)


def account_settings(request):
    """My Account: a read-only view of the contributor's details (name, email), with the
    changes themselves routed to dedicated pages from the Security panel — name here, and
    allauth's hardened flows for email (new email reverified) and password (current
    password required). We don't reimplement the latter two."""
    # Verification state of the login (primary) email, for an at-a-glance badge.
    primary_verified = False
    try:
        from allauth.account.models import EmailAddress
        primary_verified = EmailAddress.objects.filter(
            user=request.user, email__iexact=request.user.email, verified=True
        ).exists()
    except Exception:  # noqa: BLE001 — a badge must never break the page
        primary_verified = False

    return render(request, 'account_settings.html', {
        'primary_verified': primary_verified,
    })


def change_name(request):
    """Edit first/last name — its own page so it looks and behaves like allauth's
    change-email / change-password flows (linked from the Security panel)."""
    from .forms import NameForm

    if request.method == 'POST':
        form = NameForm(request.POST, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, 'Your name has been updated.')
            return redirect('account-settings')
    else:
        form = NameForm(instance=request.user)

    return render(request, 'account_name.html', {'form': form})


def why_contribute(request):
    """Public 'why contribute your data' page — the pitch, linked from the Contribute menu."""
    return render(request, 'why_contribute.html', {})


def leaderboard(request):
    """Public recognition board. Each contributor's promoted data is reached via the
    persistent Publication.Contributor handle (see the promote flow), so it follows a
    user across email changes. Three metrics per contributor:
      * papers      — distinct Publications
      * fluxes      — RawMeasurementTreatment rows with a non-null N2O flux
      * continents  — distinct Site.Continent values (filled by `backfill_geography`)
    Default rank is by papers; the template lets the visitor re-sort by any metric.
    """
    from django.db.models import Count, Q
    from .models import User

    # ponytail: the flux + continent joins fan out per publication, but each metric is a
    # COUNT(DISTINCT ...) so the multiplication doesn't inflate it. Fine at contributor
    # scale; revisit with subquery annotations if the board ever gets huge.
    agg = list(Publication.objects
               .filter(Contributor__isnull=False)
               .values('Contributor')
               .annotate(
                   papers=Count('PubID', distinct=True),
                   fluxes=Count('experiment__treatment__measurements',
                                filter=Q(experiment__treatment__measurements__n2oflux__isnull=False),
                                distinct=True),
                   continents=Count('experiment__SiteID__Continent', distinct=True),
               )
               .order_by('-papers', '-fluxes', 'Contributor'))
    users = {u.public_id: u for u in
             User.objects.filter(public_id__in=[a['Contributor'] for a in agg])}
    entries = []
    for a in agg:
        u = users.get(a['Contributor'])
        # A user removed from the system leaves their promoted data (and its credit) intact.
        name = (u.get_full_name().strip() or 'Contributor') if u else 'Former contributor'
        entries.append({'name': name, 'papers': a['papers'],
                        'fluxes': a['fluxes'], 'continents': a['continents']})

    totals = {
        'contributors': len(entries),
        'papers': sum(e['papers'] for e in entries),
        'fluxes': sum(e['fluxes'] for e in entries),
        'continents': (Publication.objects
                       .filter(Contributor__isnull=False)
                       .exclude(experiment__SiteID__Continent__isnull=True)
                       .values('experiment__SiteID__Continent').distinct().count()),
    }
    return render(request, 'leaderboard.html', {'entries': entries, 'totals': totals})


def index(request):
    return render(request, 'index.html')


def about(request):
    return render(request, 'about.html', {
    })

def about_data(request):
    return render(request, 'about-data.html', {})

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

            # Lazy-load a publication abstract on demand (kept out of the
            # initial map payload to keep first paint light).
            if data.get('action') == 'fetch_abstract':
                return get_abstract_json(data.get('experiment_id'))
            
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)

        # 1. EXTRACT FILTERS
        selected_ins = data.get('instruments', [])
        selected_mgmt = data.get('management', [])
        selected_env = data.get('environment', [])
        selected_fert = data.get('fertilizer', [])
        selected_crops = data.get('crops', [])
        selected_soil = data.get('soil', [])
        selected_cont = data.get('continent', [])
        selected_country = data.get('country', [])

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

        # For moisture metrics a treatment counts as "having" the variable if
        # either the directly measured column OR the derived *Calculated column
        # (filled by the soil_moisture_calculation command from the alternate
        # metric + bulk density) is populated. Measured and calculated are
        # treated as equivalent for availability filtering.
        ENV_CALCULATED_MAP = {
            'vwc':  'vwccalculated',
            'wfps': 'wfpscalculated',
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

        # A. Apply Environmental Filters
        if selected_env:
            for env_label in selected_env:
                if env_label in ENV_COLUMN_MAP:
                    column_name = ENV_COLUMN_MAP[env_label]

                    # Measured value present...
                    availability = Q(**{f"measurements__{column_name}__isnull": False})
                    # ...or, for VWC/WFPS, the calculated equivalent present.
                    if column_name in ENV_CALCULATED_MAP:
                        calc_column = ENV_CALCULATED_MAP[column_name]
                        availability |= Q(**{f"measurements__{calc_column}__isnull": False})

                    # 1. Find IDs that have THIS variable
                    ids_with_var = set(
                        Treatment.objects.filter(availability)
                        .values_list('pk', flat=True)
                    )
                    
                    # 2. Perform Intersection
                    valid_treatment_ids = valid_treatment_ids.intersection(ids_with_var)
                else:
                    logger.warning("Unmapped environmental filter: %s", env_label)

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
        if selected_cont:
            treatment_qs = treatment_qs.filter(ExperimentID__SiteID__Continent__in=selected_cont)
        if selected_country:
            treatment_qs = treatment_qs.filter(ExperimentID__SiteID__Country__in=selected_country)
        if selected_fert:
            # Note: This join can cause duplicates, so we distinct() later
            treatment_qs = treatment_qs.filter(measurements__nitrogentype__in=selected_fert)

        # C. Execute Data Fetch
        filtered_treatments = list(treatment_qs.distinct().values(
            'ExperimentID', 'TreatmentID', 'TreatmentName', 'PrimaryCrop', 
            'SecondaryCrop', 'CoverCrop', 'Management', 'FluxInstrument'
        ))

        # ---------------------------------------------------------------------
        # 4. FETCH PARENTS (Valid JSON Packaging)
        # ---------------------------------------------------------------------
        valid_exp_ids = list(set([t['ExperimentID'] for t in filtered_treatments]))
        
        # Experiments (Abstract is fetched lazily on click, not embedded here)
        exp_qs = Experiment.objects.filter(ExperimentID__in=valid_exp_ids)
        experiment_data = list(exp_qs.annotate(
            Lat=F('SiteID__Latitude'),
            Lon=F('SiteID__Longitude'),
            SiteName=F('SiteID__SiteName'),
            MAP=F('SiteID__MAP'),
            MAT=F('SiteID__MAT'),
            PubTitle=F('PubID__PubTitle'),
            Citation=F('PubID__Citation')
        ).values(
            'ExperimentID', 'ExperimentName',
            'Lat', 'Lon', 'SiteName', 'MAP', 'MAT', 'PubTitle', 'Citation'
        ))

        # --- NEW: FETCH MEASUREMENT DATES IN ONE BULK QUERY ---
        valid_treatment_ids = [t['TreatmentID'] for t in filtered_treatments]

        date_aggregates = RawMeasurementTreatment.objects.filter(
            treatmentid__in=valid_treatment_ids
        ).values('treatmentid').annotate(
            first_date=Min('date'),
            last_date=Max('date')
        )

        # Create a fast lookup dictionary: {101: {'first': '2020-01-01', 'last': '2022-12-31'}}
        date_lookup = {
            item['treatmentid']: {
                # Converting to string so it serializes cleanly into JSON later
                'first_date': item['first_date'].strftime('%Y-%m-%d') if item['first_date'] else None,
                'last_date': item['last_date'].strftime('%Y-%m-%d') if item['last_date'] else None,
            } 
            for item in date_aggregates
        }

        # Attach treatments to experiments. Drop any treatment with no
        # RawMeasurementTreatment rows: with no data its graph is empty and it is
        # useless on the map. A treatment has data iff it appears in date_lookup
        # (built from the measurement aggregates above).
        treatment_map = defaultdict(list)
        for t in filtered_treatments:
            if t['TreatmentID'] not in date_lookup:
                continue
            t_dates = date_lookup[t['TreatmentID']]

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

        # Drop experiments left with no data-bearing treatments (empty map points).
        experiment_data = [
            exp for exp in experiment_data
            if treatment_map.get(exp['ExperimentID'])
        ]
        for exp in experiment_data:
            exp['TreatmentIDs'] = treatment_map[exp['ExperimentID']]

        # 5. RETURN VALID JSON
        # Only json_experiments is consumed by the front-end (submitFilter);
        # the map is redrawn from it. Site/pub/treatment blobs were dead weight.
        return JsonResponse({'json_experiments': json.dumps(experiment_data)})

    # -------------------------------------------------------------------------
    # PART 2: INITIAL LOAD (GET/POST Request for Dropdowns)
    # -------------------------------------------------------------------------
    elif request.method == 'POST':
        selectedList = request.POST.get('selected', '')
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
            continents = list(
                Site.objects.exclude(Continent__isnull=True)
                .values_list('Continent', flat=True).distinct().order_by('Continent')
            )
            dropdown_dict['Continent'] = continents
            dropdown_dict['cont'] = continents
            labels.append('Continent')
        if 'country' in selectedList:
            countries = list(
                Site.objects.exclude(Country__isnull=True)
                .values_list('Country', flat=True).distinct().order_by('Country')
            )
            dropdown_dict['Country'] = countries
            dropdown_dict['country'] = countries
            labels.append('Country')
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
        labels_dict['country'] = 'Country (any of these)'
        labels_dict['soil'] = 'Soil Measurements (all of these)'
        labels_dict['texture'] = 'Soil Texture (any of these)'

        dropdown_dict['labels_dict'] = labels_dict

        # 1. Query Experiments with related Site and Publication data.
        #    Abstract is intentionally NOT fetched here — it's the largest field
        #    and is only needed when a user clicks a point, so it's lazy-loaded
        #    via get_abstract_json (keeps the initial page payload small).
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

                # --- NEW: FETCH MEASUREMENT DATES IN ONE BULK QUERY ---
        valid_treatment_ids = [t['TreatmentID'] for t in treatments]

        date_aggregates = RawMeasurementTreatment.objects.filter(
            treatmentid__in=valid_treatment_ids
        ).values('treatmentid').annotate(
            first_date=Min('date'),
            last_date=Max('date')
        )

        # Create a fast lookup dictionary: {101: {'first': '2020-01-01', 'last': '2022-12-31'}}
        date_lookup = {
            item['treatmentid']: {
                # Converting to string so it serializes cleanly into JSON later
                'first_date': item['first_date'].strftime('%Y-%m-%d') if item['first_date'] else None,
                'last_date': item['last_date'].strftime('%Y-%m-%d') if item['last_date'] else None,
            } 
            for item in date_aggregates
        }

        # 3. Group Treatments by ExperimentID using a Python dictionary.
        #    Drop treatments with no RawMeasurementTreatment rows: no data means
        #    an empty graph, so they are useless on the map. Membership in
        #    date_lookup means the treatment has at least one measurement.
        treatment_map = defaultdict(list)
        for t in treatments:
            if t['TreatmentID'] not in date_lookup:
                continue
            t_dates = date_lookup[t['TreatmentID']]
            row = {
                'id': t['TreatmentID'],
                'name': t['TreatmentName'] if t['TreatmentName'] else "Unnamed Treatment",
                'crop': t['PrimaryCrop'],
                'mgmt': t['Management'],
                'first_date': t_dates.get('first_date'),
                'last_date': t_dates.get('last_date'),
            }
            treatment_map[t['ExperimentID']].append(row)

        # 4. Attach the Treatment list to each Experiment object, and drop
        #    experiments left with no data-bearing treatments (empty map points).
        experiment_data = [
            exp for exp in experiment_data
            if treatment_map.get(exp['ExperimentID'])
        ]
        for exp in experiment_data:
            exp['TreatmentIDs'] = treatment_map[exp['ExperimentID']]

        # 5. Add to your dropdown_dict for the frontend
        dropdown_dict['json_experiments'] = dumps(experiment_data)        
        
        return render(request, 'get-data.html', dropdown_dict)

    # A plain GET (bookmark, shared link, refresh) has no filter selection to build
    # the page from — send it to the filter picker instead of erroring.
    return redirect('query-filters')


def get_graph_json(treatment_id):
    try:
        # 1. Verify Treatment
        treatment = Treatment.objects.filter(pk=treatment_id).first()
        if not treatment:
            return JsonResponse({'error': f'Treatment ID {treatment_id} not found.'})

        # 2. Define All Possible Columns
        fields_map = {
            'n2oflux': 'N₂O Flux',
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
                'name': 'N₂O Flux',
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
        logger.exception("Error building graph JSON for treatment %s", treatment_id)
        return JsonResponse({'error': 'Unable to load graph data.'}, status=500)

def get_abstract_json(experiment_id):
    """Return the publication abstract for a single experiment, on demand.

    Abstracts are the heaviest text field, so instead of embedding every one
    in the initial map payload we fetch just the clicked point's abstract here.
    """
    try:
        exp = Experiment.objects.filter(pk=experiment_id).values(
            abstract=F('PubID__Abstract')
        ).first()
        if not exp:
            return JsonResponse({'abstract': None})
        return JsonResponse({'abstract': exp['abstract']})
    except Exception:
        logger.exception("Error fetching abstract for experiment %s", experiment_id)
        return JsonResponse({'abstract': None})


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
            max_ids = getattr(settings, 'PUBLIC_CART_MAX_IDS', 500)
            if len(treatment_ids) > max_ids:
                return JsonResponse(
                    {'error': f'Please select no more than {max_ids} treatments.'},
                    status=400,
                )

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
            # Number of rows where N2OFlux is not null
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

            # B. Map Data (bubble size = measurement count per site).
            #    One grouped query replaces the old per-site COUNT loop (N+1):
            #    group all cart measurements by their site and count in the DB.
            site_counts = (
                measurements
                .values('treatmentid__ExperimentID__SiteID')
                .annotate(
                    z=Count('pk'),
                    lat=Min('treatmentid__ExperimentID__SiteID__Latitude'),
                    lon=Min('treatmentid__ExperimentID__SiteID__Longitude'),
                    name=Min('treatmentid__ExperimentID__SiteID__SiteName'),
                )
            )
            map_data = [
                {'lat': r['lat'], 'lon': r['lon'], 'name': r['name'], 'z': r['z']}
                for r in site_counts if r['z'] > 0
            ]

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

            # F. Histogram Data — bin counts computed in the DB over ALL non-null
            # measurements (not a truncated 10k sample), so the bars sum exactly to
            # total_flux_measurements. Bins match the front-end labels/edges.
            flux_bins = measurements.filter(n2oflux__isnull=False).aggregate(
                le0=Count('pk', filter=Q(n2oflux__lte=0)),
                b1=Count('pk', filter=Q(n2oflux__gt=0, n2oflux__lte=2.5)),
                b2=Count('pk', filter=Q(n2oflux__gt=2.5, n2oflux__lte=5)),
                b3=Count('pk', filter=Q(n2oflux__gt=5, n2oflux__lte=10)),
                b4=Count('pk', filter=Q(n2oflux__gt=10, n2oflux__lte=20)),
                b5=Count('pk', filter=Q(n2oflux__gt=20, n2oflux__lte=50)),
                b6=Count('pk', filter=Q(n2oflux__gt=50, n2oflux__lte=100)),
                b7=Count('pk', filter=Q(n2oflux__gt=100, n2oflux__lte=500)),
                b8=Count('pk', filter=Q(n2oflux__gt=500, n2oflux__lte=1000)),
                b9=Count('pk', filter=Q(n2oflux__gt=1000)),
            )
            flux_histogram = [
                {'name': '≤ 0',  'y': flux_bins['le0'], 'neg': True},
                {'name': '0-2.5',    'y': flux_bins['b1']},
                {'name': '2.5-5',    'y': flux_bins['b2']},
                {'name': '5-10',     'y': flux_bins['b3']},
                {'name': '10-20',    'y': flux_bins['b4']},
                {'name': '20-50',    'y': flux_bins['b5']},
                {'name': '50-100',   'y': flux_bins['b6']},
                {'name': '100-500',  'y': flux_bins['b7']},
                {'name': '500-1000', 'y': flux_bins['b8']},
                {'name': '1000+',    'y': flux_bins['b9']},
            ]

            # 4. Return JSON
            data = {
                'sidebar_list': sidebar_list,
                'total_rows': total_flux_measurements,
                'map_data': map_data,
                'hmmad_stats': {'HM': hmmad_true, 'BG': hmmad_false, 'HM_flux': hmmad_true_flux, 'BG_flux': hmmad_false_flux},
                'crop_data': crop_data,
                'soil_stats': soil_stats,
                'flux_histogram': flux_histogram
            }
            return JsonResponse(data)

        except Exception:
            logger.exception("get_cart_data failed")
            return JsonResponse({'error': 'Unable to prepare cart data.'}, status=500)

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
    max_ids = getattr(settings, 'PUBLIC_CART_MAX_IDS', 500)
    if len(parent_ids) > max_ids:
        return StreamingHttpResponse(
            f"Please select no more than {max_ids} treatments.", status=400
        )
    email = request.POST.get('email_address')
    result_count = len(parent_ids)
    
    if not parent_ids:
        return StreamingHttpResponse("No data selected", status=400)
    
    # Log selections
    if parent_ids:
        _log_user_action(
        request,
        'DOWNLOAD_CART',
        ids=parent_ids,
        count=result_count,
        email=email,
        payload={}
    )
    
    # --- 1. Load the README + modeled-values manifest from disk ---
    files_dir = os.path.join(settings.BASE_DIR, 'n2odb', 'static', 'files')
    readme_path = os.path.join(files_dir, 'README.txt')
    manifest_path = os.path.join(files_dir, 'MODELED_VALUES.md')

    try:
        with open(readme_path, 'r', encoding='utf-8') as f:
            readme_content = f.read()
    except FileNotFoundError:
        # Fallback if file is missing so export doesn't crash
        readme_content = "Documentation file not found. Contact admin."

    # Provenance manifest: which sites/treatments carry SoilGrids/NASA-POWER
    # modeled covariates (see the "Measured vs modeled data" section of the README).
    try:
        with open(manifest_path, 'r', encoding='utf-8') as f:
            manifest_content = f.read()
    except FileNotFoundError:
        manifest_content = "Modeled-values manifest not available."

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
            'VWC (m3/m3)':      'vwc',
            'WFPS (fraction)':  'wfps',
            # Derived by the soil_moisture_calculation command; README explains the equation.
            'VWC Calculated (from WFPS + BD)':  'vwccalculated',
            'WFPS Calculated (from VWC + BD)':  'wfpscalculated',
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
            'Country':          'treatmentid__ExperimentID__SiteID__Country',
            'Continent':        'treatmentid__ExperimentID__SiteID__Continent',
            'Mean Annual Precipitation (mm)': 'treatmentid__ExperimentID__SiteID__MAP',
            'Mean Annual Temperature (°C)':   'treatmentid__ExperimentID__SiteID__MAT',

            # --- Level 5 Branch B: Publication ---
            'DOI':               'treatmentid__ExperimentID__PubID__Link',
            'Lead Author':       'treatmentid__ExperimentID__PubID__LeadAuthor',
            'Citation':          'treatmentid__ExperimentID__PubID__Citation'
        }

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

    # Add modeled-values provenance manifest
    z.add(manifest_content.encode('utf-8'), 'MODELED_VALUES.md')

    # Add CSV (Dynamic Stream)
    z.add(csv_generator(parent_ids), 'n2odb_data_export.csv')

    # --- 4. Return Response ---
    response = StreamingHttpResponse(z, content_type="application/zip")
    response['Content-Disposition'] = 'attachment; filename="n2odb_export.zip"'
    
    return response

def staff_login_redirect(request, extra_context=None):
    """Login for the admin and SQL Explorer, via allauth's rate-limited login.

    Django's admin login form (which SQL Explorer also renders) has no throttling,
    so it would be a brute-force target. Staff never reach this view (they're
    already let in); a signed-in user without access gets a 403 rather than a
    password form to guess against.
    """
    from django.contrib.auth.views import redirect_to_login
    from django.core.exceptions import PermissionDenied
    from django.urls import reverse

    if request.user.is_authenticated:
        raise PermissionDenied
    if request.path == reverse('admin:login'):
        next_url = request.GET.get('next') or reverse('admin:index')
    else:
        next_url = request.get_full_path()   # SQL Explorer: come back to the page asked for
    return redirect_to_login(next_url, login_url=reverse('account_login'))


def robots_txt(request):
    """Let crawlers index public pages; keep them out of admin/data endpoints."""
    lines = [
        "User-agent: *",
        "Disallow: /admin/",
        "Disallow: /explorer/",
        "Disallow: /api/",
        "Disallow: /cart/",
        "Disallow: /data-cart/",
        "Allow: /",
    ]
    return HttpResponse("\n".join(lines) + "\n", content_type="text/plain")


def contact_view(request):
    if request.method == 'POST':
        # Honeypot: the hidden 'company' field is invisible to humans but bots
        # fill every field. If it's populated, silently drop and fake success so
        # the bot can't tell it was filtered.
        if request.POST.get('company'):
            logger.info("Contact honeypot triggered; dropping submission.")
            messages.success(request, "Thank you! Your message has been sent successfully.")
            return redirect('contact')

        # 1. Extract Data
        name = request.POST.get('name')
        email = request.POST.get('email')
        subject_type = request.POST.get('subject')
        message_content = request.POST.get('message')

        # 2. Format the Email Subject & Body
        # We put [GRAND] in the subject so you can filter these easily in your inbox.
        # Body is built line-by-line (no leading indentation, and robust to a
        # multi-line message) so it renders clean in an inbox.
        email_subject = f"[GRAND Explorer] {subject_type}: {name}"

        email_body = (
            "You have received a new message via the GRAND Explorer Contact Form.\n\n"
            "--------------------------------------------------\n"
            "SENDER INFO\n"
            "--------------------------------------------------\n"
            f"Name:    {name}\n"
            f"Email:   {email}\n"
            f"Subject: {subject_type}\n\n"
            "--------------------------------------------------\n"
            "MESSAGE\n"
            "--------------------------------------------------\n"
            f"{message_content}\n"
        )

        try:
            # 3. Send Email. EmailMessage (vs send_mail) lets us set CC and a
            # Reply-To of the submitter, so hitting "Reply" reaches them directly
            # instead of the no-reply From address.
            EmailMessage(
                subject=email_subject,
                body=email_body,
                from_email=settings.DEFAULT_FROM_EMAIL,   # verified sender at your provider
                to=[settings.CONTACT_EMAIL],              # primary recipient
                cc=settings.CONTACT_CC,                   # optional CC list (CONTACT_CC env)
                reply_to=[email] if email else [],        # submitter's address
            ).send(fail_silently=False)

            # 4. Success Feedback
            messages.success(request, "Thank you! Your message has been sent successfully.")
            return redirect('contact')
            
        except Exception as e:
            # 5. Error Feedback
            logger.error("Error sending contact email: %s", e)
            messages.error(request, "There was a technical error sending your message. Please try again later.")
            return redirect('contact')

    # If GET request, just show the form
    return render(request, 'contact.html')

def team_view (request):
    return render(request, 'team.html')
