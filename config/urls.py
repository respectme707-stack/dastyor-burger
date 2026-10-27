from django.contrib import admin
from django.urls import include, path
from django.utils.translation import gettext_lazy as _

from core import views

admin.site.site_header = "DASTYOR"
admin.site.site_title = "DASTYOR"
admin.site.index_title = _("Все таблицы")
admin.site.site_url = "/"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("i18n/", include("django.conf.urls.i18n")),
    path("", views.dashboard, name="dashboard"),
    path("money/", views.money, name="money"),
    path("shops/", views.shops, name="shops"),
    path("shops/<int:pk>/", views.shop_detail, name="shop_detail"),
    path("products/", views.products, name="products"),
    path("products/<int:pk>/", views.product_detail, name="product_detail"),
    path("staff/", views.staff, name="staff"),
    path("staff/<int:pk>/", views.staff_detail, name="staff_detail"),
    path("stock/", views.stock, name="stock"),
    path("entry/", views.entry, name="entry"),
    path("entry/recipes/", views.recipes, name="recipes"),
    path("search/", views.search, name="search"),
    path("settings/", views.settings_page, name="settings"),
]
