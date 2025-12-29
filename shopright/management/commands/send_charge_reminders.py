"""
Management command to send charge reminder notifications

Run daily via cron:
0 9 * * * cd /path/to/project && python manage.py send_charge_reminders
"""

import logging
from datetime import date, timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from shopright.models import DeliverySubscription
from shopright.services.notification_service import NotificationService

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Send charge reminder notifications 24 hours before billing'

    def handle(self, *args, **options):
        """
        Find subscriptions that will be charged tomorrow and send notifications
        """
        tomorrow = date.today() + timedelta(days=1)

        # Find active subscriptions with billing cycle starting tomorrow
        # (Current model: charge on delivery day, which is billing_cycle_start)
        subscriptions = DeliverySubscription.objects.filter(
            status='active',
            billing_cycle_start__date=tomorrow
        ).select_related('customer', 'shopping_list', 'store')

        sent_count = 0
        error_count = 0

        for sub in subscriptions:
            try:
                # Get next delivery info
                next_delivery = None
                if hasattr(sub, 'weekly_deliveries'):
                    next_delivery = sub.weekly_deliveries.filter(
                        delivery_date__gte=date.today()
                    ).order_by('delivery_date').first()

                # Send notification
                self.send_notification(
                    user=sub.customer,
                    subscription=sub,
                    charge_date=tomorrow,
                    next_delivery=next_delivery
                )
                sent_count += 1

            except Exception as e:
                logger.error(f"Failed to send notification for subscription {sub.id}: {e}")
                error_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f'Sent {sent_count} charge reminders. Errors: {error_count}'
            )
        )

    def send_notification(self, user, subscription, charge_date, next_delivery):
        """
        Send push notification to user via Firebase Cloud Messaging (FCM)
        """
        # Calculate cost based on tier
        cost = 30.00 if subscription.subscription_tier == 'premium' else 15.00

        # Get delivery date
        delivery_date = next_delivery.delivery_date if next_delivery else charge_date

        # Send notification via unified service
        success = NotificationService.send_charge_reminder(
            user=user,
            amount=cost,
            charge_date=charge_date,
            delivery_date=delivery_date
        )

        if success:
            logger.info(f"Charge reminder sent to {user.username} (user_id={user.id})")
        else:
            logger.warning(f"Failed to send charge reminder to {user.username} (no FCM token or error)")

