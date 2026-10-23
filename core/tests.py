from decimal import Decimal as D

from django.core.exceptions import ValidationError
from django.test import TestCase

from . import models as m


class AccountingTests(TestCase):
    def setUp(self):
        self.supplier = m.Supplier.objects.create(name="Оптовик")
        self.bun = m.RawMaterial.objects.create(name="Булочка")
        self.sausage = m.RawMaterial.objects.create(name="Сосиска")
        self.ketchup = m.RawMaterial.objects.create(name="Кетчуп", unit="kg")
        purchase = m.Purchase.objects.create(supplier=self.supplier, paid=D("100000"))
        m.PurchaseItem.objects.create(purchase=purchase, material=self.bun, qty=100, price=1500)
        m.PurchaseItem.objects.create(purchase=purchase, material=self.sausage, qty=100, price=3500)
        m.PurchaseItem.objects.create(purchase=purchase, material=self.ketchup, qty=2, price=30000)

        self.hotdog = m.Product.objects.create(name="Хот-дог", price=12000)
        m.RecipeItem.objects.create(product=self.hotdog, material=self.bun, qty=1)
        m.RecipeItem.objects.create(product=self.hotdog, material=self.sausage, qty=1)
        m.RecipeItem.objects.create(product=self.hotdog, material=self.ketchup, qty=D("0.020"))
        self.shop = m.Shop.objects.create(name="Нур", district="Чиланзар")

    def test_avg_price_is_weighted(self):
        purchase = m.Purchase.objects.create(supplier=self.supplier)
        m.PurchaseItem.objects.create(purchase=purchase, material=self.bun, qty=100, price=2500)
        self.assertEqual(self.bun.avg_price(), D("2000"))

    def test_unit_cost_from_recipe(self):
        # 1500 + 3500 + 0.02 * 30000
        self.assertEqual(self.hotdog.unit_cost(), D("5600"))

    def test_production_consumes_materials(self):
        p = m.Production(product=self.hotdog, qty=40)
        p.full_clean()
        p.save()
        self.assertEqual(self.bun.stock(), 60)
        self.assertEqual(self.ketchup.stock(), D("1.2"))
        self.assertEqual(p.unit_cost, D("5600"))
        self.assertEqual(self.hotdog.stock(), 40)

        p.qty = 50
        p.full_clean()
        p.save()
        self.assertEqual(self.bun.stock(), 50)

    def test_production_blocked_when_not_enough_materials(self):
        p = m.Production(product=self.hotdog, qty=101)
        with self.assertRaisesMessage(ValidationError, "Булочка"):
            p.full_clean()

    def test_shop_debt(self):
        shipment = m.Shipment.objects.create(shop=self.shop, paid=D("200000"))
        m.ShipmentItem.objects.create(shipment=shipment, product=self.hotdog, qty=50)
        self.assertEqual(shipment.total(), D("600000"))
        m.Return.objects.create(shop=self.shop, product=self.hotdog, qty=5)
        m.Payment.objects.create(shop=self.shop, amount=D("100000"))
        # 600 000 - 60 000 - 200 000 - 100 000
        self.assertEqual(self.shop.debt(), D("240000"))

    def test_supplier_debt(self):
        # 150 000 + 350 000 + 60 000 - 100 000 paid
        self.assertEqual(self.supplier.debt(), D("460000"))
        m.Payment.objects.create(supplier=self.supplier, amount=D("60000"))
        self.assertEqual(self.supplier.debt(), D("400000"))

    def test_payment_needs_exactly_one_party(self):
        with self.assertRaises(ValidationError):
            m.Payment(amount=1).clean()
        with self.assertRaises(ValidationError):
            m.Payment(amount=1, shop=self.shop, supplier=self.supplier).clean()
