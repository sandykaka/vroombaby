"""
SubscriptionService - Manages user subscription and usage quota tracking

This service:
1. Auto-creates UserSubscription on first access (for new users)
2. Checks and resets daily nutrition scan limits at midnight
3. Validates quota before allowing nutrition scans
4. Verifies Apple In-App Purchase receipts for premium subscriptions
"""

from django.utils import timezone
from django.contrib.auth.models import User
from shopright.models import UserSubscription
from typing import Optional, Tuple
import logging

logger = logging.getLogger(__name__)


class SubscriptionService:
    """Service for managing user subscriptions and usage quotas"""

    @staticmethod
    def get_or_create_subscription(user: User) -> UserSubscription:
        """
        Get or create subscription record for user

        Args:
            user: Django User instance

        Returns:
            UserSubscription instance
        """
        subscription, created = UserSubscription.objects.get_or_create(
            user=user,
            defaults={
                'subscription_type': 'free',
                'is_premium': False,
                'daily_nutrition_scans_used': 0,
                'last_nutrition_scan_reset': timezone.now().date()
            }
        )

        if created:
            logger.info(f"✅ Created new subscription for user {user.username}")

        return subscription

    @staticmethod
    def check_and_reset_daily_limits(user: User) -> UserSubscription:
        """
        Check if date has changed and reset daily limits if needed

        This should be called before checking quota to ensure accurate counts.

        Args:
            user: Django User instance

        Returns:
            UserSubscription instance (updated if reset occurred)
        """
        subscription = SubscriptionService.get_or_create_subscription(user)

        # Check if we need to reset (new day)
        today = timezone.now().date()
        if subscription.last_nutrition_scan_reset < today:
            logger.info(f"🔄 Resetting daily limits for user {user.username}")
            subscription.reset_daily_nutrition_scans()

        return subscription

    @staticmethod
    def can_use_nutrition_scan(user: User) -> Tuple[bool, str, int]:
        """
        Check if user has quota remaining for nutrition scan

        Args:
            user: Django User instance

        Returns:
            Tuple of (can_scan: bool, reason: str, scans_remaining: int)
            Examples:
                (True, "Premium user", 999)
                (True, "4 scans remaining today", 4)
                (False, "Daily limit reached. Upgrade to Premium for unlimited scans.", 0)
        """
        # Reset if new day
        subscription = SubscriptionService.check_and_reset_daily_limits(user)

        # Premium users have unlimited scans
        if subscription.is_premium_active:
            return (True, "Premium user", 999)

        # Free users have daily limit
        scans_remaining = subscription.nutrition_scans_remaining

        if scans_remaining > 0:
            return (True, f"{scans_remaining} scans remaining today", scans_remaining)
        else:
            return (False, "Daily limit reached. Upgrade to Premium for unlimited scans.", 0)

    @staticmethod
    def use_nutrition_scan(user: User) -> Tuple[bool, str, int]:
        """
        Attempt to use a nutrition scan quota

        This method:
        1. Checks if user has quota remaining
        2. Increments counter if allowed
        3. Returns success/failure with remaining count

        Args:
            user: Django User instance

        Returns:
            Tuple of (success: bool, message: str, scans_remaining: int)
            Examples:
                (True, "Scan successful. 4 scans remaining today.", 4)
                (False, "Daily limit reached. Upgrade to Premium for unlimited scans.", 0)
        """
        # Check quota
        can_scan, reason, scans_before = SubscriptionService.can_use_nutrition_scan(user)

        if not can_scan:
            logger.warning(f"❌ Nutrition scan denied for user {user.username}: {reason}")
            return (False, reason, 0)

        # Premium users don't need counter increment
        subscription = UserSubscription.objects.get(user=user)
        if subscription.is_premium_active:
            logger.info(f"✅ Nutrition scan allowed for premium user {user.username}")
            return (True, "Scan successful. Premium user.", 999)

        # Increment counter for free users
        subscription.increment_nutrition_scan()
        scans_remaining = subscription.nutrition_scans_remaining

        logger.info(f"✅ Nutrition scan used for user {user.username}. {scans_remaining} remaining today.")
        return (True, f"Scan successful. {scans_remaining} scans remaining today.", scans_remaining)

    @staticmethod
    def get_subscription_status(user: User) -> dict:
        """
        Get full subscription status for user

        Args:
            user: Django User instance

        Returns:
            Dict with subscription details:
            {
                'is_premium': bool,
                'subscription_type': str,
                'scans_remaining': int,
                'scans_used_today': int,
                'premium_expires_at': datetime or None,
                'is_expired': bool
            }
        """
        subscription = SubscriptionService.check_and_reset_daily_limits(user)

        return {
            'is_premium': subscription.is_premium_active,
            'subscription_type': subscription.subscription_type,
            'scans_remaining': subscription.nutrition_scans_remaining,
            'scans_used_today': subscription.daily_nutrition_scans_used,
            'premium_expires_at': subscription.premium_expires_at,
            'is_expired': not subscription.is_premium_active and subscription.is_premium
        }
