import os

from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import get_object_or_404, render

from . import models as m
from . import reports


def _period(request):
    period = request.GET.get("period", "month")
    return period if period in reports.PERIODS else "month"


def _render(request, template, tab, context, period=None):
    keep = request.GET.copy()
    keep.pop("period", None)
    context.update(tab=tab, period=period, periods=reports.PERIODS.items(), keep=keep.urlencode())
    return render(request, template, context)


@staff_member_required
def dashboard(request):
    period = _period(request)
    data = reports.build(period)
    data["staff_ratings"] = reports.staff(period)["ratings"]
    return _render(request, "core/overview.html", "overview", data, period)


@staff_member_required
def money(request):
    period = _period(request)
    return _render(request, "core/money.html", "money", reports.money(period), period)


@staff_member_required
def shops(request):
    period = _period(request)
    data = reports.build(period)
    listed = {s["shop"].pk for s in data["shops"]}
    data["quiet_shops"] = m.Shop.objects.filter(active=True).exclude(pk__in=listed)
    return _render(request, "core/shops.html", "shops", data, period)


@staff_member_required
def shop_detail(request, pk):
    period = _period(request)
    shop = get_object_or_404(m.Shop, pk=pk)
    return _render(request, "core/shop_detail.html", "shops", reports.shop_detail(shop, period), period)


@staff_member_required
def products(request):
    period = _period(request)
    return _render(request, "core/products.html", "products", reports.build(period), period)


@staff_member_required
def product_detail(request, pk):
    period = _period(request)
    product = get_object_or_404(m.Product, pk=pk)
    return _render(request, "core/product_detail.html", "products", reports.product_detail(product, period),
                   period)


@staff_member_required
def staff(request):
    period = _period(request)
    query = request.GET.get("q", "").strip()
    role = request.GET.get("role", "")
    inactive = request.GET.get("inactive") == "1"
    data = reports.staff(period, query, role, inactive)
    data.update(q=query, role=role, inactive=inactive, roles=m.Worker.Role.choices)
    return _render(request, "core/staff.html", "staff", data, period)


@staff_member_required
def staff_detail(request, pk):
    period = _period(request)
    worker = get_object_or_404(m.Worker, pk=pk)
    return _render(request, "core/staff_detail.html", "staff", reports.staff_detail(worker, period), period)


@staff_member_required
def stock(request):
    data = reports.build("all")
    return _render(request, "core/stock.html", "stock", data)


@staff_member_required
def entry(request):
    return _render(request, "core/entry.html", "entry",
                   {"recent": reports.recent(20), "purchases": reports.purchases(15)})


@staff_member_required
def recipes(request):
    return _render(request, "core/recipes.html", "entry", reports.recipes())


@staff_member_required
def search(request):
    query = request.GET.get("q", "").strip()
    return _render(request, "core/search.html", "", {"q": query, "groups": reports.search(query)})


@staff_member_required
def settings_page(request):
    counts = {
        "products": m.Product.objects.count(),
        "materials": m.RawMaterial.objects.count(),
        "categories": m.MaterialCategory.objects.count(),
        "units": m.Unit.objects.count(),
        "suppliers": m.Supplier.objects.count(),
        "shops": m.Shop.objects.count(),
        "workers": m.Worker.objects.count(),
        "vehicles": m.Vehicle.objects.count(),
    }
    bot = {
        "username": os.environ.get("TELEGRAM_BOT_USERNAME", ""),
        "reminder_hour": os.environ.get("REMINDER_HOUR", "9"),
        "report_hour": os.environ.get("DAILY_REPORT_HOUR", "20"),
        "owners": len([x for x in os.environ.get("OWNER_TELEGRAM_IDS", "").replace(",", " ").split() if x]),
    }
    return _render(request, "core/settings.html", "settings", {"counts": counts, "bot": bot})
