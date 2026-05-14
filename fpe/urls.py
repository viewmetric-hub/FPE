"""
URL configuration for fpe project.

The `urlpatterns` list routes URLs to views. For more information please see:
https://docs.djangoproject.com/en/5.2/topics/http/urls/
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
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include

from dashboard.views import generator_reports_page, plant_user_reports_page, plant_user_settings_page

urlpatterns = [
    path('admin/', admin.site.urls),
    path('generator-reports/', generator_reports_page, name='generator_reports'),
    path('plant-user-reports/', plant_user_reports_page, name='plant_user_reports'),
    path('plant-user-settings/', plant_user_settings_page, name='plant_user_settings'),
    path('', include('dashboard.urls')),
    path('api/', include('accounts.api.urls')),
    path('api/', include('core.api.urls')),
    path('api/', include('allocation.api.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
