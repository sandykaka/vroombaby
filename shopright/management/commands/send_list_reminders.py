"""
Management command to send list update reminder notifications

Run daily via cron:
0 9 * * * cd /path/to/project && python manage.py send_list_reminders

Sends reminder 1 day before delivery (24 hours notice to update shopping list)
"""

import logging
from datetime import date, timedelta
from django.core.management.base import BaseCommand
from shopright.models import WeeklyDelivery
from shopright.services.notification_service import NotificationService

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Send list update reminders 1 day before delivery (day before shopper shops)'

    def handle(self, *args, **options):
        """
        Find deliveries scheduled tomorrow and remind customers to update lists
        Gives customers final chance to review before shopper starts shopping
        """
        tomorrow = date.today() + timedelta(days=1)

        # Find scheduled deliveries tomorrow that haven't started yet
        deliveries = WeeklyDelivery.objects.filter(
            delivery_date=tomorrow,
            status__in=['scheduled', 'assigned']  # Not yet started shopping
        ).select_related('subscription__customer')

        sent_count = 0
        error_count = 0
        skipped_count = 0

        for delivery in deliveries:
            try:
                # Skip if no subscription or customer
                if not delivery.subscription or not delivery.subscription.customer:
                    logger.warning(f"Delivery {delivery.id} has no customer")
                    skipped_count += 1
                    continue

                customer = delivery.subscription.customer

                # Send notification
                success = NotificationService.send_list_update_reminder(
                    user=customer,
                    delivery_date=delivery.delivery_date
                )

                if success:
                    sent_count += 1
                    logger.info(f"List reminder sent to {customer.username} for delivery tomorrow ({delivery.delivery_date})")
                else:
                    error_count += 1
                    logger.warning(f"Failed to send reminder to {customer.username} (no FCM token or error)")

            except Exception as e:
                logger.error(f"Failed to send reminder for delivery {delivery.id}: {e}")
                error_count += 1

        # Summary output
        self.stdout.write(
            self.style.SUCCESS(
                f'Sent {sent_count} list reminders. Errors: {error_count}. Skipped: {skipped_count}'
            )
        )

        if sent_count == 0 and error_count == 0 and skipped_count == 0:
            self.stdout.write(
                self.style.WARNING(
                    f'No deliveries scheduled for tomorrow ({tomorrow.strftime("%Y-%m-%d")})'
                )
            )
