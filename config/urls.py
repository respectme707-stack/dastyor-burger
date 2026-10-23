from django.contrib import admin
from django.urls import path
from django.views.generic import RedirectView

admin.site.site_header = "Dastyor Burger — учёт"
admin.site.site_title = "Dastyor Burger"
admin.site.index_title = "Учёт"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", RedirectView.as_view(url="/admin/", permanent=False)),
]
