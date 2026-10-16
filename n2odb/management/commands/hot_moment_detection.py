from django.core.management.base import BaseCommand

from n2odb.services import hot_moments


class Command(BaseCommand):
    help = 'Detects flux outliers (Hot Moments) and updates the HMMAD/HMMCD columns.'

    def handle(self, *args, **options):
        self.stdout.write("Fetching measurements from the database...")
        total, failed = hot_moments.detect(log=self.stdout.write)
        for tid, err in failed.items():
            self.stdout.write(self.style.WARNING(f"Treatment {tid} skipped: {err}"))
        if not total:
            self.stdout.write(self.style.WARNING("No data found."))
            return
        self.stdout.write(self.style.SUCCESS(f"Success! Database fully updated with {total} records."))
