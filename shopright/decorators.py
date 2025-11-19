"""
Custom decorators for API authorization and quota enforcement

This module provides decorators for:
1. Nutrition scan quota enforcement (freemium model)
2. Future: Premium feature access control
"""

from functools import wraps
from django.http import JsonResponse
from shopright.services.subscription_service import SubscriptionService
import logging

logger = logging.getLogger(__name__)


def require_nutrition_scan_quota(view_func):
    """
    Decorator to enforce nutrition scan quota limits

    This decorator:
    1. Checks if user has quota remaining (free: 5/day, premium: unlimited)
    2. Increments counter if allowed
    3. Returns 429 error if quota exceeded

    Usage:
        @require_firebase_auth
        @require_nutrition_scan_quota
        def lookup_barcode_api(request):
            # User has quota - proceed with scan
            ...

    Returns:
        429 Too Many Requests if quota exceeded
        Proceeds to wrapped function if quota available
    """
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        # User should already be authenticated by @require_firebase_auth
        user = request.user

        if not user.is_authenticated:
            logger.error("❌ require_nutrition_scan_quota called without authenticated user")
            return JsonResponse({
                'error': 'Authentication required'
            }, status=401)

        # Check and use quota
        success, message, scans_remaining = SubscriptionService.use_nutrition_scan(user)

        if not success:
            # Quota exceeded
            logger.warning(f"❌ Nutrition scan quota exceeded for user {user.username}")
            return JsonResponse({
                'error': 'quota_exceeded',
                'message': message,
                'scans_remaining': 0,
                'upgrade_required': True,
                'upgrade_message': 'Upgrade to Premium for unlimited nutrition scans'
            }, status=429)  # 429 Too Many Requests

        # Quota available - add info to request for view to use
        request.nutrition_quota = {
            'scans_remaining': scans_remaining,
            'message': message
        }

        logger.info(f"✅ Nutrition scan quota OK for user {user.username}. {scans_remaining} remaining.")

        # Proceed to wrapped view
        return view_func(request, *args, **kwargs)

    return wrapper


def require_premium_subscription(view_func):
    """
    Decorator to restrict access to premium-only features

    Usage:
        @require_firebase_auth
        @require_premium_subscription
        def premium_feature_api(request):
            # Only premium users can access this
            ...

    Returns:
        403 Forbidden if user is not premium
        Proceeds to wrapped function if user is premium
    """
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        user = request.user

        if not user.is_authenticated:
            return JsonResponse({
                'error': 'Authentication required'
            }, status=401)

        # Check premium status
        subscription = SubscriptionService.check_and_reset_daily_limits(user)

        if not subscription.is_premium_active:
            logger.warning(f"❌ Premium feature access denied for user {user.username}")
            return JsonResponse({
                'error': 'premium_required',
                'message': 'This feature requires a Premium subscription',
                'subscription_type': subscription.subscription_type
            }, status=403)  # 403 Forbidden

        # User is premium - proceed
        logger.info(f"✅ Premium feature access granted for user {user.username}")
        return view_func(request, *args, **kwargs)

    return wrapper
