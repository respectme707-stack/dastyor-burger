from django.contrib import admin
from django.urls import path

from core.views import dashboard

admin.site.site_header = "Dastyor Burger — учёт"
admin.site.site_title = "Dastyor Burger"
admin.site.index_title = "Учёт"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", dashboard, name="dashboard"),
]
