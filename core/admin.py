import os
from decimal import Decimal

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.options import FORMFIELD_FOR_DBFIELD_DEFAULTS
from django.core.exceptions import ValidationError
from django.contrib.humanize.templatetags.humanize import intcomma
from django.db import models
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from . import models as m


class PlainNumberInput(forms.NumberInput):
    """Числа в формах без лишних нулей: 0.00 → 0, 1.500 → 1.5."""

    def format_value(self, value):
        text = super().format_value(value)
        if text and "." in text and "e" not in text.lower():
            text = text.rstrip("0").rstrip(".")
        return text


FORMFIELD_FOR_DBFIELD_DEFAULTS[models.DecimalField] = {"widget": PlainNumberInput}


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


@admin.register(m.Unit)
class UnitAdmin(admin.ModelAdmin):
    list_display = ["name", "big_name", "big_ratio"]
    search_fields = ["name"]


class RawMaterialInline(admin.TabularInline):
    model = m.RawMaterial
    extra = 3
    fields = ["name", "unit", "pack_size", "homemade"]


@admin.register(m.MaterialCategory)
class MaterialCategoryAdmin(admin.ModelAdmin):
    list_display = ["name", "materials_display"]
    search_fields = ["name"]
    inlines = [RawMaterialInline]

    @admin.display(description="Сырьё")
    def materials_display(self, obj):
        return ", ".join(x.name for x in obj.materials.all()) or "—"


class MaterialRecipeInline(admin.TabularInline):
    model = m.MaterialRecipeItem
    fk_name = "material"
    extra = 3
    autocomplete_fields = ["ingredient"]


@admin.register(m.RawMaterial)
class RawMaterialAdmin(admin.ModelAdmin):
    list_display = ["name", "category", "unit", "homemade", "stock_display", "avg_price_display", "min_stock"]
    list_filter = ["homemade", "category", "unit"]
    search_fields = ["name"]
    fields = ["name", "category", "unit", "pack_name", "pack_size", "homemade", "min_stock", "recipe_cost_display"]
    readonly_fields = ["recipe_cost_display"]
    inlines = [MaterialRecipeInline]

    class Media:
        js = ["core/homemade.js"]

    @admin.display(description="Себестоимость 1 ед. по рецепту")
    def recipe_cost_display(self, obj):
        return money(obj.recipe_cost()) if obj.pk and obj.homemade else "—"

    @admin.display(description="Остаток")
    def stock_display(self, obj):
        text = f"{qty(obj.stock())} {obj.unit}"
        if obj.is_low():
            return format_html('<b style="color:#b4410c">{} (мало)</b>', text)
        return text

    @admin.display(description="Средняя цена")
    def avg_price_display(self, obj):
        return money(obj.avg_price())


class PurchaseItemForm(forms.ModelForm):
    """Если у сырья задана упаковка, количество и цена вводятся упаковками, а на склад идут гр/шт."""

    class Meta:
        model = m.PurchaseItem
        fields = ["material", "qty", "price"]
        labels = {"qty": "Количество", "price": "Цена"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.legacy = False
        if not self.instance.pk:
            return
        item = self.instance
        qty, price = item.qty, item.price
        size = item.material.pack_size
        if item.before_packs():
            # Записано до упаковок: «1» значило 1 упаковку, а попало как 1 гр. Покажем как упаковки,
            # при сохранении пересчитается в граммы.
            self.legacy = True
        elif size:
            qty, price = qty / size, price * size
        self.initial["qty"] = _plain(qty, "0.001")
        self.initial["price"] = _plain(price, "0.01")

    def has_changed(self):
        return self.legacy or super().has_changed()

    def clean(self):
        data = super().clean()
        material = data.get("material")
        qty, price = data.get("qty"), data.get("price")
        if material is None or qty is None or price is None:
            return data
        data["qty"], data["price"] = material.from_packs(qty, price)
        if self.instance.pk and not self.legacy and material == self.instance.material:
            # Поле не трогали — оставляем точное значение (на экране оно округлено).
            if qty == self.initial["qty"]:
                data["qty"] = self.instance.qty
            if price == self.initial["price"]:
                data["price"] = self.instance.price
        return data


def _plain(value, step):
    """Без лишних нулей: 18000000.00 → 18000000, 0.333333 → 0.333."""
    value = Decimal(value).quantize(Decimal(step))
    return value.quantize(Decimal(1)) if value == value.to_integral() else value.normalize()


class PurchaseItemInline(admin.TabularInline):
    model = m.PurchaseItem
    form = PurchaseItemForm
    extra = 3
    autocomplete_fields = ["material"]


@admin.register(m.Purchase)
class PurchaseAdmin(admin.ModelAdmin):
    list_display = ["__str__", "date", "supplier", "total_display", "paid"]
    list_filter = ["supplier", "date"]
    date_hierarchy = "date"
    inlines = [PurchaseItemInline]

    class Media:
        js = ["core/totals.js"]

    def render_change_form(self, request, context, *args, **kwargs):
        context["material_units"] = {
            x.pk: {"unit": x.unit.name, "pack": x.pack_word(), "pack_size": float(x.pack_size or 0)}
            for x in m.RawMaterial.objects.select_related("unit")}
        return super().render_change_form(request, context, *args, **kwargs)

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
    list_display = ["name", "price", "shelf_life_days", "unit_cost_display", "profit_display", "stock", "active"]
    list_editable = ["shelf_life_days"]
    list_filter = ["active"]
    search_fields = ["name"]
    inlines = [RecipeItemInline]
    readonly_fields = ["unit_cost_display", "profit_display", "breakdown_display"]

    @admin.display(description="Из чего складывается")
    def breakdown_display(self, obj):
        if not obj.pk:
            return "—"
        lines = []
        for r in obj.recipe.select_related("material__unit"):
            price = r.material.avg_price()
            cost = r.qty * price
            warn = cost > obj.price / 2
            lines.append(format_html(
                '<div{}>{}: {} {} × {} за 1 {} = <b>{}</b>{}</div>',
                format_html(' style="color:#f85149"') if warn else "", r.material.name, qty(r.qty),
                r.material.unit, money(price), r.material.unit, money(cost),
                " ← проверьте цену закупки и единицу" if warn else ""))
        return mark_safe("".join(lines)) if lines else "Рецепт пуст"

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
    fk_name = "production"
    extra = 0
    can_delete = False
    fields = readonly_fields = ["material", "qty", "cost"]

    def has_add_permission(self, request, obj=None):
        return False


class BatchConsumptionInline(ConsumptionInline):
    fk_name = "batch"


@admin.register(m.MaterialBatch)
class MaterialBatchAdmin(admin.ModelAdmin):
    list_display = ["material", "date", "qty", "worker", "unit_cost", "total_cost_display"]
    list_filter = ["material", "worker", "date"]
    date_hierarchy = "date"
    readonly_fields = ["unit_cost"]
    inlines = [BatchConsumptionInline]

    @admin.display(description="Себестоимость партии")
    def total_cost_display(self, obj):
        return money(obj.total_cost())


@admin.register(m.Production)
class ProductionAdmin(admin.ModelAdmin):
    list_display = ["product", "date", "qty", "worker", "unit_cost", "total_cost_display"]
    list_filter = ["product", "worker", "date"]
    date_hierarchy = "date"
    readonly_fields = ["unit_cost"]
    inlines = [ConsumptionInline]

    @admin.display(description="Себестоимость партии")
    def total_cost_display(self, obj):
        return money(obj.total_cost())


def bot_link(obj, whom):
    if not obj.bot_code:
        return "Появится после сохранения"
    username = os.environ.get("TELEGRAM_BOT_USERNAME")
    if not username:
        return f"Код: {obj.bot_code} (задайте TELEGRAM_BOT_USERNAME, чтобы здесь была ссылка)"
    url = f"https://t.me/{username}?start={obj.bot_code}"
    return format_html('<a href="{0}">{0}</a><br>Отправьте эту ссылку {1}.', url, whom)


@admin.register(m.Shop)
class ShopAdmin(admin.ModelAdmin):
    list_display = ["name", "firm", "district", "phone", "debt_display", "bot_connected", "active", "archived"]
    list_filter = ["district", "active", "archived"]
    search_fields = ["name", "firm", "district", "address", "phone", "contact"]
    fields = ["name", "firm", "district", "address", "latitude", "longitude", "map_link", "contact", "phone",
              "active", "archived", "telegram_id", "bot_link"]
    readonly_fields = ["bot_link", "map_link"]

    @admin.display(description="Бот", boolean=True)
    def bot_connected(self, obj):
        return bool(obj.telegram_id)

    @admin.display(description="Ссылка для подключения бота")
    def bot_link(self, obj):
        return bot_link(obj, "магазину")

    @admin.display(description="На карте")
    def map_link(self, obj):
        url = obj.map_url()
        return format_html('<a href="{}" target="_blank">Открыть карту</a>', url) if url else "—"

    @admin.display(description="Долг нам")
    def debt_display(self, obj):
        return money(obj.debt())


class ShipmentItemInline(admin.TabularInline):
    model = m.ShipmentItem
    extra = 2
    fields = ["product", "qty", "price"]


@admin.register(m.Shipment)
class ShipmentAdmin(admin.ModelAdmin):
    list_display = ["__str__", "date", "shop", "worker", "total_display", "method", "paid", "debt_display"]
    list_filter = ["method", "worker", "shop__district", "shop", "date"]
    date_hierarchy = "date"
    autocomplete_fields = ["shop"]
    inlines = [ShipmentItemInline]

    class Media:
        js = ["core/totals.js"]

    def render_change_form(self, request, context, *args, **kwargs):
        context["product_prices"] = {p.pk: float(p.price) for p in m.Product.objects.all()}
        return super().render_change_form(request, context, *args, **kwargs)

    @admin.display(description="Сумма")
    def total_display(self, obj):
        return money(obj.total())

    @admin.display(description="В долг")
    def debt_display(self, obj):
        return money(obj.debt())


@admin.register(m.Return)
class ReturnAdmin(admin.ModelAdmin):
    list_display = ["product", "shop", "date", "qty", "reason", "worker", "total_display"]
    list_filter = ["reason", "worker", "shop__district", "shop", "product", "date"]
    date_hierarchy = "date"
    autocomplete_fields = ["shop"]

    @admin.display(description="Сумма")
    def total_display(self, obj):
        return money(obj.total())


@admin.register(m.Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ["date", "shop", "supplier", "amount", "method", "worker"]
    list_filter = ["method", "worker", "date"]
    date_hierarchy = "date"
    autocomplete_fields = ["shop", "supplier"]


@admin.register(m.Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ["date", "category", "amount", "worker", "note"]
    list_filter = ["category", "worker", "date"]
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


@admin.register(m.Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = ["name", "drivers_display", "active"]

    @admin.display(description="Кто ездит")
    def drivers_display(self, obj):
        return ", ".join(w.name for w in obj.workers.all()) or "—"


@admin.register(m.Worker)
class WorkerAdmin(admin.ModelAdmin):
    list_display = ["name", "role", "phone", "vehicle", "districts", "stock_display", "bot_connected", "active"]
    list_filter = ["role", "active"]
    search_fields = ["name", "phone"]
    fields = ["name", "role", "phone", "districts", "shops", "vehicle", "active", "telegram_id", "bot_link"]
    filter_horizontal = ["shops"]
    readonly_fields = ["bot_link"]

    class Media:
        js = ["core/worker_form.js"]

    @admin.display(description="На руках")
    def stock_display(self, obj):
        if not obj.is_seller():
            return "—"
        return ", ".join(f"{p.name} {n}" for p, n in obj.stock().items()) or "пусто"

    @admin.display(description="Бот", boolean=True)
    def bot_connected(self, obj):
        return bool(obj.telegram_id)

    @admin.display(description="Ссылка для подключения бота")
    def bot_link(self, obj):
        return bot_link(obj, "сотруднику")


class VanLoadItemInline(admin.TabularInline):
    model = m.VanLoadItem
    extra = 2


@admin.register(m.VanLoad)
class VanLoadAdmin(admin.ModelAdmin):
    list_display = ["__str__", "date", "kind", "worker", "given_by"]
    list_filter = ["kind", "worker", "date"]
    date_hierarchy = "date"
    inlines = [VanLoadItemInline]


@admin.register(m.WorkDay)
class WorkDayAdmin(admin.ModelAdmin):
    list_display = ["date", "worker", "status", "note"]
    list_editable = ["status"]
    list_filter = ["status", "worker", "date"]
    date_hierarchy = "date"
    actions = ["mark_worked", "mark_absent"]

    @admin.action(description="Отметить: работал")
    def mark_worked(self, request, queryset):
        queryset.update(status=m.WorkDay.Status.WORKED)

    @admin.action(description="Отметить: не вышел")
    def mark_absent(self, request, queryset):
        queryset.update(status=m.WorkDay.Status.ABSENT)
