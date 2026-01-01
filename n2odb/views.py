from django.shortcuts import render
from django.db import models
from n2odb.models import Publication, Site, Experiment, Treatment, Replication, RawMeasurementTreatment
from django.template.defaulttags import register
from django.template import loader
from django.template import Template, Context
from json import dumps
from django.http import JsonResponse
import json


def index(request): 
    return render(request, 'index.html')


def about(request):
    return render(request, 'about.html', {
    })

def query_filters(request):
    return render(request, 'query-filters.html', {
    })

def get_data(request):
    if request.method == "POST" and request.headers.get('x-requested-with') == 'XMLHttpRequest':
        data_from_post = request.POST['selectedArray']
        selectedList = data_from_post.split(",")
        ins_options = list(
                Treatment.objects.values_list('FluxInstrument', flat=True).distinct())
        ins_selected = []
        env_options = ['Soil VWC', 'Soil WFPS', 'Soil Temp', 'Air Temp', 'Precipitation']
        env_selected = []
        mgmt_options = list(Treatment.objects.values_list('Management', flat=True).distinct())
        mgmt_selected = []
        for selection in selectedList:
            if selection in ins_options:
                ins_selected.append(selection)
            if selection in env_options:
                env_selected.append(selection)
            if selection in mgmt_options:
                mgmt_selected.append(selection)

        # Handle the selected sensors query- Replication table
        kwargs = {}
        for item in env_selected:
            if item == 'Soil VWC':
                kwargs['VWC_Meas'] = 1
            if item == 'Soil WFPS':
                kwargs['WFPS_Meas'] = 1
            if item == 'Soil Temp':
                kwargs['Soil_T_Meas'] = 1
            if item == 'Air Temp':
                kwargs['Air_T_Meas'] = 1
            if item == 'Precipitation':
                kwargs['Precip_Meas'] = 1
        table_data = Replication.objects.filter(**kwargs).select_related()

        # Handle the Primary Crop, Management, Tillage, and Instrument queries, Treatment table
        if len(ins_selected) > 0:
            table_data = table_data.filter(TreatmentID__FluxInstrument__in=ins_selected)
        if len(mgmt_selected) > 0:
            table_data = table_data.filter(TreatmentID__Management__in=mgmt_selected)

        update_data = {}
        rep_ids = list(table_data.values('RepID', 'Air_T_Meas', 'Precip_Meas', 'Soil_T_Meas', 'VWC_Meas', 'WFPS_Meas'))
        update_data['Rep_update'] = dumps(rep_ids)
        treatment_ids = list(Treatment.objects.filter(TreatmentID__in=table_data).values('TreatmentID', 'PrimaryCrop',
            'SecondaryCrop', 'CoverCrop', 'Management', 'FluxInstrument'))
        update_data['Treatment_update'] = dumps(treatment_ids)
        pub_ids = list(Publication.objects.filter(PubID__in=table_data).values('PubID', 'PubTitle', 'PubYear',
            'LeadAuthor', 'Citation'))
        update_data['Pub_update'] = dumps(pub_ids)
        site_ids = list(Site.objects.filter(SiteID__in=table_data).values('SiteID', 'Latitude', 'Longitude', 'MAP',
            'MAT'))
        update_data['Site_update'] = dumps(site_ids)
        return JsonResponse(update_data)

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
        labels_dict['env'] = 'Environmental Variables'
        labels_dict['ins'] = 'Flux Instrument'
        labels_dict['fert'] = 'Fertilizer Form'
        labels_dict['mgmt'] = 'Management'
        labels_dict['crop'] = 'Crop'
        labels_dict['cont'] = 'Continent'

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
        return render(request, 'get-data.html', dropdown_dict)

def submit_data(request):
    return render(request, 'submit_data.html', {
    })
# Create your views here.
