from django.shortcuts import render
from django.db import models
from n2odb.models import Publication, Site, Experiment, Treatment, Replication, LinearMeasurement
from django.template.defaulttags import register
from django.template import loader
from django.template import Template, Context
from json import dumps
from django.http import JsonResponse
import json
import pandas as pd

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
        print("AJAX code")
        selectedList = data_from_post.split(",")
        print(selectedList)
        ins_options = list(
                Treatment.objects.values_list('FluxInstrument', flat=True).distinct())
        ins_selected = []
        env_options = ['Soil VWC', 'Soil WFPS', 'Soil Temp', 'Air Temp', 'Precipitation']
        env_selected = []
        till_selected = []
        till_options = list(Treatment.objects.values_list('Tillage', flat=True).distinct())
        mgmt_options = list(Treatment.objects.values_list('Management', flat=True).distinct())
        mgmt_selected = []
        for selection in selectedList:
            if selection in ins_options:
                ins_selected.append(selection)
            if selection in env_options:
                env_selected.append(selection)
            if selection in mgmt_options:
                mgmt_selected.append(selection)
            if selection in till_options:
                till_selected.append(selection)

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
        if len(till_selected) > 0:
            table_data = table_data.filter(TreatmentID__Tillage__in=till_selected)

        update_data = {}
        rep_ids = list(table_data.values('RepID', 'Air_T_Meas', 'Precip_Meas', 'Soil_T_Meas', 'VWC_Meas', 'WFPS_Meas'))
        update_data['Rep_update'] = dumps(rep_ids)
        treatment_ids = list(Treatment.objects.filter(TreatmentID__in=table_data).values('TreatmentID', 'PrimaryCrop',
            'SecondaryCrop', 'CoverCrop', 'Management', 'Tillage', 'FluxInstrument'))
        update_data['Treatment_update'] = dumps(treatment_ids)
        pub_ids = list(Publication.objects.filter(PubID__in=table_data).values('PubID', 'PubTitle', 'PubYear',
            'LeadAuthor', 'Citation'))
        update_data['Pub_update'] = dumps(pub_ids)
        site_ids = list(Site.objects.filter(SiteID__in=table_data).values('SiteID', 'Latitude', 'Longitude', 'MAP',
            'MAT'))
        update_data['Site_update'] = dumps(site_ids)
        print(update_data)
        return JsonResponse(update_data)

    elif request.method == 'POST':
        selectedList = request.POST['selected']
        print(selectedList)
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
        if 'till' in selectedList:
            print("here")
            dropdown_dict['Tillage'] = list(Treatment.objects.values_list('Tillage', flat=True).distinct())
            dropdown_dict['till'] = list(Treatment.objects.values_list('Tillage', flat=True).distinct())
            labels.append('Tillage')
        if 'map' in selectedList:
            dropdown_dict['Mean Annual Precipitation'] = list(Site.objects.values_list('MAP', flat=True).distinct())
            labels.append('Mean Annual Precipitation')
        if 'mat' in selectedList:
            dropdown_dict['Mean Annual Temperature'] = list(Site.objects.values_list('MAT', flat=True).distinct())
            labels.append('Mean Annual Temperature')
        if 'soil' in selectedList:
            dropdown_dict['Soil Texture'] = ['Sand', 'Loamy Sand', 'Sandy Loam', 'Sandy Clay Loam', 'Loam', 'Silt Loam',
                                     'Silt', 'Silty Clay Loam', 'Clay Loam', 'Silty Clay', 'Sandy Clay', 'Clay']
            labels.append('Soil Texture')
        if 'mgmt' in selectedList:
            dropdown_dict['Management'] = list(Treatment.objects.values_list('Management', flat=True).distinct())
            dropdown_dict['mgmt'] = list(Treatment.objects.values_list('Management', flat=True).distinct())
            labels.append('Management')
        if 'fert' in selectedList:
            dropdown_dict['Fertilizer Form'] = list(LinearMeasurement.objects.values_list('NitrogenForm', flat=True).distinct())
            labels.append('Fertilizer Form')
        if 'cc' in selectedList:
            dropdown_dict['Cover Crop'] = list(Treatment.objects.values_list('CoverCrop', flat=True).distinct())
            labels.append('Cover Crop')

        dropdown_dict['keys'] = selectedList.split(",")
        dropdown_dict['labels'] = labels
        dropdown_dict['dropdown_dict'] = dropdown_dict

        labels_dict = {}
        labels_dict['env'] = 'Environmental Variables'
        labels_dict['ins'] = 'Flux Instrument'
        labels_dict['till'] = 'Tillage'
        labels_dict['fert'] = 'Fertilizer Form'
        labels_dict['map'] = 'Mean Annual Precipitation'
        labels_dict['mat'] = 'Mean Annual Temperature'
        labels_dict['cc'] = 'Cover Crop'
        labels_dict['mgmt'] = 'Management'
        labels_dict['soil'] = 'Soil Texture'
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
            list(treatmentdata.values('TreatmentID', 'PrimaryCrop', 'SecondaryCrop', 'CoverCrop', 'Management', 'Tillage', 'FluxInstrument')))
        dropdown_dict['json_rep'] = dumps(
            list(replicationdata.values('RepID', 'Air_T_Meas', 'Precip_Meas', 'Soil_T_Meas', 'VWC_Meas', 'WFPS_Meas')))
        print("end")
        print(dropdown_dict['dropdown_dict'])
        #return render_to_response('get-data.html', dropdown_dict)
        return render(request, 'get-data.html', dropdown_dict)

def submit_data(request):
    return render(request, 'submit_data.html', {
    })
# Create your views here.
