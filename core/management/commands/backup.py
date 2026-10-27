from pathlib import Path

from django.core.management.base import BaseCommand

from core.backup import make_backup


class Command(BaseCommand):
    help = "Сохранить резервную копию базы в файл (по умолчанию в текущую папку)"

    def add_arguments(self, parser):
        parser.add_argument("folder", nargs="?", default=".")

    def handle(self, folder, **options):
        name, data = make_backup()
        path = Path(folder) / name
        path.write_bytes(data)
        self.stdout.write(f"Сохранено: {path} ({len(data) // 1024} КБ)")
