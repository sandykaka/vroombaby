"""
Management command to check bank account balances and send alerts.

Two types of alerts:
1. Low balance: account drops below threshold
2. Cash flow: current balance won't cover upcoming predicted expenses

Logic:
- Fetch fresh balances from Plaid for all linked accounts
- Low balance: notify once per drop below threshold, reset when balance recovers
- Cash flow: compare total balance against cached AI predictions for upcoming week
  - Only alert once per prediction period (don't spam daily)

Usage:
    # Run balance check (3x/day during business hours via cron)
    python manage.py check_balances

    # Dry run (check but don't notify)
    python manage.py check_balances --dry-run
"""

import logging
from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import models
from django.utils import timezone

from shillak.models import BankAccount, CashFlowPrediction, Home, HomeMember, PlaidItem
from shillak.services import plaid_service
from shillak.services.notification_service import NotificationService

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Check bank account balances and send low balance + cash flow alerts'

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

            # ========================================
            # STEP 1: Refresh balances + low balance alerts
            # ========================================

            for item in plaid_items:
                try:
                    plaid_accounts = plaid_service.get_account_balances(
                        item.access_token
                    )

                    for acct_data in plaid_accounts:
                        balance = Decimal(str(acct_data['balance_current']))

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
                            if account.last_alert_balance is not None:
                                account.last_alert_balance = None
                                account.save(update_fields=['last_alert_balance'])
                                self.stdout.write(
                                    f"  Reset: {account.account_name} "
                                    f"back above threshold (${balance:,.2f})"
                                )
                            continue

                        if account.last_alert_balance is not None:
                            self.stdout.write(
                                f"  Skip (already alerted): "
                                f"{account.account_name} ${balance:,.2f}"
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
                    self.stderr.write(
                        f"Error checking {item.institution_name}: {e}"
                    )

            # ========================================
            # STEP 2: Cash flow prediction check
            # Only alert if:
            # - No low balance alert was sent (avoid double alerts)
            # - We haven't already alerted for this prediction
            # - Balance won't cover predicted spend
            # ========================================

            # Skip if we already sent low balance alerts for this home
            home_had_low_balance_alert = any(
                a.last_alert_balance is not None
                for a in BankAccount.objects.filter(home=home)
            )

            if home_had_low_balance_alert:
                self.stdout.write(
                    f"  Cashflow skip: {home.name} — "
                    f"low balance alert already active"
                )
            else:
                total_balance = BankAccount.objects.filter(
                    home=home
                ).aggregate(
                    total=models.Sum('balance')
                )['total'] or Decimal('0')

                today = date.today()
                upcoming = CashFlowPrediction.objects.filter(
                    home=home,
                    week_start__lte=today + timedelta(days=7),
                    week_end__gte=today,
                    alerted=False,
                ).first()

                if upcoming and upcoming.predicted_spend > total_balance:
                    bills_str = ', '.join(
                        f"{b['name']} ${b['amount']}"
                        for b in upcoming.bills_due[:3]
                    )

                    self.stdout.write(
                        self.style.WARNING(
                            f"  CASHFLOW: {home.name} — balance ${total_balance:,.2f} "
                            f"may not cover predicted spend "
                            f"${upcoming.predicted_spend:,.2f} "
                            f"(risk: {upcoming.risk_level})"
                        )
                    )

                    if not dry_run:
                        shortfall = upcoming.predicted_spend - total_balance
                        for user in members:
                            NotificationService.send_notification(
                                user=user,
                                title='Upcoming Expenses Alert',
                                body=(
                                    f"You may be ${shortfall:,.2f} short this week. "
                                    f"Upcoming: {bills_str or 'regular expenses'}."
                                ),
                                data={
                                    'risk_level': upcoming.risk_level,
                                    'predicted_spend': str(upcoming.predicted_spend),
                                    'current_balance': str(total_balance),
                                    'action': 'open_dashboard',
                                },
                                notification_type='cashflow_alert',
                            )
                        # Mark as alerted — won't alert again unless balance changes >10%
                        upcoming.alerted = True
                        upcoming.alerted_at_balance = total_balance
                        upcoming.save(update_fields=['alerted', 'alerted_at_balance'])

                    total_alerts += 1
                elif upcoming:
                    self.stdout.write(
                        f"  Cashflow OK: {home.name} — "
                        f"${total_balance:,.2f} covers "
                        f"${upcoming.predicted_spend:,.2f} predicted"
                    )

        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}Done. Refreshed {total_refreshed} accounts, "
                f"{total_alerts} alert(s)."
            )
        )
