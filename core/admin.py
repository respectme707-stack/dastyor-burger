import os

from django.contrib import admin, messages
from django.core.exceptions import ValidationError
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
    list_display = ["name", "district", "phone", "debt_display", "bot_connected", "active"]
    list_filter = ["district", "active"]
    search_fields = ["name", "district", "phone", "contact"]
    readonly_fields = ["bot_link"]

    @admin.display(description="Бот", boolean=True)
    def bot_connected(self, obj):
        return bool(obj.telegram_id)

    @admin.display(description="Ссылка для подключения бота")
    def bot_link(self, obj):
        if not obj.bot_code:
            return "Появится после сохранения"
        username = os.environ.get("TELEGRAM_BOT_USERNAME")
        if not username:
            return f"Код: {obj.bot_code} (задайте TELEGRAM_BOT_USERNAME, чтобы здесь была ссылка)"
        url = f"https://t.me/{username}?start={obj.bot_code}"
        return format_html('<a href="{0}">{0}</a><br>Отправьте эту ссылку магазину.', url)

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


class OrderItemInline(admin.TabularInline):
    model = m.OrderItem
    extra = 0


@admin.register(m.Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ["__str__", "created_at", "shop", "status", "total_display", "shipment"]
    list_filter = ["status", "shop__district", "shop"]
    date_hierarchy = "created_at"
    readonly_fields = ["shipment"]
    inlines = [OrderItemInline]
    actions = ["make_shipments"]

    @admin.display(description="Сумма")
    def total_display(self, obj):
        return money(obj.total())

    @admin.action(description="Оформить отгрузку по выбранным заказам")
    def make_shipments(self, request, queryset):
        done = 0
        for order in queryset:
            try:
                if not order.shipment_id:
                    order.to_shipment()
                    done += 1
            except ValidationError as e:
                self.message_user(request, e.messages[0], messages.WARNING)
        if done:
            self.message_user(request, f"Создано отгрузок: {done}. Оплату внесите в самой отгрузке.")
