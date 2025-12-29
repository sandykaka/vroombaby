"""
Unified notification service using Firebase Cloud Messaging (FCM)
Handles all push notifications: recalls, payments, deliveries, etc.
"""

import firebase_admin
from firebase_admin import messaging, credentials
from shopright.models import UserProfile
import logging
import os

logger = logging.getLogger(__name__)


class NotificationService:
    """Send push notifications via FCM"""

    @staticmethod
    def _get_firebase_app():
        """Get or initialize ShopRight Firebase app"""
        try:
            # Try to get existing app
            return firebase_admin.get_app('shopright')
        except ValueError:
            # App doesn't exist, initialize it
            shopright_service_account_path = None
            if os.path.exists('/Users/sandeshkakade/gitRepos/vroombaby/shopright_service_account_key.json'):
                shopright_service_account_path = '/Users/sandeshkakade/gitRepos/vroombaby/shopright_service_account_key.json'
            elif os.path.exists('/home/ubuntu/vroombaby/shopright_service_account_key.json'):
                shopright_service_account_path = '/home/ubuntu/vroombaby/shopright_service_account_key.json'

            if not shopright_service_account_path:
                raise FileNotFoundError("ShopRight Firebase service account key not found")

            logger.info(f"Initializing ShopRight Firebase app with: {shopright_service_account_path}")
            shopright_cred = credentials.Certificate(shopright_service_account_path)
            return firebase_admin.initialize_app(shopright_cred, name='shopright')

    @staticmethod
    def send_notification(user, title, body, data=None, notification_type='general'):
        """
        Send push notification to a user

        Args:
            user: Django User object
            title: Notification title
            body: Notification message
            data: Optional dict of extra data for app to handle
            notification_type: Type of notification (for analytics/routing)

        Returns:
            bool: True if sent successfully
        """
        try:
            # Get user's FCM token
            profile = UserProfile.objects.filter(user=user).first()

            if not profile or not profile.fcm_token:
                logger.warning(f"No FCM token for user {user.id}")
                return False

            # Build notification payload
            message = messaging.Message(
                notification=messaging.Notification(
                    title=title,
                    body=body
                ),
                data={
                    'type': notification_type,
                    **{k: str(v) for k, v in (data or {}).items()}  # Convert all values to strings
                },
                token=profile.fcm_token
            )

            # Get ShopRight Firebase app and send via FCM
            app = NotificationService._get_firebase_app()
            response = messaging.send(message, app=app)
            logger.info(f"Notification sent to {user.username}: {response}")
            return True

        except messaging.UnregisteredError:
            # Token is invalid/expired - clear it
            logger.warning(f"Invalid FCM token for user {user.id}, clearing")
            profile.fcm_token = None
            profile.save()
            return False

        except Exception as e:
            logger.error(f"Failed to send notification to {user.id}: {e}")
            return False

    @staticmethod
    def send_list_update_reminder(user, delivery_date):
        """
        Remind user to update their shopping list before shopper starts
        """
        return NotificationService.send_notification(
            user=user,
            title="📝 Update Your List",
            body=f"Your shopper starts tomorrow. Review your list now.",
            data={
                'delivery_date': delivery_date.isoformat(),
                'action': 'open_shopping_list'
            },
            notification_type='list_update_reminder'
        )

    @staticmethod
    def send_charge_reminder(user, amount, charge_date, delivery_date):
        """
        Remind user about upcoming weekly subscription charge
        """
        return NotificationService.send_notification(
            user=user,
            title="📦 Subscription Reminder",
            body=f"${amount:.0f} weekly charge processes tomorrow.",
            data={
                'charge_date': charge_date.isoformat(),
                'delivery_date': delivery_date.isoformat(),
                'amount': str(amount),
                'action': 'open_subscriptions'
            },
            notification_type='charge_reminder'
        )

    @staticmethod
    def send_payment_hold_notification(user, amount):
        """
        Notify user about grocery payment hold
        """
        return NotificationService.send_notification(
            user=user,
            title="💳 Payment Hold Placed",
            body=f"${amount:.2f} hold placed for your grocery list. Final charge after delivery.",
            data={
                'amount': str(amount),
                'action': 'open_delivery_tracking'
            },
            notification_type='payment_hold'
        )

    @staticmethod
    def send_payment_failed(user):
        """
        Notify customer that payment failed when shopper tried to start
        """
        return NotificationService.send_notification(
            user=user,
            title="💳 Payment Failed",
            body="Please update your payment method. Your shopper is waiting.",
            data={
                'action': 'open_payment_settings'
            },
            notification_type='payment_failed'
        )

    @staticmethod
    def send_shopping_started(user, delivery_id, hold_amount):
        """
        Notify user that shopper has started shopping and payment hold is placed
        """
        return NotificationService.send_notification(
            user=user,
            title="🛒 Shopping Started",
            body=f"${hold_amount:.2f} hold placed. Your shopper is getting your groceries now!",
            data={
                'delivery_id': str(delivery_id),
                'hold_amount': str(hold_amount),
                'action': 'open_delivery_tracking'
            },
            notification_type='shopping_started'
        )

    @staticmethod
    def send_delivery_status_update(user, delivery_id, status, estimated_time=None):
        """
        Update user on delivery status
        """
        status_messages = {
            'packing': ('📦 Order Packing', 'Your order is being packed.'),
            'out_for_delivery': ('🚗 On the Way!', f"Arriving {estimated_time}" if estimated_time else "Your order is on the way!"),
            'delivered': ('✅ Delivered', 'Your order has been delivered.')
        }

        title, body = status_messages.get(status, ('Delivery Update', status.replace('_', ' ').title()))

        return NotificationService.send_notification(
            user=user,
            title=title,
            body=body,
            data={
                'delivery_id': str(delivery_id),
                'status': status,
                'action': 'open_delivery_tracking'
            },
            notification_type='delivery_status'
        )

    @staticmethod
    def send_charge_adjusted(user, final_amount, hold_amount):
        """
        Notify user about final charge after grocery delivery
        """
        hold_released = hold_amount - final_amount
        return NotificationService.send_notification(
            user=user,
            title="💵 Final Charge",
            body=f"Charged ${final_amount:.2f} for groceries. ${hold_released:.2f} hold released.",
            data={
                'final_amount': str(final_amount),
                'hold_amount': str(hold_amount),
                'hold_released': str(hold_released),
                'action': 'open_receipt'
            },
            notification_type='charge_adjusted'
        )

    @staticmethod
    def send_recall_alert(user, recall, product_name, confidence_score):
        """
        Alert user about product recall match
        """
        severity_emoji = {
            'Class I': '🚨',
            'Class II': '⚠️',
            'Class III': 'ℹ️'
        }.get(recall.classification, '⚠️')

        return NotificationService.send_notification(
            user=user,
            title=f"{severity_emoji} Product Recall",
            body=f"{product_name} - {recall.reason_for_recall[:60]}... Tap for details.",
            data={
                'recall_id': str(recall.id),
                'recall_number': recall.recall_number,
                'confidence': str(confidence_score),
                'action': 'open_recall_details'
            },
            notification_type='recall_alert'
        )

    @staticmethod
    def send_delivery_confirmed(user, delivery_date, amount):
        """
        Notify customer that shopper accepted delivery and subscription fee charged
        """
        return NotificationService.send_notification(
            user=user,
            title="✅ Delivery Confirmed",
            body=f"${amount:.0f} subscription charged. Your delivery on {delivery_date.strftime('%a, %b %d')} is confirmed!",
            data={
                'delivery_date': delivery_date.isoformat(),
                'amount': str(amount),
                'action': 'open_deliveries'
            },
            notification_type='delivery_confirmed'
        )

    @staticmethod
    def send_delivery_unavailable(user, delivery_date, reason=None):
        """
        Notify customer that delivery cannot be fulfilled (not charged)
        """
        body = f"Sorry, we can't fulfill your delivery on {delivery_date.strftime('%a, %b %d')}. You were not charged."

        if reason:
            body = f"Sorry, we can't fulfill your delivery on {delivery_date.strftime('%a, %b %d')}. {reason}. You were not charged."

        return NotificationService.send_notification(
            user=user,
            title="⚠️ Delivery Unavailable",
            body=body,
            data={
                'delivery_date': delivery_date.isoformat(),
                'reason': reason or 'No shopper available',
                'action': 'open_deliveries'
            },
            notification_type='delivery_unavailable'
        )

    @staticmethod
    def send_subscription_payment_failed(user, delivery_date):
        """
        Notify customer that subscription payment failed when shopper tried to accept
        """
        return NotificationService.send_notification(
            user=user,
            title="💳 Subscription Payment Failed",
            body=f"Please update your payment method to confirm your {delivery_date.strftime('%a, %b %d')} delivery.",
            data={
                'delivery_date': delivery_date.isoformat(),
                'action': 'open_payment_settings'
            },
            notification_type='subscription_payment_failed'
        )

    @staticmethod
    def send_new_delivery_available(shopper, delivery):
        """
        Notify shopper of new delivery available to accept
        (Sent to ALL approved shoppers when customer subscribes)
        """
        from shopright.models import WeeklyDelivery

        # Get store name from shopping list
        store_name = delivery.shopping_list.store_name if delivery.shopping_list else "Unknown Store"

        # Calculate estimated amount (if available)
        estimated_amount = 0
        if delivery.subscription:
            # Basic = $15, Premium = $30
            estimated_amount = 30.00 if delivery.subscription.subscription_tier == 'premium' else 15.00

        return NotificationService.send_notification(
            user=shopper,
            title="📦 New Delivery",
            body=f"{store_name} - {delivery.delivery_date.strftime('%a %b %d')} - ${estimated_amount:.0f}",
            data={
                'delivery_id': str(delivery.id),
                'store_name': store_name,
                'delivery_date': delivery.delivery_date.isoformat(),
                'estimated_amount': str(estimated_amount),
                'action': 'open_shopper_dashboard'
            },
            notification_type='new_delivery_available'
        )


# Module-level convenience function for easy importing
def send_notification(user, title, body, data=None, notification_type='general'):
    """
    Convenience function that calls NotificationService.send_notification
    This allows direct import: from shopright.services.notification_service import send_notification
    """
    return NotificationService.send_notification(user, title, body, data, notification_type)

