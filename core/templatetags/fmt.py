from decimal import Decimal

from django import template

register = template.Library()

NBSP = " "


@register.filter
def money(value):
    """1234567.8 → «1 234 568»."""
    try:
        n = round(Decimal(value))
    except (TypeError, ValueError, ArithmeticError):
        return value
    return f"{n:,}".replace(",", NBSP)


@register.filter
def qty(value):
    """11.500 → «11,5», 300.000 → «300»."""
    try:
        d = Decimal(value).normalize()
    except (TypeError, ValueError, ArithmeticError):
        return value
    text = f"{d:f}"
    if "." in text:
        whole, frac = text.split(".")
        return f"{int(whole):,}".replace(",", NBSP) + "," + frac
    return f"{int(d):,}".replace(",", NBSP)
