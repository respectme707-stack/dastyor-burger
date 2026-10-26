from decimal import Decimal as D

from django.core.exceptions import ValidationError
from django.test import TestCase

from . import models as m


class AccountingTests(TestCase):
    def setUp(self):
        self.supplier = m.Supplier.objects.create(name="Оптовик")
        self.bun = m.RawMaterial.objects.create(name="Булочка")
        self.sausage = m.RawMaterial.objects.create(name="Сосиска")
        self.ketchup = m.RawMaterial.objects.create(name="Кетчуп", unit=m.Unit.objects.get_or_create(name="кг")[0])
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
        self.assertContains(response, "Внести данные")

    def test_all_pages_render(self):
        self.client.force_login(self.user)
        agent = m.Worker.objects.create(name="Азиз", role=m.Worker.Role.AGENT)
        load = m.VanLoad.objects.create(worker=agent)
        m.VanLoadItem.objects.create(load=load, product=m.Product.objects.first(), qty=5)
        m.Expense.objects.create(category=m.Expense.Category.FUEL, amount=1000, worker=agent)
        pages = ["/money/", "/shops/", "/products/", "/staff/", "/stock/", "/entry/", "/settings/",
                 "/admin/core/shipment/add/", "/admin/core/materialbatch/add/", "/admin/core/rawmaterial/add/",
                 "/admin/core/materialcategory/add/"]
        pages += [f"/shops/{s.pk}/" for s in m.Shop.objects.all()]
        pages += [f"/products/{p.pk}/" for p in m.Product.objects.all()]
        pages += [f"/staff/{w.pk}/" for w in m.Worker.objects.all()]
        for url in pages:
            for period in ["today", "all"]:
                response = self.client.get(url, {"period": period})
                self.assertEqual(response.status_code, 200, url)
        self.assertContains(self.client.get("/shops/"), "Нур (пример)")
        self.assertContains(self.client.get("/entry/"), "Приход сырья")
        self.assertEqual(self.client.get("/shops/999999/").status_code, 404)

    def test_staff_and_money_numbers(self):
        from . import reports

        money = reports.money("all")
        self.assertEqual(money["receivable"], D("1492000"))
        detail = reports.shop_detail(m.Shop.objects.get(name="Нур (пример)"), "all")
        self.assertEqual(detail["sales"], D("1440000"))
        self.assertEqual(detail["debt"], D("892000"))
        self.assertTrue(reports.recent(5))


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
        self.assertIn("Заказы из бота: новых 1", botlogic.summary_text())


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
        self.worker = m.Worker.objects.create(name="Азиз", vehicle=self.van, role=m.Worker.Role.AGENT)
        self.cook = m.Worker.objects.create(name="Шерзод", role=m.Worker.Role.COOK)
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
        mayo = self.logic.get_or_create_material("Майонез", "кг")
        supplier = self.logic.get_or_create_supplier("Новый склад")
        lines = [[mayo.pk, "10", "35000"]]
        text, total = self.logic.purchase_lines_text(lines)
        self.assertEqual(total, D("350000"))
        purchase = self.logic.create_purchase(supplier.pk, lines, D("100000"))
        self.assertEqual(mayo.stock(), 10)
        self.assertEqual(supplier.debt(), D("250000"))
        self.assertIn("в долг 250\xa0000", self.logic.purchase_text(purchase))

    def test_agent_flow(self):
        logic = self.logic
        with self.assertRaisesMessage(ValidationError, "в цеху 100"):
            logic.create_handover(self.worker, self.cook, {self.hotdog.pk: 150})
        load = logic.create_handover(self.worker, self.cook, {self.hotdog.pk: 60})
        self.assertEqual((load.given_by, load.vehicle), (self.cook, self.van))
        self.assertEqual(self.hotdog.stock(), 40)
        self.assertEqual(self.worker.stock(), {self.hotdog: 60})

        with self.assertRaisesMessage(ValidationError, "у вас 60"):
            logic.create_shipment(self.shop, {self.hotdog.pk: 61}, 0, self.worker)
        order = logic.create_order(self.shop, {self.hotdog.pk: 20})
        self.assertEqual(logic.open_order(self.shop), order)
        shipment = logic.create_shipment(self.shop, {self.hotdog.pk: 20}, D("100000"), self.worker, order.pk)
        order.refresh_from_db()
        self.assertEqual(order.status, m.Order.Status.SHIPPED)
        self.assertIsNone(logic.open_order(self.shop))
        self.assertEqual(self.shop.debt(), D("140000"))
        self.assertIn("в долг 140\xa0000", logic.shipment_text(shipment))
        # Отгрузка агента не трогает остаток цеха.
        self.assertEqual(self.hotdog.stock(), 40)
        self.assertEqual(self.worker.stock(), {self.hotdog: 40})

        logic.create_payment(self.shop, D("40000"), self.worker)
        self.assertEqual(self.shop.debt(), D("100000"))
        self.assertIn("Получено денег сегодня: 140\xa0000", logic.agent_stock_text(self.worker))

        logic.create_return(self.shop, self.hotdog.pk, 2, m.Return.Reason.DEFECT)
        self.assertEqual(self.shop.debt(), D("76000"))

        # Начальник отгружает прямо из цеха.
        logic.create_shipment(self.shop, {self.hotdog.pk: 10}, D("120000"))
        self.assertEqual(self.hotdog.stock(), 30)

        logic.unload_all(self.worker, self.cook)
        self.assertEqual(self.worker.stock(), {})
        self.assertEqual(self.hotdog.stock(), 70)
        self.assertIsNone(logic.unload_all(self.worker))

    def test_roles(self):
        import os
        from unittest import mock

        logic = self.logic
        m.Worker.objects.filter(pk=self.worker.pk).update(telegram_id=11)
        m.Worker.objects.filter(pk=self.cook.pk).update(telegram_id=12)
        boss = m.Worker.objects.create(name="Ойбек", role=m.Worker.Role.BOSS, telegram_id=13)
        with mock.patch.dict(os.environ, {"OWNER_TELEGRAM_IDS": "1"}):
            self.assertEqual(logic.role_of(1), ("boss", None))
            self.assertEqual(logic.role_of(11)[0], "seller")
            self.assertEqual(logic.role_of(12)[0], "workshop")
            self.assertEqual(logic.role_of(13), ("boss", boss))
            self.assertEqual(logic.role_of(99), (None, None))
            self.assertEqual(logic.boss_ids(), {1, 13})
        self.assertEqual(logic.sellers(), [self.worker])

    def test_agent_shops(self):
        logic = self.logic
        other = m.Shop.objects.create(name="Барака", district="Юнусабад")
        self.assertEqual(len(logic.shops_for_worker(self.worker)), 2)
        self.worker.districts = "юнусабад"
        self.worker.save()
        self.assertEqual(logic.shops_for_worker(self.worker), [other])
        self.worker.shops.add(self.shop)
        self.assertEqual(logic.shops_for_worker(self.worker), [self.shop])
        self.assertEqual(logic.districts(self.worker), ["Чиланзар"])
        # Новый магазин агента сразу прикрепляется к нему.
        shop = logic.create_shop({"name": "Ок", "district": "Чиланзар"}, self.worker)
        self.assertIn(shop, self.worker.shops.all())

    def test_categories(self):
        logic = self.logic
        buns = logic.get_or_create_category("Булочки")
        flour = logic.get_or_create_material("Мука", "гр", buns.pk)
        self.assertEqual(str(flour.unit), "гр")
        self.assertEqual(flour.category, buns)
        self.assertEqual(logic.materials(buns.pk), [flour])
        self.assertEqual([x.name for x in logic.materials()], ["Булочка"])

    def test_production_and_expense(self):
        logic = self.logic
        production = logic.create_production(self.hotdog.pk, 50, self.cook)
        self.assertIn("В цеху теперь: 150", logic.production_text(production))
        with self.assertRaisesMessage(ValidationError, "Не хватает сырья"):
            logic.create_production(self.hotdog.pk, 1000, self.cook)
        logic.create_expense("taxi", D("30000"), "до Чиланзара", self.worker)
        self.assertEqual(self.worker.expenses.get().amount, D("30000"))

    def test_worker_card_and_summary(self):
        logic = self.logic
        logic.create_handover(self.worker, self.cook, {self.hotdog.pk: 30})
        logic.create_shipment(self.shop, {self.hotdog.pk: 10}, D("50000"), self.worker)
        logic.create_expense("fuel", D("80000"), "", self.worker)
        card = logic.worker_card(self.worker)
        self.assertIn("Получил из цеха: 30 шт", card)
        self.assertIn("Продал: 10 шт на 120\xa0000", card)
        self.assertIn("Отдал в долг: 70\xa0000", card)
        self.assertIn("Сейчас на руках: Хот-дог 20", card)
        self.assertIn("Расходы: 80\xa0000", card)
        self.assertIn("Передал агентам: 30 шт", logic.worker_card(self.cook))

        text = logic.summary_text()
        self.assertIn("Продажи: 120\xa0000", text)
        self.assertIn("Получено денег: 50\xa0000", text)
        self.assertIn("Прочие расходы: 80\xa0000", text)
        self.assertIn("в цеху 70 · у агентов 20", text)
        self.assertIn("Азиз: продал на 120\xa0000", logic.daily_report_text())

    def test_payment_methods(self):
        logic = self.logic
        card = m.Shipment.Method.CARD
        shipment = logic.create_shipment(self.shop, {self.hotdog.pk: 5}, D("60000"), None, None, "", card)
        self.assertIn("картой 60\xa0000", logic.summary_text())
        logic.create_shipment(self.shop, {self.hotdog.pk: 5}, 0, None, None, "", m.Shipment.Method.CONSIGNMENT)
        self.assertIn("На консигнации за период: 60\xa0000", logic.summary_text())
        payment = logic.create_payment(self.shop, D("10000"), None, m.Payment.Method.CARD)
        self.assertEqual((shipment.method, payment.method), (card, "card"))
        self.assertIn("картой 70\xa0000", logic.summary_text())

    def test_parse_location(self):
        p = self.logic.parse_location
        self.assertEqual(p("https://www.google.com/maps/place/X/@41.311081,69.240562,17z"), (41.311081, 69.240562))
        self.assertEqual(p("41.31, 69.24"), (41.31, 69.24))
        self.assertEqual(p("https://yandex.uz/maps/?ll=69.240562%2C41.311081&z=16"), (41.311081, 69.240562))
        self.assertIsNone(p("1"))
        self.assertIsNone(p("Пропустить"))
        self.assertIsNone(self.logic.resolve_location("1"))

    def test_dashboard_shows_agents(self):
        from . import reports

        self.logic.create_handover(self.worker, None, {self.hotdog.pk: 30})
        self.logic.create_shipment(self.shop, {self.hotdog.pk: 10}, D("50000"), self.worker)
        data = reports.build("all")
        self.assertEqual(data["agents"][0]["cash"], D("50000"))
        self.assertEqual(data["agents"][0]["stock"], "Хот-дог 20")
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


class HomemadeTests(TestCase):
    """Своя булочка: печём из муки, потом идёт в рецепт хот-дога."""

    def setUp(self):
        kg = m.Unit.objects.get_or_create(name="кг")[0]
        supplier = m.Supplier.objects.create(name="Мука-опт")
        self.flour = m.RawMaterial.objects.create(name="Мука", unit=kg)
        self.sausage = m.RawMaterial.objects.create(name="Сосиска", unit=kg)
        purchase = m.Purchase.objects.create(supplier=supplier)
        m.PurchaseItem.objects.create(purchase=purchase, material=self.flour, qty=10, price=8000)
        m.PurchaseItem.objects.create(purchase=purchase, material=self.sausage, qty=5, price=60000)
        self.bun = m.RawMaterial.objects.create(name="Булочка 90 г", homemade=True)
        m.MaterialRecipeItem.objects.create(material=self.bun, ingredient=self.flour, qty=D("0.060"))
        self.hotdog = m.Product.objects.create(name="Хот-дог", price=12000)
        m.RecipeItem.objects.create(product=self.hotdog, material=self.bun, qty=1)
        m.RecipeItem.objects.create(product=self.hotdog, material=self.sausage, qty=D("0.050"))

    def test_cost_before_baking_uses_bun_recipe(self):
        # булочка 0,06 × 8000 = 480, сосиска 0,05 × 60 000 = 3000
        self.assertEqual(self.bun.avg_price(), D("480"))
        self.assertEqual(self.hotdog.unit_cost(), D("3480"))

    def test_baking_and_production_chain(self):
        batch = m.MaterialBatch(material=self.bun, qty=100)
        batch.full_clean()
        batch.save()
        self.assertEqual(batch.unit_cost, D("480"))
        self.assertEqual(self.flour.stock(), 4)
        self.assertEqual(self.bun.stock(), 100)

        p = m.Production(product=self.hotdog, qty=40)
        p.full_clean()
        p.save()
        self.assertEqual(self.bun.stock(), 60)
        self.assertEqual(p.unit_cost, D("3480"))

    def test_baking_blocked_without_flour(self):
        with self.assertRaisesMessage(ValidationError, "Мука"):
            m.MaterialBatch(material=self.bun, qty=1000).full_clean()

    def test_min_stock_optional(self):
        x = m.RawMaterial(name="Соль", min_stock=None)
        x.save()
        self.assertEqual(x.min_stock, 0)


class FeedbackTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User

        self.user = User.objects.create_superuser("owner", password="x")
        self.client.force_login(self.user)
        self.gram = m.Unit.objects.get(name="гр")  # крупную единицу (кг) заполняет миграция
        self.assertEqual(self.gram.big_ratio, 1000)
        self.mayo = m.RawMaterial.objects.create(name="Майонез", unit=self.gram)
        self.supplier = m.Supplier.objects.create(name="Бозор")

    def test_purchase_without_pack_is_in_material_units(self):
        data = {
            "supplier": self.supplier.pk, "date": "25.10.2026", "paid": "20000", "note": "",
            "items-TOTAL_FORMS": "1", "items-INITIAL_FORMS": "0", "items-MIN_NUM_FORMS": "0",
            "items-MAX_NUM_FORMS": "1000",
            "items-0-material": self.mayo.pk, "items-0-qty": "2000", "items-0-price": "36",
        }
        self.assertEqual(self.client.post("/admin/core/purchase/add/", data).status_code, 302)
        self.assertEqual(self.mayo.stock(), 2000)
        self.assertEqual(self.supplier.debt(), D("52000"))

    def test_staff_filters_and_ratings(self):
        from . import reports

        a = m.Worker.objects.create(name="Азиз", role=m.Worker.Role.AGENT)
        m.Worker.objects.create(name="Гулноза", role=m.Worker.Role.WORKSHOP)
        m.WorkDay.objects.create(worker=a)
        data = reports.staff("month", role=m.Worker.Role.AGENT)
        self.assertEqual([r["worker"].name for r in data["rows"]], ["Азиз"])
        self.assertEqual(data["rows"][0]["days"], 1)
        self.assertEqual(data["ratings"]["days"][0]["worker"], a)
        response = self.client.get("/staff/", {"q": "гул"})
        self.assertContains(response, "Гулноза")
        self.assertNotContains(response, "Азиз</a>")
        self.assertContains(self.client.get("/"), "Рейтинг сотрудников")


    def test_purchase_in_packs(self):
        self.mayo.pack_name, self.mayo.pack_size = "банка", 900
        self.mayo.save()
        data = {
            "supplier": self.supplier.pk, "date": "25.10.2026", "paid": "0", "note": "",
            "items-TOTAL_FORMS": "1", "items-INITIAL_FORMS": "0", "items-MIN_NUM_FORMS": "0",
            "items-MAX_NUM_FORMS": "1000",
            "items-0-material": self.mayo.pk, "items-0-qty": "2", "items-0-price": "16800",
        }
        response = self.client.post("/admin/core/purchase/add/", data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.mayo.stock(), 1800)
        self.assertEqual(round(self.supplier.debt()), 33600)
        # 11,25 г майонеза на хот-дог: 16 800 / 900 × 11,25 = 210 сум
        self.assertEqual(round(self.mayo.avg_price() * D("11.25")), 210)

        # Открыли закупку снова: видно упаковками, как вводили
        purchase = m.Purchase.objects.get()
        page = self.client.get(f"/admin/core/purchase/{purchase.pk}/change/").content.decode()
        self.assertIn('name="items-0-qty" value="2"', page)
        self.assertIn('name="items-0-price" value="16800"', page)
        # Бот тоже считает упаковками
        from . import botlogic
        botlogic.create_purchase(self.supplier.pk, [[self.mayo.pk, "1", "16800"]], 0)
        self.assertEqual(self.mayo.stock(), 2700)
        self.assertEqual(self.mayo.pack_label(), "банка по 900 гр")

    def _resave(self, purchase, item, qty, price):
        data = {
            "supplier": self.supplier.pk, "date": "25.10.2026", "paid": "0", "note": "",
            "items-TOTAL_FORMS": "1", "items-INITIAL_FORMS": "1", "items-MIN_NUM_FORMS": "0",
            "items-MAX_NUM_FORMS": "1000", "items-0-id": item.pk, "items-0-purchase": purchase.pk,
            "items-0-material": self.mayo.pk, "items-0-qty": qty, "items-0-price": price,
        }
        response = self.client.post(f"/admin/core/purchase/{purchase.pk}/change/", data)
        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()

    def test_old_purchase_written_before_packs(self):
        # До упаковок записали «1» колбасу за 30 000 — на склад попал 1 гр по 30 000
        purchase = m.Purchase.objects.create(supplier=self.supplier)
        item = m.PurchaseItem.objects.create(purchase=purchase, material=self.mayo, qty=1, price=30000)
        self.mayo.pack_size = 600
        self.mayo.save()
        self.assertIn("проверить</span>", self.client.get("/entry/").content.decode())
        page = self.client.get(f"/admin/core/purchase/{purchase.pk}/change/").content.decode()
        self.assertIn('name="items-0-qty" value="1"', page)
        self.assertIn('name="items-0-price" value="30000"', page)
        self.assertIn('name="paid" value="0"', page)
        self._resave(purchase, item, "1", "30000")
        self.assertEqual((item.qty, item.price), (600, 50))
        self.assertEqual(round(purchase.total()), 30000)
        self.assertFalse(item.before_packs())

    def test_resave_keeps_exact_numbers(self):
        # 10 шт из упаковки по 30: на экране 0,333 упак., а после сохранения остаются ровно 10
        self.mayo.pack_size = 30
        self.mayo.save()
        purchase = m.Purchase.objects.create(supplier=self.supplier)
        item = m.PurchaseItem.objects.create(purchase=purchase, material=self.mayo, qty=10, price=250)
        page = self.client.get(f"/admin/core/purchase/{purchase.pk}/change/").content.decode()
        self.assertIn('name="items-0-qty" value="0.333"', page)
        self.assertIn('name="items-0-price" value="7500"', page)
        self._resave(purchase, item, "0.333", "8000")
        self.assertEqual(item.qty, 10)
        self.assertEqual(round(item.price, 2), D("266.67"))

    def test_search_recipes_and_purchases_on_entry(self):
        shop = m.Shop.objects.create(name="Корзинка", district="Юнусабад", phone="+998 90 111")
        hot_dog = m.Product.objects.create(name="Хот-дог", price=12000)
        m.RecipeItem.objects.create(product=hot_dog, material=self.mayo, qty=D("11.25"))
        bun = m.RawMaterial.objects.create(name="Булочка", unit=self.gram, homemade=True)
        m.MaterialRecipeItem.objects.create(material=bun, ingredient=self.mayo, qty=1)
        response = self.client.get("/search/", {"q": "КОРЗ"})
        self.assertContains(response, f"/shops/{shop.pk}/")
        self.assertContains(self.client.get("/search/", {"q": "майон"}), "Майонез")
        self.assertContains(self.client.get("/search/", {"q": "нет такого"}), "Ничего не нашлось")
        page = self.client.get("/entry/recipes/")
        self.assertContains(page, "Хот-дог")
        self.assertContains(page, "Булочка")
        purchase = m.Purchase.objects.create(supplier=self.supplier)
        m.PurchaseItem.objects.create(purchase=purchase, material=self.mayo, qty=1800, price=D("18.666667"))
        self.mayo.pack_name, self.mayo.pack_size = "банка", 900
        self.mayo.save()
        entry = self.client.get("/entry/").content.decode()
        self.assertIn("Что купили", entry)
        self.assertIn("2 банка", entry)
        self.assertNotIn("проверить</span>", entry)
        self.assertIn("Назад", entry)
        self.assertIn('action="/search/"', self.client.get("/admin/").content.decode())


class LanguageTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User

        self.client.force_login(User.objects.create_superuser("owner", password="x"))

    def test_switch_to_uzbek_and_back(self):
        page = self.client.get("/").content.decode()
        self.assertIn("DASTYOR", page)
        self.assertIn("Обзор", page)
        response = self.client.post("/i18n/setlang/", {"language": "uz", "next": "/entry/"})
        self.assertRedirects(response, "/entry/", fetch_redirect_response=False)
        for url in ["/", "/money/", "/shops/", "/products/", "/staff/", "/stock/", "/entry/", "/entry/recipes/",
                    "/settings/", "/search/?q=x", "/admin/", "/admin/core/purchase/add/"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        entry = self.client.get("/entry/").content.decode()
        self.assertIn("Maʼlumot kiritish", entry)
        self.assertNotIn("Внести данные", entry)
        self.assertIn("Saqlash", self.client.get("/admin/core/purchase/add/").content.decode())
        self.client.post("/i18n/setlang/", {"language": "ru", "next": "/"})
        self.assertIn("Внести данные", self.client.get("/entry/").content.decode())
