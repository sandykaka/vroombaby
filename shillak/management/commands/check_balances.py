"""
Management command to check bank account balances and send low balance alerts.

Usage:
    # Run balance check (every 4 hours via cron)
    python manage.py check_balances

    # Dry run (check but don't notify)
    python manage.py check_balances --dry-run
"""

import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from shillak.models import BankAccount, Home, HomeMember, PlaidItem
from shillak.services import plaid_service
from shillak.services.notification_service import NotificationService

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Check bank account balances and send low balance alerts'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Check balances but do not send notifications',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        homes = Home.objects.filter(plaid_items__isnull=False).distinct()

        self.stdout.write(f"Checking balances for {homes.count()} home(s)...")

        total_refreshed = 0
        total_alerts = 0

        for home in homes:
            threshold = home.low_balance_threshold
            plaid_items = PlaidItem.objects.filter(home=home)

            # Get all home members for notifications
            members = [m.user for m in HomeMember.objects.filter(home=home).select_related('user')]

            for item in plaid_items:
                try:
                    plaid_accounts = plaid_service.get_account_balances(item.access_token)

                    for acct_data in plaid_accounts:
                        balance = acct_data['balance_current']

                        # Update balance in DB
                        updated = BankAccount.objects.filter(
                            plaid_account_id=acct_data['plaid_account_id']
                        ).update(
                            balance=balance,
                            balance_available=acct_data.get('balance_available'),
                            last_synced_at=timezone.now(),
                        )
                        total_refreshed += updated

                        # Check if below threshold
                        if balance < float(threshold):
                            account = BankAccount.objects.filter(
                                plaid_account_id=acct_data['plaid_account_id']
                            ).first()

                            if not account:
                                continue

                            # Dedup: skip if we already alerted for this exact balance
                            if account.last_alert_balance is not None and float(account.last_alert_balance) == balance:
                                self.stdout.write(
                                    f"  Skip (already alerted): {account.account_name} "
                                    f"${balance:,.2f}"
                                )
                                continue

                            self.stdout.write(
                                self.style.WARNING(
                                    f"  LOW: {account.account_name} at "
                                    f"${balance:,.2f} (threshold ${threshold:,.2f})"
                                )
                            )

                            if not dry_run:
                                NotificationService.send_low_balance_alert(
                                    account=account,
                                    home=home,
                                    recipients=members,
                                )
                                account.last_alert_balance = balance
                                account.save(update_fields=['last_alert_balance'])

                            total_alerts += 1

                except Exception as e:
                    logger.error(f"Failed to check item {item.item_id}: {e}")
                    self.stderr.write(f"Error checking {item.institution_name}: {e}")

        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}Done. Refreshed {total_refreshed} accounts, "
                f"{total_alerts} low balance alert(s)."
            )
        )
