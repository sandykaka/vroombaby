import logging
import os

import firebase_admin
from firebase_admin import credentials, messaging

from shillak.models import UserProfile

logger = logging.getLogger(__name__)


class NotificationService:
    """Send push notifications via Firebase Cloud Messaging for Shillak."""

    @staticmethod
    def _get_firebase_app():
        """Get or initialize the Shillak Firebase app."""
        try:
            return firebase_admin.get_app('shillak')
        except ValueError:
            key_path = None
            for path in [
                '/Users/sandeshkakade/gitRepos/vroombaby/shillak_service_account_key.json',
                '/home/ubuntu/vroombaby/shillak_service_account_key.json',
            ]:
                if os.path.exists(path):
                    key_path = path
                    break

            if not key_path:
                raise FileNotFoundError("Shillak Firebase service account key not found")

            logger.info(f"Initializing Shillak Firebase app with: {key_path}")
            cred = credentials.Certificate(key_path)
            return firebase_admin.initialize_app(cred, name='shillak')

    @staticmethod
    def send_notification(user, title, body, data=None, notification_type='general'):
        """Send a push notification to a user."""
        try:
            profile = UserProfile.objects.filter(user=user).first()

            if not profile or not profile.fcm_token:
                logger.warning(f"No FCM token for user {user.id}")
                return False

            message = messaging.Message(
                notification=messaging.Notification(
                    title=title,
                    body=body,
                ),
                data={
                    'type': notification_type,
                    **{k: str(v) for k, v in (data or {}).items()},
                },
                token=profile.fcm_token,
            )

            app = NotificationService._get_firebase_app()
            response = messaging.send(message, app=app)
            logger.info(f"Notification sent to {user.username}: {response}")
            return True

        except messaging.UnregisteredError:
            logger.warning(f"Invalid FCM token for user {user.id}, clearing")
            profile.fcm_token = None
            profile.save()
            return False

        except Exception as e:
            logger.error(f"Failed to send notification to {user.id}: {e}")
            return False

    @staticmethod
    def send_low_balance_alert(account, home, recipients):
        """Send low balance alert to all recipients."""
        balance_str = f"${account.balance:,.2f}"
        threshold_str = f"${home.low_balance_threshold:,.2f}"

        owner_profile = UserProfile.objects.filter(user=account.user).first()
        owner_display_name = (owner_profile.display_name if owner_profile else None) or "Someone"

        for user in recipients:
            is_owner = (user == account.user)

            if is_owner:
                title = f"{account.account_name} is low"
                body = f"Balance is {balance_str} (below {threshold_str}). Tap to request a transfer."
            else:
                title = f"{owner_display_name}'s {account.account_name} is low"
                body = f"Balance is {balance_str}. Tap to send a transfer."

            NotificationService.send_notification(
                user=user,
                title=title,
                body=body,
                data={
                    'account_id': str(account.id),
                    'balance': str(account.balance),
                    'threshold': str(home.low_balance_threshold),
                    'action': 'open_dashboard',
                },
                notification_type='low_balance',
            )
