import secrets
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import DecimalField, ExpressionWrapper, F, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

ZERO = Decimal("0")
MONEY = dict(max_digits=14, decimal_places=2)
QTY = dict(max_digits=12, decimal_places=3)
CENT = Decimal("0.01")


def _sum(qs, expr):
    """Sum of an expression over a queryset, 0 when empty."""
    value = qs.aggregate(
        s=Sum(ExpressionWrapper(expr, output_field=DecimalField(**MONEY)))
    )["s"]
    return value or ZERO


class Supplier(models.Model):
    name = models.CharField(_("Название"), max_length=200, unique=True)
    phone = models.CharField(_("Телефон"), max_length=50, blank=True)
    note = models.TextField(_("Заметка"), blank=True)

    class Meta:
        verbose_name = _("Поставщик")
        verbose_name_plural = _("Поставщики")
        ordering = ["name"]

    def __str__(self):
        return self.name

    def debt(self):
        """Сколько мы должны поставщику."""
        purchased = _sum(PurchaseItem.objects.filter(purchase__supplier=self), F("qty") * F("price"))
        paid_on_purchase = _sum(Purchase.objects.filter(supplier=self), F("paid"))
        paid_later = _sum(Payment.objects.filter(supplier=self), F("amount"))
        return purchased - paid_on_purchase - paid_later


class MaterialCategory(models.Model):
    name = models.CharField(_("Название"), max_length=100, unique=True,
                            help_text=_("Например: Бургер, Булочки, Упаковка"))

    class Meta:
        verbose_name = _("Категория сырья")
        verbose_name_plural = _("Категории сырья")
        ordering = ["name"]

    def __str__(self):
        return self.name


class Unit(models.Model):
    name = models.CharField(_("Единица"), max_length=20, unique=True, help_text=_("Например: шт, кг, гр, л, пачка"))
    big_name = models.CharField(_("Крупная единица"), max_length=20, blank=True,
                                help_text=_("Для закупки. Например, для «гр» это «кг», для «мл» это «л»"))
    big_ratio = models.DecimalField(_("Сколько в ней"), max_digits=12, decimal_places=3, null=True, blank=True,
                                    help_text=_("Для «гр» → «кг» это 1000"))

    class Meta:
        verbose_name = _("Единица измерения")
        verbose_name_plural = _("Единицы измерения")
        ordering = ["name"]

    def __str__(self):
        return self.name


def default_unit():
    # Только pk: функция вызывается и из старых миграций, где у Unit ещё нет новых колонок.
    pk = Unit.objects.filter(name="шт").values_list("pk", flat=True).first()
    return pk if pk is not None else Unit.objects.create(name="шт").pk


class RawMaterial(models.Model):
    name = models.CharField(_("Название"), max_length=200, unique=True)
    category = models.ForeignKey(MaterialCategory, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="materials", verbose_name=_("Категория"))
    unit = models.ForeignKey(Unit, on_delete=models.PROTECT, default=default_unit, related_name="materials",
                             verbose_name=_("Единица"))
    min_stock = models.DecimalField(_("Мин. остаток"), default=0, blank=True, **QTY,
                                    help_text=_("Это не начальный остаток. Порог для напоминания: когда на складе "
                                                "станет меньше этого числа, сайт напишет «пора закупить». "
                                                "0 = не напоминать. Остаток появляется сам после закупки"))
    pack_name = models.CharField(_("Название упаковки"), max_length=30, blank=True,
                                 help_text=_("Необязательно, только слово: банка, пачка, ведро. Не число"))
    pack_size = models.DecimalField(_("Сколько в 1 упаковке"), null=True, blank=True, **QTY,
                                    help_text=_("В единицах этого сырья. Майонез в гр: 900. Колбаса в гр: 600. "
                                              "Если заполнено, закупка вводится упаковками"))
    homemade = models.BooleanField(_("Делаем сами"), default=False,
                                   help_text=_("Например, булочки или котлеты: не покупаем, а делаем в цеху. "
                                               "Рецепт заполняется в «Рецепты», делаем через «Производство»"))

    class Meta:
        verbose_name = _("Сырьё")
        verbose_name_plural = _("Сырьё")
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.pack_label()})"

    def pack_word(self):
        name = self.pack_name.strip()
        return name if name and not any(ch.isdigit() for ch in name) else _("упак.")

    def pack_label(self):
        """«банка по 900 гр» или просто единица, если упаковки нет."""
        if not self.pack_size:
            return str(self.unit)
        return _("%(pack)s по %(size)s %(unit)s") % {
            "pack": self.pack_word(), "size": f"{Decimal(self.pack_size).normalize():f}", "unit": self.unit}

    def from_packs(self, qty, price):
        """Количество и цена упаковками → в единицах сырья (для склада и себестоимости)."""
        if not self.pack_size:
            return qty, price
        return qty * self.pack_size, (price / self.pack_size).quantize(Decimal("0.000001"))

    def purchased_qty(self):
        return self.purchase_items.aggregate(s=Sum("qty"))["s"] or ZERO

    def linked_product(self):
        """Продукт с тем же рецептом: когда его производят, этот полуфабрикат приходит на склад."""
        try:
            return self.product
        except Product.DoesNotExist:
            return None

    def made_qty(self):
        made = self.batches.aggregate(s=Sum("qty"))["s"] or ZERO
        product = self.linked_product()
        if product:
            made += product.produced_qty()
        return made

    def sold_qty(self):
        """Полуфабрикат, который продали магазинам или передали агентам как товар."""
        product = self.linked_product()
        return Decimal(product.shipped_qty() + product.loaded_qty()) if product else ZERO

    def used_qty(self):
        return self.consumptions.aggregate(s=Sum("qty"))["s"] or ZERO

    def written_off_qty(self):
        return self.writeoffs.aggregate(s=Sum("qty"))["s"] or ZERO

    def out_qty(self):
        return self.used_qty() + self.written_off_qty() + self.sold_qty()

    def stock(self):
        return self.purchased_qty() + self.made_qty() - self.out_qty()

    def lots(self):
        """Всё, что приходило на склад, от старого к новому: [(дата, количество, цена за единицу)]."""
        rows = [(i.purchase.date, i.qty, i.price) for i in self.purchase_items.select_related("purchase")]
        rows += [(b.date, b.qty, b.unit_cost) for b in self.batches.all()]
        product = self.linked_product()
        if product:
            rows += [(p.date, Decimal(p.qty), p.unit_cost) for p in product.productions.all()]
        return sorted(rows, key=lambda r: r[0])

    def avg_price(self):
        """Средневзвешенная цена за единицу: по закупкам и по тому, что сделали сами."""
        rows = self.lots()
        qty = sum((r[1] for r in rows), ZERO)
        if not qty:
            return self.recipe_cost() if self.homemade else ZERO
        return sum((r[1] * r[2] for r in rows), ZERO) / qty

    def fifo_price(self, need):
        """Цена за единицу, если взять `need` по FIFO: сначала то, что пришло раньше."""
        rows = self.lots()
        if not rows or need <= 0:
            return self.avg_price()
        skip, left, cost = self.out_qty(), need, ZERO
        for _date, qty, price in rows:
            if skip >= qty:
                skip -= qty
                continue
            take = min(qty - skip, left)
            skip = ZERO
            cost += take * price
            left -= take
            if not left:
                break
        cost += left * rows[-1][2]  # не хватило приходов: по последней цене
        return cost / need

    def recipe_cost(self, _seen=()):
        """Себестоимость 1 единицы своего полуфабриката по рецепту и текущим ценам."""
        if self.pk in _seen:  # рецепт по кругу: дальше не считаем
            return ZERO
        seen = (*_seen, self.pk)
        product = self.linked_product()
        if product and product.recipe.exists():
            return product.unit_cost()
        total = ZERO
        for r in self.ingredients.select_related("ingredient"):
            x = r.ingredient
            price = x.avg_price() if x.purchased_qty() + x.made_qty() or not x.homemade else x.recipe_cost(seen)
            total += r.qty * price
        return total

    def save(self, *args, **kwargs):
        if self.min_stock is None:
            self.min_stock = 0
        super().save(*args, **kwargs)

    def is_low(self):
        return self.stock() < self.min_stock


class MaterialRecipeItem(models.Model):
    """Рецепт своего полуфабриката: из чего делаем 1 единицу (например, 1 булочку)."""

    material = models.ForeignKey(RawMaterial, on_delete=models.CASCADE, related_name="ingredients")
    ingredient = models.ForeignKey(RawMaterial, on_delete=models.PROTECT, related_name="used_in",
                                   verbose_name=_("Сырьё"))
    qty = models.DecimalField(_("Расход на 1 единицу"), **QTY,
                              help_text=_("В единицах этого сырья: 40 г муки = 0,040 кг"))

    class Meta:
        verbose_name = _("Строка рецепта")
        verbose_name_plural = _("Рецепт (из чего делаем 1 единицу)")
        unique_together = [("material", "ingredient")]

    def __str__(self):
        return f"{self.ingredient.name}: {self.qty}"

    def clean(self):
        if self.material_id and self.material_id == self.ingredient_id:
            raise ValidationError(_("Сырьё не может быть в своём же рецепте."))


def _shortages(lines, times, own_consumptions=None):
    """Чего не хватает на складе, чтобы сделать `times` единиц по рецепту [(сырьё, расход на 1)]."""
    result = []
    for material, per_unit in lines:
        need = per_unit * times
        available = material.stock()
        if own_consumptions is not None:
            available += own_consumptions.filter(material=material).aggregate(s=Sum("qty"))["s"] or ZERO
        if need > available:
            result.append(_("%(name)s: нужно %(need)s, есть %(have)s") % {
                "name": material.name, "need": f"{need:g}", "have": f"{available:g}"})
    return result


class MaterialBatch(models.Model):
    """Сделали свой полуфабрикат в цеху (например, испекли булочки). Списывает сырьё по его рецепту."""

    date = models.DateField(_("Дата"), default=timezone.localdate)
    material = models.ForeignKey(RawMaterial, on_delete=models.PROTECT, related_name="batches",
                                 verbose_name=_("Что сделали"), limit_choices_to={"homemade": True})
    qty = models.DecimalField(_("Сколько сделали"), **QTY)
    unit_cost = models.DecimalField(_("Себестоимость 1 ед."), default=0, editable=False, **MONEY)
    worker = models.ForeignKey("Worker", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="batches", verbose_name=_("Кто сделал"))

    class Meta:
        verbose_name = _("Выпечка / заготовка")
        verbose_name_plural = _("Выпечка / заготовки")
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.material.name} × {self.qty:g} ({self.date:%d.%m.%Y})"

    def total_cost(self):
        return self.unit_cost * self.qty

    def clean(self):
        if not self.material_id or not self.qty:
            return
        recipe = [(r.ingredient, r.qty) for r in self.material.ingredients.select_related("ingredient")]
        if not recipe:
            raise ValidationError(_("У «%(name)s» нет рецепта. Заполните его в карточке сырья.") % {"name": self.material.name})
        shortages = _shortages(recipe, self.qty, self.consumptions if self.pk else None)
        if shortages:
            raise ValidationError(_("Не хватает сырья. ") + "; ".join(shortages))

    @transaction.atomic
    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.consumptions.all().delete()
        unit_cost = ZERO
        for r in self.material.ingredients.select_related("ingredient"):
            price = r.ingredient.fifo_price(r.qty * self.qty)
            unit_cost += r.qty * price
            Consumption.objects.create(batch=self, material=r.ingredient, qty=r.qty * self.qty,
                                       cost=(r.qty * self.qty * price).quantize(CENT))
        unit_cost = unit_cost.quantize(CENT)
        MaterialBatch.objects.filter(pk=self.pk).update(unit_cost=unit_cost)
        self.unit_cost = unit_cost


class Purchase(models.Model):
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="purchases",
                                 verbose_name=_("Поставщик"))
    date = models.DateField(_("Дата"), default=timezone.localdate)
    class Method(models.TextChoices):
        CASH = "cash", _("Наличные")
        CARD = "card", _("Карта")

    paid = models.DecimalField(_("Оплачено сразу"), default=0, **MONEY)
    method = models.CharField(_("Чем оплатили"), max_length=10, choices=Method.choices, default=Method.CASH)
    note = models.CharField(_("Заметка"), max_length=300, blank=True)

    class Meta:
        verbose_name = _("Закупка")
        verbose_name_plural = _("Закупки")
        ordering = ["-date", "-id"]

    def __str__(self):
        return _("Закупка №%(pk)s от %(date)s, %(who)s") % {"pk": self.pk, "date": f"{self.date:%d.%m.%Y}", "who": self.supplier}

    def total(self):
        return _sum(self.items.all(), F("qty") * F("price"))


class PurchaseItem(models.Model):
    purchase = models.ForeignKey(Purchase, on_delete=models.CASCADE, related_name="items")
    material = models.ForeignKey(RawMaterial, on_delete=models.PROTECT, related_name="purchase_items",
                                 verbose_name=_("Сырьё"))
    qty = models.DecimalField(_("Количество"), **QTY)
    price = models.DecimalField(_("Цена"), max_digits=20, decimal_places=6,
                                help_text=_("За то, как считаете: за 1 гр, за 1 кг или за 1 упаковку"))

    class Meta:
        verbose_name = _("Строка закупки")
        verbose_name_plural = _("Строки закупки")

    def before_packs(self):
        """Записано до упаковок: «1» колбасу по 600 гр попала на склад как 1 гр по 30 000 сум.

        Целое число меньше упаковки, единица гр/мл, а цена за 1 гр/мл от 1 000 сум — так не бывает.
        Такую строку показываем упаковками; при сохранении закупки она пересчитается.
        """
        size = self.material.pack_size
        return bool(size and self.material.unit.big_ratio and self.qty < size
                    and self.qty == self.qty.to_integral() and self.price >= 1000)

    def __str__(self):
        return f"{self.material.name}: {self.qty}"


class MaterialWriteOff(models.Model):
    material = models.ForeignKey(RawMaterial, on_delete=models.PROTECT, related_name="writeoffs",
                                 verbose_name=_("Сырьё"))
    date = models.DateField(_("Дата"), default=timezone.localdate)
    qty = models.DecimalField(_("Количество"), **QTY)
    reason = models.CharField(_("Причина"), max_length=300, blank=True,
                              help_text=_("Испортилось, просрочилось, выбросили, или при пересчёте оказалось меньше"))

    class Meta:
        verbose_name = _("Списание сырья")
        verbose_name_plural = _("Списания сырья")
        ordering = ["-date", "-id"]

    def __str__(self):
        return _("Списание %(name)s: %(qty)s") % {"name": self.material.name, "qty": self.qty}


class Product(models.Model):
    name = models.CharField(_("Название"), max_length=200, unique=True)
    price = models.DecimalField(_("Цена для магазинов"), default=0, blank=True, **MONEY,
                                help_text=_("Пусто или 0 = магазинам не продаём (например, котлеты только для бургеров)"))
    material = models.OneToOneField(
        RawMaterial, on_delete=models.SET_NULL, null=True, blank=True, related_name="product",
        limit_choices_to={"homemade": True}, verbose_name=_("Полуфабрикат из списка сырья"),
        help_text=_("Котлеты, булочки: что сделали, приходит на склад сырья и идёт в другие рецепты"))
    shelf_life_days = models.PositiveSmallIntegerField(
        _("Срок годности, дней"), default=5,
        help_text=_("Летом можно уменьшить. По нему бот напоминает о магазинах без заказа"))
    active = models.BooleanField(_("Продаётся"), default=True)

    class Meta:
        verbose_name = _("Продукт")
        verbose_name_plural = _("Продукты")
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.price is None:
            self.price = 0
        super().save(*args, **kwargs)

    def for_sale(self):
        return self.active and self.price > 0

    def unit_cost(self):
        """Себестоимость 1 шт по рецепту и текущим средним ценам сырья."""
        return sum((r.qty * r.material.avg_price() for r in self.recipe.select_related("material")), ZERO)

    def produced_qty(self):
        return self.productions.aggregate(s=Sum("qty"))["s"] or 0

    def shipped_qty(self):
        """Отгружено прямо из цеха (не со склада агента)."""
        return self.shipment_items.filter(shipment__worker=None).aggregate(s=Sum("qty"))["s"] or 0

    def loaded_qty(self):
        """Передано агентам минус сданное обратно."""
        loaded = self.van_items.filter(load__kind=VanLoad.Kind.LOAD).aggregate(s=Sum("qty"))["s"] or 0
        back = self.van_items.filter(load__kind=VanLoad.Kind.UNLOAD).aggregate(s=Sum("qty"))["s"] or 0
        return loaded - back

    def stock(self):
        """Готовая продукция в цеху. Возвраты обратно не приходуются: это еда."""
        if self.material_id:
            # Полуфабрикат: один остаток со складом сырья, там же его тратят рецепты.
            left = self.material.stock()
            return int(left) if left == left.to_integral() else left
        return self.produced_qty() - self.shipped_qty() - self.loaded_qty()


class RecipeItem(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="recipe")
    material = models.ForeignKey(RawMaterial, on_delete=models.PROTECT, related_name="recipe_items",
                                 verbose_name=_("Сырьё"))
    qty = models.DecimalField(_("Расход на 1 шт"), **QTY,
                              help_text=_("В единицах сырья: шт, кг (20 г = 0,020) или л"))

    class Meta:
        verbose_name = _("Строка рецепта")
        verbose_name_plural = _("Рецепт")
        unique_together = [("product", "material")]

    def __str__(self):
        return f"{self.material.name}: {self.qty}"

    def clean(self):
        if self.product_id and self.material_id and self.product.material_id == self.material_id:
            raise ValidationError(_("Продукт не может быть в своём же рецепте."))


class Production(models.Model):
    date = models.DateField(_("Дата"), default=timezone.localdate)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="productions",
                                verbose_name=_("Продукт"))
    qty = models.PositiveIntegerField(_("Количество, шт"))
    unit_cost = models.DecimalField(_("Себестоимость 1 шт"), default=0, editable=False, **MONEY)
    worker = models.ForeignKey("Worker", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="productions", verbose_name=_("Кто сделал"))

    class Meta:
        verbose_name = _("Производство")
        verbose_name_plural = _("Производство")
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.product} × {self.qty} ({self.date:%d.%m.%Y})"

    def total_cost(self):
        return self.unit_cost * self.qty

    def clean(self):
        if not self.product_id or not self.qty:
            return
        recipe = [(r.material, r.qty) for r in self.product.recipe.select_related("material")]
        if not recipe:
            raise ValidationError(_("У продукта нет рецепта. Сначала заполните рецепт."))
        shortages = _shortages(recipe, self.qty, self.consumptions if self.pk else None)
        if shortages:
            raise ValidationError(_("Не хватает сырья. ") + "; ".join(shortages))

    @transaction.atomic
    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Пересчитываем списание сырья по рецепту при каждом сохранении.
        self.consumptions.all().delete()
        unit_cost = ZERO
        for r in self.product.recipe.select_related("material"):
            price = r.material.fifo_price(r.qty * self.qty)
            unit_cost += r.qty * price
            Consumption.objects.create(production=self, material=r.material,
                                       qty=r.qty * self.qty,
                                       cost=(r.qty * self.qty * price).quantize(CENT))
        unit_cost = unit_cost.quantize(CENT)
        Production.objects.filter(pk=self.pk).update(unit_cost=unit_cost)
        self.unit_cost = unit_cost


class Consumption(models.Model):
    """Сколько сырья ушло на партию. Создаётся автоматически."""

    production = models.ForeignKey(Production, on_delete=models.CASCADE, null=True, blank=True,
                                   related_name="consumptions")
    batch = models.ForeignKey(MaterialBatch, on_delete=models.CASCADE, null=True, blank=True,
                              related_name="consumptions")
    material = models.ForeignKey(RawMaterial, on_delete=models.PROTECT, related_name="consumptions",
                                 verbose_name=_("Сырьё"))
    qty = models.DecimalField(_("Количество"), **QTY)
    cost = models.DecimalField(_("Стоимость"), **MONEY)

    class Meta:
        verbose_name = _("Расход сырья")
        verbose_name_plural = _("Расход сырья")


class Shop(models.Model):
    name = models.CharField(_("Название"), max_length=200)
    firm = models.CharField(_("Фирма (юр. название)"), max_length=200, blank=True)
    district = models.CharField(_("Район"), max_length=100)
    address = models.CharField(_("Адрес"), max_length=300, blank=True)
    latitude = models.DecimalField(_("Широта"), max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(_("Долгота"), max_digits=9, decimal_places=6, null=True, blank=True)
    contact = models.CharField(_("Контакт"), max_length=200, blank=True)
    phone = models.CharField(_("Телефон"), max_length=50, blank=True)
    telegram_id = models.BigIntegerField("Telegram ID", null=True, blank=True, unique=True,
                                         help_text=_("Заполнится сам, когда магазин подключит бота"))
    bot_code = models.CharField(_("Код для бота"), max_length=16, unique=True, null=True, blank=True,
                                editable=False)
    active = models.BooleanField(_("Работаем"), default=True)
    archived = models.BooleanField(_("В архиве"), default=False,
                                   help_text=_("Бот не напоминает. Снимется сам, когда магазин закажет или получит товар"))
    snoozed_shipment = models.IntegerField(null=True, blank=True, editable=False)
    reminded_on = models.DateField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = _("Магазин")
        verbose_name_plural = _("Магазины")
        ordering = ["district", "name"]

    def __str__(self):
        return f"{self.name} ({self.district})"

    def save(self, *args, **kwargs):
        if not self.bot_code:
            self.bot_code = secrets.token_hex(4)
        super().save(*args, **kwargs)

    def last_shipment(self):
        return self.shipments.order_by("-date", "-id").first()

    def freshness(self, today=None):
        """Когда последний раз привезли товар и до какого числа он годен. None, если не возили."""
        shipment = self.last_shipment()
        if not shipment:
            return None
        today = today or timezone.localdate()
        lives = [i.product.shelf_life_days for i in shipment.items.select_related("product")]
        expires = shipment.date + timedelta(days=min(lives) if lives else 5)
        return {
            "shipment": shipment,
            "date": shipment.date,
            "days_ago": (today - shipment.date).days,
            "expires": expires,
            "days_left": (expires - today).days,
        }

    def map_url(self):
        if self.latitude is None or self.longitude is None:
            return ""
        return f"https://maps.google.com/?q={self.latitude},{self.longitude}"

    def shipped_total(self):
        return _sum(ShipmentItem.objects.filter(shipment__shop=self), F("qty") * F("price"))

    def returned_total(self):
        return _sum(self.returns.all(), F("qty") * F("price"))

    def paid_total(self):
        return _sum(self.shipments.all(), F("paid")) + _sum(self.payments.all(), F("amount"))

    def debt(self):
        """Сколько магазин должен нам."""
        return self.shipped_total() - self.returned_total() - self.paid_total()


class Shipment(models.Model):
    class Method(models.TextChoices):
        CASH = "cash", _("Наличные")
        CARD = "card", _("Карта")
        CONSIGNMENT = "consign", _("Консигнация")
        DEBT = "debt", _("В долг")

    shop = models.ForeignKey(Shop, on_delete=models.PROTECT, related_name="shipments",
                             verbose_name=_("Магазин"))
    date = models.DateField(_("Дата"), default=timezone.localdate)
    worker = models.ForeignKey("Worker", on_delete=models.PROTECT, null=True, blank=True,
                               related_name="shipments", verbose_name=_("Агент"),
                               help_text=_("Товар списывается с остатка этого агента. Пусто = прямо из цеха"))
    vehicle = models.ForeignKey("Vehicle", on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="shipments", verbose_name=_("Машина"))
    paid = models.DecimalField(_("Оплачено при отгрузке"), default=0, **MONEY,
                               help_text=_("Остаток уходит магазину в долг"))
    method = models.CharField(_("Оплата"), max_length=10, choices=Method.choices, default=Method.CASH,
                              help_text=_("Консигнация: магазин платит за проданное, непроданное оформляется возвратом"))
    note = models.CharField(_("Заметка"), max_length=300, blank=True)

    class Meta:
        verbose_name = _("Отгрузка")
        verbose_name_plural = _("Отгрузки")
        ordering = ["-date", "-id"]

    def __str__(self):
        return _("Отгрузка №%(pk)s от %(date)s, %(who)s") % {"pk": self.pk, "date": f"{self.date:%d.%m.%Y}", "who": self.shop}

    def total(self):
        return _sum(self.items.all(), F("qty") * F("price"))

    def debt(self):
        return self.total() - self.paid

    def save(self, *args, **kwargs):
        adding = self._state.adding
        super().save(*args, **kwargs)
        if adding:
            # Магазин снова получает товар: достаём из архива.
            Shop.objects.filter(pk=self.shop_id, archived=True).update(archived=False)


class ShipmentItem(models.Model):
    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="shipment_items",
                                verbose_name=_("Продукт"))
    qty = models.PositiveIntegerField(_("Количество, шт"))
    price = models.DecimalField(_("Цена"), blank=True, **MONEY,
                                help_text=_("Пусто = цена продукта"))
    unit_cost = models.DecimalField(_("Себестоимость 1 шт"), default=0, editable=False, **MONEY)

    class Meta:
        verbose_name = _("Строка отгрузки")
        verbose_name_plural = _("Строки отгрузки")

    def __str__(self):
        return f"{self.product}: {self.qty}"

    def save(self, *args, **kwargs):
        if self.price is None:
            self.price = self.product.price
        if not self.pk:
            last = self.product.productions.order_by("-date", "-id").first()
            self.unit_cost = last.unit_cost if last else self.product.unit_cost().quantize(CENT)
        super().save(*args, **kwargs)


class Return(models.Model):
    class Reason(models.TextChoices):
        EXPIRED = "expired", _("Просрочено")
        DEFECT = "defect", _("Брак")
        UNSOLD = "unsold", _("Не продалось")
        OTHER = "other", _("Другое")

    shop = models.ForeignKey(Shop, on_delete=models.PROTECT, related_name="returns",
                             verbose_name=_("Магазин"))
    date = models.DateField(_("Дата"), default=timezone.localdate)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="returns",
                                verbose_name=_("Продукт"))
    qty = models.PositiveIntegerField(_("Количество, шт"))
    price = models.DecimalField(_("Цена возврата"), blank=True, **MONEY,
                                help_text=_("На эту сумму уменьшится долг. Пусто = цена продукта"))
    reason = models.CharField(_("Причина"), max_length=10, choices=Reason.choices, default=Reason.EXPIRED)
    worker = models.ForeignKey("Worker", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="returns", verbose_name=_("Кто принял"))

    class Meta:
        verbose_name = _("Возврат")
        verbose_name_plural = _("Возвраты")
        ordering = ["-date", "-id"]

    def __str__(self):
        return _("Возврат %(product)s × %(qty)s от %(shop)s") % {"product": self.product, "qty": self.qty, "shop": self.shop}

    def total(self):
        return self.qty * self.price

    def save(self, *args, **kwargs):
        if self.price is None:
            self.price = self.product.price
        super().save(*args, **kwargs)


class Order(models.Model):
    """Заказ магазина из Telegram-бота."""

    class Status(models.TextChoices):
        NEW = "new", _("Новый")
        CONFIRMED = "confirmed", _("Подтверждён")
        REJECTED = "rejected", _("Отклонён")
        SHIPPED = "shipped", _("Отгружен")

    shop = models.ForeignKey(Shop, on_delete=models.PROTECT, related_name="orders",
                             verbose_name=_("Магазин"))
    created_at = models.DateTimeField(_("Создан"), default=timezone.now)
    status = models.CharField(_("Статус"), max_length=10, choices=Status.choices, default=Status.NEW)
    note = models.CharField(_("Комментарий"), max_length=300, blank=True)
    shipment = models.OneToOneField(Shipment, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="order", verbose_name=_("Отгрузка"))

    class Meta:
        verbose_name = _("Заказ из бота")
        verbose_name_plural = _("Заказы из бота")
        ordering = ["-created_at"]

    def __str__(self):
        return _("Заказ №%(pk)s, %(shop)s") % {"pk": self.pk, "shop": self.shop}

    def total(self):
        return sum((i.qty * i.price for i in self.items.all()), ZERO)

    def save(self, *args, **kwargs):
        adding = self._state.adding
        super().save(*args, **kwargs)
        if adding:
            Shop.objects.filter(pk=self.shop_id, archived=True).update(archived=False)

    @transaction.atomic
    def to_shipment(self):
        """Оформляет подтверждённый заказ как отгрузку (без оплаты, её вносят потом)."""
        if self.shipment_id:
            return self.shipment
        if self.status not in (self.Status.NEW, self.Status.CONFIRMED):
            raise ValidationError(_("Заказ №%(pk)s отклонён, отгрузить нельзя.") % {"pk": self.pk})
        shipment = Shipment.objects.create(shop=self.shop, note=f"По заказу №{self.pk} из бота")
        for item in self.items.all():
            ShipmentItem.objects.create(shipment=shipment, product=item.product,
                                        qty=item.qty, price=item.price)
        self.shipment = shipment
        self.status = self.Status.SHIPPED
        self.save(update_fields=["shipment", "status"])
        return shipment


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="order_items",
                                verbose_name=_("Продукт"))
    qty = models.PositiveIntegerField(_("Количество, шт"))
    price = models.DecimalField(_("Цена"), **MONEY)

    class Meta:
        verbose_name = _("Строка заказа")
        verbose_name_plural = _("Строки заказа")

    def __str__(self):
        return f"{self.product}: {self.qty}"


class Payment(models.Model):
    class Method(models.TextChoices):
        CASH = "cash", _("Наличные")
        CARD = "card", _("Карта")
        TRANSFER = "transfer", _("Перечисление")

    date = models.DateField(_("Дата"), default=timezone.localdate)
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT, null=True, blank=True,
                             related_name="payments", verbose_name=_("От магазина"))
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, null=True, blank=True,
                                 related_name="payments", verbose_name=_("Поставщику"))
    amount = models.DecimalField(_("Сумма"), **MONEY)
    method = models.CharField(_("Способ"), max_length=10, choices=Method.choices, default=Method.CASH)
    worker = models.ForeignKey("Worker", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="payments", verbose_name=_("Кто принял"))
    note = models.CharField(_("Заметка"), max_length=300, blank=True)

    class Meta:
        verbose_name = _("Оплата долга")
        verbose_name_plural = _("Оплаты долгов")
        ordering = ["-date", "-id"]

    def __str__(self):
        who = self.shop or self.supplier
        return _("%(amount)s сум, %(who)s") % {"amount": f"{self.amount:,.0f}", "who": who}

    def clean(self):
        if bool(self.shop_id) == bool(self.supplier_id):
            raise ValidationError(_("Укажите либо магазин, либо поставщика."))


class Expense(models.Model):
    class Category(models.TextChoices):
        SALARY = "salary", _("Зарплата")
        RENT = "rent", _("Аренда")
        UTILITIES = "utilities", _("Свет, газ, вода")
        FUEL = "fuel", _("Бензин, солярка")
        TAXI = "taxi", _("Такси")
        FOOD = "food", _("Обед")
        REPAIR = "repair", _("Ремонт")
        OTHER = "other", _("Другое")

    date = models.DateField(_("Дата"), default=timezone.localdate)
    category = models.CharField(_("Статья"), max_length=10, choices=Category.choices)
    amount = models.DecimalField(_("Сумма"), **MONEY)
    note = models.CharField(_("Заметка"), max_length=300, blank=True)
    worker = models.ForeignKey("Worker", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="expenses", verbose_name=_("Сотрудник"))

    class Meta:
        verbose_name = _("Прочий расход")
        verbose_name_plural = _("Прочие расходы")
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.get_category_display()}: {self.amount:,.0f}"


class Vehicle(models.Model):
    name = models.CharField(_("Машина"), max_length=100, unique=True,
                            help_text=_("Например: Damas 01 A 123 BC"))
    active = models.BooleanField(_("Работает"), default=True)

    class Meta:
        verbose_name = _("Машина")
        verbose_name_plural = _("Машины")
        ordering = ["name"]

    def __str__(self):
        return self.name


class Worker(models.Model):
    class Role(models.TextChoices):
        BOSS = "boss", _("Начальник")
        AGENT = "agent", _("Торговый агент")
        VANSELLER = "van", _("Ванселлер")
        COOK = "cook", _("Повар")
        BAKER = "baker", _("Пекарь")
        WORKSHOP = "workshop", _("Сотрудник цеха")

    SELLERS = (Role.AGENT, Role.VANSELLER)
    WORKSHOP_ROLES = (Role.COOK, Role.BAKER, Role.WORKSHOP)

    name = models.CharField(_("Имя"), max_length=200)
    role = models.CharField(_("Роль"), max_length=10, choices=Role.choices, default=Role.AGENT)
    phone = models.CharField(_("Телефон"), max_length=50, blank=True)
    salary = models.DecimalField(_("Оклад в месяц"), default=0, **MONEY,
                                 help_text=_("Фиксированная зарплата. Выдачу вносите в «Расход» → «Зарплата»"))
    districts = models.CharField(_("Районы"), max_length=300, blank=True,
                                 help_text=_("Для агентов. Через запятую. Его магазины и напоминания по ним"))
    shops = models.ManyToManyField("Shop", blank=True, related_name="agents", verbose_name=_("Магазины"),
                                   help_text=_("Для агентов. Если пусто, бот покажет все магазины его районов"))
    vehicle = models.ForeignKey(Vehicle, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="workers", verbose_name=_("Машина"))
    telegram_id = models.BigIntegerField("Telegram ID", null=True, blank=True, unique=True,
                                         help_text=_("Заполнится сам, когда сотрудник откроет ссылку"))
    bot_code = models.CharField(_("Код для бота"), max_length=16, unique=True, null=True, blank=True,
                                editable=False)
    active = models.BooleanField(_("Работает"), default=True)

    class Meta:
        verbose_name = _("Сотрудник")
        verbose_name_plural = _("Сотрудники")
        ordering = ["name"]

    def __str__(self):
        return self.name

    def district_list(self):
        return [d.strip().lower() for d in self.districts.split(",") if d.strip()]

    def is_seller(self):
        return self.role in self.SELLERS

    def is_workshop(self):
        return self.role in self.WORKSHOP_ROLES

    def salary_paid(self, month=None):
        """Сколько зарплаты выдали за месяц (по умолчанию за текущий)."""
        month = month or timezone.localdate().replace(day=1)
        nxt = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
        paid = self.expenses.filter(category=Expense.Category.SALARY, date__gte=month, date__lt=nxt)
        return paid.aggregate(s=Sum("amount"))["s"] or ZERO

    def stock(self):
        """{product: qty} — что сейчас на руках у агента."""
        result = defaultdict(int)
        for row in VanLoadItem.objects.filter(load__worker=self).values("product", "load__kind").annotate(
                q=Sum("qty")):
            sign = 1 if row["load__kind"] == VanLoad.Kind.LOAD else -1
            result[row["product"]] += sign * row["q"]
        for row in ShipmentItem.objects.filter(shipment__worker=self).values("product").annotate(q=Sum("qty")):
            result[row["product"]] -= row["q"]
        products = Product.objects.in_bulk([k for k, v in result.items() if v])
        return {products[k]: v for k, v in result.items() if v}

    def cash_on(self, day):
        """Сколько денег агент получил от магазинов за день: при отгрузке и за старые долги."""
        return (_sum(self.shipments.filter(date=day), F("paid"))
                + _sum(self.payments.filter(date=day, shop__isnull=False), F("amount")))

    def save(self, *args, **kwargs):
        if not self.bot_code:
            # Буква w отличает код сотрудника от кода магазина (там только 0-9a-f).
            self.bot_code = "w" + secrets.token_hex(4)
        super().save(*args, **kwargs)


class VanLoad(models.Model):
    """Цех передаёт товар агенту или агент сдаёт остаток обратно в цех."""

    class Kind(models.TextChoices):
        LOAD = "load", _("Передача агенту")
        UNLOAD = "unload", _("Сдача остатка в цех")

    worker = models.ForeignKey(Worker, on_delete=models.PROTECT, null=True, related_name="loads",
                               verbose_name=_("Агент"))
    given_by = models.ForeignKey(Worker, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="handovers", verbose_name=_("Кто передал"))
    vehicle = models.ForeignKey(Vehicle, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="loads", verbose_name=_("Машина"))
    kind = models.CharField(_("Что"), max_length=10, choices=Kind.choices, default=Kind.LOAD)
    date = models.DateField(_("Дата"), default=timezone.localdate)

    class Meta:
        verbose_name = _("Передача агенту")
        verbose_name_plural = _("Передачи агентам")
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.worker}, {self.date:%d.%m.%Y}"


class VanLoadItem(models.Model):
    load = models.ForeignKey(VanLoad, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="van_items",
                                verbose_name=_("Продукт"))
    qty = models.PositiveIntegerField(_("Количество, шт"))

    class Meta:
        verbose_name = _("Строка передачи")
        verbose_name_plural = _("Продукты")

    def __str__(self):
        return f"{self.product}: {self.qty}"


class WorkDay(models.Model):
    """Табель: вышел сотрудник на работу или нет."""

    class Status(models.TextChoices):
        WORKED = "worked", _("Работал")
        OFF = "off", _("Выходной")
        SICK = "sick", _("Болел")
        ABSENT = "absent", _("Не вышел")

    worker = models.ForeignKey(Worker, on_delete=models.CASCADE, related_name="workdays", verbose_name=_("Сотрудник"))
    date = models.DateField(_("Дата"), default=timezone.localdate)
    status = models.CharField(_("Статус"), max_length=10, choices=Status.choices, default=Status.WORKED)
    note = models.CharField(_("Заметка"), max_length=200, blank=True)

    class Meta:
        verbose_name = _("День в табеле")
        verbose_name_plural = _("Табель")
        ordering = ["-date", "worker__name"]
        unique_together = [("worker", "date")]

    def __str__(self):
        return f"{self.worker} {self.date:%d.%m}: {self.get_status_display()}"


class BotUser(models.Model):
    """Язык, который человек выбрал в боте. Один на Telegram-аккаунт: владелец, сотрудник или магазин."""

    class Language(models.TextChoices):
        RU = "ru", "Русский"
        UZ = "uz", "Oʻzbekcha"

    telegram_id = models.BigIntegerField(unique=True)
    language = models.CharField(_("Язык"), max_length=2, choices=Language.choices, default=Language.RU)

    class Meta:
        verbose_name = _("Язык в боте")
        verbose_name_plural = _("Языки в боте")

    def __str__(self):
        return f"{self.telegram_id}: {self.language}"
