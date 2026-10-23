from django.contrib import admin
from django.contrib.humanize.templatetags.humanize import intcomma
from django.utils.html import format_html

from . import models as m


def money(value):
    return f"{intcomma(round(value))} сум"


def qty(value):
    return f"{value:g}"


@admin.register(m.Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ["name", "phone", "debt_display"]
    search_fields = ["name", "phone"]

    @admin.display(description="Мы должны")
    def debt_display(self, obj):
        return money(obj.debt())


@admin.register(m.RawMaterial)
class RawMaterialAdmin(admin.ModelAdmin):
    list_display = ["name", "unit", "stock_display", "avg_price_display", "min_stock"]
    search_fields = ["name"]

    @admin.display(description="Остаток")
    def stock_display(self, obj):
        text = f"{qty(obj.stock())} {obj.get_unit_display()}"
        if obj.is_low():
            return format_html('<b style="color:#b4410c">{} (мало)</b>', text)
        return text

    @admin.display(description="Средняя цена")
    def avg_price_display(self, obj):
        return money(obj.avg_price())


class PurchaseItemInline(admin.TabularInline):
    model = m.PurchaseItem
    extra = 3
    autocomplete_fields = ["material"]


@admin.register(m.Purchase)
class PurchaseAdmin(admin.ModelAdmin):
    list_display = ["__str__", "date", "supplier", "total_display", "paid"]
    list_filter = ["supplier", "date"]
    date_hierarchy = "date"
    autocomplete_fields = ["supplier"]
    inlines = [PurchaseItemInline]

    @admin.display(description="Сумма")
    def total_display(self, obj):
        return money(obj.total())


@admin.register(m.MaterialWriteOff)
class MaterialWriteOffAdmin(admin.ModelAdmin):
    list_display = ["material", "date", "qty", "reason"]
    list_filter = ["material", "date"]
    autocomplete_fields = ["material"]


class RecipeItemInline(admin.TabularInline):
    model = m.RecipeItem
    extra = 3
    autocomplete_fields = ["material"]


@admin.register(m.Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ["name", "price", "unit_cost_display", "profit_display", "stock", "active"]
    list_filter = ["active"]
    search_fields = ["name"]
    inlines = [RecipeItemInline]
    readonly_fields = ["unit_cost_display", "profit_display"]

    @admin.display(description="Себестоимость 1 шт")
    def unit_cost_display(self, obj):
        return money(obj.unit_cost()) if obj.pk else "—"

    @admin.display(description="Прибыль с 1 шт")
    def profit_display(self, obj):
        if not obj.pk:
            return "—"
        cost = obj.unit_cost()
        profit = obj.price - cost
        pct = round(profit / obj.price * 100) if obj.price else 0
        return f"{money(profit)} ({pct}%)"


class ConsumptionInline(admin.TabularInline):
    model = m.Consumption
    extra = 0
    can_delete = False
    readonly_fields = ["material", "qty", "cost"]

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(m.Production)
class ProductionAdmin(admin.ModelAdmin):
    list_display = ["product", "date", "qty", "unit_cost", "total_cost_display"]
    list_filter = ["product", "date"]
    date_hierarchy = "date"
    readonly_fields = ["unit_cost"]
    inlines = [ConsumptionInline]

    @admin.display(description="Себестоимость партии")
    def total_cost_display(self, obj):
        return money(obj.total_cost())


@admin.register(m.Shop)
class ShopAdmin(admin.ModelAdmin):
    list_display = ["name", "district", "phone", "debt_display", "active"]
    list_filter = ["district", "active"]
    search_fields = ["name", "district", "phone", "contact"]

    @admin.display(description="Долг нам")
    def debt_display(self, obj):
        return money(obj.debt())


class ShipmentItemInline(admin.TabularInline):
    model = m.ShipmentItem
    extra = 2
    fields = ["product", "qty", "price"]


@admin.register(m.Shipment)
class ShipmentAdmin(admin.ModelAdmin):
    list_display = ["__str__", "date", "shop", "total_display", "paid", "debt_display"]
    list_filter = ["shop__district", "shop", "date"]
    date_hierarchy = "date"
    autocomplete_fields = ["shop"]
    inlines = [ShipmentItemInline]

    @admin.display(description="Сумма")
    def total_display(self, obj):
        return money(obj.total())

    @admin.display(description="В долг")
    def debt_display(self, obj):
        return money(obj.debt())


@admin.register(m.Return)
class ReturnAdmin(admin.ModelAdmin):
    list_display = ["product", "shop", "date", "qty", "reason", "total_display"]
    list_filter = ["reason", "shop__district", "shop", "product", "date"]
    date_hierarchy = "date"
    autocomplete_fields = ["shop"]

    @admin.display(description="Сумма")
    def total_display(self, obj):
        return money(obj.total())


@admin.register(m.Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ["date", "shop", "supplier", "amount", "method"]
    list_filter = ["method", "date"]
    date_hierarchy = "date"
    autocomplete_fields = ["shop", "supplier"]


@admin.register(m.Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ["date", "category", "amount", "note"]
    list_filter = ["category", "date"]
    date_hierarchy = "date"
