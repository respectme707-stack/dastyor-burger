from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render

from . import reports


@staff_member_required
def dashboard(request):
    period = request.GET.get("period", "month")
    if period not in reports.PERIODS:
        period = "month"
    context = reports.build(period)
    context.update(period=period, periods=reports.PERIODS.items())
    return render(request, "core/dashboard.html", context)
