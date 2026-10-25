from django.contrib import admin
from django.urls import path

from core import views

admin.site.site_header = "Dastyor Burger — учёт"
admin.site.site_title = "Dastyor Burger"
admin.site.index_title = "Все таблицы"
admin.site.site_url = "/"

urlpatterns = [
    path("admin/", admin.site.urls),
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
    path("settings/", views.settings_page, name="settings"),
]
