"""
Delivery Service API Views - Phase 1 MVP
Simplified workflow: Store/shopper access customer's shopping list directly
"""

import logging
import json
import re
from datetime import datetime, timedelta, date
from functools import wraps

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.contrib.auth.models import User

from .models import (
    Store, Shopper, DeliveryZone, UserProfile,
    DeliverySubscription, WeeklyDelivery, ShoppingList, FamilyMember
)
from .views import require_firebase_auth
from .services.stripe_service import StripeService

logger = logging.getLogger(__name__)


# ========================================
# HELPER FUNCTIONS
# ========================================

def validate_delivery_zip_code(address):
    """
    Validate if delivery address is in our service area

    Args:
        address: Full delivery address string

    Returns:
        dict: {
            'valid': bool,
            'zip_code': str,
            'message': str,
            'available_areas': list
        }
    """
    # Extract ZIP code from address using improved regex
    # Look for 5 digits after state abbreviation or at end of address
    # Pattern: CA 95014, CA 95014-1234, or just 95014 at end of address
    zip_pattern = r'(?:,\s*[A-Z]{2}\s+|,\s*)(\d{5}(?:-\d{4})?)\b'
    zip_match = re.search(zip_pattern, address)

    if not zip_match:
        return {
            'valid': False,
            'zip_code': None,
            'message': 'Please provide a valid ZIP code in your delivery address.',
            'available_areas': []
        }

    zip_code = zip_match.group(1)[:5]  # Take only 5-digit ZIP

    # Check if ZIP code is in any active delivery zone
    active_zones = DeliveryZone.objects.filter(is_active=True)

    # If no active zones exist, reject all deliveries
    if not active_zones.exists():
        return {
            'valid': False,
            'zip_code': zip_code,
            'message': 'Delivery service not yet available in any areas.',
            'available_areas': []
        }

    # Check each zone for ZIP code match
    zones_with_zips = []
    for zone in active_zones:
        if zone.zip_codes:  # Only check zones that have ZIP codes configured
            zones_with_zips.append(zone)
            if zip_code in zone.zip_codes:
                return {
                    'valid': True,
                    'zip_code': zip_code,
                    'message': f'Delivery available in {zone.name}',
                    'available_areas': []
                }

    # If no zones have ZIP codes configured yet, reject all deliveries
    if not zones_with_zips:
        return {
            'valid': False,
            'zip_code': zip_code,
            'message': 'Delivery zones are being configured. Check back soon!',
            'available_areas': []
        }

    # ZIP code not in service area - provide helpful info
    available_areas = []
    for zone in zones_with_zips:  # Only show zones that have ZIP codes configured
        available_areas.append({
            'name': zone.name,
            'zip_codes': zone.zip_codes[:5]  # Show first 5 ZIP codes as examples
        })

    return {
        'valid': False,
        'zip_code': zip_code,
        'message': f'We don\'t deliver to {zip_code} yet. Current service areas: {", ".join([z.name for z in zones_with_zips])}',
        'available_areas': available_areas
    }


def validate_store_customer_distance(store_address, customer_address, max_miles=5):
    """
    Validate that the store is within acceptable distance of customer's delivery address.

    Uses Google Maps Distance Matrix API to calculate driving distance.

    Args:
        store_address: Store's full address string
        customer_address: Customer's delivery address string
        max_miles: Maximum allowed distance in miles (default: 5)

    Returns:
        dict: {
            'valid': bool,
            'distance_miles': float or None,
            'distance_text': str or None,
            'message': str
        }
    """
    import googlemaps
    from django.conf import settings

    # If store address is empty/missing, skip validation (backwards compatibility)
    if not store_address or not store_address.strip():
        logger.warning("Store address is empty, skipping distance validation")
        return {
            'valid': True,
            'distance_miles': None,
            'distance_text': None,
            'message': 'Store address not provided, skipping distance check'
        }

    try:
        gmaps = googlemaps.Client(key=settings.GOOGLE_API_KEY)

        # Get distance matrix (driving distance)
        result = gmaps.distance_matrix(
            origins=[store_address],
            destinations=[customer_address],
            mode="driving",
            units="imperial"  # Get miles
        )

        # Parse result
        if result['status'] != 'OK':
            logger.error(f"Distance Matrix API error: {result['status']}")
            return {
                'valid': True,  # Allow on API error (fail open)
                'distance_miles': None,
                'distance_text': None,
                'message': 'Could not verify distance, allowing request'
            }

        element = result['rows'][0]['elements'][0]

        if element['status'] != 'OK':
            logger.warning(f"Could not calculate distance: {element['status']}")
            return {
                'valid': True,  # Allow if address can't be geocoded
                'distance_miles': None,
                'distance_text': None,
                'message': f'Could not calculate distance: {element["status"]}'
            }

        # Extract distance in meters and convert to miles
        distance_meters = element['distance']['value']
        distance_miles = distance_meters / 1609.344
        distance_text = element['distance']['text']

        logger.info(f"📍 Distance check: {store_address} → {customer_address} = {distance_text} ({distance_miles:.1f} miles)")

        if distance_miles > max_miles:
            return {
                'valid': False,
                'distance_miles': round(distance_miles, 1),
                'distance_text': distance_text,
                'message': f'Store is {distance_text} away from your delivery address. We currently only deliver from stores within {max_miles} miles.'
            }

        return {
            'valid': True,
            'distance_miles': round(distance_miles, 1),
            'distance_text': distance_text,
            'message': f'Store is {distance_text} away - within delivery range'
        }

    except Exception as e:
        logger.error(f"Distance validation error: {e}")
        # Fail open - don't block delivery on API errors
        return {
            'valid': True,
            'distance_miles': None,
            'distance_text': None,
            'message': f'Distance check failed: {str(e)}'
        }


# ========================================
# DECORATOR: Account Type Authorization
# ========================================

def require_account_type(*allowed_types):
    """
    Restrict API access by account type

    Usage:
        @require_firebase_auth
        @require_account_type('customer')
        def view(request): ...
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            user = request.user
            if not user.is_authenticated:
                return JsonResponse({'error': 'Authentication required'}, status=401)

            profile, created = UserProfile.objects.get_or_create(
                user=user,
                defaults={'account_type': 'customer'}
            )

            if profile.account_type not in allowed_types:
                return JsonResponse({
                    'error': 'forbidden',
                    'message': f'Requires: {", ".join(allowed_types)}',
                    'your_type': profile.account_type
                }, status=403)

            request.user_profile = profile
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator


# ========================================
# ACCOUNT MANAGEMENT APIs
# ========================================

@csrf_exempt
@require_firebase_auth
@require_http_methods(["POST"])
def set_account_type(request):
    """
    Set user's account type (customer, shopper, store, store_owner)

    POST /api/account/set-type/
    Body: { account_type: "customer" | "shopper" | "store" | "store_owner" }
    """
    try:
        data = json.loads(request.body)
        account_type = data.get('account_type')

        if not account_type:
            return JsonResponse({'error': 'Missing account_type'}, status=400)

        valid_types = ['customer', 'shopper', 'store', 'store_owner']
        if account_type not in valid_types:
            return JsonResponse({
                'error': 'Invalid account_type',
                'valid_types': valid_types
            }, status=400)

        # Get or create user profile
        profile, created = UserProfile.objects.get_or_create(
            user=request.user,
            defaults={'account_type': account_type}
        )

        # Update if already exists
        if not created:
            profile.account_type = account_type
            profile.save()

        logger.info(f"User {request.user.username} account type set to: {account_type}")

        return JsonResponse({
            'success': True,
            'account_type': account_type
        })

    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        logger.error(f"Set account type error: {e}", exc_info=True)
        return JsonResponse({'error': str(e)}, status=500)


# ========================================
# CUSTOMER APIs
# ========================================

@csrf_exempt
@require_http_methods(["GET"])
def get_service_areas(request):
    """
    Get available delivery service areas

    GET /api/delivery/service-areas/

    Returns all active delivery zones with ZIP codes for iOS validation
    """
    try:
        active_zones = DeliveryZone.objects.filter(is_active=True)

        areas = []
        for zone in active_zones:
            areas.append({
                'id': zone.id,
                'name': zone.name,
                'zip_codes': zone.zip_codes,
                'created_at': zone.created_at.isoformat()
            })

        return JsonResponse({
            'success': True,
            'service_areas': areas,
            'total_zip_codes': sum(len(area['zip_codes']) for area in areas)
        })

    except Exception as e:
        logger.error(f"Get service areas error: {e}", exc_info=True)
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
@require_http_methods(["POST"])
def validate_address_endpoint(request):
    """
    Validate delivery address against service areas

    POST /api/delivery/validate-address/
    Body: { "address": "10221 Vicksburg Dr, Cupertino, CA 95014" }

    Returns address validation result for immediate user feedback
    """
    try:
        data = json.loads(request.body)
        address = data.get('address', '').strip()

        if not address:
            return JsonResponse({
                'valid': False,
                'message': 'Address is required',
                'zip_code': None,
                'available_areas': []
            }, status=400)

        # Use existing validation function
        validation_result = validate_delivery_zip_code(address)

        # Format response for iOS client
        response_data = {
            'valid': validation_result['valid'],
            'zip_code': validation_result['zip_code'],
            'message': validation_result['message']
        }

        # Add available areas if validation failed
        if not validation_result['valid'] and validation_result['available_areas']:
            available_area_names = [area['name'] for area in validation_result['available_areas']]
            response_data['available_areas'] = available_area_names

        return JsonResponse(response_data)

    except json.JSONDecodeError:
        return JsonResponse({
            'valid': False,
            'message': 'Invalid JSON format',
            'zip_code': None,
            'available_areas': []
        }, status=400)
    except Exception as e:
        logger.error(f"Address validation error: {e}", exc_info=True)
        return JsonResponse({
            'valid': False,
            'message': 'Validation service temporarily unavailable',
            'zip_code': None,
            'available_areas': []
        }, status=500)


@csrf_exempt
@require_http_methods(["GET"])
def stripe_config(request):
    """
    Get Stripe configuration (publishable key)

    GET /api/delivery/stripe-config/

    Public endpoint - no auth required (publishable key is safe to expose)
    """
    from django.conf import settings

    return JsonResponse({
        'publishable_key': settings.STRIPE_PUBLISHABLE_KEY
    })


@csrf_exempt
@require_firebase_auth
@require_account_type('customer')
@require_http_methods(["POST"])
def create_setup_intent(request):
    """
    Create a SetupIntent for collecting payment method

    POST /api/delivery/create-setup-intent/

    Returns client_secret for Stripe PaymentSheet
    """
    try:
        user = request.user

        # Get or create Stripe customer
        success, stripe_customer_id, error = StripeService.get_or_create_stripe_customer(user)
        if not success:
            logger.error(f"Failed to create Stripe customer for {user.username}: {error}")
            return JsonResponse({
                'error': 'payment_setup_failed',
                'message': 'Could not set up payment. Please try again.',
                'details': error
            }, status=500)

        # Create SetupIntent
        success, client_secret, error = StripeService.create_setup_intent(stripe_customer_id)
        if not success:
            logger.error(f"Failed to create SetupIntent for {user.username}: {error}")
            return JsonResponse({
                'error': 'setup_intent_failed',
                'message': 'Could not initialize payment collection.',
                'details': error
            }, status=500)

        logger.info(f"✅ Created SetupIntent for {user.username}")

        return JsonResponse({
            'success': True,
            'client_secret': client_secret,
            'customer_id': stripe_customer_id
        })

    except Exception as e:
        logger.error(f"Create SetupIntent error: {e}", exc_info=True)
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
@require_firebase_auth
@require_account_type('customer')
@require_http_methods(["POST"])
def attach_payment_method(request):
    """
    Attach payment method to customer's Stripe account

    POST /api/delivery/attach-payment-method/
    Body: { "payment_method_id": "pm_xxx" }

    PaymentMethod is created client-side using Stripe iOS SDK
    """
    try:
        data = json.loads(request.body)
        payment_method_id = data.get('payment_method_id')

        if not payment_method_id:
            return JsonResponse({'error': 'Missing payment_method_id'}, status=400)

        user = request.user

        # Get or create Stripe customer
        success, stripe_customer_id, error = StripeService.get_or_create_stripe_customer(user)
        if not success:
            logger.error(f"Failed to create Stripe customer for {user.username}: {error}")
            return JsonResponse({
                'error': 'payment_setup_failed',
                'message': 'Could not set up payment. Please try again.',
                'details': error
            }, status=500)

        # Attach payment method to customer
        success, error = StripeService.attach_payment_method(
            customer_id=stripe_customer_id,
            payment_method_id=payment_method_id
        )

        if not success:
            logger.error(f"Failed to attach payment method for {user.username}: {error}")
            return JsonResponse({
                'error': 'payment_method_failed',
                'message': 'Could not attach payment method. Please try again.',
                'details': error
            }, status=500)

        # IMPORTANT: Save payment method ID to user profile
        profile, created = UserProfile.objects.get_or_create(user=user)
        profile.stripe_customer_id = stripe_customer_id
        profile.default_payment_method = payment_method_id
        profile.save()

        logger.info(f"✅ Payment method attached and saved for {user.username}")

        return JsonResponse({
            'success': True,
            'customer_id': stripe_customer_id,
            'message': 'Payment method added successfully'
        })

    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        logger.error(f"Attach payment method error: {e}", exc_info=True)
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
@require_firebase_auth
@require_account_type('customer')
@require_http_methods(["POST"])
def setup_subscription(request):
    """
    Set up subscription preferences without requiring store selection

    POST /api/delivery/setup-subscription/

    Body: {
        subscription_tier: "basic" or "premium",
        delivery_address: "full address with ZIP",
        delivery_instructions: "optional instructions"
    }

    Creates a subscription with default delivery settings that can be modified later
    """
    try:
        data = json.loads(request.body)
        user = request.user

        logger.info(f"Subscription setup request from user {user.username} (id={user.id})")

        # Validate tier
        tier = data.get('subscription_tier', 'basic')
        if tier not in ['basic', 'premium']:
            return JsonResponse({'error': 'Invalid tier. Use "basic" or "premium"'}, status=400)

        # Validate address
        delivery_address = data.get('delivery_address', '').strip()
        if not delivery_address:
            return JsonResponse({'error': 'Missing: delivery_address'}, status=400)

        # Validate delivery address is in service area
        zip_validation_result = validate_delivery_zip_code(delivery_address)
        if not zip_validation_result['valid']:
            return JsonResponse({
                'error': 'address_outside_service_area',
                'message': zip_validation_result['message'],
                'available_areas': zip_validation_result.get('available_areas', [])
            }, status=400)

        # Check for existing subscriptions
        existing_subs = DeliverySubscription.objects.filter(
            customer=user,
            status__in=['active', 'pending_confirmation', 'setup_complete']
        )

        if existing_subs.exists():
            return JsonResponse({
                'error': 'subscription_exists',
                'message': 'You already have a subscription. Use modify endpoint to update preferences.',
                'tier': existing_subs.first().subscription_tier
            }, status=400)

        # Get/create user profile for Stripe customer
        profile, created = UserProfile.objects.get_or_create(user=user)

        # Setup Stripe customer if needed
        stripe_service = StripeService()
        if not profile.stripe_customer_id:
            try:
                stripe_customer = stripe_service.create_customer(
                    email=user.email,
                    name=f"{user.first_name} {user.last_name}".strip()
                )
                profile.stripe_customer_id = stripe_customer.id
                profile.save()
                logger.info(f"Created Stripe customer {stripe_customer.id} for user {user.username}")
            except Exception as e:
                logger.error(f"Failed to create Stripe customer: {e}")
                return JsonResponse({'error': 'Payment setup failed'}, status=400)

        # Create subscription with default delivery settings
        subscription = DeliverySubscription.objects.create(
            customer=user,
            subscription_tier=tier,
            delivery_day="Saturday",  # Default day
            delivery_window="1-3 PM",  # Default window
            delivery_address=delivery_address,
            delivery_instructions=data.get('delivery_instructions', ''),
            status='setup_complete',  # New status for setup-only subscriptions
            billing_cycle_start=None,  # Will be set when first delivery is scheduled
            billing_cycle_end=None,
            stripe_customer_id=profile.stripe_customer_id
        )

        logger.info(f"✅ Subscription setup created: {subscription.id} for {user.username}")

        return JsonResponse({
            'success': True,
            'subscription_id': subscription.id,
            'message': 'Subscription preferences saved. You can now schedule deliveries.',
            'tier': tier,
            'status': 'setup_complete'
        })

    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        logger.error(f"Subscription setup error: {e}", exc_info=True)
        return JsonResponse({'error': 'Internal server error'}, status=500)


@csrf_exempt
@require_firebase_auth
@require_account_type('customer')
@require_http_methods(["POST"])
def subscribe_delivery(request):
    """
    Create delivery subscription

    POST /api/delivery/subscribe/

    For Basic tier:
    Body: {
        store_id, shopping_list_id,
        delivery_day, delivery_window, delivery_address,
        subscription_tier: "basic"
    }

    For Premium tier (2 different stores):
    Body: {
        subscription_tier: "premium",
        delivery_address: "...",
        delivery_instructions: "...",
        deliveries: [
            {
                store_id, shopping_list_id,
                delivery_day, delivery_window
            },
            {
                store_id, shopping_list_id,
                delivery_day, delivery_window
            }
        ]
    }
    """
    try:
        data = json.loads(request.body)
        user = request.user

        logger.info(f"Subscribe request from user {user.username} (id={user.id})")
        logger.info(f"Request data: tier={data.get('subscription_tier')}, deliveries={len(data.get('deliveries', []))} if premium")

        # Validate tier
        tier = data.get('subscription_tier', 'basic')
        if tier not in ['basic', 'premium']:
            return JsonResponse({'error': 'Invalid tier. Use "basic" or "premium"'}, status=400)

        # Define day_map and today (used throughout function)
        today = date.today()
        day_map = {
            'Monday': 0, 'Tuesday': 1, 'Wednesday': 2, 'Thursday': 3,
            'Friday': 4, 'Saturday': 5, 'Sunday': 6
        }

        # Check existing subscriptions (include pending_confirmation for upgrade detection)
        existing_subs = DeliverySubscription.objects.filter(
            customer=user,
            status__in=['active', 'pending_confirmation', 'setup_complete']
        )

        # DEBUG: Log subscription details
        logger.info(f"User {user.username} subscription check: tier={tier}, existing_subs_count={existing_subs.count()}")
        for sub in existing_subs:
            logger.info(f"  Existing sub: id={sub.id}, tier={sub.subscription_tier}, status={sub.status}")

        # Allow upgrading basic to premium
        is_upgrading_basic_to_premium = (
            tier == 'premium' and
            existing_subs.count() == 1 and
            existing_subs.first().subscription_tier == 'basic'
        )

        # Allow premium users with 1 delivery to add 2nd delivery
        is_adding_second_delivery = (
            tier == 'premium' and
            existing_subs.count() == 1 and
            existing_subs.first().subscription_tier == 'premium'
        )

        # Allow setup_complete subscriptions to add their FIRST delivery
        is_activating_setup_complete = (
            existing_subs.count() == 1 and
            existing_subs.first().status == 'setup_complete'
        )

        if existing_subs.exists() and not is_adding_second_delivery and not is_upgrading_basic_to_premium and not is_activating_setup_complete:
            # Block if:
            # - Basic user trying to add another subscription (but not upgrading or activating)
            # - Premium user already has 2 deliveries
            if existing_subs.first().subscription_tier == 'basic' and tier == 'basic':
                return JsonResponse({
                    'error': 'subscription_exists',
                    'message': 'You already have a Basic subscription. Upgrade to Premium to add more deliveries.',
                    'tier': 'basic'
                }, status=400)
            elif existing_subs.count() >= 2:
                return JsonResponse({
                    'error': 'subscription_limit_reached',
                    'message': 'You already have 2 deliveries (Premium max). Modify or cancel existing deliveries.',
                    'tier': 'premium'
                }, status=400)

        # Shared fields
        delivery_address = data.get('delivery_address', '').strip()
        delivery_instructions = data.get('delivery_instructions', '').strip()

        if not delivery_address:
            return JsonResponse({'error': 'Missing: delivery_address'}, status=400)

        # Validate delivery address is in our service area
        logger.info(f"Validating address: '{delivery_address}'")
        zip_validation_result = validate_delivery_zip_code(delivery_address)
        logger.info(f"ZIP validation result: {zip_validation_result}")

        if not zip_validation_result['valid']:
            logger.warning(f"ZIP validation failed: {zip_validation_result['message']}")
            return JsonResponse({
                'error': 'service_area_unavailable',
                'message': zip_validation_result['message'],
                'zip_code': zip_validation_result['zip_code'],
                'available_areas': zip_validation_result['available_areas']
            }, status=400)

        # Parse deliveries based on tier
        deliveries = []

        if tier == 'basic':
            # Basic: Single delivery (old format)
            required = ['shopping_list_id', 'delivery_day', 'delivery_window']
            for field in required:
                if field not in data:
                    return JsonResponse({'error': f'Missing: {field}'}, status=400)

            deliveries.append({
                'shopping_list_id': data['shopping_list_id'],
                'delivery_day': data['delivery_day'],
                'delivery_window': data['delivery_window']
            })

        else:
            # Premium: Up to 2 deliveries (2nd is optional, same store allowed)
            if 'deliveries' not in data or not isinstance(data['deliveries'], list):
                return JsonResponse({'error': 'Premium tier requires "deliveries" array'}, status=400)

            if len(data['deliveries']) < 1 or len(data['deliveries']) > 2:
                return JsonResponse({'error': 'Premium tier allows 1 or 2 deliveries'}, status=400)

            deliveries = data['deliveries']

            # Validate each delivery has required fields (no store_id needed anymore)
            required = ['shopping_list_id', 'delivery_day', 'delivery_window']
            for i, delivery in enumerate(deliveries):
                for field in required:
                    if field not in delivery:
                        return JsonResponse({'error': f'Delivery {i+1} missing: {field}'}, status=400)

        # Validate shopping lists exist (check both personal and family lists)
        validated_deliveries = []

        # Get user's family (if any) via FamilyMember
        family_membership = FamilyMember.objects.filter(user=user).first()
        user_family = family_membership.family if family_membership else None
        logger.info(f"User {user.id} family: {user_family.id if user_family else None}")

        for delivery in deliveries:
            logger.info(f"Looking for shopping list {delivery['shopping_list_id']} for user {user.id} (family={user_family})")
            try:
                # Look for list owned by user OR their family
                if user_family:
                    shopping_list = ShoppingList.objects.get(
                        Q(id=delivery['shopping_list_id']) &
                        (Q(user=user) | Q(family=user_family))
                    )
                else:
                    shopping_list = ShoppingList.objects.get(id=delivery['shopping_list_id'], user=user)

                logger.info(f"Found shopping list: {shopping_list.store_name}")
            except ShoppingList.DoesNotExist:
                logger.error(f"Shopping list {delivery['shopping_list_id']} not found for user {user.id}")
                # Check if list exists but belongs to different family
                try:
                    list_check = ShoppingList.objects.get(id=delivery['shopping_list_id'])
                    logger.error(f"List exists but belongs to user_id={list_check.user_id}, family_id={list_check.family_id}")
                except ShoppingList.DoesNotExist:
                    logger.error(f"List {delivery['shopping_list_id']} does not exist at all")
                return JsonResponse({'error': f'Shopping list {delivery["shopping_list_id"]} not found'}, status=404)

            validated_deliveries.append({
                'shopping_list': shopping_list,
                'delivery_day': delivery['delivery_day'],
                'delivery_window': delivery['delivery_window']
            })

        # STEP 1: Handle Stripe subscription
        if is_adding_second_delivery:
            # Reuse existing subscription's Stripe details
            existing_sub = existing_subs.first()
            stripe_customer_id = existing_sub.stripe_customer_id
            stripe_subscription_id = existing_sub.stripe_subscription_id
            billing_cycle_start = existing_sub.billing_cycle_start
            billing_cycle_end = existing_sub.billing_cycle_end
            logger.info(f"Adding 2nd delivery to existing subscription {existing_sub.id}: reusing billing cycle {billing_cycle_start.date()} → {billing_cycle_end.date()}")
        elif is_activating_setup_complete:
            # Skip Stripe creation here - we'll create it in the is_activating_setup_complete block below
            # Just set placeholder variables that won't be used
            existing_sub = existing_subs.first()
            stripe_customer_id = existing_sub.stripe_customer_id
            stripe_subscription_id = None  # Will be created in activation block
            billing_cycle_start = None
            billing_cycle_end = None
            logger.info(f"Activating setup_complete subscription {existing_sub.id}: Stripe subscription will be created during activation")
        elif is_upgrading_basic_to_premium:
            # Apple-way upgrade: Modify existing Stripe subscription with prorated billing
            existing_sub = existing_subs.first()
            stripe_customer_id = existing_sub.stripe_customer_id
            logger.info(f"Upgrading basic to premium: modifying Stripe subscription {existing_sub.stripe_subscription_id}")

            # Get premium price ID
            stripe_price_id = StripeService.get_price_id_for_tier('premium')
            if not stripe_price_id:
                return JsonResponse({
                    'error': 'configuration_error',
                    'message': 'Premium pricing not configured. Contact support.'
                }, status=500)

            # Upgrade existing Stripe subscription (Apple way: prorated billing)
            first_list = validated_deliveries[0]['shopping_list']
            success, error = StripeService.upgrade_subscription(
                subscription_id=existing_sub.stripe_subscription_id,
                new_price_id=stripe_price_id,
                metadata={
                    'user_id': user.id,
                    'username': user.username,
                    'tier': 'premium',
                    'delivery_count': len(validated_deliveries),
                    'store_name': first_list.store_name,
                    'upgrade_from': 'basic'  # Track that this was an upgrade
                }
            )

            if not success:
                logger.error(f"Failed to upgrade Stripe subscription for {user.username}: {error}")
                return JsonResponse({
                    'error': 'upgrade_failed',
                    'message': 'Could not upgrade subscription. Please check payment method.',
                    'details': error
                }, status=500)

            # Keep existing billing cycle (don't recalculate - Apple way!)
            # Stripe handles the prorated billing automatically
            billing_cycle_start = existing_sub.billing_cycle_start
            billing_cycle_end = existing_sub.billing_cycle_end
            stripe_subscription_id = existing_sub.stripe_subscription_id  # Same subscription, just upgraded

            logger.info(f"✅ Stripe subscription upgraded with prorated billing. Cycle: {billing_cycle_start.date()} → {billing_cycle_end.date()}")
        else:
            # Create new Stripe customer and subscription
            # STEP 1: Create or get Stripe customer
            success, stripe_customer_id, error = StripeService.get_or_create_stripe_customer(user)
            if not success:
                logger.error(f"Failed to create Stripe customer for {user.username}: {error}")
                return JsonResponse({
                    'error': 'payment_setup_failed',
                    'message': 'Could not set up payment method. Please try again.',
                    'details': error
                }, status=500)

            # STEP 1.5: VALIDATE that customer has a valid payment method attached
            user_profile, _ = UserProfile.objects.get_or_create(user=user)
            if not user_profile.default_payment_method:
                logger.error(f"No payment method found for user {user.username}")
                return JsonResponse({
                    'error': 'payment_method_required',
                    'message': 'Please add a payment method before subscribing.',
                    'action': 'collect_payment_method'
                }, status=400)

            # Verify payment method is valid in Stripe
            try:
                import stripe
                payment_method = stripe.PaymentMethod.retrieve(user_profile.default_payment_method)
                if not payment_method or payment_method.customer != stripe_customer_id:
                    logger.error(f"Invalid payment method {user_profile.default_payment_method} for customer {stripe_customer_id}")
                    return JsonResponse({
                        'error': 'invalid_payment_method',
                        'message': 'Payment method is invalid. Please add a new payment method.',
                        'action': 'collect_payment_method'
                    }, status=400)
                logger.info(f"✅ Validated payment method {payment_method.id} for {user.username}")
            except stripe.error.StripeError as e:
                logger.error(f"Stripe error validating payment method: {e}")
                return JsonResponse({
                    'error': 'payment_validation_failed',
                    'message': 'Could not validate payment method. Please try again.',
                    'action': 'collect_payment_method'
                }, status=400)

            # STEP 2: Get Stripe price ID for tier
            stripe_price_id = StripeService.get_price_id_for_tier(tier)
            if not stripe_price_id:
                return JsonResponse({
                    'error': 'configuration_error',
                    'message': 'Subscription pricing not configured. Contact support.'
                }, status=500)

            # STEP 3: Create ONE Stripe subscription (for all deliveries)
            first_list = validated_deliveries[0]['shopping_list']
            success, stripe_subscription_id, error = StripeService.create_subscription(
                customer_id=stripe_customer_id,
                price_id=stripe_price_id,
                metadata={
                    'user_id': user.id,
                    'username': user.username,
                    'tier': tier,
                    'delivery_count': len(validated_deliveries),
                    'store_name': first_list.store_name
                }
            )

            if not success:
                logger.error(f"Failed to create Stripe subscription for {user.username}: {error}")
                return JsonResponse({
                    'error': 'subscription_failed',
                    'message': 'Could not create subscription. Please check payment method.',
                    'details': error
                }, status=500)

            # Calculate billing cycle for new subscriptions
            # Find earliest delivery date across all deliveries
            earliest_delivery_date = None
            for delivery_data in validated_deliveries:
                target_weekday = day_map.get(delivery_data['delivery_day'], 5)
                days_ahead = target_weekday - today.weekday()
                if days_ahead <= 0:
                    days_ahead += 7
                next_delivery_date = today + timedelta(days=days_ahead)

                if earliest_delivery_date is None or next_delivery_date < earliest_delivery_date:
                    earliest_delivery_date = next_delivery_date

            # Set billing cycle (7 days from first delivery)
            billing_cycle_start = timezone.make_aware(
                datetime.combine(earliest_delivery_date, datetime.min.time())
            )
            billing_cycle_end = billing_cycle_start + timedelta(days=7)

            logger.info(f"Calculated billing cycle: {billing_cycle_start.date()} → {billing_cycle_end.date()} (first delivery: {earliest_delivery_date})")

        # STEP 4: Handle upgrade from basic to premium OR create new subscriptions
        created_subscriptions = []

        if is_upgrading_basic_to_premium:
            # Update existing basic subscription to premium (already upgraded in Stripe above)
            existing_subscription = existing_subs.first()

            # NO NEED to cancel old subscription - we upgraded it in place! (Apple way)
            # Update the database record to match the upgraded Stripe subscription
            existing_subscription.subscription_tier = 'premium'
            existing_subscription.deliveries_this_cycle = len(validated_deliveries) + 1  # +1 for existing delivery

            # IMPORTANT: Keep existing delivery details - DO NOT overwrite them!
            # Only update address if provided (user might have moved)
            existing_subscription.delivery_address = delivery_address
            existing_subscription.delivery_instructions = delivery_instructions

            existing_subscription.save()
            created_subscriptions.append(existing_subscription)

            # IMPORTANT: Do NOT create new delivery for existing subscription if one already exists and is accepted
            # Check if there's already a pending or active delivery for the existing subscription
            existing_delivery = WeeklyDelivery.objects.filter(
                subscription=existing_subscription,
                delivery_date__gte=date.today(),
                status__in=['pending_shopper', 'assigned', 'scheduled', 'packing', 'out_for_delivery']
            ).first()

            if not existing_delivery:
                # Only create new delivery if none exists or if all existing ones are completed/cancelled
                # Use EXISTING subscription's delivery day, not the new one from request
                target_weekday = day_map.get(existing_subscription.delivery_day, 5)
                days_ahead = target_weekday - today.weekday()
                if days_ahead <= 0:
                    days_ahead += 7
                next_delivery_date = today + timedelta(days=days_ahead)

                # Create WeeklyDelivery for EXISTING subscription (keep original shopping list!)
                WeeklyDelivery.objects.create(
                    subscription=existing_subscription,
                    shopping_list=existing_subscription.shopping_list,  # Use EXISTING list, not new one!
                    delivery_date=next_delivery_date,
                    status='pending_shopper'  # Waiting for shopper to accept
                )
                logger.info(f"Created new delivery for existing subscription {existing_subscription.id}")
            else:
                logger.info(f"Preserving existing delivery {existing_delivery.id} for subscription {existing_subscription.id} (status: {existing_delivery.status})")

            # NOW add the NEW delivery to the SAME subscription (don't create second subscription)
            # During upgrade, validated_deliveries contains the NEW delivery being added
            if len(validated_deliveries) > 0:
                delivery_data = validated_deliveries[0]  # The new delivery from upgrade request
                logger.info(f"🔧 Creating second delivery: shopping_list={delivery_data['shopping_list'].id}, delivery_day={delivery_data['delivery_day']}")

                # Create WeeklyDelivery for the NEW delivery on the existing subscription
                target_weekday_2 = day_map.get(delivery_data['delivery_day'], 5)
                days_ahead_2 = target_weekday_2 - today.weekday()
                if days_ahead_2 <= 0:
                    days_ahead_2 += 7
                next_delivery_date_2 = today + timedelta(days=days_ahead_2)

                # Use explicit transaction to ensure delivery creation
                from django.db import transaction
                try:
                    with transaction.atomic():
                        # Check existing deliveries for this subscription first
                        existing_weekly_deliveries = WeeklyDelivery.objects.filter(
                            subscription=existing_subscription
                        )
                        logger.info(f"🔍 Existing WeeklyDeliveries for subscription {existing_subscription.id}: {list(existing_weekly_deliveries.values('id', 'delivery_date', 'shopping_list_id', 'status'))}")

                        new_delivery = WeeklyDelivery.objects.create(
                            subscription=existing_subscription,  # Use SAME subscription, not a new one
                            shopping_list=delivery_data['shopping_list'],  # NEW shopping list
                            delivery_date=next_delivery_date_2,
                            status='pending_shopper'  # Waiting for shopper to accept
                        )
                        logger.info(f"✅ SUCCESS: Created WeeklyDelivery {new_delivery.id} for subscription {existing_subscription.id}")

                        # Verify it was actually created by re-querying
                        verify_delivery = WeeklyDelivery.objects.filter(id=new_delivery.id).first()
                        if verify_delivery:
                            logger.info(f"✅ VERIFIED: WeeklyDelivery {new_delivery.id} exists in database")
                        else:
                            logger.error(f"❌ FAILED: WeeklyDelivery {new_delivery.id} not found after creation!")

                        # Show all deliveries for this subscription after creation
                        all_deliveries_after = WeeklyDelivery.objects.filter(
                            subscription=existing_subscription
                        )
                        logger.info(f"🔍 All WeeklyDeliveries after creation: {list(all_deliveries_after.values('id', 'delivery_date', 'shopping_list_id', 'status'))}")

                        logger.info(f"✅ Created SECOND delivery for subscription {existing_subscription.id} with shopping list '{delivery_data['shopping_list'].store_name}' on {next_delivery_date_2}")
                except Exception as e:
                    logger.error(f"❌ FAILED to create second delivery: {e}", exc_info=True)
                    return JsonResponse({
                        'error': 'upgrade_delivery_failed',
                        'message': f'Failed to create second delivery: {str(e)}'
                    }, status=500)
            else:
                logger.error(f"❌ No validated_deliveries found during upgrade. validated_deliveries: {validated_deliveries}")
                return JsonResponse({
                    'error': 'upgrade_validation_failed',
                    'message': 'No valid delivery found to add during upgrade'
                }, status=400)

            logger.info(f"✅ Upgraded existing subscription {existing_subscription.id} from basic to premium and added second delivery")
        elif is_activating_setup_complete:
            # User has setup_complete subscription and is now scheduling their FIRST delivery
            existing_subscription = existing_subs.first()
            primary_delivery = validated_deliveries[0]

            # Update existing subscription with delivery details
            existing_subscription.shopping_list = primary_delivery['shopping_list']
            existing_subscription.delivery_day = primary_delivery['delivery_day']
            existing_subscription.delivery_window = primary_delivery['delivery_window']
            existing_subscription.delivery_address = delivery_address
            existing_subscription.delivery_instructions = delivery_instructions
            existing_subscription.status = 'pending_confirmation'  # Now waiting for shopper
            existing_subscription.deliveries_this_cycle = 1

            # Set billing cycle now that first delivery is being scheduled
            target_weekday = day_map.get(primary_delivery['delivery_day'], 5)
            days_ahead = target_weekday - today.weekday()
            if days_ahead <= 0:
                days_ahead += 7
            next_delivery_date = today + timedelta(days=days_ahead)

            existing_subscription.billing_cycle_start = timezone.make_aware(
                datetime.combine(next_delivery_date, datetime.min.time())
            )
            existing_subscription.billing_cycle_end = existing_subscription.billing_cycle_start + timedelta(days=7)

            # Create Stripe subscription now (billing starts with first delivery)
            stripe_price_id = StripeService.get_price_id_for_tier(existing_subscription.subscription_tier)
            if stripe_price_id and existing_subscription.stripe_customer_id:
                success, stripe_subscription_id, error = StripeService.create_subscription(
                    customer_id=existing_subscription.stripe_customer_id,
                    price_id=stripe_price_id,
                    metadata={
                        'user_id': user.id,
                        'username': user.username,
                        'tier': existing_subscription.subscription_tier,
                        'delivery_count': 1,
                        'store_name': primary_delivery['shopping_list'].store_name
                    }
                )
                if success:
                    existing_subscription.stripe_subscription_id = stripe_subscription_id
                    logger.info(f"✅ Created Stripe subscription {stripe_subscription_id} for activated subscription")
                else:
                    logger.error(f"Failed to create Stripe subscription: {error}")
                    return JsonResponse({
                        'error': 'billing_failed',
                        'message': 'Could not start billing. Please check payment method.',
                        'details': error
                    }, status=500)

            existing_subscription.save()
            created_subscriptions.append(existing_subscription)

            # Create the WeeklyDelivery
            from django.db import transaction
            from .services.notification_service import NotificationService

            weekly_delivery = WeeklyDelivery.objects.create(
                subscription=existing_subscription,
                shopping_list=primary_delivery['shopping_list'],
                delivery_date=next_delivery_date,
                status='pending_shopper'
            )

            logger.info(f"✅ Activated setup_complete subscription {existing_subscription.id} with first delivery on {next_delivery_date}")

            # Notify shoppers
            def send_notifications(delivery_id=weekly_delivery.id):
                try:
                    delivery = WeeklyDelivery.objects.get(id=delivery_id)
                    approved_shoppers = User.objects.filter(
                        profile__account_type='shopper',
                        profile__is_approved_shopper=True,
                        profile__fcm_token__isnull=False
                    ).exclude(profile__fcm_token='')

                    for shopper in approved_shoppers:
                        NotificationService.send_new_delivery_available(
                            shopper=shopper,
                            delivery=delivery
                        )
                except WeeklyDelivery.DoesNotExist:
                    logger.error(f"WeeklyDelivery {delivery_id} not found for notification")

            transaction.on_commit(lambda: send_notifications(weekly_delivery.id))
        else:
            # Normal case: Create ONE subscription with multiple deliveries

            # Determine total deliveries count (for deliveries_this_cycle)
            total_delivery_count = len(validated_deliveries)
            if is_adding_second_delivery:
                # Adding to existing subscription - get it and add deliveries to it
                subscription = existing_subs.first()
                subscription.deliveries_this_cycle = 2  # Now has 2 deliveries
                subscription.save()
                logger.info(f"Adding delivery to existing subscription {subscription.id}")
            else:
                # Create NEW subscription for this user (Basic or new Premium)
                # For Premium tier, use first delivery's details as primary subscription info
                primary_delivery = validated_deliveries[0]

                subscription = DeliverySubscription.objects.create(
                    customer=user,
                    store=None,  # Will be linked when store becomes a partner
                    shopping_list=primary_delivery['shopping_list'],  # Use first delivery's list as primary
                    delivery_day=primary_delivery['delivery_day'],    # Use first delivery's day as primary
                    delivery_window=primary_delivery['delivery_window'], # Use first delivery's window as primary
                    delivery_address=delivery_address,
                    delivery_instructions=delivery_instructions,
                    subscription_tier=tier,
                    status='pending_confirmation',  # NOT active yet - waiting for shopper to accept
                    stripe_customer_id=stripe_customer_id,
                    stripe_subscription_id=stripe_subscription_id,
                    billing_cycle_start=billing_cycle_start,
                    billing_cycle_end=billing_cycle_end,
                    deliveries_this_cycle=total_delivery_count
                )
                logger.info(f"Created new {tier} subscription {subscription.id} for {user.username}")

            created_subscriptions.append(subscription)

            # Create WeeklyDelivery objects for each delivery under this ONE subscription
            from django.db import transaction
            from .services.notification_service import NotificationService

            for delivery_data in validated_deliveries:
                # Calculate next delivery date
                target_weekday = day_map.get(delivery_data['delivery_day'], 5)  # Default Saturday
                days_ahead = target_weekday - today.weekday()
                if days_ahead <= 0:
                    days_ahead += 7
                next_delivery_date = today + timedelta(days=days_ahead)

                weekly_delivery = WeeklyDelivery.objects.create(
                    subscription=subscription,  # Same subscription for all deliveries
                    shopping_list=delivery_data['shopping_list'],
                    delivery_date=next_delivery_date,
                    status='pending_shopper'  # Waiting for shopper to accept
                )

                logger.info(f"Created WeeklyDelivery {weekly_delivery.id} for subscription {subscription.id}: {delivery_data['shopping_list'].store_name} on {next_delivery_date}")

                # Notify all approved shoppers about new delivery (AFTER transaction commits)
                def send_notifications(delivery_id=weekly_delivery.id):
                    # Re-fetch delivery to ensure it exists after commit
                    try:
                        delivery = WeeklyDelivery.objects.get(id=delivery_id)
                        approved_shoppers = User.objects.filter(
                            profile__account_type='shopper',
                            profile__is_approved_shopper=True,
                            profile__fcm_token__isnull=False
                        ).exclude(profile__fcm_token='')

                        for shopper in approved_shoppers:
                            NotificationService.send_new_delivery_available(
                                shopper=shopper,
                                delivery=delivery
                            )
                    except WeeklyDelivery.DoesNotExist:
                        logger.error(f"WeeklyDelivery {delivery_id} not found for notification")

                # Schedule notification to run after database transaction commits
                transaction.on_commit(lambda: send_notifications(weekly_delivery.id))

        logger.info(f"Subscription created: {tier} with {len(created_subscriptions)} deliveries for {user.username}")

        # Calculate first delivery date for response
        first_new_delivery_day = created_subscriptions[0].delivery_day
        target_weekday = day_map.get(first_new_delivery_day, 5)
        days_ahead = target_weekday - today.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        first_delivery_date = today + timedelta(days=days_ahead)

        # Response message
        if is_adding_second_delivery:
            # User just added their 2nd delivery to existing premium
            new_store_name = created_subscriptions[0].shopping_list.store_name
            message = f'2nd delivery added! Pending shopper confirmation. We\'ll notify you within 24 hours.'
        elif is_upgrading_basic_to_premium:
            # User upgraded from basic to premium (Apple way: prorated billing)
            store_name = created_subscriptions[0].shopping_list.store_name
            message = f'Upgraded to Premium! You were charged the prorated difference. Your {created_subscriptions[0].delivery_day} delivery from {store_name} is pending shopper confirmation.'
        elif is_activating_setup_complete:
            # User activated their setup_complete subscription with first delivery
            store_name = created_subscriptions[0].shopping_list.store_name
            message = f'Delivery scheduled! We\'ll notify you within 24 hours once a shopper accepts your {created_subscriptions[0].delivery_day} delivery from {store_name}.'
        elif tier == 'basic':
            store_name = created_subscriptions[0].shopping_list.store_name
            message = f'Subscription pending confirmation. We\'ll notify you within 24 hours once a shopper accepts your {created_subscriptions[0].delivery_day} delivery from {store_name}.'
        else:
            # Premium: Handle 1 or 2 deliveries (new subscription)
            if len(created_subscriptions) == 1:
                store_name = created_subscriptions[0].shopping_list.store_name
                message = f'Premium subscription pending confirmation. We\'ll notify you within 24 hours once a shopper accepts your delivery from {store_name}.'
            else:
                # Check if same store or different stores
                store1_name = created_subscriptions[0].shopping_list.store_name
                store2_name = created_subscriptions[1].shopping_list.store_name

                if store1_name.lower() == store2_name.lower():
                    message = f'Premium subscription pending confirmation. We\'ll notify you within 24 hours once a shopper accepts your 2 deliveries from {store1_name}.'
                else:
                    message = f'Premium subscription pending confirmation. We\'ll notify you within 24 hours once a shopper accepts your deliveries from {store1_name} and {store2_name}.'

        # Get billing cycle from created subscription if not set (for setup_complete activation)
        if billing_cycle_start is None and created_subscriptions:
            billing_cycle_start = created_subscriptions[0].billing_cycle_start
            billing_cycle_end = created_subscriptions[0].billing_cycle_end

        return JsonResponse({
            'success': True,
            'subscription_ids': [s.id for s in created_subscriptions],
            'weekly_cost': 15.00 if tier == 'basic' else 30.00,
            'tier': tier,
            'status': 'pending_confirmation',
            'message': message,
            'billing_cycle_start': billing_cycle_start.date().isoformat() if billing_cycle_start else first_delivery_date.isoformat(),
            'billing_cycle_end': billing_cycle_end.date().isoformat() if billing_cycle_end else (first_delivery_date + timedelta(days=7)).isoformat(),
            'first_delivery': first_delivery_date.isoformat()
        })

    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        logger.error(f"Subscribe error: {e}", exc_info=True)
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
@require_firebase_auth
@require_account_type('customer')
@require_http_methods(["GET"])
def my_subscriptions(request):
    """
    Get customer's active and pending subscriptions

    GET /api/delivery/my-subscriptions/
    """
    try:
        subscriptions = DeliverySubscription.objects.filter(
            customer=request.user,
            status__in=['active', 'pending_confirmation', 'setup_complete']
        ).select_related('store', 'shopping_list')

        subs_data = []
        for sub in subscriptions:
            # Get ALL upcoming deliveries for this subscription (exclude cancelled)
            upcoming_deliveries = WeeklyDelivery.objects.filter(
                subscription=sub,
                delivery_date__gte=date.today()
            ).exclude(
                status='cancelled'
            ).order_by('delivery_date')

            # Create ONE entry per subscription (not per delivery)
            # For Premium subscriptions, we show the primary subscription info with next delivery details
            next_delivery_info = None
            all_deliveries_info = []  # For Premium users to see all their deliveries

            if upcoming_deliveries.exists():
                next_delivery = upcoming_deliveries.first()  # Get earliest delivery

                # Get shopper info if assigned
                shopper_info = None
                if next_delivery.shopper:
                    shopper_info = {
                        'name': next_delivery.shopper.username,
                        'phone': next_delivery.shopper.username  # Username is phone number
                    }

                next_delivery_info = {
                    'id': next_delivery.id,
                    'date': next_delivery.delivery_date.isoformat(),
                    'status': next_delivery.status,
                    'store_name': next_delivery.shopping_list.store_name if next_delivery.shopping_list else 'Unknown Store',
                    'delivery_day': next_delivery.delivery_date.strftime('%A') if next_delivery.delivery_date else sub.delivery_day,
                    'shopper': shopper_info
                }

                # Collect all delivery info (for both Basic and Premium)
                # Basic has 1 delivery, Premium can have up to 2
                for delivery in upcoming_deliveries:
                    all_deliveries_info.append({
                        'id': delivery.id,
                        'date': delivery.delivery_date.isoformat(),
                        'status': delivery.status,
                        'store_name': delivery.shopping_list.store_name if delivery.shopping_list else 'Unknown Store',
                        'delivery_day': delivery.delivery_date.strftime('%A') if delivery.delivery_date else sub.delivery_day,
                        'shopping_list_id': delivery.shopping_list.id if delivery.shopping_list else None,
                        'item_count': delivery.shopping_list.list_items.count() if delivery.shopping_list else 0
                    })

            subs_data.append({
                'id': sub.id,
                'status': sub.status,
                'store': {
                    'id': sub.shopping_list.id if sub.shopping_list else 0,
                    'name': sub.shopping_list.store_name if sub.shopping_list else 'Unknown Store',
                    'address': sub.shopping_list.store_location if sub.shopping_list else ''
                },
                'shopping_list': {
                    'id': sub.shopping_list.id,
                    'store_name': sub.shopping_list.store_name,
                    'item_count': sub.shopping_list.list_items.count()
                } if sub.shopping_list else None,
                'delivery_day': sub.delivery_day,
                'delivery_window': sub.delivery_window,
                'delivery_address': sub.delivery_address,
                'delivery_instructions': sub.delivery_instructions,
                'subscription_tier': sub.subscription_tier,
                'billing_cycle_start': sub.billing_cycle_start if sub.billing_cycle_start else None,
                'billing_cycle_end': sub.billing_cycle_end if sub.billing_cycle_end else None,
                'deliveries_this_cycle': sub.deliveries_this_cycle,
                'pending_schedule': sub.pending_schedule,
                'next_delivery': next_delivery_info,
                'all_deliveries': all_deliveries_info,  # All deliveries for Premium users
                'total_upcoming_deliveries': upcoming_deliveries.count()
            })

        # Add upgrade capability information for iOS app
        user_profile, _ = UserProfile.objects.get_or_create(user=request.user)
        has_payment_method = bool(user_profile.stripe_customer_id and user_profile.default_payment_method)

        # Check for upgradeable subscriptions (same logic as subscribe endpoint)
        upgrade_check_subs = DeliverySubscription.objects.filter(
            customer=request.user,
            status__in=['active', 'pending_confirmation', 'setup_complete']
        )
        can_upgrade = (
            len(subs_data) == 1 and
            subs_data[0]['subscription_tier'] == 'basic' and
            has_payment_method
        )

        return JsonResponse({
            'subscriptions': subs_data,
            'upgrade_info': {
                'can_upgrade': can_upgrade,
                'has_payment_method': has_payment_method,
                'current_tier': subs_data[0]['subscription_tier'] if subs_data else None
            }
        })

    except Exception as e:
        logger.error(f"Get subscriptions error: {e}", exc_info=True)
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
@require_firebase_auth
@require_account_type('customer')
@require_http_methods(["GET"])
def billing_history(request):
    """
    Get customer's billing history from Stripe

    GET /api/delivery/billing-history/?limit=12

    Returns subscription charges, upgrades, and billing events
    """
    try:
        # Get user's profile and Stripe customer ID
        user = request.user
        user_profile, _ = UserProfile.objects.get_or_create(user=user)

        if not user_profile.stripe_customer_id:
            return JsonResponse({
                'charges': [],
                'total_count': 0,
                'message': 'No billing history yet'
            })

        # Get limit from query params (default 12 months)
        limit = int(request.GET.get('limit', 12))

        # Fetch charges from Stripe
        import stripe
        try:
            charges = stripe.Charge.list(
                customer=user_profile.stripe_customer_id,
                limit=limit,
                expand=['data.invoice']
            )

            billing_data = []
            for charge in charges.data:
                # Get charge details
                charge_date = datetime.fromtimestamp(charge.created).date()
                amount = charge.amount / 100  # Convert cents to dollars

                # Determine charge type and description
                description = charge.description or "Weekly Delivery"
                charge_type = "subscription"

                # Check if it's an upgrade charge (prorated billing)
                if hasattr(charge, 'invoice') and charge.invoice and 'proration' in str(charge.invoice):
                    charge_type = "upgrade"
                    description = "Premium Upgrade (Prorated)"
                elif "upgrade" in description.lower():
                    charge_type = "upgrade"

                # Get last 4 digits of payment method
                payment_method = "••••"
                try:
                    if (hasattr(charge, 'payment_method_details') and
                        charge.payment_method_details and
                        hasattr(charge.payment_method_details, 'card') and
                        charge.payment_method_details.card and
                        hasattr(charge.payment_method_details.card, 'last4')):
                        payment_method = "••••" + charge.payment_method_details.card.last4
                except (AttributeError, KeyError):
                    payment_method = "••••"

                billing_data.append({
                    'id': charge.id,
                    'date': charge_date.isoformat(),
                    'amount': amount,
                    'description': description,
                    'type': charge_type,  # 'subscription', 'upgrade', 'delivery'
                    'status': charge.status,  # 'succeeded', 'failed', etc.
                    'payment_method': payment_method,
                    'receipt_url': charge.receipt_url
                })

            logger.info(f"Retrieved {len(billing_data)} billing records for {user.username}")

            return JsonResponse({
                'charges': billing_data,
                'total_count': len(billing_data),
                'customer_id': user_profile.stripe_customer_id
            })

        except stripe.error.StripeError as e:
            logger.error(f"Stripe error fetching billing history: {e}")
            return JsonResponse({
                'charges': [],
                'total_count': 0,
                'error': 'Could not fetch billing history'
            }, status=500)

    except Exception as e:
        logger.error(f"Billing history error: {e}", exc_info=True)
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
@require_firebase_auth
@require_account_type('customer')
@require_http_methods(["GET"])
def delivery_history(request):
    """
    Get customer's delivery history (actual grocery deliveries with receipts)

    GET /api/delivery/delivery-history/?limit=20

    Returns completed deliveries with shopping details and charges
    """
    try:
        user = request.user

        # Get limit from query params (default 20 deliveries)
        limit = int(request.GET.get('limit', 20))

        # Fetch completed deliveries for this customer
        from .models import WeeklyDelivery

        deliveries = WeeklyDelivery.objects.filter(
            subscription__customer=user,
            status='delivered'
        ).select_related(
            'subscription__shopping_list',
            'shopping_trip'
        ).order_by('-delivery_date')[:limit]

        delivery_data = []
        for delivery in deliveries:
            # Get shopping list details
            shopping_list = delivery.subscription.shopping_list
            store_name = shopping_list.store_name if shopping_list else "Unknown Store"

            # Get delivery cost from shopping trip (receipt scan)
            delivery_cost = None
            receipt_url = None
            if hasattr(delivery, 'shopping_trip') and delivery.shopping_trip:
                shopping_trip = delivery.shopping_trip
                delivery_cost = shopping_trip.total_amount  # Use total_amount instead of receipt_total
                if shopping_trip.receipt_image:
                    receipt_url = shopping_trip.receipt_image.url

            delivery_data.append({
                'id': delivery.id,
                'date': delivery.delivery_date.isoformat(),
                'store_name': store_name,
                'store_location': shopping_list.store_location if shopping_list else "",
                'delivery_cost': delivery_cost,  # Actual grocery cost
                'item_count': shopping_list.list_items.count() if shopping_list else 0,
                'delivery_window': delivery.subscription.delivery_window,
                'delivery_address': delivery.subscription.delivery_address,
                'status': delivery.status,
                'delivered_at': delivery.delivered_at.isoformat() if delivery.delivered_at else None,
                'receipt_url': receipt_url,
                'subscription_tier': delivery.subscription.subscription_tier
            })

        logger.info(f"Retrieved {len(delivery_data)} delivery records for {user.username}")

        return JsonResponse({
            'deliveries': delivery_data,
            'total_count': len(delivery_data)
        })

    except Exception as e:
        logger.error(f"Delivery history error: {e}", exc_info=True)
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
@require_firebase_auth
@require_account_type('customer')
@require_http_methods(["POST"])
def remove_delivery(request):
    """
    Remove a specific delivery from subscription (clean, single-purpose endpoint)

    POST /api/delivery/remove-delivery/
    Body: {
        "delivery_id": 123,           # Specific delivery to remove
        OR
        "shopping_list_id": 456       # Remove delivery for this list
    }

    For Premium users with 2 deliveries, this removes just one.
    Subscription remains active.
    """
    try:
        data = json.loads(request.body)
        user = request.user

        delivery_id = data.get('delivery_id')
        shopping_list_id = data.get('shopping_list_id')

        if not delivery_id and not shopping_list_id:
            return JsonResponse({
                'error': 'missing_identifier',
                'message': 'Provide either delivery_id or shopping_list_id'
            }, status=400)

        # Get user's subscription
        subscription = DeliverySubscription.objects.filter(
            customer=user,
            status__in=['active', 'pending_confirmation', 'setup_complete']
        ).first()

        if not subscription:
            return JsonResponse({
                'error': 'no_subscription',
                'message': 'No active subscription found'
            }, status=404)

        # Find the specific delivery to remove
        if delivery_id:
            try:
                delivery = WeeklyDelivery.objects.get(
                    id=delivery_id,
                    subscription=subscription,
                    delivery_date__gte=date.today()
                )
            except WeeklyDelivery.DoesNotExist:
                return JsonResponse({
                    'error': 'delivery_not_found',
                    'message': 'Delivery not found or already past'
                }, status=404)
        else:
            # Find by shopping list ID (exclude delivered and cancelled)
            delivery = WeeklyDelivery.objects.filter(
                subscription=subscription,
                shopping_list_id=shopping_list_id,
                delivery_date__gte=date.today()
            ).exclude(status__in=['cancelled', 'delivered']).first()

            if not delivery:
                return JsonResponse({
                    'error': 'delivery_not_found',
                    'message': 'No active delivery found for this list'
                }, status=404)

        # Check if delivery is in progress (can't cancel)
        if delivery.status in ['packing', 'ready', 'out_for_delivery']:
            status_messages = {
                'packing': 'Your shopper is preparing this order. Cannot cancel.',
                'ready': 'Your shopper has finished shopping. Cannot cancel.',
                'out_for_delivery': 'Order is out for delivery. Cannot cancel.'
            }
            return JsonResponse({
                'error': 'delivery_in_progress',
                'message': status_messages.get(delivery.status, 'Delivery in progress')
            }, status=400)

        # Cancel just this one delivery
        store_name = delivery.shopping_list.store_name if delivery.shopping_list else 'Unknown'
        delivery.status = 'cancelled'
        delivery.save()

        logger.info(f"✅ Removed delivery {delivery.id} ({store_name}) for user {user.username}")

        # Count remaining active deliveries (exclude delivered and cancelled)
        remaining_deliveries = WeeklyDelivery.objects.filter(
            subscription=subscription,
            delivery_date__gte=date.today()
        ).exclude(status__in=['cancelled', 'delivered']).count()

        # Update subscription delivery count
        subscription.deliveries_this_cycle = remaining_deliveries
        subscription.save()

        return JsonResponse({
            'success': True,
            'message': f'Delivery from {store_name} removed.',
            'remaining_deliveries': remaining_deliveries,
            'subscription_active': True
        })

    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        logger.error(f"Remove delivery error: {e}", exc_info=True)
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
@require_firebase_auth
@require_account_type('customer')
@require_http_methods(["POST"])
def cancel_subscription(request):
    """
    Cancel delivery subscription

    POST /api/delivery/cancel/
    Body: { subscription_id: int }

    Prevents cancellation if shopper has started working on the order:
    - status = 'packing': Shopper is shopping
    - status = 'out_for_delivery': Shopper is delivering
    """
    try:
        data = json.loads(request.body)
        subscription_id = data.get('subscription_id')

        if not subscription_id:
            return JsonResponse({'error': 'Missing subscription_id'}, status=400)

        try:
            subscription = DeliverySubscription.objects.get(
                id=subscription_id,
                customer=request.user,
                status__in=['active', 'pending_confirmation', 'setup_complete']
            )
        except DeliverySubscription.DoesNotExist:
            return JsonResponse({'error': 'Subscription not found or already cancelled'}, status=404)

        # Check how many active subscriptions this user has (for Premium downgrade logic)
        user_active_subscriptions = DeliverySubscription.objects.filter(
            customer=request.user,
            status__in=['active', 'pending_confirmation', 'setup_complete']
        )
        total_subscriptions = user_active_subscriptions.count()
        is_premium_with_multiple = (total_subscriptions > 1 and subscription.subscription_tier == 'premium')

        logger.info(f"Cancel request: user has {total_subscriptions} subscriptions, removing subscription {subscription_id} (tier: {subscription.subscription_tier})")

        # Check ALL upcoming deliveries for this subscription
        upcoming_deliveries = WeeklyDelivery.objects.filter(
            subscription=subscription,
            delivery_date__gte=date.today(),
            status__in=['pending_shopper', 'scheduled', 'packing', 'out_for_delivery']
        ).order_by('delivery_date')

        # BLOCK if ANY shopper has started packing or delivering
        blocking_deliveries = upcoming_deliveries.filter(status__in=['packing', 'ready', 'out_for_delivery'])
        if blocking_deliveries.exists():
            blocking_delivery = blocking_deliveries.first()
            status_messages = {
                'packing': 'Your shopper is preparing this order and cannot be cancelled. Please contact support if needed.',
                'ready': 'Your shopper has finished shopping and cannot be cancelled. Please contact support if needed.',
                'out_for_delivery': 'Your order is out for delivery and cannot be cancelled. Please contact support if needed.'
            }
            return JsonResponse({
                'error': 'order_in_progress',
                'message': status_messages.get(blocking_delivery.status, 'Order in progress'),
                'delivery_status': blocking_delivery.status
            }, status=400)

        # ALLOWED: Cancel ALL upcoming deliveries if none are in progress
        cancelled_count = 0
        for delivery in upcoming_deliveries:
            if delivery.status in ['pending_shopper', 'scheduled']:
                delivery.status = 'cancelled'
                delivery.save()
                cancelled_count += 1
                logger.info(f"Cancelled WeeklyDelivery {delivery.id} (was {delivery.status}) for subscription {subscription_id}")

        if cancelled_count > 0:
            logger.info(f"Cancelled {cancelled_count} upcoming deliveries for subscription {subscription_id}")

        # Handle subscription based on user's remaining deliveries
        # NEVER auto-cancel subscriptions - users manage billing in Profile
        if is_premium_with_multiple:
            # Premium user removing 1 of 2 deliveries → Keep Premium active
            logger.info(f"Premium user removing 1 delivery: keeping Premium subscription active")

            # Update remaining subscription delivery count but keep Premium tier and pricing
            remaining_subscription = user_active_subscriptions.exclude(id=subscription_id).first()
            if remaining_subscription:
                remaining_subscription.deliveries_this_cycle = 1  # Now has 1 delivery
                remaining_subscription.save()
                logger.info(f"Updated remaining subscription {remaining_subscription.id} delivery count to 1 (staying Premium)")

            # NO Stripe changes - keep Premium subscription and billing active

            # Mark THIS subscription as cancelled since user now has only the other one
            subscription.status = 'cancelled'
            subscription.save()

        else:
            # Last delivery - but DON'T auto-cancel subscription (Apple way)
            logger.info(f"User removing last delivery: keeping subscription active, no deliveries scheduled")

            # NO Stripe cancellation - user keeps their subscription tier and can add deliveries anytime
            # They manage subscription cancellation manually in Profile > Subscriptions

            # KEEP subscription active but with no deliveries (Apple-style UX)
            # User maintains their Basic/Premium tier and can schedule new deliveries
            subscription.status = 'active'  # Keep active for easy re-engagement
            subscription.save()
            logger.info(f"Kept subscription {subscription_id} active with no scheduled deliveries (Apple-style)")

        # Clear messaging about subscription vs delivery
        if is_premium_with_multiple:
            message = 'Delivery removed. You still have Premium access and 1 active delivery.'
        elif subscription.subscription_tier == 'premium':
            message = 'Delivery removed. Your Premium subscription remains active - add another delivery anytime. To cancel subscription and stop billing, go to Profile > Subscriptions.'
        else:
            message = 'Delivery removed. Your Basic subscription remains active - add another delivery anytime. To cancel subscription and stop billing, go to Profile > Subscriptions.'

        logger.info(f"Subscription {subscription_id} cancelled by {request.user.username}")

        return JsonResponse({
            'success': True,
            'message': message
        })

    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        logger.error(f"Cancel subscription error: {e}", exc_info=True)
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
@require_firebase_auth
@require_account_type('customer')
@require_http_methods(["POST"])
def cancel_subscription_completely(request):
    """
    Completely cancel subscription and stop all billing

    POST /api/delivery/cancel-subscription-completely/
    Body: { subscription_id: int }

    This actually cancels the Stripe subscription and stops billing,
    unlike cancel_subscription which just removes deliveries but keeps subscription active.
    """
    try:
        data = json.loads(request.body)
        subscription_id = data.get('subscription_id')

        if not subscription_id:
            return JsonResponse({'error': 'Missing subscription_id'}, status=400)

        try:
            subscription = DeliverySubscription.objects.get(
                id=subscription_id,
                customer=request.user,
                status__in=['active', 'pending_confirmation', 'setup_complete']
            )
        except DeliverySubscription.DoesNotExist:
            return JsonResponse({'error': 'Subscription not found or already cancelled'}, status=404)

        # Check ALL upcoming deliveries for this subscription
        upcoming_deliveries = WeeklyDelivery.objects.filter(
            subscription=subscription,
            delivery_date__gte=date.today(),
            status__in=['pending_shopper', 'scheduled', 'packing', 'out_for_delivery']
        ).order_by('delivery_date')

        # BLOCK if ANY shopper has started packing or delivering
        blocking_deliveries = upcoming_deliveries.filter(status__in=['packing', 'ready', 'out_for_delivery'])
        if blocking_deliveries.exists():
            blocking_delivery = blocking_deliveries.first()
            status_messages = {
                'packing': 'Your shopper is preparing this order and subscription cannot be cancelled. Please contact support if needed.',
                'ready': 'Your shopper has finished shopping and subscription cannot be cancelled. Please contact support if needed.',
                'out_for_delivery': 'Your order is out for delivery and subscription cannot be cancelled. Please contact support if needed.'
            }
            return JsonResponse({
                'error': 'order_in_progress',
                'message': status_messages.get(blocking_delivery.status, 'Order in progress'),
                'delivery_status': blocking_delivery.status
            }, status=400)

        # Cancel ALL upcoming deliveries first
        cancelled_count = 0
        for delivery in upcoming_deliveries:
            if delivery.status in ['pending_shopper', 'scheduled']:
                delivery.status = 'cancelled'
                delivery.save()
                cancelled_count += 1
                logger.info(f"Cancelled WeeklyDelivery {delivery.id} for complete subscription cancellation")

        # Cancel the Stripe subscription to stop billing
        if subscription.stripe_subscription_id:
            success, error = StripeService.cancel_subscription(subscription.stripe_subscription_id)
            if not success:
                logger.error(f"Failed to cancel Stripe subscription {subscription.stripe_subscription_id}: {error}")
                return JsonResponse({
                    'error': 'billing_cancellation_failed',
                    'message': 'Could not cancel billing. Please contact support.',
                    'details': error
                }, status=500)

            logger.info(f"✅ Cancelled Stripe subscription {subscription.stripe_subscription_id}")

        # Mark subscription as completely cancelled
        subscription.status = 'cancelled'
        subscription.save()

        logger.info(f"✅ Completely cancelled subscription {subscription_id} for {request.user.username}")

        return JsonResponse({
            'success': True,
            'message': 'Subscription cancelled successfully. You will not be charged again.',
            'cancelled_deliveries': cancelled_count
        })

    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        logger.error(f"Complete subscription cancellation error: {e}", exc_info=True)
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
@require_firebase_auth
@require_account_type('customer')
@require_http_methods(["POST"])
def modify_subscription(request):
    """
    Modify delivery subscription (day, time, store, or shopping list)

    POST /api/delivery/modify/
    Body: {
        subscription_id: int,
        delivery_day: string (optional),
        delivery_window: string (optional),
        store_id: int (optional),
        shopping_list_id: int (optional)
    }

    Prevents modifications if shopper has started working on the order.
    Otherwise, changes apply immediately if delivery is >24h away,
    or queued for next billing cycle if within current cycle.
    """
    try:
        data = json.loads(request.body)
        subscription_id = data.get('subscription_id')

        if not subscription_id:
            return JsonResponse({'error': 'Missing subscription_id'}, status=400)

        try:
            # Allow modifications for active, pending_confirmation, and setup_complete subscriptions
            subscription = DeliverySubscription.objects.get(
                id=subscription_id,
                customer=request.user,
                status__in=['active', 'pending_confirmation', 'setup_complete']
            )
        except DeliverySubscription.DoesNotExist:
            return JsonResponse({'error': 'Subscription not found or already cancelled'}, status=404)

        # BLOCK modifications if shopper has started working on upcoming delivery
        upcoming_delivery_check = WeeklyDelivery.objects.filter(
            subscription=subscription,
            delivery_date__gte=date.today(),
            status__in=['packing', 'ready', 'out_for_delivery']
        ).first()

        if upcoming_delivery_check:
            status_messages = {
                'packing': 'Your shopper is preparing this order. Changes cannot be made.',
                'ready': 'Your shopper has finished shopping. Changes cannot be made.',
                'out_for_delivery': 'Your order is out for delivery. Changes cannot be made.'
            }
            return JsonResponse({
                'error': 'order_in_progress',
                'message': status_messages.get(upcoming_delivery_check.status, 'Order in progress'),
                'delivery_status': upcoming_delivery_check.status
            }, status=400)

        # Build pending schedule changes
        pending_changes = {}

        if 'delivery_day' in data:
            pending_changes['delivery_day'] = data['delivery_day']

        if 'delivery_window' in data:
            pending_changes['delivery_window'] = data['delivery_window']

        if 'store_id' in data:
            # Validate store exists
            try:
                store = Store.objects.get(id=data['store_id'], is_active=True)
                pending_changes['store_id'] = store.id
                pending_changes['store_name'] = store.name
            except Store.DoesNotExist:
                return JsonResponse({'error': f'Store {data["store_id"]} not found'}, status=404)

        if 'shopping_list_id' in data:
            # Validate shopping list exists and belongs to user
            try:
                shopping_list = ShoppingList.objects.get(id=data['shopping_list_id'], user=request.user)
                pending_changes['shopping_list_id'] = shopping_list.id
            except ShoppingList.DoesNotExist:
                return JsonResponse({'error': f'Shopping list {data["shopping_list_id"]} not found'}, status=404)

        if not pending_changes:
            return JsonResponse({'error': 'No changes provided'}, status=400)

        # Check if we're within current billing cycle OR have upcoming deliveries
        now = timezone.now()
        within_cycle = (subscription.billing_cycle_start and
                       subscription.billing_cycle_end and
                       subscription.billing_cycle_start <= now < subscription.billing_cycle_end)

        # Check if there's a scheduled delivery coming up (including pending_shopper status)
        upcoming_delivery = WeeklyDelivery.objects.filter(
            subscription=subscription,
            delivery_date__gte=date.today(),
            status__in=['scheduled', 'pending_shopper', 'assigned']  # Include pending_shopper for unassigned deliveries
        ).first()

        # Allow changes if delivery is more than 24 hours away
        can_change_immediately = False
        if upcoming_delivery:
            # Calculate hours until delivery (assume delivery at start of window)
            # For simplicity, compare dates: if delivery is tomorrow or later, allow change
            days_until_delivery = (upcoming_delivery.delivery_date - date.today()).days
            can_change_immediately = days_until_delivery > 1  # More than 1 day away

        # For pending_shopper deliveries (no shopper assigned yet), always allow immediate changes
        if upcoming_delivery and upcoming_delivery.status == 'pending_shopper':
            can_change_immediately = True

        # If there's an upcoming delivery within 24h OR we're in billing cycle, queue changes
        if (within_cycle or (upcoming_delivery and not can_change_immediately)):
            # Queue changes for next billing cycle
            subscription.pending_schedule = pending_changes
            subscription.save()

            logger.info(f"Queued subscription {subscription_id} changes for next cycle: {pending_changes}")

            # Determine when changes will apply
            if upcoming_delivery:
                applies_message = f"Changes will take effect after your next delivery on {upcoming_delivery.delivery_date.isoformat()}"
            else:
                applies_message = "Changes will take effect at next billing cycle"

            return JsonResponse({
                'success': True,
                'message': applies_message,
                'applies_on': subscription.billing_cycle_end.date().isoformat(),
                'pending_changes': pending_changes,
                'current_schedule': {
                    'delivery_day': subscription.delivery_day,
                    'delivery_window': subscription.delivery_window,
                    'store_id': subscription.store.id if subscription.store else None,
                    'store_name': subscription.store.name if subscription.store else None,
                    'shopping_list_id': subscription.shopping_list.id if subscription.shopping_list else None
                }
            })
        else:
            # Apply changes immediately (delivery is >24h away or no upcoming delivery)
            if 'delivery_day' in pending_changes:
                subscription.delivery_day = pending_changes['delivery_day']

            if 'delivery_window' in pending_changes:
                subscription.delivery_window = pending_changes['delivery_window']

            if 'store_id' in pending_changes:
                subscription.store = Store.objects.get(id=pending_changes['store_id'])

            if 'shopping_list_id' in pending_changes:
                subscription.shopping_list = ShoppingList.objects.get(id=pending_changes['shopping_list_id'])

            subscription.save()

            # If there's an upcoming delivery >24h away (or pending_shopper), update it too
            if upcoming_delivery and can_change_immediately:
                # Update the shopping list if changed
                if 'shopping_list_id' in pending_changes:
                    upcoming_delivery.shopping_list = subscription.shopping_list

                # Update delivery window if changed
                if 'delivery_window' in pending_changes:
                    upcoming_delivery.delivery_window = pending_changes['delivery_window']

                # If day changed, need to recalculate delivery date
                if 'delivery_day' in pending_changes:
                    day_map = {
                        'Monday': 0, 'Tuesday': 1, 'Wednesday': 2, 'Thursday': 3,
                        'Friday': 4, 'Saturday': 5, 'Sunday': 6
                    }
                    target_weekday = day_map.get(pending_changes['delivery_day'], 5)
                    today = date.today()
                    days_ahead = target_weekday - today.weekday()
                    if days_ahead <= 0:
                        days_ahead += 7
                    upcoming_delivery.delivery_date = today + timedelta(days=days_ahead)

                upcoming_delivery.save()
                logger.info(f"Updated WeeklyDelivery {upcoming_delivery.id} with new schedule: {pending_changes}")

            logger.info(f"Applied subscription {subscription_id} changes immediately: {pending_changes}")

            return JsonResponse({
                'success': True,
                'message': 'Changes applied successfully and will take effect starting with your next delivery',
                'applied_immediately': True
            })

    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        logger.error(f"Modify subscription error: {e}", exc_info=True)
        return JsonResponse({'error': str(e)}, status=500)


# ========================================
# STORE APIs
# ========================================

@csrf_exempt
@require_firebase_auth
@require_account_type('store', 'store_owner')
@require_http_methods(["GET"])
def store_deliveries(request):
    """
    Get all deliveries for store on specified date

    GET /api/store/deliveries/?date=2025-12-07
    """
    try:
        profile = request.user_profile
        if not profile.store:
            return JsonResponse({'error': 'No store assigned'}, status=400)

        # Get date from query param (default tomorrow for packing)
        date_str = request.GET.get('date')
        if date_str:
            delivery_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        else:
            delivery_date = date.today() + timedelta(days=1)

        deliveries = WeeklyDelivery.objects.filter(
            subscription__store=profile.store,
            delivery_date=delivery_date
        ).select_related('subscription__customer', 'subscription__shopping_list', 'shopper')

        deliveries_data = []
        for d in deliveries:
            shopping_list = d.subscription.shopping_list
            items = shopping_list.list_items.filter(is_checked=True) if shopping_list else []

            deliveries_data.append({
                'id': d.id,
                'customer_name': d.subscription.customer.username,
                'delivery_window': d.subscription.delivery_window,
                'delivery_address': d.subscription.delivery_address,
                'is_premium': d.subscription.subscription_tier == 'premium',
                'status': d.status,
                'shopping_list_id': shopping_list.id if shopping_list else None,
                'items_to_pack': items.count(),
                'shopper_name': d.shopper.full_name if d.shopper else None
            })

        return JsonResponse({
            'date': delivery_date.isoformat(),
            'total_deliveries': len(deliveries_data),
            'deliveries': deliveries_data
        })

    except Exception as e:
        logger.error(f"Store deliveries error: {e}", exc_info=True)
        return JsonResponse({'error': str(e)}, status=500)


# ========================================
# SHOPPER APIs
# ========================================

@csrf_exempt
@require_firebase_auth
@require_account_type('shopper')
@require_http_methods(["GET"])
def shopper_route(request):
    """
    Get shopper's delivery route for specified date

    GET /api/shopper/route/?date=2025-12-07
    """
    try:
        try:
            shopper = Shopper.objects.get(user=request.user)
        except Shopper.DoesNotExist:
            return JsonResponse({'error': 'Shopper profile not found'}, status=404)

        # Get date (default today)
        date_str = request.GET.get('date')
        if date_str:
            delivery_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        else:
            delivery_date = date.today()

        deliveries = WeeklyDelivery.objects.filter(
            shopper=shopper,
            delivery_date=delivery_date
        ).select_related('subscription__customer', 'subscription__store', 'subscription__shopping_list')

        route_data = []
        for d in deliveries:
            shopping_list = d.subscription.shopping_list
            items_to_pack = shopping_list.list_items.filter(is_checked=True).count() if shopping_list else 0

            route_data.append({
                'id': d.id,
                'customer_name': d.subscription.customer.username,
                'delivery_address': d.subscription.delivery_address,
                'delivery_window': d.subscription.delivery_window,
                'delivery_instructions': d.subscription.delivery_instructions,
                'is_premium': d.subscription.subscription_tier == 'premium',
                'status': d.status,
                'store_name': d.subscription.store.name,
                'store_address': d.subscription.store.address,
                'shopping_list_id': shopping_list.id if shopping_list else None,
                'items_count': items_to_pack
            })

        return JsonResponse({
            'date': delivery_date.isoformat(),
            'total_deliveries': len(route_data),
            'deliveries': route_data
        })

    except Exception as e:
        logger.error(f"Shopper route error: {e}", exc_info=True)
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
@require_firebase_auth
@require_account_type('shopper')
@require_http_methods(["POST"])
def mark_delivered(request):
    """
    Mark delivery complete

    POST /api/shopper/mark-delivered/
    Body: { delivery_id: int }
    """
    try:
        data = json.loads(request.body)
        delivery_id = data.get('delivery_id')

        if not delivery_id:
            return JsonResponse({'error': 'Missing delivery_id'}, status=400)

        delivery = WeeklyDelivery.objects.get(id=delivery_id, shopper__user=request.user)
        delivery.status = 'delivered'
        delivery.delivered_at = timezone.now()
        delivery.save()

        # Update shopper stats
        shopper = delivery.shopper
        shopper.total_deliveries += 1
        shopper.save()

        logger.info(f"Delivery {delivery_id} marked delivered by {request.user.username}")

        return JsonResponse({'success': True})

    except WeeklyDelivery.DoesNotExist:
        return JsonResponse({'error': 'Delivery not found'}, status=404)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        logger.error(f"Mark delivered error: {e}", exc_info=True)
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
@require_firebase_auth
@require_account_type('shopper')
@require_http_methods(["POST"])
def shopper_respond_to_delivery(request):
    """
    Shopper accepts or rejects a pending delivery

    POST /api/delivery/shopper-respond/
    Body: {
        "delivery_id": 123,
        "action": "accept" | "reject",
        "reason": "Optional reason for rejection"
    }

    For acceptance: Changes subscription from pending_confirmation → active, starts billing
    For rejection: Immediately cancels subscription, suggests alternatives
    """
    try:
        data = json.loads(request.body)
        delivery_id = data.get('delivery_id')
        action = data.get('action')  # 'accept' or 'reject'
        reason = data.get('reason', '')

        if not delivery_id or action not in ['accept', 'reject']:
            return JsonResponse({
                'error': 'Missing delivery_id or invalid action (use "accept" or "reject")'
            }, status=400)

        # Get the delivery and subscription
        try:
            delivery = WeeklyDelivery.objects.get(id=delivery_id, status='scheduled')
            subscription = delivery.subscription
        except WeeklyDelivery.DoesNotExist:
            return JsonResponse({
                'error': 'Delivery not found or already processed'
            }, status=404)

        # Verify shopper is in service area (future: assignment logic)
        shopper = request.user

        if action == 'accept':
            # ACCEPT: Activate subscription and start billing
            delivery.shopper = shopper
            delivery.status = 'assigned'
            delivery.save()

            # Activate subscription (starts Stripe billing cycle)
            subscription.status = 'active'
            subscription.save()

            logger.info(f"Shopper {shopper.username} accepted delivery {delivery_id}")

            # Send success notification to customer
            from .services.notification_service import NotificationService
            NotificationService.send_delivery_accepted(
                user=subscription.customer,
                delivery_date=delivery.delivery_date,
                shopper_name=shopper.first_name or shopper.username
            )

            return JsonResponse({
                'success': True,
                'action': 'accepted',
                'message': f'Delivery accepted! Customer will be notified.',
                'delivery_status': delivery.status,
                'subscription_status': subscription.status
            })

        else:
            # REJECT: Immediately cancel subscription with helpful suggestions
            delivery.status = 'cancelled'
            delivery.save()

            subscription.status = 'cancelled'
            subscription.save()

            # Cancel Stripe subscription (no billing occurred yet)
            if subscription.stripe_subscription_id:
                success, error = StripeService.cancel_subscription(subscription.stripe_subscription_id)
                if not success:
                    logger.warning(f"Failed to cancel Stripe subscription {subscription.stripe_subscription_id}: {error}")

            logger.info(f"Shopper {shopper.username} rejected delivery {delivery_id}. Reason: {reason}")

            # Find alternative suggestions for customer
            suggestions = find_delivery_alternatives(subscription)

            # Send rejection notification with alternatives to customer
            from .services.notification_service import NotificationService
            NotificationService.send_delivery_rejected(
                user=subscription.customer,
                delivery_date=delivery.delivery_date,
                reason=reason,
                suggestions=suggestions
            )

            return JsonResponse({
                'success': True,
                'action': 'rejected',
                'message': f'Delivery rejected. Customer notified with alternatives.',
                'reason': reason,
                'suggestions': suggestions,
                'subscription_status': 'cancelled'
            })

    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        logger.error(f"Shopper respond error: {e}", exc_info=True)
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
@require_firebase_auth
@require_account_type('shopper')
@require_http_methods(["POST"])
def shopper_release_delivery(request):
    """
    Shopper releases an assigned delivery back to available pool

    POST /api/delivery/shopper-release/
    Body: {
        "delivery_id": 123,
        "reason": "Optional reason for release"
    }

    Only allowed when status is 'assigned' (before packing starts).
    Delivery goes back to 'scheduled' status for other shoppers to accept.
    """
    try:
        data = json.loads(request.body)
        delivery_id = data.get('delivery_id')
        reason = data.get('reason', '')

        if not delivery_id:
            return JsonResponse({'error': 'Missing delivery_id'}, status=400)

        # Get the delivery
        try:
            delivery = WeeklyDelivery.objects.get(id=delivery_id)
        except WeeklyDelivery.DoesNotExist:
            return JsonResponse({'error': 'Delivery not found'}, status=404)

        # Verify this shopper owns this delivery
        if delivery.shopper != request.user:
            return JsonResponse({'error': 'You are not assigned to this delivery'}, status=403)

        # Only allow release when status is 'assigned' (before packing)
        if delivery.status != 'assigned':
            status_messages = {
                'packing': 'Cannot release - you have already started shopping. Please complete or contact admin.',
                'ready': 'Cannot release - order is ready for delivery. Please complete or contact admin.',
                'out_for_delivery': 'Cannot release - you are currently delivering. Please complete the delivery.',
                'delivered': 'This delivery has already been completed.',
                'cancelled': 'This delivery has been cancelled.',
                'scheduled': 'This delivery is not assigned to you.'
            }
            return JsonResponse({
                'error': status_messages.get(delivery.status, 'Cannot release delivery in current status')
            }, status=400)

        # Release the delivery
        delivery.shopper = None
        delivery.status = 'scheduled'
        delivery.save()

        logger.info(f"Shopper {request.user.username} released delivery {delivery_id}. Reason: {reason}")

        # Notify all approved shoppers about available delivery
        from .services.notification_service import NotificationService

        def send_notifications(delivery_id=delivery.id):
            try:
                delivery_obj = WeeklyDelivery.objects.get(id=delivery_id)
                approved_shoppers = User.objects.filter(
                    profile__account_type='shopper',
                    profile__is_approved_shopper=True
                ).exclude(id=request.user.id)  # Exclude the releasing shopper

                for shopper in approved_shoppers:
                    NotificationService.send_new_delivery_available(
                        user=shopper,
                        store_name=delivery_obj.shopping_list.store_name if delivery_obj.shopping_list else 'Unknown Store',
                        delivery_date=delivery_obj.delivery_date
                    )
                logger.info(f"Notified {approved_shoppers.count()} shoppers about released delivery {delivery_id}")
            except Exception as e:
                logger.error(f"Failed to notify shoppers about released delivery: {e}")

        # Run notifications after response
        from django.db import transaction
        transaction.on_commit(lambda: send_notifications())

        return JsonResponse({
            'success': True,
            'message': 'Delivery released successfully. Other shoppers can now accept it.',
            'delivery_id': delivery_id
        })

    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        logger.error(f"Shopper release error: {e}", exc_info=True)
        return JsonResponse({'error': str(e)}, status=500)


def find_delivery_alternatives(cancelled_subscription):
    """
    Find alternative delivery options when shopper rejects

    Args:
        cancelled_subscription: DeliverySubscription that was just cancelled

    Returns:
        list: Array of suggestion objects with alternative days/stores
    """
    suggestions = []

    # Get delivery zones for customer's ZIP code
    customer_zip = extract_zip_from_address(cancelled_subscription.delivery_address)

    if customer_zip:
        # Find other active shoppers in same area
        # (This is placeholder - real implementation would check shopper availability)

        # Suggest different delivery days
        other_days = ['Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
        if cancelled_subscription.delivery_day in other_days:
            other_days.remove(cancelled_subscription.delivery_day)

        for day in other_days[:2]:  # Suggest top 2 alternatives
            suggestions.append({
                'type': 'different_day',
                'delivery_day': day,
                'message': f'Try {day} delivery instead',
                'estimated_availability': 'Good'  # Placeholder
            })

        # Suggest different stores (if multiple stores in area)
        suggestions.append({
            'type': 'different_store',
            'message': 'Try a different store in your area',
            'estimated_availability': 'Limited'
        })

    return suggestions[:3]  # Return top 3 suggestions


def extract_zip_from_address(address):
    """Extract ZIP code from delivery address"""
    import re
    zip_pattern = r'\b(\d{5}(?:-\d{4})?)\b'
    zip_match = re.search(zip_pattern, address)
    return zip_match.group(1)[:5] if zip_match else None


# ========================================
# STRIPE WEBHOOK (for subscription lifecycle events)
# ========================================

@csrf_exempt
@require_http_methods(["POST"])
def stripe_webhook(request):
    """
    Handle Stripe webhook events for subscription lifecycle

    POST /api/delivery/stripe-webhook/

    Events handled:
    - customer.subscription.deleted: Cancel subscription
    - customer.subscription.updated: Update subscription status
    - invoice.payment_failed: Pause subscription
    - invoice.payment_succeeded: Resume subscription
    """
    import stripe
    from django.conf import settings

    payload = request.body
    sig_header = request.META.get('HTTP_STRIPE_SIGNATURE')
    webhook_secret = settings.STRIPE_WEBHOOK_SECRET

    if not webhook_secret:
        logger.error("Stripe webhook secret not configured")
        return JsonResponse({'error': 'Webhook not configured'}, status=500)

    try:
        # Verify webhook signature
        event = stripe.Webhook.construct_event(
            payload, sig_header, webhook_secret
        )
    except ValueError as e:
        logger.error(f"Invalid webhook payload: {e}")
        return JsonResponse({'error': 'Invalid payload'}, status=400)
    except stripe.error.SignatureVerificationError as e:
        logger.error(f"Invalid webhook signature: {e}. Check STRIPE_WEBHOOK_SECRET matches Stripe dashboard.")
        return JsonResponse({'error': 'Invalid signature'}, status=400)

    # Handle different event types
    event_type = event['type']
    data = event['data']['object']

    logger.info(f"Received Stripe webhook: {event_type}")

    try:
        if event_type == 'customer.subscription.deleted':
            # Subscription canceled - mark as cancelled
            subscription_id = data['id']
            try:
                sub = DeliverySubscription.objects.get(stripe_subscription_id=subscription_id)
                sub.status = 'cancelled'
                sub.save()
                logger.info(f"Subscription {sub.id} cancelled via webhook")
            except DeliverySubscription.DoesNotExist:
                logger.warning(f"Subscription not found for Stripe ID: {subscription_id}")

        elif event_type == 'customer.subscription.updated':
            # Subscription status changed
            subscription_id = data['id']
            stripe_status = data['status']  # active, past_due, canceled, etc.

            try:
                sub = DeliverySubscription.objects.get(stripe_subscription_id=subscription_id)

                # Map Stripe status to our status
                if stripe_status == 'active':
                    sub.status = 'active'
                elif stripe_status == 'past_due':
                    sub.status = 'paused'  # Payment failed, pause until resolved
                elif stripe_status in ['canceled', 'unpaid']:
                    sub.status = 'cancelled'

                sub.save()
                logger.info(f"Subscription {sub.id} updated: {stripe_status} → {sub.status}")
            except DeliverySubscription.DoesNotExist:
                logger.warning(f"Subscription not found for Stripe ID: {subscription_id}")

        elif event_type == 'invoice.payment_failed':
            # Payment failed - pause subscription
            subscription_id = data.get('subscription')
            if subscription_id:
                try:
                    sub = DeliverySubscription.objects.get(stripe_subscription_id=subscription_id)
                    sub.status = 'paused'
                    sub.save()
                    logger.warning(f"Subscription {sub.id} paused due to payment failure")
                except DeliverySubscription.DoesNotExist:
                    logger.warning(f"Subscription not found for Stripe ID: {subscription_id}")

        elif event_type == 'invoice.payment_succeeded':
            # Payment succeeded - resume if was paused
            subscription_id = data.get('subscription')
            if subscription_id:
                try:
                    sub = DeliverySubscription.objects.get(stripe_subscription_id=subscription_id)
                    if sub.status == 'paused':
                        sub.status = 'active'
                        sub.save()
                        logger.info(f"Subscription {sub.id} resumed after successful payment")
                except DeliverySubscription.DoesNotExist:
                    logger.warning(f"Subscription not found for Stripe ID: {subscription_id}")

        return JsonResponse({'status': 'success'})

    except Exception as e:
        logger.error(f"Error processing webhook: {e}", exc_info=True)
        return JsonResponse({'error': str(e)}, status=500)


# ========================================
# FCM Push Notifications
# ========================================

@csrf_exempt
@require_firebase_auth
def update_fcm_token(request):
    """
    Save user's FCM token for push notifications
    Called when app launches or token refreshes

    POST /api/update-fcm-token/
    Body: {"fcm_token": "firebase_token_here"}
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    try:
        data = json.loads(request.body)
        fcm_token = data.get('fcm_token')

        if not fcm_token:
            return JsonResponse({'error': 'fcm_token required'}, status=400)

        # Get or create profile
        profile, created = UserProfile.objects.get_or_create(user=request.user)
        profile.fcm_token = fcm_token
        profile.save()

        logger.info(f"FCM token saved for user {request.user.id}")

        return JsonResponse({
            'success': True,
            'message': 'FCM token saved'
        })

    except Exception as e:
        logger.error(f"Error saving FCM token: {e}", exc_info=True)
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
@require_firebase_auth
@require_account_type('customer')
@require_http_methods(["POST"])
def add_delivery(request):
    """
    Add a delivery to user's existing subscription (clean, single-purpose endpoint)

    POST /api/delivery/add-delivery/
    Body: {
        "shopping_list_id": 123,
        "delivery_day": "Saturday",
        "delivery_window": "1-3 PM"
    }

    Enforces limits:
    - Basic: max 1 delivery
    - Premium: max 2 deliveries

    Returns error if limit exceeded or no subscription exists.
    """
    try:
        data = json.loads(request.body)
        user = request.user

        logger.info(f"Add delivery request from user {user.username} (id={user.id})")

        # Validate required fields
        shopping_list_id = data.get('shopping_list_id')
        delivery_day = data.get('delivery_day')
        delivery_window = data.get('delivery_window')
        delivery_instructions = data.get('delivery_instructions', '').strip()  # Optional

        if not all([shopping_list_id, delivery_day, delivery_window]):
            return JsonResponse({
                'error': 'missing_fields',
                'message': 'Required: shopping_list_id, delivery_day, delivery_window'
            }, status=400)

        # Get user's active subscription
        subscription = DeliverySubscription.objects.filter(
            customer=user,
            status__in=['active', 'pending_confirmation', 'setup_complete']
        ).first()

        if not subscription:
            return JsonResponse({
                'error': 'no_subscription',
                'message': 'No active subscription found. Please subscribe first.'
            }, status=404)

        # Count current active deliveries for this subscription
        # Exclude delivered and cancelled - only count deliveries that are pending/in-progress
        active_deliveries = WeeklyDelivery.objects.filter(
            subscription=subscription,
            delivery_date__gte=date.today()
        ).exclude(status__in=['cancelled', 'delivered']).count()

        logger.info(f"User {user.username} has {active_deliveries} active deliveries (tier: {subscription.subscription_tier})")

        # Enforce tier limits
        max_deliveries = 1 if subscription.subscription_tier == 'basic' else 2

        if active_deliveries >= max_deliveries:
            if subscription.subscription_tier == 'basic':
                return JsonResponse({
                    'error': 'upgrade_required',
                    'message': 'Basic plan allows 1 delivery. Upgrade to Premium for 2 deliveries.',
                    'current_count': active_deliveries,
                    'max_allowed': max_deliveries
                }, status=400)
            else:
                return JsonResponse({
                    'error': 'limit_reached',
                    'message': 'Premium plan allows maximum 2 deliveries.',
                    'current_count': active_deliveries,
                    'max_allowed': max_deliveries
                }, status=400)

        # Validate shopping list belongs to user (or their family)
        family_membership = FamilyMember.objects.filter(user=user).first()
        user_family = family_membership.family if family_membership else None

        try:
            if user_family:
                shopping_list = ShoppingList.objects.get(
                    Q(id=shopping_list_id) &
                    (Q(user=user) | Q(family=user_family))
                )
            else:
                shopping_list = ShoppingList.objects.get(id=shopping_list_id, user=user)
        except ShoppingList.DoesNotExist:
            return JsonResponse({
                'error': 'list_not_found',
                'message': f'Shopping list {shopping_list_id} not found'
            }, status=404)

        # Validate store is within acceptable distance of customer's delivery address
        store_location = shopping_list.store_location
        customer_address = subscription.delivery_address

        if store_location and customer_address:
            distance_result = validate_store_customer_distance(
                store_address=store_location,
                customer_address=customer_address,
                max_miles=5  # Configurable: maximum allowed distance
            )

            if not distance_result['valid']:
                logger.warning(f"Store too far from customer: {store_location} → {customer_address} = {distance_result['distance_text']}")
                return JsonResponse({
                    'error': 'store_too_far',
                    'message': distance_result['message'],
                    'store_name': shopping_list.store_name,
                    'store_location': store_location,
                    'distance': distance_result['distance_text'],
                    'max_allowed': '5 miles'
                }, status=400)

            logger.info(f"✅ Distance check passed: {distance_result['message']}")

        # Calculate next delivery date FIRST (needed for duplicate check)
        day_map = {
            'Monday': 0, 'Tuesday': 1, 'Wednesday': 2, 'Thursday': 3,
            'Friday': 4, 'Saturday': 5, 'Sunday': 6
        }
        today = date.today()
        target_weekday = day_map.get(delivery_day, 5)
        days_ahead = target_weekday - today.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        next_delivery_date = today + timedelta(days=days_ahead)

        # Check if this list already has an active delivery for the SAME WEEKDAY
        # This prevents scheduling Trader Joe's for Sunday when there's already a Sunday delivery pending
        # Check all active deliveries for this list, then filter by weekday
        existing_deliveries_for_list = WeeklyDelivery.objects.filter(
            subscription=subscription,
            shopping_list=shopping_list,
            delivery_date__gte=today - timedelta(days=7)  # Include recent deliveries still pending
        ).exclude(status__in=['cancelled', 'delivered'])

        # Check if any existing delivery falls on the same weekday
        for existing_delivery in existing_deliveries_for_list:
            if existing_delivery.delivery_date.weekday() == target_weekday:
                existing_day_name = existing_delivery.delivery_date.strftime('%A')
                return JsonResponse({
                    'error': 'duplicate_delivery',
                    'message': f'This list already has a {existing_day_name} delivery scheduled for {existing_delivery.delivery_date.strftime("%b %d")}'
                }, status=400)

        # Create WeeklyDelivery
        from django.db import transaction
        from .services.notification_service import NotificationService

        logger.info(f"📦 Creating delivery for user {user.username}: shopping_list_id={shopping_list.id} ({shopping_list.store_name}), date={next_delivery_date}")

        weekly_delivery = WeeklyDelivery.objects.create(
            subscription=subscription,
            shopping_list=shopping_list,
            delivery_date=next_delivery_date,
            delivery_window=delivery_window,  # Store per-delivery time window
            status='pending_shopper'
        )

        logger.info(f"✅ Created WeeklyDelivery {weekly_delivery.id} with shopping_list_id={weekly_delivery.shopping_list_id}")

        # Update subscription delivery count and instructions
        subscription.deliveries_this_cycle = active_deliveries + 1
        if delivery_instructions:
            subscription.delivery_instructions = delivery_instructions
        subscription.save()

        logger.info(f"✅ Created WeeklyDelivery {weekly_delivery.id} for {shopping_list.store_name} on {next_delivery_date}")

        # Notify shoppers about new delivery (after transaction commits)
        def send_notifications(delivery_id=weekly_delivery.id):
            try:
                delivery = WeeklyDelivery.objects.get(id=delivery_id)
                approved_shoppers = User.objects.filter(
                    profile__account_type='shopper',
                    profile__is_approved_shopper=True,
                    profile__fcm_token__isnull=False
                ).exclude(profile__fcm_token='')

                for shopper in approved_shoppers:
                    NotificationService.send_new_delivery_available(
                        shopper=shopper,
                        delivery=delivery
                    )
            except WeeklyDelivery.DoesNotExist:
                logger.error(f"WeeklyDelivery {delivery_id} not found for notification")

        transaction.on_commit(lambda: send_notifications(weekly_delivery.id))

        return JsonResponse({
            'success': True,
            'delivery_id': weekly_delivery.id,
            'message': f'Delivery scheduled for {delivery_day}. Pending shopper confirmation.',
            'delivery_date': next_delivery_date.isoformat(),
            'store_name': shopping_list.store_name,
            'current_deliveries': active_deliveries + 1,
            'max_deliveries': max_deliveries
        })

    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        logger.error(f"Add delivery error: {e}", exc_info=True)
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
@require_firebase_auth
def upgrade_subscription_tier(request):
    """
    Upgrade subscription from basic to premium (Apple way: immediate Stripe upgrade with prorated billing)

    POST /api/delivery/upgrade-tier/

    Body: {
        subscription_id: optional - specific subscription to upgrade
    }

    This endpoint:
    1. Upgrades Stripe subscription immediately with prorated billing
    2. Updates the subscription_tier in database from 'basic' to 'premium'
    3. User is charged prorated difference right away (Apple way)
    """
    try:
        user = request.user
        data = json.loads(request.body) if request.body else {}

        logger.info(f"Upgrade tier request from user {user.username} (id={user.id})")

        # Find user's active basic subscription
        subscription_id = data.get('subscription_id')

        if subscription_id:
            subscription = DeliverySubscription.objects.filter(
                id=subscription_id,
                customer=user,
                status__in=['active', 'pending_confirmation', 'setup_complete']
            ).first()
        else:
            subscription = DeliverySubscription.objects.filter(
                customer=user,
                subscription_tier='basic',
                status__in=['active', 'pending_confirmation', 'setup_complete']
            ).first()

        if not subscription:
            return JsonResponse({
                'error': 'no_subscription',
                'message': 'No active basic subscription found to upgrade'
            }, status=404)

        if subscription.subscription_tier.lower() == 'premium':
            return JsonResponse({
                'error': 'already_premium',
                'message': 'Subscription is already premium',
                'subscription_id': subscription.id
            }, status=400)

        # Check if subscription has Stripe subscription ID
        if not subscription.stripe_subscription_id:
            # No Stripe subscription yet (billing hasn't started)
            # Allow upgrade for any active-ish status - billing will start at premium rate
            if subscription.status in ['setup_complete', 'active', 'pending_confirmation']:
                old_tier = subscription.subscription_tier
                subscription.subscription_tier = 'premium'
                subscription.save()

                logger.info(f"✅ Upgraded subscription {subscription.id} from {old_tier} to premium (no billing yet, status={subscription.status})")

                return JsonResponse({
                    'success': True,
                    'message': 'Upgraded to Premium! Billing will start at $30/week when your delivery begins.',
                    'subscription_id': subscription.id,
                    'old_tier': old_tier,
                    'new_tier': 'premium'
                })
            else:
                return JsonResponse({
                    'error': 'no_stripe_subscription',
                    'message': 'No active billing subscription found. Please schedule a delivery first.'
                }, status=400)

        # Apple way: Upgrade Stripe subscription immediately with prorated billing
        stripe_price_id = StripeService.get_price_id_for_tier('premium')

        success, error = StripeService.upgrade_subscription(
            subscription_id=subscription.stripe_subscription_id,
            new_price_id=stripe_price_id,
            metadata={
                'user_id': user.id,
                'username': user.username,
                'tier': 'premium',
                'upgrade_from': 'basic',
                'upgrade_source': 'upgrade_tier_endpoint'
            }
        )

        if not success:
            logger.error(f"Failed to upgrade Stripe subscription for {user.username}: {error}")
            return JsonResponse({
                'error': 'upgrade_failed',
                'message': 'Could not upgrade subscription. Please check payment method.',
                'details': error
            }, status=500)

        # Update tier in database
        old_tier = subscription.subscription_tier
        subscription.subscription_tier = 'premium'
        subscription.save()

        logger.info(f"✅ Upgraded subscription {subscription.id} from {old_tier} to premium with immediate Stripe billing")

        return JsonResponse({
            'success': True,
            'message': 'Upgraded to Premium! You have been charged the prorated difference.',
            'subscription_id': subscription.id,
            'old_tier': old_tier,
            'new_tier': 'premium'
        })

    except Exception as e:
        logger.error(f"Error upgrading subscription tier: {e}", exc_info=True)
        return JsonResponse({'error': str(e)}, status=500)