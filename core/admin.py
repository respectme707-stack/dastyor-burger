import os
from decimal import Decimal

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.options import FORMFIELD_FOR_DBFIELD_DEFAULTS
from django.core.exceptions import ValidationError
from django.contrib.humanize.templatetags.humanize import intcomma
from django.db import models
from django.urls import reverse
from django.utils.html import format_html
from django.utils.http import urlencode
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

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
    return _("%(amount)s сум") % {"amount": intcomma(round(value))}


def qty(value):
    return f"{value:g}"


@admin.register(m.Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ["name", "phone", "debt_display"]
    search_fields = ["name", "phone"]

    @admin.display(description=_("Мы должны"))
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

    @admin.display(description=_("Сырьё"))
    def materials_display(self, obj):
        return ", ".join(x.name for x in obj.materials.all()) or "—"


@admin.register(m.RawMaterial)
class RawMaterialAdmin(admin.ModelAdmin):
    list_display = ["name", "category", "unit", "homemade", "stock_display", "avg_price_display", "min_stock"]
    list_filter = ["homemade", "category", "unit"]
    search_fields = ["name"]
    fields = ["name", "category", "unit", "pack_name", "pack_size", "homemade", "min_stock", "recipe_display"]
    readonly_fields = ["recipe_display"]

    @admin.display(description=_("Рецепт"))
    def recipe_display(self, obj):
        if not obj.pk or not obj.homemade:
            return _("Только для того, что делаем сами")
        product = obj.linked_product()
        if product:
            return format_html(_('<a href="{}">{}</a> · себестоимость 1 {}: {}'),
                               reverse("admin:core_product_change", args=[product.pk]), product.name, obj.unit,
                               money(obj.recipe_cost()))
        url = reverse("admin:core_product_add") + "?" + urlencode({"name": obj.name, "material": obj.pk})
        return format_html(_('Рецепта ещё нет. <a href="{}">Создать рецепт «{}»</a>'), url, obj.name)

    @admin.display(description=_("Остаток"))
    def stock_display(self, obj):
        text = f"{qty(obj.stock())} {obj.unit}"
        if obj.is_low():
            return format_html(_('<b style="color:#b4410c">{} (мало)</b>'), text)
        return text

    @admin.display(description=_("Средняя цена"))
    def avg_price_display(self, obj):
        return money(obj.avg_price())


class PurchaseItemForm(forms.ModelForm):
    """Если у сырья задана упаковка, количество и цена вводятся упаковками, а на склад идут гр/шт."""

    class Meta:
        model = m.PurchaseItem
        fields = ["material", "qty", "price"]
        labels = {"qty": _("Количество"), "price": _("Цена")}

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


def last_prices():
    """{сырьё: цена последней закупки} — как вводят: за упаковку, если она есть, иначе за единицу."""
    result = {}
    for item in m.PurchaseItem.objects.select_related("material").order_by("purchase__date", "purchase_id", "id"):
        size = item.material.pack_size
        price = item.price * size if size and not item.before_packs() else item.price
        result[item.material_id] = float(_plain(price, "0.01"))
    return result


@admin.register(m.Purchase)
class PurchaseAdmin(admin.ModelAdmin):
    list_display = ["__str__", "date", "supplier", "total_display", "paid", "method"]
    list_filter = ["method", "supplier", "date"]
    date_hierarchy = "date"
    fields = ["supplier", "date", "paid", "method", "note"]
    inlines = [PurchaseItemInline]

    class Media:
        js = ["core/totals.js"]

    def render_change_form(self, request, context, *args, **kwargs):
        prices = last_prices()
        context["material_units"] = {
            x.pk: {"unit": x.unit.name, "pack": x.pack_word(), "pack_size": float(x.pack_size or 0),
                   "last_price": prices.get(x.pk)}
            for x in m.RawMaterial.objects.select_related("unit")}
        return super().render_change_form(request, context, *args, **kwargs)

    @admin.display(description=_("Сумма"))
    def total_display(self, obj):
        return money(obj.total())


@admin.register(m.MaterialWriteOff)
class MaterialWriteOffAdmin(admin.ModelAdmin):
    """Сырьё испортилось или выбросили: убираем со склада, деньги за него уже потрачены."""

    list_display = ["material", "date", "qty", "reason"]
    list_filter = ["material", "date"]
    autocomplete_fields = ["material"]


class RecipeItemInline(admin.TabularInline):
    model = m.RecipeItem
    extra = 3
    autocomplete_fields = ["material"]


class ProductForm(forms.ModelForm):
    """Название подсказывается из сырья «делаем сами»: выбрали — продукт связывается с ним, дубля нет."""

    as_material = forms.BooleanField(
        label=_("Идёт в другие рецепты"), required=False,
        help_text=_("Поставьте, если это полуфабрикат (котлеты, булочки), которого ещё нет в списке сырья: "
                    "он появится там с пометкой «делаем сами»"))

    class Meta:
        model = m.Product
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        taken = m.Product.objects.exclude(material=None)
        if self.instance.pk:
            taken = taken.exclude(pk=self.instance.pk)
        free = m.RawMaterial.objects.filter(homemade=True).exclude(pk__in=taken.values("material"))
        self.fields["material"].queryset = free
        self.fields["name"].widget.attrs.update(list="homemade-names", autocomplete="off")
        self.homemade_names = list(free.values_list("name", flat=True))
        if self.instance.material_id:
            self.fields.pop("as_material", None)

    def clean(self):
        data = super().clean()
        name = (data.get("name") or "").strip()
        if not data.get("material") and name:
            data["material"] = next((x for x in self.fields["material"].queryset
                                     if x.name.strip().casefold() == name.casefold()), None)
        if data.get("material") and name.casefold() == data["material"].name.strip().casefold():
            data["name"] = data["material"].name
        return data

    def save(self, commit=True):
        product = super().save(commit=False)
        if not product.material_id and self.cleaned_data.get("as_material"):
            # Сырьё с таким же названием (например, покупное) не трогаем: называем полуфабрикат иначе.
            name = product.name
            names = {x.casefold() for x in m.RawMaterial.objects.values_list("name", flat=True)}
            if name.casefold() in names:
                name = f"{name} (свой)"
            material = m.RawMaterial.objects.create(name=name, homemade=True)
            product.material = material
        if commit:
            product.save()
            self.save_m2m()
        return product


@admin.register(m.Product)
class ProductAdmin(admin.ModelAdmin):
    form = ProductForm
    list_display = ["name", "price_display", "shelf_life_days", "unit_cost_display", "profit_display", "stock",
                    "active"]
    list_editable = ["shelf_life_days"]
    list_filter = ["active"]
    search_fields = ["name"]
    inlines = [RecipeItemInline]
    readonly_fields = ["unit_cost_display", "profit_display", "breakdown_display"]

    def get_fields(self, request, obj=None):
        fields = ["name", "price", "material", "as_material", "shelf_life_days", "active"]
        if obj is not None and obj.material_id:
            fields.remove("as_material")
        return fields + list(self.readonly_fields)

    def render_change_form(self, request, context, *args, **kwargs):
        context["homemade_names"] = context["adminform"].form.homemade_names
        return super().render_change_form(request, context, *args, **kwargs)

    @admin.display(description=_("Цена для магазинов"))
    def price_display(self, obj):
        return money(obj.price) if obj.price else _("не продаём")

    @admin.display(description=_("Из чего складывается"))
    def breakdown_display(self, obj):
        if not obj.pk:
            return "—"
        lines = []
        for r in obj.recipe.select_related("material__unit"):
            price = r.material.avg_price()
            cost = r.qty * price
            warn = obj.price > 0 and cost > obj.price / 2
            lines.append(format_html(
                _('<div{}>{}: {} {} × {} за 1 {} = <b>{}</b>{}</div>'),
                format_html(' style="color:#f85149"') if warn else "", r.material.name, qty(r.qty),
                r.material.unit, money(price), r.material.unit, money(cost),
                _(" ← проверьте цену закупки и единицу") if warn else ""))
        return mark_safe("".join(lines)) if lines else _("Рецепт пуст")

    @admin.display(description=_("Себестоимость 1 шт"))
    def unit_cost_display(self, obj):
        return money(obj.unit_cost()) if obj.pk else "—"

    @admin.display(description=_("Прибыль с 1 шт"))
    def profit_display(self, obj):
        if not obj.pk or not obj.price:
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
    """Старые записи «Испекли булочки». Новые булочки и котлеты записываются через «Производство»."""

    list_display = ["material", "date", "qty", "worker", "unit_cost", "total_cost_display"]
    list_filter = ["material", "worker", "date"]
    date_hierarchy = "date"
    readonly_fields = ["unit_cost"]
    inlines = [BatchConsumptionInline]

    def has_add_permission(self, request):
        return False

    @admin.display(description=_("Себестоимость партии"))
    def total_cost_display(self, obj):
        return money(obj.total_cost())


def recipe_stock():
    """Для формы производства: по каждому продукту — что нужно на 1 шт и сколько этого на складе."""
    result = {}
    for p in m.Product.objects.prefetch_related("recipe__material__unit"):
        result[p.pk] = [{"name": r.material.name, "unit": r.material.unit.name, "per": float(r.qty),
                         "stock": float(r.material.stock())} for r in p.recipe.all()]
    return result


@admin.register(m.Production)
class ProductionAdmin(admin.ModelAdmin):
    list_display = ["product", "date", "qty", "worker", "unit_cost", "total_cost_display"]
    list_filter = ["product", "worker", "date"]
    date_hierarchy = "date"
    readonly_fields = ["unit_cost"]
    inlines = [ConsumptionInline]

    class Media:
        js = ["core/production.js"]

    def render_change_form(self, request, context, *args, **kwargs):
        context["recipe_stock"] = recipe_stock()
        return super().render_change_form(request, context, *args, **kwargs)

    @admin.display(description=_("Себестоимость партии"))
    def total_cost_display(self, obj):
        return money(obj.total_cost())


def bot_link(obj, whom):
    if not obj.bot_code:
        return _("Появится после сохранения")
    username = os.environ.get("TELEGRAM_BOT_USERNAME")
    if not username:
        return _("Код: %(code)s (задайте TELEGRAM_BOT_USERNAME, чтобы здесь была ссылка)") % {"code": obj.bot_code}
    url = f"https://t.me/{username}?start={obj.bot_code}"
    return format_html(_('<a href="{0}">{0}</a><br>Отправьте эту ссылку {1}.'), url, whom)


@admin.register(m.Shop)
class ShopAdmin(admin.ModelAdmin):
    list_display = ["name", "firm", "district", "phone", "debt_display", "bot_connected", "active", "archived"]
    list_filter = ["district", "active", "archived"]
    search_fields = ["name", "firm", "district", "address", "phone", "contact"]
    fields = ["name", "firm", "district", "address", "latitude", "longitude", "map_link", "contact", "phone",
              "active", "archived", "telegram_id", "bot_link"]
    readonly_fields = ["bot_link", "map_link"]

    @admin.display(description=_("Бот"), boolean=True)
    def bot_connected(self, obj):
        return bool(obj.telegram_id)

    @admin.display(description=_("Ссылка для подключения бота"))
    def bot_link(self, obj):
        return bot_link(obj, _("магазину"))

    @admin.display(description=_("На карте"))
    def map_link(self, obj):
        url = obj.map_url()
        return format_html(_('<a href="{}" target="_blank">Открыть карту</a>'), url) if url else "—"

    @admin.display(description=_("Долг нам"))
    def debt_display(self, obj):
        return money(obj.debt())


class ShipmentItemInline(admin.TabularInline):
    model = m.ShipmentItem
    extra = 2
    fields = ["product", "qty", "price"]


@admin.register(m.Shipment)
class ShipmentAdmin(admin.ModelAdmin):
    list_display = ["__str__", "date", "shop", "worker", "total_display", "method", "paid", "debt_display"]
    exclude = ["vehicle"]
    list_filter = ["method", "worker", "shop__district", "shop", "date"]
    date_hierarchy = "date"
    autocomplete_fields = ["shop"]
    inlines = [ShipmentItemInline]

    class Media:
        js = ["core/totals.js"]

    def render_change_form(self, request, context, *args, **kwargs):
        context["product_prices"] = {p.pk: float(p.price) for p in m.Product.objects.all()}
        return super().render_change_form(request, context, *args, **kwargs)

    @admin.display(description=_("Сумма"))
    def total_display(self, obj):
        return money(obj.total())

    @admin.display(description=_("В долг"))
    def debt_display(self, obj):
        return money(obj.debt())


@admin.register(m.Return)
class ReturnAdmin(admin.ModelAdmin):
    list_display = ["product", "shop", "date", "qty", "reason", "worker", "total_display"]
    list_filter = ["reason", "worker", "shop__district", "shop", "product", "date"]
    date_hierarchy = "date"
    autocomplete_fields = ["shop"]

    @admin.display(description=_("Сумма"))
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

    @admin.display(description=_("Сумма"))
    def total_display(self, obj):
        return money(obj.total())

    @admin.action(description=_("Оформить отгрузку по выбранным заказам"))
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
            self.message_user(request, _("Создано отгрузок: %(n)s. Оплату внесите в самой отгрузке.") % {"n": done})


class WorkerForm(forms.ModelForm):
    """Машины и ванселлеры больше не используются: роль «ванселлер» видна только у тех, у кого она уже стоит."""

    class Meta:
        model = m.Worker
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.role != m.Worker.Role.VANSELLER and "role" in self.fields:
            self.fields["role"].choices = [c for c in self.fields["role"].choices
                                           if c[0] != m.Worker.Role.VANSELLER]


@admin.register(m.Worker)
class WorkerAdmin(admin.ModelAdmin):
    form = WorkerForm
    list_display = ["name", "role", "phone", "salary", "districts", "stock_display", "bot_connected", "active"]
    list_filter = ["role", "active"]
    search_fields = ["name", "phone"]
    fields = ["name", "role", "phone", "salary", "districts", "shops", "active", "telegram_id", "bot_link"]
    filter_horizontal = ["shops"]
    readonly_fields = ["bot_link"]

    class Media:
        js = ["core/worker_form.js"]

    @admin.display(description=_("На руках"))
    def stock_display(self, obj):
        if not obj.is_seller():
            return "—"
        return ", ".join(f"{p.name} {n}" for p, n in obj.stock().items()) or _("пусто")

    @admin.display(description=_("Бот"), boolean=True)
    def bot_connected(self, obj):
        return bool(obj.telegram_id)

    @admin.display(description=_("Ссылка для подключения бота"))
    def bot_link(self, obj):
        return bot_link(obj, _("сотруднику"))


class VanLoadItemInline(admin.TabularInline):
    model = m.VanLoadItem
    extra = 2


@admin.register(m.VanLoad)
class VanLoadAdmin(admin.ModelAdmin):
    list_display = ["__str__", "date", "kind", "worker", "given_by"]
    exclude = ["vehicle"]
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

    @admin.action(description=_("Отметить: работал"))
    def mark_worked(self, request, queryset):
        queryset.update(status=m.WorkDay.Status.WORKED)

    @admin.action(description=_("Отметить: не вышел"))
    def mark_absent(self, request, queryset):
        queryset.update(status=m.WorkDay.Status.ABSENT)
