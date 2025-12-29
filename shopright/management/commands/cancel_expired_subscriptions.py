"""
Management command to cancel expired pending subscriptions

Run daily via cron:
0 */6 * * * cd /path/to/project && python manage.py cancel_expired_subscriptions
"""

import logging
from datetime import datetime, timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from shopright.models import DeliverySubscription
from shopright.services.stripe_service import StripeService
from shopright.services.notification_service import NotificationService

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Cancel subscriptions that have been pending confirmation for more than 48 hours'

    def add_arguments(self, parser):
        parser.add_argument(
            '--timeout-hours',
            type=int,
            default=48,
            help='Hours to wait before cancelling pending subscriptions (default: 48)'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be cancelled without actually cancelling'
        )

    def handle(self, *args, **options):
        """
        Find and cancel subscriptions that have been pending too long
        """
        timeout_hours = options['timeout_hours']
        dry_run = options['dry_run']

        cutoff_time = timezone.now() - timedelta(hours=timeout_hours)

        # Find subscriptions pending confirmation that are expired
        expired_subscriptions = DeliverySubscription.objects.filter(
            status='pending_confirmation',
            created_at__lt=cutoff_time
        ).select_related('customer')

        cancelled_count = 0
        error_count = 0

        self.stdout.write(
            f"Found {expired_subscriptions.count()} expired pending subscriptions "
            f"(older than {timeout_hours} hours)"
        )

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN MODE - No changes will be made"))

        for subscription in expired_subscriptions:
            try:
                age_hours = (timezone.now() - subscription.created_at).total_seconds() / 3600
                customer = subscription.customer

                self.stdout.write(
                    f"Processing subscription {subscription.id} for {customer.username} "
                    f"(pending for {age_hours:.1f} hours)"
                )

                if not dry_run:
                    # Cancel Stripe subscription (should be safe since no billing occurred)
                    if subscription.stripe_subscription_id:
                        success, error = StripeService.cancel_subscription(
                            subscription.stripe_subscription_id
                        )
                        if not success:
                            logger.warning(
                                f"Failed to cancel Stripe subscription {subscription.stripe_subscription_id}: {error}"
                            )

                    # Cancel Django subscription
                    subscription.status = 'cancelled'
                    subscription.save()

                    # Send helpful notification to customer
                    NotificationService.send_subscription_expired(
                        user=customer,
                        delivery_day=subscription.delivery_day,
                        timeout_hours=timeout_hours
                    )

                    logger.info(f"Cancelled expired subscription {subscription.id}")

                cancelled_count += 1

            except Exception as e:
                logger.error(f"Failed to cancel subscription {subscription.id}: {e}")
                error_count += 1

        if dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    f'Would cancel {cancelled_count} subscriptions. Errors: {error_count}'
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f'Cancelled {cancelled_count} expired subscriptions. Errors: {error_count}'
                )
            )

        # Log completion
        if cancelled_count > 0:
            logger.info(
                f"Expired subscription cleanup: {cancelled_count} cancelled, {error_count} errors"
            )