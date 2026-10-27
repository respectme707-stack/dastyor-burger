"""Резервная копия базы: все записи в одном сжатом JSON-файле.

Восстановление на чистой базе: python manage.py migrate && python manage.py loaddata копия.json.gz
"""
import gzip
import io

from django.core.management import call_command
from django.utils import timezone

# Служебные таблицы Django создаются заново при migrate, их не сохраняем.
EXCLUDE = ["contenttypes", "auth.permission", "sessions", "admin.logentry"]


def make_backup():
    """Возвращает (имя файла, байты .json.gz)."""
    out = io.StringIO()
    call_command("dumpdata", exclude=EXCLUDE, natural_foreign=True, natural_primary=True, indent=0, stdout=out)
    name = f"dastyor-{timezone.localtime():%Y-%m-%d}.json.gz"
    return name, gzip.compress(out.getvalue().encode("utf-8"))
