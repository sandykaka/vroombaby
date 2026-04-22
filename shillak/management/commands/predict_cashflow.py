"""
Management command to analyze cash flow and generate predictions.

Syncs transactions from Plaid, passes to OpenAI for analysis, and generates
weekly predictions. Sends alerts for high-risk weeks.

Usage:
    # Weekly prediction (Sunday 8 PM via cron)
    python manage.py predict_cashflow

    # Dry run (sync transactions but don't call OpenAI)
    python manage.py predict_cashflow --dry-run

    # Single home
    python manage.py predict_cashflow --home-id 1
"""

import logging

from django.core.management.base import BaseCommand

from shillak.models import Home, PlaidItem
from shillak.services.cashflow_service import analyze_cashflow, sync_transactions

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Analyze cash flow and generate weekly predictions'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Sync transactions but do not call OpenAI or send notifications',
        )
        parser.add_argument(
            '--home-id',
            type=int,
            help='Analyze a single home by ID',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        home_id = options.get('home_id')

        if home_id:
            homes = Home.objects.filter(id=home_id)
        else:
            homes = Home.objects.filter(plaid_items__isnull=False).distinct()

        self.stdout.write(f"Analyzing cash flow for {homes.count()} home(s)...")

        for home in homes:
            plaid_items = PlaidItem.objects.filter(home=home)

            # Step 1: Sync transactions
            total_txns = 0
            for item in plaid_items:
                try:
                    count = sync_transactions(item)
                    total_txns += count
                    self.stdout.write(
                        f"  Synced {count} transactions from {item.institution_name}"
                    )
                except Exception as e:
                    self.stderr.write(f"  Error syncing {item.institution_name}: {e}")

            # Step 2: AI analysis
            if total_txns > 0:
                try:
                    analysis = analyze_cashflow(home, dry_run=dry_run)

                    if analysis and not dry_run:
                        risk_weeks = [
                            w for w in analysis.get('weekly_predictions', [])
                            if w.get('risk_level') in ('high', 'medium')
                        ]
                        alerts = analysis.get('alerts', [])

                        self.stdout.write(
                            self.style.SUCCESS(
                                f"  {home.name}: {len(analysis.get('weekly_predictions', []))} "
                                f"predictions, {len(risk_weeks)} risk weeks"
                            )
                        )

                        for alert in alerts:
                            self.stdout.write(
                                self.style.WARNING(f"  ALERT: {alert}")
                            )
                    elif dry_run:
                        self.stdout.write(
                            f"  [DRY RUN] Would analyze {total_txns} transactions"
                        )

                except Exception as e:
                    self.stderr.write(f"  Error analyzing {home.name}: {e}")
            else:
                self.stdout.write(f"  No transactions for {home.name}, skipping analysis")

        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(f"{prefix}Done.")
        )
