"""
Management command to check bank account balances and send low balance alerts.

Logic:
- Fetch fresh balances from Plaid for all linked accounts
- If balance < threshold AND we haven't already alerted → send notification
- If balance goes BACK ABOVE threshold → reset the alert (so we notify again if it drops)
- Only notify once per drop below threshold, not every cron run

Usage:
    # Run balance check (3x/day during business hours via cron)
    python manage.py check_balances

    # Dry run (check but don't notify)
    python manage.py check_balances --dry-run
"""

import logging
from decimal import Decimal

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

            members = [
                m.user for m in
                HomeMember.objects.filter(home=home).select_related('user')
            ]

            for item in plaid_items:
                try:
                    plaid_accounts = plaid_service.get_account_balances(
                        item.access_token
                    )

                    for acct_data in plaid_accounts:
                        balance = Decimal(str(acct_data['balance_current']))

                        # Update balance in DB
                        updated = BankAccount.objects.filter(
                            plaid_account_id=acct_data['plaid_account_id']
                        ).update(
                            balance=balance,
                            balance_available=acct_data.get('balance_available'),
                            last_synced_at=timezone.now(),
                        )
                        total_refreshed += updated

                        account = BankAccount.objects.filter(
                            plaid_account_id=acct_data['plaid_account_id']
                        ).first()

                        if not account:
                            continue

                        if balance >= threshold:
                            # Balance is healthy — reset alert so we notify
                            # again if it drops below threshold later
                            if account.last_alert_balance is not None:
                                account.last_alert_balance = None
                                account.save(update_fields=['last_alert_balance'])
                                self.stdout.write(
                                    f"  Reset: {account.account_name} "
                                    f"back above threshold (${balance:,.2f})"
                                )
                            continue

                        # Balance is below threshold
                        if account.last_alert_balance is not None:
                            # Already alerted for this drop — skip
                            self.stdout.write(
                                f"  Skip (already alerted): "
                                f"{account.account_name} ${balance:,.2f}"
                            )
                            continue

                        # New drop below threshold — send alert
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
                    self.stderr.write(
                        f"Error checking {item.institution_name}: {e}"
                    )

        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}Done. Refreshed {total_refreshed} accounts, "
                f"{total_alerts} low balance alert(s)."
            )
        )
