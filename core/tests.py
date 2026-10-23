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


class DashboardTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from django.core.management import call_command

        call_command("demo_data", stdout=open("/dev/null", "w"))
        self.user = User.objects.create_superuser("owner", password="x")

    def test_requires_login(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_numbers(self):
        from . import reports

        data = reports.build("all")
        # Нур: 60 × 12 000 + 40 × 18 000, Барака: 50 × 12 000
        self.assertEqual(data["revenue"], D("2040000"))
        self.assertEqual(data["returned"], D("48000"))
        self.assertEqual(data["sold_qty"], 150)
        # Нур 892 000 + Барака 600 000
        self.assertEqual(data["receivable"], D("1492000"))
        cogs = 110 * D("6525") + 40 * D("9050")
        self.assertEqual(data["cogs"], cogs)
        self.assertEqual(data["profit"], D("2040000") - D("48000") - cogs)

    def test_page_renders_for_each_period(self):
        self.client.force_login(self.user)
        for period in ["today", "week", "month", "all", "bogus"]:
            response = self.client.get("/", {"period": period})
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Нур (пример)")
        response = self.client.get("/admin/")
        self.assertContains(response, "Дашборд")


class FormatTests(TestCase):
    def test_filters(self):
        from .templatetags.fmt import money, qty

        self.assertEqual(money(D("1234567.6")), "1 234 568")
        self.assertEqual(qty(D("11.500")), "11,5")
        self.assertEqual(qty(D("3000.000")), "3 000")


class BotLogicTests(TestCase):
    def setUp(self):
        self.shop = m.Shop.objects.create(name="Нур", district="Чиланзар")
        self.hotdog = m.Product.objects.create(name="Хот-дог", price=12000)
        self.burger = m.Product.objects.create(name="Бургер", price=18000)

    def test_shop_gets_code_and_links(self):
        from . import botlogic

        self.assertEqual(len(self.shop.bot_code), 8)
        self.assertIsNone(botlogic.link_shop("wrong", 111))
        self.assertEqual(botlogic.link_shop(self.shop.bot_code, 111), self.shop)
        self.assertEqual(botlogic.shop_for(111), self.shop)
        # Тот же аккаунт переподключили к другому магазину.
        other = m.Shop.objects.create(name="Барака", district="Юнусабад")
        botlogic.link_shop(other.bot_code, 111)
        self.assertEqual(botlogic.shop_for(111), other)

    def test_order_flow(self):
        from . import botlogic

        cart = {self.hotdog.pk: 30, self.burger.pk: 40}
        self.assertIn("Итого: 1 080 000 сум", botlogic.cart_text(cart))
        order = botlogic.create_order(self.shop, cart)
        self.assertEqual(order.total(), D("1080000"))
        self.assertIn("Статус: Новый", botlogic.order_text(order))

        self.assertEqual(botlogic.set_status(order.pk, m.Order.Status.CONFIRMED), order)
        self.assertIsNone(botlogic.set_status(order.pk, m.Order.Status.REJECTED))

        shipment = order.to_shipment()
        self.assertEqual(shipment.total(), D("1080000"))
        self.assertEqual(order.to_shipment(), shipment)
        order.refresh_from_db()
        self.assertEqual(order.status, m.Order.Status.SHIPPED)
        self.assertEqual(botlogic.debt_text(self.shop), "Ваш долг: 1 080 000 сум.")

    def test_rejected_order_cannot_ship(self):
        from . import botlogic

        order = botlogic.create_order(self.shop, {self.hotdog.pk: 5})
        botlogic.set_status(order.pk, m.Order.Status.REJECTED)
        order.refresh_from_db()
        with self.assertRaises(ValidationError):
            order.to_shipment()

    def test_owner_ids_and_summary(self):
        import os
        from unittest import mock

        from . import botlogic

        with mock.patch.dict(os.environ, {"OWNER_TELEGRAM_IDS": "5, 7"}):
            self.assertTrue(botlogic.is_owner(7))
            self.assertFalse(botlogic.is_owner(8))
        botlogic.create_order(self.shop, {self.hotdog.pk: 5})
        self.assertIn("Новых заказов: 1", botlogic.summary_text())
