"""
Remove all plants except the one named 'Dahej' (case-insensitive).
If Dahej doesn't exist, all plants are deleted.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import Plant


class Command(BaseCommand):
    help = "Remove all plants except Dahej. Keeps only the Dahej plant."

    def add_arguments(self, parser):
        parser.add_argument(
            "--plant-name",
            default="Dahej",
            help="Plant name to keep (case-insensitive). Default: Dahej",
        )
        parser.add_argument("--dry-run", action="store_true", help="Show what would be deleted without deleting.")

    @transaction.atomic
    def handle(self, *args, **options):
        keep_name = options["plant_name"].strip()
        dry_run = options["dry_run"]

        if not keep_name:
            self.stderr.write("Plant name cannot be empty.")
            return

        to_keep = list(Plant.objects.filter(name__iexact=keep_name))
        to_delete = list(Plant.objects.exclude(name__iexact=keep_name))

        if dry_run:
            self.stdout.write(f"Would keep: {[p.name for p in to_keep]}")
            self.stdout.write(f"Would delete: {[p.name for p in to_delete]}")
            return

        deleted_count, _ = Plant.objects.exclude(name__iexact=keep_name).delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted_count} plant(s). Kept: {[p.name for p in to_keep] or 'none'}"))
