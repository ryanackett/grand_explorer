"""fluxapp URL Configuration

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/3.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path
from django.urls import re_path
from n2odb import views
from n2odb import views_workbench
from django.contrib.auth.decorators import login_required as _lr

urlpatterns = [
    # --- Assisted-upload workbench --- (contributor views are login-required (_lr) here; the
    # per-view owner check lives in the view. Curation *views* carry @staff_member_required.)
    path('submit-data/', _lr(views_workbench.submit_data), name='submit-data'),
    path('my-submissions/', _lr(views_workbench.my_submissions), name='my-submissions'),
    path('workbench/<uuid:token>/resume/',
         _lr(views_workbench.resume_submission), name='workbench-resume'),
    path('workbench/<uuid:token>/upload/',
         _lr(views_workbench.workbench_upload), name='workbench-upload'),
    path('workbench/<uuid:token>/discard/',
         _lr(views_workbench.discard_submission), name='workbench-discard'),
    path('workbench/<uuid:token>/document/',
         _lr(views_workbench.create_document), name='workbench-document'),
    path('workbench/<uuid:token>/document/extract/',
         _lr(views_workbench.extract_document), name='workbench-document-extract'),
    path('workbench/<uuid:token>/document/text/',
         _lr(views_workbench.document_text), name='workbench-document-text'),
    path('workbench/<uuid:token>/data/',
         _lr(views_workbench.upload_data), name='workbench-data'),
    path('workbench/<uuid:token>/mapping/',
         _lr(views_workbench.mapping), name='workbench-mapping'),
    path('workbench/<uuid:token>/mapping/save/',
         _lr(views_workbench.save_mapping), name='workbench-mapping-save'),
    path('workbench/<uuid:token>/mapping/sheet/',
         _lr(views_workbench.set_measurement_sheet), name='workbench-measurement-sheet'),
    path('workbench/<uuid:token>/site/',
         _lr(views_workbench.site_info), name='workbench-site'),
    path('workbench/<uuid:token>/site/save/',
         _lr(views_workbench.save_site), name='workbench-site-save'),
    path('workbench/<uuid:token>/soil/',
         _lr(views_workbench.soil_mgmt), name='workbench-soil'),
    path('workbench/<uuid:token>/soil/save/',
         _lr(views_workbench.save_soil_mgmt), name='workbench-soil-save'),
    path('workbench/<uuid:token>/timeline/',
         _lr(views_workbench.timeline), name='workbench-timeline'),
    path('workbench/<uuid:token>/timeline/save/',
         _lr(views_workbench.save_timeline), name='workbench-timeline-save'),
    path('workbench/<uuid:token>/review/',
         _lr(views_workbench.review), name='workbench-review'),
    path('workbench/<uuid:token>/submit/',
         _lr(views_workbench.submit), name='workbench-submit'),
    path('workbench/<uuid:token>/submit/finalize/',
         _lr(views_workbench.finalize_submit), name='workbench-submit-finalize'),
    path('workbench/curation/',
         views_workbench.curation_list, name='workbench-curation'),
    path('workbench/curation/<uuid:token>/',
         views_workbench.curation_detail, name='workbench-curation-detail'),
    path('workbench/curation/<uuid:token>/action/',
         views_workbench.curation_action, name='workbench-curation-action'),
    path( '', views.index, name ='home'),
    path( 'robots.txt', views.robots_txt, name='robots'),
    path( 'about/', views.about, name ='about'),
    path( 'about-data/', views.about_data, name ='about-data'),
    path( 'why-contribute/', views.why_contribute, name='why-contribute'),
    path( 'leaderboard/', views.leaderboard, name='leaderboard'),
    path( 'query-filters/', views.query_filters, name ='query-filters'),
    path( 'get-data/', views.get_data, name ='get-data'),
    path( 'data-cart/', views.data_cart, name ='data-cart'),
    path( 'api/cart-data/', views.get_cart_data, name='get_cart_data'),
    path('account/', _lr(views.account_settings), name='account-settings'),
    path('account/name/', _lr(views.change_name), name='account-name'),
    path('contact/', views.contact_view, name='contact'),
    path('team/', views.team_view, name='team'),
    path('accounts/', include('allauth.urls')),   # login / signup / email verification
    path('admin/', admin.site.urls),
    path('cart/download-zip/', views.download_zip, name='download_zip'),
    path('explorer/', include('explorer.urls')),
]
