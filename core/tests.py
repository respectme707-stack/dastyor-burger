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
        with mock.patch.dict(os.environ, {"OWNER_TELEGRAM_IDS": "YourID:409928788"}):
            self.assertEqual(botlogic.owner_ids(), {409928788})
        botlogic.create_order(self.shop, {self.hotdog.pk: 5})
        self.assertIn("Новых заказов: 1", botlogic.summary_text())


class StaffBotTests(TestCase):
    def setUp(self):
        from . import botlogic

        self.logic = botlogic
        supplier = m.Supplier.objects.create(name="Оптовик")
        bun = m.RawMaterial.objects.create(name="Булочка")
        purchase = m.Purchase.objects.create(supplier=supplier)
        m.PurchaseItem.objects.create(purchase=purchase, material=bun, qty=500, price=1500)
        self.hotdog = m.Product.objects.create(name="Хот-дог", price=12000)
        m.RecipeItem.objects.create(product=self.hotdog, material=bun, qty=1)
        m.Production.objects.create(product=self.hotdog, qty=100)
        self.van = m.Vehicle.objects.create(name="Damas 1")
        self.worker = m.Worker.objects.create(name="Азиз", vehicle=self.van)
        self.shop = m.Shop.objects.create(name="Нур", district="Чиланзар")

    def test_numbers(self):
        p = self.logic.parse_number
        self.assertEqual(p("1 500,5"), D("1500.5"))
        self.assertIsNone(p("abc"))
        self.assertIsNone(p("0"))
        self.assertEqual(p("0", allow_zero=True), 0)
        self.assertIsNone(p("-3"))
        self.assertIsNone(p("nan"))
        self.assertEqual(self.logic.parse_count("40"), 40)
        self.assertIsNone(self.logic.parse_count("2,5"))

    def test_worker_link(self):
        self.assertTrue(self.worker.bot_code.startswith("w"))
        self.assertIsNone(self.logic.link_worker(self.shop.bot_code, 5))
        self.assertEqual(self.logic.link_worker(self.worker.bot_code, 5), self.worker)
        self.assertTrue(self.logic.is_staff(5))
        self.assertFalse(self.logic.is_staff(6))

    def test_create_shop_with_location(self):
        shop = self.logic.create_shop({"name": "Барака", "firm": "OOO Baraka", "district": "Юнусабад",
                                       "address": "ул. Амира Темура 1", "phone": "+998901234567",
                                       "lat": 41.3456789, "lon": 69.2845})
        self.assertEqual(shop.latitude, D("41.345679"))
        self.assertIn("maps.google.com/?q=41.345679,69.284500", self.logic.shop_card(shop))
        self.assertEqual(self.logic.districts(), ["Чиланзар", "Юнусабад"])

    def test_purchase(self):
        mayo = self.logic.get_or_create_material("Майонез", "kg")
        supplier = self.logic.get_or_create_supplier("Новый склад")
        lines = [[mayo.pk, "10", "35000"]]
        text, total = self.logic.purchase_lines_text(lines)
        self.assertEqual(total, D("350000"))
        purchase = self.logic.create_purchase(supplier.pk, lines, D("100000"))
        self.assertEqual(mayo.stock(), 10)
        self.assertEqual(supplier.debt(), D("250000"))
        self.assertIn("в долг 250\xa0000", self.logic.purchase_text(purchase))

    def test_van_flow(self):
        logic = self.logic
        with self.assertRaisesMessage(ValidationError, "в цеху 100"):
            logic.create_van_load(self.van, self.worker, {self.hotdog.pk: 150})
        logic.create_van_load(self.van, self.worker, {self.hotdog.pk: 60})
        self.assertEqual(self.hotdog.stock(), 40)
        self.assertEqual(self.van.stock(), {self.hotdog: 60})

        with self.assertRaisesMessage(ValidationError, "в машине 60"):
            logic.create_van_shipment(self.van, self.shop, {self.hotdog.pk: 61}, 0)
        order = logic.create_order(self.shop, {self.hotdog.pk: 20})
        self.assertEqual(logic.open_order(self.shop), order)
        shipment = logic.create_van_shipment(self.van, self.shop, {self.hotdog.pk: 20}, D("100000"),
                                             self.worker, order.pk)
        order.refresh_from_db()
        self.assertEqual(order.status, m.Order.Status.SHIPPED)
        self.assertIsNone(logic.open_order(self.shop))
        self.assertEqual(self.shop.debt(), D("140000"))
        self.assertIn("в долг 140\xa0000", logic.shipment_text(shipment))
        # Отгрузка с машины не трогает остаток цеха.
        self.assertEqual(self.hotdog.stock(), 40)
        self.assertEqual(self.van.stock(), {self.hotdog: 40})
        self.assertIn("Собрано денег сегодня: 100\xa0000", logic.van_text(self.van))

        logic.create_return(self.shop, self.hotdog.pk, 2, m.Return.Reason.DEFECT)
        self.assertEqual(self.shop.debt(), D("116000"))

        logic.unload_all(self.van, self.worker)
        self.assertEqual(self.van.stock(), {})
        self.assertEqual(self.hotdog.stock(), 80)
        self.assertIsNone(logic.unload_all(self.van, self.worker))

    def test_dashboard_shows_vehicles(self):
        from . import reports

        self.logic.create_van_load(self.van, None, {self.hotdog.pk: 30})
        self.logic.create_van_shipment(self.van, self.shop, {self.hotdog.pk: 10}, D("50000"))
        data = reports.build("all")
        self.assertEqual(data["vehicles"][0]["cash"], D("50000"))
        self.assertEqual(data["vehicles"][0]["stock"], "Хот-дог 20")
        self.assertEqual(data["products"][0]["in_vans"], 20)
        self.assertEqual(data["products"][0]["stock"], 70)


class ShelfLifeTests(TestCase):
    def setUp(self):
        from datetime import date

        from . import botlogic

        self.logic = botlogic
        self.day0 = date(2026, 10, 19)
        self.hotdog = m.Product.objects.create(name="Хот-дог", price=12000, shelf_life_days=5)
        self.burger = m.Product.objects.create(name="Бургер", price=18000, shelf_life_days=6)
        self.shop = m.Shop.objects.create(name="Нур", district="Чиланзар", phone="+998901112233")
        self.other = m.Shop.objects.create(name="Барака", district="Юнусабад")
        shipment = m.Shipment.objects.create(shop=self.shop, date=self.day0)
        m.ShipmentItem.objects.create(shipment=shipment, product=self.hotdog, qty=10)
        m.ShipmentItem.objects.create(shipment=shipment, product=self.burger, qty=10)
        self.shipment = shipment

    def due(self, day):
        from datetime import timedelta

        return [(s.name, expired) for s, _, expired in self.logic.due_reminders(self.day0 + timedelta(days=day))]

    def test_freshness_uses_shortest_shelf_life(self):
        info = self.shop.freshness(self.day0)
        self.assertEqual(info["expires"].day, 24)
        self.assertEqual(info["days_left"], 5)

    def test_reminders_on_days_3_to_5_then_expired(self):
        self.assertEqual(self.due(2), [])
        self.assertEqual(self.due(3), [("Нур", False)])
        self.assertEqual(self.due(5), [("Нур", False)])
        self.assertEqual(self.due(6), [("Нур", True)])
        text = self.logic.reminder_text(self.shop, self.shop.freshness(self.day0.replace(day=25)))
        self.assertIn("истёк 24.10", text)
        self.assertIn("+998901112233", text)

    def test_reminded_once_a_day(self):
        from datetime import timedelta

        day = self.day0 + timedelta(days=3)
        self.logic.mark_reminded(self.shop, day)
        self.assertEqual(self.due(3), [])
        self.assertEqual(self.due(4), [("Нур", False)])

    def test_new_order_stops_reminders(self):
        self.logic.create_order(self.shop, {self.hotdog.pk: 5})
        self.assertEqual(self.due(4), [])

    def test_snooze_and_archive(self):
        self.logic.snooze_shop(self.shop.pk)
        self.assertEqual(self.due(6), [])
        # Новая поставка снова включает напоминания.
        new = m.Shipment.objects.create(shop=self.shop, date=self.day0.replace(day=25))
        m.ShipmentItem.objects.create(shipment=new, product=self.hotdog, qty=1)
        self.assertEqual(self.due(10), [("Нур", False)])

        self.logic.archive_shop(self.shop.pk)
        self.assertEqual(self.due(12), [])
        self.logic.create_order(self.shop, {self.hotdog.pk: 5})
        self.shop.refresh_from_db()
        self.assertFalse(self.shop.archived)

    def test_recipients_by_district(self):
        import os
        from unittest import mock

        m.Worker.objects.create(name="Азиз", telegram_id=7, districts="чиланзар, Сергели")
        m.Worker.objects.create(name="Бобур", telegram_id=8, districts="Юнусабад")
        m.Worker.objects.create(name="Без бота", districts="Чиланзар")
        with mock.patch.dict(os.environ, {"OWNER_TELEGRAM_IDS": "1"}):
            self.assertEqual(self.logic.reminder_recipients(self.shop), {1, 7})

    def test_dashboard_freshness_and_ratings(self):
        from unittest import mock

        from . import reports

        m.Return.objects.create(shop=self.shop, product=self.hotdog, qty=2, date=self.day0)
        with mock.patch("django.utils.timezone.localdate", return_value=self.day0.replace(day=24)):
            data = reports.build("all")
        row = data["freshness"][0]
        self.assertEqual((row["shop"], row["days_left"], row["level"], row["visits"]), (self.shop, 0, "bad", 1))
        self.assertEqual(data["expiring"][0]["shop"], self.shop)
        self.assertEqual(data["top_shops"][0]["shop"], self.shop)
        self.assertEqual(data["top_products"][0]["product"], self.burger)
        self.assertEqual(data["returners"][0]["qty"], 2)
        self.assertEqual(data["chart_visits"], [1])
