import secrets
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import DecimalField, ExpressionWrapper, F, Sum
from django.utils import timezone

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
    name = models.CharField("Название", max_length=200, unique=True)
    phone = models.CharField("Телефон", max_length=50, blank=True)
    note = models.TextField("Заметка", blank=True)

    class Meta:
        verbose_name = "Поставщик"
        verbose_name_plural = "Поставщики"
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
    name = models.CharField("Название", max_length=100, unique=True,
                            help_text="Например: Бургер, Булочки, Упаковка")

    class Meta:
        verbose_name = "Категория сырья"
        verbose_name_plural = "Категории сырья"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Unit(models.Model):
    name = models.CharField("Единица", max_length=20, unique=True, help_text="Например: шт, кг, гр, л, пачка")

    class Meta:
        verbose_name = "Единица измерения"
        verbose_name_plural = "Единицы измерения"
        ordering = ["name"]

    def __str__(self):
        return self.name


def default_unit():
    return Unit.objects.get_or_create(name="шт")[0].pk


class RawMaterial(models.Model):
    name = models.CharField("Название", max_length=200, unique=True)
    category = models.ForeignKey(MaterialCategory, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="materials", verbose_name="Категория")
    unit = models.ForeignKey(Unit, on_delete=models.PROTECT, default=default_unit, related_name="materials",
                             verbose_name="Единица")
    min_stock = models.DecimalField("Мин. остаток", default=0, blank=True, **QTY,
                                    help_text="Необязательно. Если остаток станет меньше, сайт предупредит")
    homemade = models.BooleanField("Делаем сами", default=False,
                                   help_text="Например, булочки: не покупаем, а печём в цеху по своему рецепту")

    class Meta:
        verbose_name = "Сырьё"
        verbose_name_plural = "Сырьё"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.unit})"

    def purchased_qty(self):
        return self.purchase_items.aggregate(s=Sum("qty"))["s"] or ZERO

    def made_qty(self):
        return self.batches.aggregate(s=Sum("qty"))["s"] or ZERO

    def used_qty(self):
        return self.consumptions.aggregate(s=Sum("qty"))["s"] or ZERO

    def written_off_qty(self):
        return self.writeoffs.aggregate(s=Sum("qty"))["s"] or ZERO

    def stock(self):
        return self.purchased_qty() + self.made_qty() - self.used_qty() - self.written_off_qty()

    def avg_price(self):
        """Средневзвешенная цена за единицу: по закупкам и по тому, что сделали сами."""
        qty = self.purchased_qty() + self.made_qty()
        if not qty:
            return self.recipe_cost() if self.homemade else ZERO
        cost = _sum(self.purchase_items.all(), F("qty") * F("price")) + _sum(self.batches.all(),
                                                                              F("qty") * F("unit_cost"))
        return cost / qty

    def recipe_cost(self, _seen=()):
        """Себестоимость 1 единицы своего полуфабриката по рецепту и текущим ценам."""
        if self.pk in _seen:  # рецепт по кругу: дальше не считаем
            return ZERO
        seen = (*_seen, self.pk)
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
                                   verbose_name="Сырьё")
    qty = models.DecimalField("Расход на 1 единицу", **QTY,
                              help_text="В единицах этого сырья: 40 г муки = 0,040 кг")

    class Meta:
        verbose_name = "Строка рецепта"
        verbose_name_plural = "Рецепт (из чего делаем 1 единицу)"
        unique_together = [("material", "ingredient")]

    def __str__(self):
        return f"{self.ingredient.name}: {self.qty}"

    def clean(self):
        if self.material_id and self.material_id == self.ingredient_id:
            raise ValidationError("Сырьё не может быть в своём же рецепте.")


def _shortages(lines, times, own_consumptions=None):
    """Чего не хватает на складе, чтобы сделать `times` единиц по рецепту [(сырьё, расход на 1)]."""
    result = []
    for material, per_unit in lines:
        need = per_unit * times
        available = material.stock()
        if own_consumptions is not None:
            available += own_consumptions.filter(material=material).aggregate(s=Sum("qty"))["s"] or ZERO
        if need > available:
            result.append(f"{material.name}: нужно {need:g}, есть {available:g}")
    return result


class MaterialBatch(models.Model):
    """Сделали свой полуфабрикат в цеху (например, испекли булочки). Списывает сырьё по его рецепту."""

    date = models.DateField("Дата", default=timezone.localdate)
    material = models.ForeignKey(RawMaterial, on_delete=models.PROTECT, related_name="batches",
                                 verbose_name="Что сделали", limit_choices_to={"homemade": True})
    qty = models.DecimalField("Сколько сделали", **QTY)
    unit_cost = models.DecimalField("Себестоимость 1 ед.", default=0, editable=False, **MONEY)
    worker = models.ForeignKey("Worker", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="batches", verbose_name="Кто сделал")

    class Meta:
        verbose_name = "Выпечка / заготовка"
        verbose_name_plural = "Выпечка / заготовки"
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
            raise ValidationError(f"У «{self.material.name}» нет рецепта. Заполните его в карточке сырья.")
        shortages = _shortages(recipe, self.qty, self.consumptions if self.pk else None)
        if shortages:
            raise ValidationError("Не хватает сырья. " + "; ".join(shortages))

    @transaction.atomic
    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.consumptions.all().delete()
        unit_cost = ZERO
        for r in self.material.ingredients.select_related("ingredient"):
            price = r.ingredient.avg_price()
            unit_cost += r.qty * price
            Consumption.objects.create(batch=self, material=r.ingredient, qty=r.qty * self.qty,
                                       cost=(r.qty * self.qty * price).quantize(CENT))
        unit_cost = unit_cost.quantize(CENT)
        MaterialBatch.objects.filter(pk=self.pk).update(unit_cost=unit_cost)
        self.unit_cost = unit_cost


class Purchase(models.Model):
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="purchases",
                                 verbose_name="Поставщик")
    date = models.DateField("Дата", default=timezone.localdate)
    paid = models.DecimalField("Оплачено сразу", default=0, **MONEY)
    note = models.CharField("Заметка", max_length=300, blank=True)

    class Meta:
        verbose_name = "Закупка"
        verbose_name_plural = "Закупки"
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"Закупка №{self.pk} от {self.date:%d.%m.%Y}, {self.supplier}"

    def total(self):
        return _sum(self.items.all(), F("qty") * F("price"))


class PurchaseItem(models.Model):
    purchase = models.ForeignKey(Purchase, on_delete=models.CASCADE, related_name="items")
    material = models.ForeignKey(RawMaterial, on_delete=models.PROTECT, related_name="purchase_items",
                                 verbose_name="Сырьё")
    qty = models.DecimalField("Количество", **QTY)
    price = models.DecimalField("Цена за единицу", **MONEY)

    class Meta:
        verbose_name = "Строка закупки"
        verbose_name_plural = "Строки закупки"

    def __str__(self):
        return f"{self.material.name}: {self.qty}"


class MaterialWriteOff(models.Model):
    material = models.ForeignKey(RawMaterial, on_delete=models.PROTECT, related_name="writeoffs",
                                 verbose_name="Сырьё")
    date = models.DateField("Дата", default=timezone.localdate)
    qty = models.DecimalField("Количество", **QTY)
    reason = models.CharField("Причина", max_length=300, blank=True,
                              help_text="Испорчено, просрочено, инвентаризация")

    class Meta:
        verbose_name = "Списание сырья"
        verbose_name_plural = "Списания сырья"
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"Списание {self.material.name}: {self.qty}"


class Product(models.Model):
    name = models.CharField("Название", max_length=200, unique=True)
    price = models.DecimalField("Цена для магазинов", **MONEY)
    shelf_life_days = models.PositiveSmallIntegerField(
        "Срок годности, дней", default=5,
        help_text="Летом можно уменьшить. По нему бот напоминает о магазинах без заказа")
    active = models.BooleanField("Продаётся", default=True)

    class Meta:
        verbose_name = "Продукт"
        verbose_name_plural = "Продукты"
        ordering = ["name"]

    def __str__(self):
        return self.name

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
        return self.produced_qty() - self.shipped_qty() - self.loaded_qty()


class RecipeItem(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="recipe")
    material = models.ForeignKey(RawMaterial, on_delete=models.PROTECT, related_name="recipe_items",
                                 verbose_name="Сырьё")
    qty = models.DecimalField("Расход на 1 шт", **QTY,
                              help_text="В единицах сырья: шт, кг (20 г = 0,020) или л")

    class Meta:
        verbose_name = "Строка рецепта"
        verbose_name_plural = "Рецепт"
        unique_together = [("product", "material")]

    def __str__(self):
        return f"{self.material.name}: {self.qty}"


class Production(models.Model):
    date = models.DateField("Дата", default=timezone.localdate)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="productions",
                                verbose_name="Продукт")
    qty = models.PositiveIntegerField("Количество, шт")
    unit_cost = models.DecimalField("Себестоимость 1 шт", default=0, editable=False, **MONEY)
    worker = models.ForeignKey("Worker", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="productions", verbose_name="Кто сделал")

    class Meta:
        verbose_name = "Производство"
        verbose_name_plural = "Производство"
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
            raise ValidationError("У продукта нет рецепта. Сначала заполните рецепт.")
        shortages = _shortages(recipe, self.qty, self.consumptions if self.pk else None)
        if shortages:
            raise ValidationError("Не хватает сырья. " + "; ".join(shortages))

    @transaction.atomic
    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Пересчитываем списание сырья по рецепту при каждом сохранении.
        self.consumptions.all().delete()
        unit_cost = ZERO
        for r in self.product.recipe.select_related("material"):
            price = r.material.avg_price()
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
                                 verbose_name="Сырьё")
    qty = models.DecimalField("Количество", **QTY)
    cost = models.DecimalField("Стоимость", **MONEY)

    class Meta:
        verbose_name = "Расход сырья"
        verbose_name_plural = "Расход сырья"


class Shop(models.Model):
    name = models.CharField("Название", max_length=200)
    firm = models.CharField("Фирма (юр. название)", max_length=200, blank=True)
    district = models.CharField("Район", max_length=100)
    address = models.CharField("Адрес", max_length=300, blank=True)
    latitude = models.DecimalField("Широта", max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField("Долгота", max_digits=9, decimal_places=6, null=True, blank=True)
    contact = models.CharField("Контакт", max_length=200, blank=True)
    phone = models.CharField("Телефон", max_length=50, blank=True)
    telegram_id = models.BigIntegerField("Telegram ID", null=True, blank=True, unique=True,
                                         help_text="Заполнится сам, когда магазин подключит бота")
    bot_code = models.CharField("Код для бота", max_length=16, unique=True, null=True, blank=True,
                                editable=False)
    active = models.BooleanField("Работаем", default=True)
    archived = models.BooleanField("В архиве", default=False,
                                   help_text="Бот не напоминает. Снимется сам, когда магазин закажет или получит товар")
    snoozed_shipment = models.IntegerField(null=True, blank=True, editable=False)
    reminded_on = models.DateField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "Магазин"
        verbose_name_plural = "Магазины"
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
        CASH = "cash", "Наличные"
        CARD = "card", "Карта"
        CONSIGNMENT = "consign", "Консигнация"
        DEBT = "debt", "В долг"

    shop = models.ForeignKey(Shop, on_delete=models.PROTECT, related_name="shipments",
                             verbose_name="Магазин")
    date = models.DateField("Дата", default=timezone.localdate)
    worker = models.ForeignKey("Worker", on_delete=models.PROTECT, null=True, blank=True,
                               related_name="shipments", verbose_name="Агент",
                               help_text="Товар списывается с остатка этого агента. Пусто = прямо из цеха")
    vehicle = models.ForeignKey("Vehicle", on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="shipments", verbose_name="Машина")
    paid = models.DecimalField("Оплачено при отгрузке", default=0, **MONEY,
                               help_text="Остаток уходит магазину в долг")
    method = models.CharField("Оплата", max_length=10, choices=Method.choices, default=Method.CASH,
                              help_text="Консигнация: магазин платит за проданное, непроданное оформляется возвратом")
    note = models.CharField("Заметка", max_length=300, blank=True)

    class Meta:
        verbose_name = "Отгрузка"
        verbose_name_plural = "Отгрузки"
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"Отгрузка №{self.pk} от {self.date:%d.%m.%Y}, {self.shop}"

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
                                verbose_name="Продукт")
    qty = models.PositiveIntegerField("Количество, шт")
    price = models.DecimalField("Цена", blank=True, **MONEY,
                                help_text="Пусто = цена продукта")
    unit_cost = models.DecimalField("Себестоимость 1 шт", default=0, editable=False, **MONEY)

    class Meta:
        verbose_name = "Строка отгрузки"
        verbose_name_plural = "Строки отгрузки"

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
        EXPIRED = "expired", "Просрочено"
        DEFECT = "defect", "Брак"
        UNSOLD = "unsold", "Не продалось"
        OTHER = "other", "Другое"

    shop = models.ForeignKey(Shop, on_delete=models.PROTECT, related_name="returns",
                             verbose_name="Магазин")
    date = models.DateField("Дата", default=timezone.localdate)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="returns",
                                verbose_name="Продукт")
    qty = models.PositiveIntegerField("Количество, шт")
    price = models.DecimalField("Цена возврата", blank=True, **MONEY,
                                help_text="На эту сумму уменьшится долг. Пусто = цена продукта")
    reason = models.CharField("Причина", max_length=10, choices=Reason.choices, default=Reason.EXPIRED)

    class Meta:
        verbose_name = "Возврат"
        verbose_name_plural = "Возвраты"
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"Возврат {self.product} × {self.qty} от {self.shop}"

    def total(self):
        return self.qty * self.price

    def save(self, *args, **kwargs):
        if self.price is None:
            self.price = self.product.price
        super().save(*args, **kwargs)


class Order(models.Model):
    """Заказ магазина из Telegram-бота."""

    class Status(models.TextChoices):
        NEW = "new", "Новый"
        CONFIRMED = "confirmed", "Подтверждён"
        REJECTED = "rejected", "Отклонён"
        SHIPPED = "shipped", "Отгружен"

    shop = models.ForeignKey(Shop, on_delete=models.PROTECT, related_name="orders",
                             verbose_name="Магазин")
    created_at = models.DateTimeField("Создан", default=timezone.now)
    status = models.CharField("Статус", max_length=10, choices=Status.choices, default=Status.NEW)
    note = models.CharField("Комментарий", max_length=300, blank=True)
    shipment = models.OneToOneField(Shipment, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="order", verbose_name="Отгрузка")

    class Meta:
        verbose_name = "Заказ из бота"
        verbose_name_plural = "Заказы из бота"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Заказ №{self.pk}, {self.shop}"

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
            raise ValidationError(f"Заказ №{self.pk} отклонён, отгрузить нельзя.")
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
                                verbose_name="Продукт")
    qty = models.PositiveIntegerField("Количество, шт")
    price = models.DecimalField("Цена", **MONEY)

    class Meta:
        verbose_name = "Строка заказа"
        verbose_name_plural = "Строки заказа"

    def __str__(self):
        return f"{self.product}: {self.qty}"


class Payment(models.Model):
    class Method(models.TextChoices):
        CASH = "cash", "Наличные"
        CARD = "card", "Карта"
        TRANSFER = "transfer", "Перечисление"

    date = models.DateField("Дата", default=timezone.localdate)
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT, null=True, blank=True,
                             related_name="payments", verbose_name="От магазина")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, null=True, blank=True,
                                 related_name="payments", verbose_name="Поставщику")
    amount = models.DecimalField("Сумма", **MONEY)
    method = models.CharField("Способ", max_length=10, choices=Method.choices, default=Method.CASH)
    worker = models.ForeignKey("Worker", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="payments", verbose_name="Кто принял")
    note = models.CharField("Заметка", max_length=300, blank=True)

    class Meta:
        verbose_name = "Оплата долга"
        verbose_name_plural = "Оплаты долгов"
        ordering = ["-date", "-id"]

    def __str__(self):
        who = self.shop or self.supplier
        return f"{self.amount:,.0f} сум, {who}"

    def clean(self):
        if bool(self.shop_id) == bool(self.supplier_id):
            raise ValidationError("Укажите либо магазин, либо поставщика.")


class Expense(models.Model):
    class Category(models.TextChoices):
        SALARY = "salary", "Зарплата"
        RENT = "rent", "Аренда"
        UTILITIES = "utilities", "Свет, газ, вода"
        FUEL = "fuel", "Бензин, солярка"
        TAXI = "taxi", "Такси"
        FOOD = "food", "Обед"
        REPAIR = "repair", "Ремонт"
        OTHER = "other", "Другое"

    date = models.DateField("Дата", default=timezone.localdate)
    category = models.CharField("Статья", max_length=10, choices=Category.choices)
    amount = models.DecimalField("Сумма", **MONEY)
    note = models.CharField("Заметка", max_length=300, blank=True)
    worker = models.ForeignKey("Worker", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="expenses", verbose_name="Сотрудник")

    class Meta:
        verbose_name = "Прочий расход"
        verbose_name_plural = "Прочие расходы"
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.get_category_display()}: {self.amount:,.0f}"


class Vehicle(models.Model):
    name = models.CharField("Машина", max_length=100, unique=True,
                            help_text="Например: Damas 01 A 123 BC")
    active = models.BooleanField("Работает", default=True)

    class Meta:
        verbose_name = "Машина"
        verbose_name_plural = "Машины"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Worker(models.Model):
    class Role(models.TextChoices):
        BOSS = "boss", "Начальник"
        AGENT = "agent", "Торговый агент"
        VANSELLER = "van", "Ванселлер"
        COOK = "cook", "Повар"
        BAKER = "baker", "Пекарь"
        WORKSHOP = "workshop", "Сотрудник цеха"

    SELLERS = (Role.AGENT, Role.VANSELLER)
    WORKSHOP_ROLES = (Role.COOK, Role.BAKER, Role.WORKSHOP)

    name = models.CharField("Имя", max_length=200)
    role = models.CharField("Роль", max_length=10, choices=Role.choices, default=Role.AGENT)
    phone = models.CharField("Телефон", max_length=50, blank=True)
    districts = models.CharField("Районы", max_length=300, blank=True,
                                 help_text="Для агентов. Через запятую. Его магазины и напоминания по ним")
    shops = models.ManyToManyField("Shop", blank=True, related_name="agents", verbose_name="Магазины",
                                   help_text="Для агентов. Если пусто, бот покажет все магазины его районов")
    vehicle = models.ForeignKey(Vehicle, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="workers", verbose_name="Машина")
    telegram_id = models.BigIntegerField("Telegram ID", null=True, blank=True, unique=True,
                                         help_text="Заполнится сам, когда сотрудник откроет ссылку")
    bot_code = models.CharField("Код для бота", max_length=16, unique=True, null=True, blank=True,
                                editable=False)
    active = models.BooleanField("Работает", default=True)

    class Meta:
        verbose_name = "Сотрудник"
        verbose_name_plural = "Сотрудники"
        ordering = ["name"]

    def __str__(self):
        return self.name

    def district_list(self):
        return [d.strip().lower() for d in self.districts.split(",") if d.strip()]

    def is_seller(self):
        return self.role in self.SELLERS

    def is_workshop(self):
        return self.role in self.WORKSHOP_ROLES

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
        LOAD = "load", "Передача агенту"
        UNLOAD = "unload", "Сдача остатка в цех"

    worker = models.ForeignKey(Worker, on_delete=models.PROTECT, null=True, related_name="loads",
                               verbose_name="Агент")
    given_by = models.ForeignKey(Worker, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="handovers", verbose_name="Кто передал")
    vehicle = models.ForeignKey(Vehicle, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="loads", verbose_name="Машина")
    kind = models.CharField("Что", max_length=10, choices=Kind.choices, default=Kind.LOAD)
    date = models.DateField("Дата", default=timezone.localdate)

    class Meta:
        verbose_name = "Передача агенту"
        verbose_name_plural = "Передачи агентам"
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.worker}, {self.date:%d.%m.%Y}"


class VanLoadItem(models.Model):
    load = models.ForeignKey(VanLoad, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="van_items",
                                verbose_name="Продукт")
    qty = models.PositiveIntegerField("Количество, шт")

    class Meta:
        verbose_name = "Строка передачи"
        verbose_name_plural = "Продукты"

    def __str__(self):
        return f"{self.product}: {self.qty}"
