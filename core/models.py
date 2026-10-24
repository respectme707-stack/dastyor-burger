import secrets
from collections import defaultdict
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


class RawMaterial(models.Model):
    class Unit(models.TextChoices):
        PIECE = "pc", "шт"
        KG = "kg", "кг"
        LITER = "l", "л"

    name = models.CharField("Название", max_length=200, unique=True)
    unit = models.CharField("Единица", max_length=4, choices=Unit.choices, default=Unit.PIECE)
    min_stock = models.DecimalField("Мин. остаток", default=0, **QTY,
                                    help_text="Ниже этого остатка будет предупреждение")

    class Meta:
        verbose_name = "Сырьё"
        verbose_name_plural = "Сырьё"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.get_unit_display()})"

    def purchased_qty(self):
        return self.purchase_items.aggregate(s=Sum("qty"))["s"] or ZERO

    def used_qty(self):
        return self.consumptions.aggregate(s=Sum("qty"))["s"] or ZERO

    def written_off_qty(self):
        return self.writeoffs.aggregate(s=Sum("qty"))["s"] or ZERO

    def stock(self):
        return self.purchased_qty() - self.used_qty() - self.written_off_qty()

    def avg_price(self):
        """Средневзвешенная цена за единицу по всем закупкам."""
        qty = self.purchased_qty()
        if not qty:
            return ZERO
        return _sum(self.purchase_items.all(), F("qty") * F("price")) / qty

    def is_low(self):
        return self.stock() < self.min_stock


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
        """Отгружено прямо из цеха, без машины."""
        return self.shipment_items.filter(shipment__vehicle=None).aggregate(s=Sum("qty"))["s"] or 0

    def loaded_qty(self):
        """Уехало из цеха в машины минус сданное обратно."""
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
        recipe = list(self.product.recipe.select_related("material"))
        if not recipe:
            raise ValidationError("У продукта нет рецепта. Сначала заполните рецепт.")
        shortages = []
        for r in recipe:
            need = r.qty * self.qty
            available = r.material.stock()
            if self.pk:
                available += self.consumptions.filter(material=r.material).aggregate(
                    s=Sum("qty"))["s"] or ZERO
            if need > available:
                shortages.append(f"{r.material.name}: нужно {need:g}, есть {available:g}")
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

    production = models.ForeignKey(Production, on_delete=models.CASCADE, related_name="consumptions")
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
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT, related_name="shipments",
                             verbose_name="Магазин")
    date = models.DateField("Дата", default=timezone.localdate)
    vehicle = models.ForeignKey("Vehicle", on_delete=models.PROTECT, null=True, blank=True,
                                related_name="shipments", verbose_name="С машины",
                                help_text="Пусто = отгрузка прямо из цеха")
    worker = models.ForeignKey("Worker", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="shipments", verbose_name="Сотрудник")
    paid = models.DecimalField("Оплачено при отгрузке", default=0, **MONEY,
                               help_text="Остаток уходит магазину в долг")
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
        FUEL = "fuel", "Бензин, доставка"
        OTHER = "other", "Другое"

    date = models.DateField("Дата", default=timezone.localdate)
    category = models.CharField("Статья", max_length=10, choices=Category.choices)
    amount = models.DecimalField("Сумма", **MONEY)
    note = models.CharField("Заметка", max_length=300, blank=True)

    class Meta:
        verbose_name = "Расход цеха"
        verbose_name_plural = "Расходы цеха"
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

    def stock(self):
        """{product: qty} — что сейчас лежит в машине."""
        result = defaultdict(int)
        for row in VanLoadItem.objects.filter(load__vehicle=self).values("product", "load__kind").annotate(
                q=Sum("qty")):
            sign = 1 if row["load__kind"] == VanLoad.Kind.LOAD else -1
            result[row["product"]] += sign * row["q"]
        for row in ShipmentItem.objects.filter(shipment__vehicle=self).values("product").annotate(q=Sum("qty")):
            result[row["product"]] -= row["q"]
        products = Product.objects.in_bulk([k for k, v in result.items() if v])
        return {products[k]: v for k, v in result.items() if v}

    def cash_on(self, day):
        """Сколько денег собрано с магазинов за день."""
        return _sum(self.shipments.filter(date=day), F("paid"))


class Worker(models.Model):
    name = models.CharField("Имя", max_length=200)
    phone = models.CharField("Телефон", max_length=50, blank=True)
    vehicle = models.ForeignKey(Vehicle, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="workers", verbose_name="Обычно ездит на",
                                help_text="Бот будет сразу выбирать эту машину")
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

    def save(self, *args, **kwargs):
        if not self.bot_code:
            # Буква w отличает код сотрудника от кода магазина (там только 0-9a-f).
            self.bot_code = "w" + secrets.token_hex(4)
        super().save(*args, **kwargs)


class VanLoad(models.Model):
    """Загрузка машины в цеху или сдача остатка обратно в цех."""

    class Kind(models.TextChoices):
        LOAD = "load", "Загрузка в машину"
        UNLOAD = "unload", "Сдача остатка в цех"

    vehicle = models.ForeignKey(Vehicle, on_delete=models.PROTECT, related_name="loads",
                                verbose_name="Машина")
    worker = models.ForeignKey(Worker, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="loads", verbose_name="Сотрудник")
    kind = models.CharField("Что", max_length=10, choices=Kind.choices, default=Kind.LOAD)
    date = models.DateField("Дата", default=timezone.localdate)

    class Meta:
        verbose_name = "Загрузка машины"
        verbose_name_plural = "Загрузки машин"
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.vehicle}, {self.date:%d.%m.%Y}"


class VanLoadItem(models.Model):
    load = models.ForeignKey(VanLoad, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="van_items",
                                verbose_name="Продукт")
    qty = models.PositiveIntegerField("Количество, шт")

    class Meta:
        verbose_name = "Строка загрузки"
        verbose_name_plural = "Продукты"

    def __str__(self):
        return f"{self.product}: {self.qty}"
