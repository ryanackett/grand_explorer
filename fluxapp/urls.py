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

urlpatterns = [
    path( '', views.index, name ='home'),
    path( 'robots.txt', views.robots_txt, name='robots'),
    path( 'about/', views.about, name ='about'),
    path( 'about-data/', views.about_data, name ='about-data'),
    path( 'query-filters/', views.query_filters, name ='query-filters'),
    path( 'get-data/', views.get_data, name ='get-data'),
    path( 'data-cart/', views.data_cart, name ='data-cart'),
    path( 'api/cart-data/', views.get_cart_data, name='get_cart_data'),
    path('contact/', views.contact_view, name='contact'),
    path('team/', views.team_view, name='team'),
    path('admin/', admin.site.urls),
    path('cart/download-zip/', views.download_zip, name='download_zip'),
    path('explorer/', include('explorer.urls')),
]
