from decimal import Decimal as D

from django.core.management.base import BaseCommand
from django.db import transaction

from core import models as m


class Command(BaseCommand):
    help = "Заполняет пустую базу примерными данными, чтобы посмотреть, как всё работает."

    @transaction.atomic
    def handle(self, *args, **options):
        if m.Product.objects.exists():
            self.stdout.write("В базе уже есть данные, ничего не делаю.")
            return
        supplier = m.Supplier.objects.create(name="Оптовый склад (пример)")
        bun = m.RawMaterial.objects.create(name="Булочка", min_stock=200)
        sausage = m.RawMaterial.objects.create(name="Сосиска", min_stock=200)
        cutlet = m.RawMaterial.objects.create(name="Котлета", min_stock=100)
        ketchup = m.RawMaterial.objects.create(name="Кетчуп", unit="kg", min_stock=5)
        mayo = m.RawMaterial.objects.create(name="Майонез", unit="kg", min_stock=5)
        box = m.RawMaterial.objects.create(name="Тара", min_stock=300)

        purchase = m.Purchase.objects.create(supplier=supplier, paid=D("3000000"))
        for material, qty, price in [
            (bun, 1000, 1500), (sausage, 600, 3500), (cutlet, 400, 6000),
            (ketchup, 20, 30000), (mayo, 20, 35000), (box, 1000, 400),
        ]:
            m.PurchaseItem.objects.create(purchase=purchase, material=material, qty=qty, price=price)

        hotdog = m.Product.objects.create(name="Хот-дог", price=12000)
        burger = m.Product.objects.create(name="Бургер", price=18000)
        for product, material, qty in [
            (hotdog, bun, 1), (hotdog, sausage, 1), (hotdog, ketchup, D("0.020")),
            (hotdog, mayo, D("0.015")), (hotdog, box, 1),
            (burger, bun, 1), (burger, cutlet, 1), (burger, ketchup, D("0.015")),
            (burger, mayo, D("0.020")), (burger, box, 1),
        ]:
            m.RecipeItem.objects.create(product=product, material=material, qty=qty)

        m.Production.objects.create(product=hotdog, qty=300)
        m.Production.objects.create(product=burger, qty=200)

        nur = m.Shop.objects.create(name="Нур (пример)", district="Чиланзар")
        baraka = m.Shop.objects.create(name="Барака (пример)", district="Юнусабад")
        s1 = m.Shipment.objects.create(shop=nur, paid=D("500000"))
        m.ShipmentItem.objects.create(shipment=s1, product=hotdog, qty=60)
        m.ShipmentItem.objects.create(shipment=s1, product=burger, qty=40)
        s2 = m.Shipment.objects.create(shop=baraka, paid=0)
        m.ShipmentItem.objects.create(shipment=s2, product=hotdog, qty=50)
        m.Return.objects.create(shop=nur, product=hotdog, qty=4)
        self.stdout.write(self.style.SUCCESS("Примерные данные добавлены."))
