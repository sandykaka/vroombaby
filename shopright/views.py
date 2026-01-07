import logging
import json
import base64
from datetime import datetime, timedelta
from functools import wraps

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone
from firebase_admin import auth as firebase_auth
from openai import OpenAI
from shopright.services.apple_receipt_service import verify_subscription_receipt
from django.conf import settings

from .models import (
    Family, FamilyMember, ShoppingTrip, GroceryItem,
    ShoppingList, ShoppingListItem, AisleLocation, LocationVote,
    WeeklyDelivery, DeliverySubscription, UserProfile, Shopper
)
from .services.openfoodfacts_service import get_service as get_openfoodfacts_service
from .services.subscription_service import SubscriptionService
from .utils.product_cleanup import clean_product_name_and_size
from .decorators import require_nutrition_scan_quota, require_approved_shopper

logger = logging.getLogger(__name__)


# ========================================
# UTILITY FUNCTIONS
# ========================================

def normalize_store_location(location):
    """
    Normalize store location to prevent duplicate shopping lists.

    Examples:
        "7250 Bollinger Rd, San Jose, CA 95129" → "7250 bollinger rd, san jose, ca 95129"
        "  123 Main St, Cupertino, CA  " → "123 main st, cupertino, ca"

    Handles: case differences, extra whitespace, zip code variations
    """
    if not location:
        return ""

    # Lowercase, strip, and normalize whitespace
    return ' '.join(location.strip().lower().split())


def normalize_weighted_item_size(size):
    """
    Normalize size for weighted items (produce, deli, meat) so different weights match.

    Examples:
        "0.69 lb" → "per lb"
        "2.00 lb" → "per lb"
        "1.5 kg" → "per kg"
        "64oz" → "64oz" (not weighted, unchanged)
        "" → ""

    This allows all purchases of the same weighted product (regardless of actual weight)
    to share the same GroceryItem record for images/locations.
    """
    if not size:
        return ""

    size_lower = size.strip().lower()

    # Check for weight patterns: "X.XX lb", "X lb", "X.XX kg", etc.
    import re

    # Pattern: optional number + space + weight unit
    # Matches: "0.69 lb", "2 lb", "1.5 kg", "0.5 lbs", etc.
    weight_pattern = r'^\d+\.?\d*\s*(lb|lbs|kg|kgs|oz|pound|pounds)$'

    if re.match(weight_pattern, size_lower):
        # Extract the unit (lb, kg, oz, etc.)
        unit_match = re.search(r'(lb|lbs|kg|kgs|oz|pound|pounds)$', size_lower)
        if unit_match:
            unit = unit_match.group(1)
            # Normalize unit variations
            if unit in ['lb', 'lbs', 'pound', 'pounds']:
                return 'per lb'
            elif unit in ['kg', 'kgs']:
                return 'per kg'
            elif unit == 'oz':
                return 'per oz'

    # Not a weighted item - return as-is
    return size


def fuzzy_match_product_names(selected_item_name, scanned_product_name):
    """
    Simple fuzzy matching to verify user is scanning the correct product.

    Examples:
        "Milk" vs "Organic Whole Milk" → 100% (exact word match)
        "Banana" vs "Organic Bananas" → 100% (similar word match)
        "Bananas" vs "Organic Milk" → 0% (no match)

    Returns: {
        'match': True/False,
        'confidence': 0-100,
        'reason': 'explanation'
    }
    """
    # Normalize text
    selected = selected_item_name.lower().strip()
    scanned = scanned_product_name.lower().strip()

    # Extract words (ignore common words)
    stop_words = {'the', 'a', 'an', 'and', 'or', 'of', 'in', 'to', 'for'}

    selected_words = set(word for word in selected.split() if word not in stop_words)
    scanned_words = set(word for word in scanned.split() if word not in stop_words)

    # Check for exact word matches
    common_words = selected_words & scanned_words

    if common_words:
        confidence = 100
        return {
            'match': True,
            'confidence': confidence,
            'reason': f'Matching words: {", ".join(common_words)}'
        }

    # Check for partial word matches (e.g., "banana" in "bananas")
    for sel_word in selected_words:
        for scan_word in scanned_words:
            if sel_word in scan_word or scan_word in sel_word:
                if len(sel_word) >= 4 and len(scan_word) >= 4:  # Only for substantial words
                    return {
                        'match': True,
                        'confidence': 80,
                        'reason': f'Partial match: "{sel_word}" ≈ "{scan_word}"'
                    }

    # No match found
    return {
        'match': False,
        'confidence': 0,
        'reason': 'No matching words found'
    }


# ========================================
# AUTHENTICATION DECORATOR (Reused from Crave)
# ========================================

def require_firebase_auth(f):
    """
    Decorator to verify Firebase ID token and get/create Django user
    Uses ShopRight Firebase app (not Crave's default app)
    """
    @wraps(f)
    def decorated_function(request, *args, **kwargs):
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return JsonResponse({'error': 'Missing or invalid Authorization header'}, status=401)

        id_token = auth_header.split('Bearer ')[1]

        try:
            # Verify Firebase token using ShopRight app
            import firebase_admin
            shopright_app = firebase_admin.get_app('shopright')
            decoded_token = firebase_auth.verify_id_token(id_token, app=shopright_app)
            firebase_uid = decoded_token['uid']
            email = decoded_token.get('email')
            phone = decoded_token.get('phone_number')

            # Get or create Django user (use phone number as username for ShopRight)
            username = phone if phone else firebase_uid
            user, created = User.objects.get_or_create(
                username=username,
                defaults={'email': email or ''}
            )

            # Attach to request
            request.user = user
            request.firebase_uid = firebase_uid

            return f(request, *args, **kwargs)

        except Exception as e:
            logger.error(f"Firebase auth failed: {e}")
            return JsonResponse({'error': 'Invalid authentication token'}, status=401)

    return decorated_function


# ========================================
# RECEIPT SCANNING API
# ========================================

@csrf_exempt
@require_firebase_auth
def scan_receipt_api(request):
    """
    Upload receipt image, parse with OpenAI Vision API, save items

    POST /shopright/api/scan-receipt/
    Body: {
        "receipt_image": "base64_encoded_image",
        "store_name": "Trader Joe's",  # optional
        "store_location": "Cupertino, CA",  # optional
        "trip_date": "2025-11-01T14:30:00"  # optional, defaults to now
    }

    Returns: {
        "trip_id": 123,
        "items": [...],
        "total_amount": "45.67",
        "receipt_image_url": "/media/receipts/2025/11/receipt_123.jpg"
    }
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    receipt_image_b64 = data.get('receipt_image')
    if not receipt_image_b64:
        return JsonResponse({'error': 'Missing receipt_image'}, status=400)

    # Optional fields (can be overridden by user, but will be extracted from receipt)
    trip_date_str = data.get('trip_date')

    # Parse trip date
    if trip_date_str:
        try:
            trip_date = datetime.fromisoformat(trip_date_str.replace('Z', '+00:00'))
        except ValueError:
            return JsonResponse({'error': 'Invalid trip_date format'}, status=400)
    else:
        trip_date = datetime.now()

    # Call OpenAI Vision API to parse receipt (extracts store info + items)
    try:
        store_name, store_location, parsed_items, ai_total_amount = _parse_receipt_with_openai(receipt_image_b64)
    except Exception as e:
        logger.error(f"OpenAI receipt parsing failed: {e}")
        return JsonResponse({'error': f'Failed to parse receipt: {str(e)}'}, status=500)

    # Calculate total from item prices (more reliable than AI extraction)
    calculated_total = 0.0
    for item in parsed_items:
        price_str = item.get('price', '').strip()
        quantity = item.get('quantity', 1)  # Default to 1 if not specified
        if price_str:
            try:
                unit_price = float(price_str.replace('$', '').replace(',', ''))
                calculated_total += unit_price * quantity
            except ValueError:
                logger.warning(f"Could not parse item price: {price_str}")

    # Compare AI total vs calculated total
    total_matches = False
    if ai_total_amount and calculated_total > 0:
        # Allow 1 cent tolerance for rounding differences
        difference = abs(ai_total_amount - calculated_total)
        total_matches = difference <= 0.01
        logger.info(f"Total comparison: AI=${ai_total_amount}, Calculated=${calculated_total}, Match={total_matches}")

    # Allow user to override store info if provided (only if not empty)
    user_store_name = data.get('store_name', '').strip()
    user_store_location = data.get('store_location', '').strip()

    if user_store_name:  # Only override if user provided non-empty value
        store_name = user_store_name
    if user_store_location:
        store_location = user_store_location

    # Normalize store location to prevent duplicates (e.g., "CA" vs "ca", extra spaces, etc.)
    store_location = normalize_store_location(store_location)

    # Get user's family (optional - user can use app without family for personal tracking)
    membership = FamilyMember.objects.filter(user=request.user).first()
    family = membership.family if membership else None

    # Save receipt image to file
    from django.core.files.base import ContentFile
    image_data = base64.b64decode(receipt_image_b64)

    # Create ShoppingTrip (family is optional)
    trip = ShoppingTrip.objects.create(
        user=request.user,
        family=family,  # Can be None for personal use
        store_name=store_name,
        store_location=store_location,
        items=parsed_items,  # JSONField
        total_amount=calculated_total if calculated_total > 0 else None,
        trip_date=trip_date
    )

    # Save image with trip ID in filename
    try:
        trip.receipt_image.save(
            f'receipt_{trip.id}.jpg',
            ContentFile(image_data),
            save=True
        )
        logger.info(f"✅ Receipt image saved: receipt_{trip.id}.jpg")
    except Exception as e:
        logger.error(f"❌ Failed to save receipt image for trip {trip.id}: {e}")
        # Don't fail the entire request if image save fails

    # Update GroceryItem master list (for search/autocomplete)
    _update_grocery_items(parsed_items, store_name)

    # Auto-add items to shopping list (personal or family)
    if family:
        _update_shopping_list_from_trip(family, None, store_name, store_location, parsed_items, trip_date)
        logger.info(f"Updated shopping list for family: {family.name}")
    else:
        _update_shopping_list_from_trip(None, request.user, store_name, store_location, parsed_items, trip_date)
        logger.info(f"Updated personal shopping list for user: {request.user.username}")

    # Build receipt image URL
    receipt_image_url = request.build_absolute_uri(trip.receipt_image.url) if trip.receipt_image else None

    response_data = {
        'id': trip.id,
        'store_name': trip.store_name,
        'store_location': trip.store_location or '',
        'trip_date': trip.trip_date.isoformat(),
        'items': parsed_items,
        'total_amount': str(calculated_total) if calculated_total > 0 else None,
        'receipt_total': str(ai_total_amount) if ai_total_amount else None,
        'totals_match': total_matches,
        'item_count': len(parsed_items),
        'shopped_by': request.user.username,
        'receipt_image_url': receipt_image_url
    }

    logger.info(f"Receipt scan response: store={trip.store_name}, items={len(parsed_items)}, calculated_total={calculated_total}, totals_match={total_matches}")

    return JsonResponse(response_data)


@csrf_exempt
@require_firebase_auth
def preview_receipt_api(request):
    """
    Preview receipt parsing WITHOUT saving to database (Step 1 of 2-step flow)

    POST /shopright/api/preview-receipt/
    Body: {
        "receipt_image": "base64_encoded_image"
    }

    Returns: {
        "store_name": "Trader Joe's",
        "store_location": "7250 Bollinger Rd, San Jose, CA 95129",
        "items": [...],
        "calculated_total": "63.54",
        "receipt_total": "69.24",
        "totals_match": false,
        "item_count": 19
    }
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    receipt_image_b64 = data.get('receipt_image')
    if not receipt_image_b64:
        return JsonResponse({'error': 'Missing receipt_image'}, status=400)

    # Call OpenAI Vision API to parse receipt
    try:
        store_name, store_location, parsed_items, ai_total_amount = _parse_receipt_with_openai(receipt_image_b64)
    except Exception as e:
        logger.error(f"OpenAI receipt parsing failed: {e}")
        return JsonResponse({'error': f'Failed to parse receipt: {str(e)}'}, status=500)

    # Calculate total from item prices
    calculated_total = 0.0
    for item in parsed_items:
        price_str = item.get('price', '').strip()
        quantity = item.get('quantity', 1)
        if price_str:
            try:
                unit_price = float(price_str.replace('$', '').replace(',', ''))
                calculated_total += unit_price * quantity
            except ValueError:
                logger.warning(f"Could not parse item price: {price_str}")

    # Compare totals
    total_matches = False
    if ai_total_amount and calculated_total > 0:
        difference = abs(ai_total_amount - calculated_total)
        total_matches = difference <= 0.01

    # Normalize store location
    store_location = normalize_store_location(store_location)

    response_data = {
        'store_name': store_name,
        'store_location': store_location or '',
        'items': parsed_items,
        'calculated_total': str(calculated_total) if calculated_total > 0 else None,
        'receipt_total': str(ai_total_amount) if ai_total_amount else None,
        'totals_match': total_matches,
        'item_count': len(parsed_items)
    }

    logger.info(f"Receipt preview: store={store_name}, items={len(parsed_items)}, calculated_total={calculated_total}, totals_match={total_matches}")

    return JsonResponse(response_data)


@csrf_exempt
@require_firebase_auth
def save_receipt_api(request):
    """
    Save parsed receipt data to database (Step 2 of 2-step flow)

    POST /shopright/api/save-receipt/
    Body: {
        "receipt_image": "base64_encoded_image",
        "store_name": "Trader Joe's",
        "store_location": "7250 Bollinger Rd, San Jose, CA 95129",
        "items": [...],
        "total_amount": "63.54",
        "trip_date": "2025-11-09T19:51:30"  # optional, defaults to now
    }

    Returns: {
        "id": 123,
        "store_name": "Trader Joe's",
        "store_location": "...",
        "items": [...],
        "total_amount": "63.54",
        "receipt_image_url": "..."
    }
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    # Validate required fields
    receipt_image_b64 = data.get('receipt_image')
    store_name = data.get('store_name', '').strip()
    parsed_items = data.get('items', [])

    # NEW: Check if this is a shopper uploading for a delivery
    delivery_id = data.get('delivery_id')  # If present, shopper is uploading for customer

    if not receipt_image_b64:
        return JsonResponse({'error': 'Missing receipt_image'}, status=400)
    if not store_name:
        return JsonResponse({'error': 'Missing store_name'}, status=400)
    if not parsed_items:
        return JsonResponse({'error': 'Missing items'}, status=400)

    # Optional fields
    store_location = normalize_store_location(data.get('store_location', ''))
    trip_date_str = data.get('trip_date')

    # Parse trip date
    if trip_date_str:
        try:
            trip_date = datetime.fromisoformat(trip_date_str.replace('Z', '+00:00'))
        except ValueError:
            return JsonResponse({'error': 'Invalid trip_date format'}, status=400)
    else:
        trip_date = datetime.now()

    # Parse total amount
    total_str = data.get('total_amount', '').strip()
    total_amount = None
    if total_str:
        try:
            total_amount = float(total_str.replace('$', '').replace(',', ''))
        except ValueError:
            logger.warning(f"Could not parse total_amount: {total_str}")

    # Handle shopper delivery upload vs. customer upload
    if delivery_id:
        # Shopper uploading receipt for customer's delivery
        try:
            from .models import WeeklyDelivery
            delivery = WeeklyDelivery.objects.get(id=delivery_id, shopper=request.user)
        except WeeklyDelivery.DoesNotExist:
            return JsonResponse({'error': 'Delivery not found or not assigned to you'}, status=404)

        # Get customer and their family (receipt will appear in customer's account)
        customer = delivery.subscription.customer
        customer_membership = FamilyMember.objects.filter(user=customer).first()
        family = customer_membership.family if customer_membership else None

        # Receipt will be owned by customer, not shopper
        receipt_owner = customer
        shopper = request.user  # Track who actually uploaded it
        created_by_shopper = True

        logger.info(f"Shopper {request.user.username} uploading receipt for delivery {delivery_id} (customer: {customer.username})")

    else:
        # Regular customer upload
        membership = FamilyMember.objects.filter(user=request.user).first()
        family = membership.family if membership else None

        receipt_owner = request.user
        shopper = None
        created_by_shopper = False

    # Save receipt image
    from django.core.files.base import ContentFile
    image_data = base64.b64decode(receipt_image_b64)

    # Create ShoppingTrip (owned by customer, created by shopper if delivery_id present)
    trip = ShoppingTrip.objects.create(
        user=receipt_owner,
        family=family,
        store_name=store_name,
        store_location=store_location,
        items=parsed_items,
        total_amount=total_amount,
        trip_date=trip_date,
        created_by_shopper=created_by_shopper,
        shopper=shopper,
        delivery=delivery if delivery_id else None  # Link to delivery if this is shopper receipt
    )

    # Save image with trip ID
    try:
        trip.receipt_image.save(
            f'receipt_{trip.id}.jpg',
            ContentFile(image_data),
            save=True
        )
        logger.info(f"✅ Receipt image saved: receipt_{trip.id}.jpg")
    except Exception as e:
        logger.error(f"❌ Failed to save receipt image for trip {trip.id}: {e}")
        # Don't fail the entire request if image save fails
        # The trip data is still valid, just without the image

    # Update GroceryItem master list
    _update_grocery_items(parsed_items, store_name)

    # Update shopping list
    if family:
        _update_shopping_list_from_trip(family, None, store_name, store_location, parsed_items, trip_date)
        logger.info(f"Updated shopping list for family: {family.name}")
    else:
        _update_shopping_list_from_trip(None, receipt_owner, store_name, store_location, parsed_items, trip_date)
        logger.info(f"Updated personal shopping list for user: {receipt_owner.username}")

    # Handle delivery workflow if this was a shopper upload
    if delivery_id:
        # Link the trip to the delivery
        delivery.shopping_trip = trip
        delivery.actual_cost = total_amount  # Store actual grocery cost
        # Receipt scan keeps status as "ready" - shopper will click "Start Delivery" separately
        # delivery.status remains 'ready'
        # Don't set delivered_at - that happens when shopper clicks "Mark Delivered"

        # PAYMENT PROCESSING: Capture final amount from receipt
        payment_adjustment_info = {}
        if total_amount and delivery.payment_authorization_id:
            from .services.stripe_service import StripeService

            # Convert dollar amount to cents for Stripe
            final_amount_cents = int(total_amount * 100)

            # Capture the pre-authorized payment with final receipt amount
            capture_result = StripeService.capture_authorized_payment(
                authorization_id=delivery.payment_authorization_id,
                final_amount=final_amount_cents,
                delivery_id=delivery_id
            )

            if capture_result['success']:
                delivery.payment_captured_amount = total_amount
                delivery.payment_charge_id = capture_result.get('charge_id')
                logger.info(f"✅ Payment captured: ${total_amount} for delivery {delivery_id}")

                # Calculate adjustment vs estimated amount
                if delivery.estimated_cost:
                    from decimal import Decimal
                    estimated_decimal = Decimal(str(delivery.estimated_cost))
                    actual_decimal = Decimal(str(total_amount))
                    adjustment = actual_decimal - estimated_decimal

                    payment_adjustment_info = {
                        'estimated_cost': float(delivery.estimated_cost),
                        'actual_cost': total_amount,
                        'adjustment': float(adjustment),
                        'adjustment_type': 'refund' if adjustment < 0 else 'additional_charge' if adjustment > 0 else 'no_change'
                    }
                    logger.info(f"Payment adjustment for delivery {delivery_id}: estimated=${delivery.estimated_cost}, actual=${total_amount}, adjustment=${adjustment}")

            else:
                # Payment capture failed - log error but don't block delivery completion
                error_msg = capture_result.get('error', 'Unknown payment error')
                logger.error(f"❌ Payment capture failed for delivery {delivery_id}: {error_msg}")

                # Store error for customer service follow-up
                payment_adjustment_info = {
                    'payment_error': error_msg,
                    'requires_manual_processing': True
                }

        delivery.save()

        # Unlock customer's shopping list
        if delivery.shopping_list:
            delivery.shopping_list.locked_for_shopping = False
            delivery.shopping_list.locked_at = None
            delivery.shopping_list.save()
            logger.info(f"Unlocked shopping list {delivery.shopping_list.id} after delivery completion")

        # Send notification to customer that receipt is uploaded and payment processed
        from .services.notification_service import send_notification
        try:
            # Build notification message with payment info
            notification_title = "Receipt Uploaded! 📋"

            # Include the exact final amount charged
            if total_amount and delivery.payment_captured_amount:
                notification_message = f"Your {store_name} receipt has been uploaded and ${total_amount:.2f} charged to your payment method. Delivery will start soon!"
            else:
                notification_message = f"Your {store_name} receipt has been uploaded and payment processed. Delivery will start soon!"

            # Add payment adjustment info to message if available
            if payment_adjustment_info:
                adjustment_type = payment_adjustment_info.get('adjustment_type')
                if adjustment_type == 'refund':
                    adjustment_amount = abs(payment_adjustment_info.get('adjustment', 0))
                    notification_message += f"\n\nGood news! We're refunding ${adjustment_amount:.2f} to your payment method."
                elif adjustment_type == 'additional_charge':
                    adjustment_amount = payment_adjustment_info.get('adjustment', 0)
                    notification_message += f"\n\nThe final amount was ${adjustment_amount:.2f} more than estimated. Payment adjusted automatically."
                elif payment_adjustment_info.get('payment_error'):
                    notification_message += "\n\nPayment processing in progress. You'll receive a receipt shortly."

            # Build extra_data with payment info
            extra_data = {
                "delivery_id": delivery_id,
                "trip_id": trip.id,
                "store_name": store_name,
                "total_amount": str(total_amount) if total_amount else None
            }

            # Add payment adjustment details
            if payment_adjustment_info:
                extra_data.update({
                    "payment_adjustment": payment_adjustment_info,
                    "estimated_cost": str(payment_adjustment_info.get('estimated_cost', '')) if payment_adjustment_info.get('estimated_cost') else None,
                    "actual_cost": str(payment_adjustment_info.get('actual_cost', '')) if payment_adjustment_info.get('actual_cost') else None,
                    "adjustment_amount": str(payment_adjustment_info.get('adjustment', '')) if payment_adjustment_info.get('adjustment') else None
                })

            send_notification(
                user=customer,
                title=notification_title,
                body=notification_message,
                notification_type="receipt_uploaded",
                data=extra_data
            )
        except Exception as e:
            logger.warning(f"Failed to send delivery completion notification: {e}")

        logger.info(f"✅ Receipt scanned for delivery {delivery_id} by shopper {request.user.username}, payment processed, receipt saved as trip {trip.id} for customer {customer.username}")

    # Build response
    receipt_image_url = request.build_absolute_uri(trip.receipt_image.url) if trip.receipt_image else None

    response_data = {
        'id': trip.id,
        'store_name': trip.store_name,
        'store_location': trip.store_location or '',
        'trip_date': trip.trip_date.isoformat(),
        'items': parsed_items,
        'total_amount': str(trip.total_amount) if trip.total_amount else None,
        'item_count': len(parsed_items),
        'shopped_by': receipt_owner.username,  # Customer's name (receipt owner)
        'receipt_image_url': receipt_image_url
    }

    # Add delivery info for shopper uploads
    if delivery_id:
        response_data['delivery_id'] = delivery_id
        response_data['uploaded_by_shopper'] = request.user.username
        response_data['message'] = "Receipt uploaded and payment processed successfully"

    logger.info(f"Receipt saved: trip_id={trip.id}, store={trip.store_name}, items={len(parsed_items)}")

    return JsonResponse(response_data)


def _merge_duplicate_items(items):
    """
    Merge duplicate items in receipt based on name, brand, size, and price.
    Items with same name/brand/size/price are combined with quantity summed.

    Args:
        items: List of item dicts from OpenAI

    Returns:
        List of merged items with quantities
    """
    from collections import defaultdict

    # Group items by (name, brand, size, price)
    grouped = defaultdict(list)

    for item in items:
        # Create key from identifying fields (empty string if None)
        key = (
            item.get('name', '').strip().lower(),
            item.get('brand', '').strip().lower(),
            item.get('size', '').strip().lower(),
            item.get('price', '').strip()
        )
        grouped[key].append(item)

    # Merge duplicates
    merged_items = []
    for key, item_list in grouped.items():
        if len(item_list) == 1:
            # No duplicates - keep original item with quantity 1
            item = item_list[0]
            if 'quantity' not in item:
                item['quantity'] = 1
            merged_items.append(item)
        else:
            # Duplicates found - merge them
            base_item = item_list[0].copy()

            # Sum quantities (default to 1 per item if not specified)
            total_quantity = sum(item.get('quantity', 1) for item in item_list)
            base_item['quantity'] = total_quantity

            # Keep unit price unchanged - let frontend calculate line totals
            # (iOS already multiplies unit_price * quantity for display)

            merged_items.append(base_item)
            logger.info(f"Merged {len(item_list)} duplicates of '{base_item.get('name')}' into quantity {total_quantity}")

    return merged_items


def _parse_receipt_with_openai(receipt_image_b64):
    """
    Parse receipt image using OpenAI Vision API (gpt-4o)

    Returns: (store_name, store_location, items_list, total_amount)
    store_name: str (e.g., "Trader Joe's")
    store_location: str (e.g., "123 Main St, Cupertino, CA")
    items_list: [{"name": "Horizon Organic Milk", "brand": "Horizon", "size": "64oz", "price": "4.99", "category": "Dairy"}, ...]
    total_amount: Decimal or None
    """
    client = OpenAI(api_key=settings.OPENAI_API_KEY)

    # Prompt for structured extraction
    prompt = """
You are analyzing a grocery store receipt. Extract the following information:

1. Store information (from top of receipt):
   - store_name: Name of the store (e.g., "Trader Joe's", "Whole Foods")
   - store_location: Store address or location (e.g., "123 Main St, Cupertino, CA")

2. All items purchased:

   CRITICAL: Extract EVERY SINGLE LINE ITEM from the receipt. Scan the ENTIRE receipt carefully from top to bottom.
   Do NOT skip ANY items. Count each line carefully to ensure you capture everything.

   UNDERSTANDING RECEIPT LAYOUT (READ THIS FIRST):
   ═══════════════════════════════════════════════════
   Grocery receipts are VISUAL TABLES with columns:
   - LEFT column: Item name
   - RIGHT column: Price (right-aligned)
   - Each item's price appears on THE SAME HORIZONTAL LINE as its name

   Example receipt layout:
   ┌─────────────────────────────────────────────┐
   │ Banana Organic               $2.99          │  ← Item "Banana Organic" with price $2.99 on SAME line
   │   0.5 lb @ $5.98/lb                         │  ← This is indented (breakdown), NOT a new item
   │ Apple Fuji                   $3.49          │  ← Item "Apple Fuji" with price $3.49 on SAME line
   │ Orange Navel                 $4.99          │  ← Item "Orange Navel" with price $4.99 on SAME line
   └─────────────────────────────────────────────┘

   GOLDEN RULE: The rightmost dollar amount on an item's line is THAT ITEM'S PRICE.
   Do NOT take prices from lines above or below the item!
   ═══════════════════════════════════════════════════

   For each line item, provide:
   - name: Product name WITHOUT size/quantity (e.g., "Horizon Organic Whole Milk" NOT "Horizon Organic Whole Milk 64oz")
   - brand: Brand name if visible (e.g., "Horizon")
   - size: Size/quantity ONLY, extracted separately from name (e.g., "64oz", "1 lb", "12 ct")
   - price: ACTUAL AMOUNT PAID for this line item (see detailed rules below)
   - quantity: Number of units purchased (default is 1 if not specified)
   - category: Best-guess category (e.g., "Dairy", "Produce", "Meat", "Snacks", "Beverages", "Bakery", "Frozen", "Pantry")

   CRITICAL - PRICE MUST BE ON SAME LINE AS ITEM:
   ════════════════════════════════════════════════
   STEP 1: Locate the item name on the left
   STEP 2: Look to the RIGHT on THE SAME HORIZONTAL LINE
   STEP 3: The rightmost $ value on that line is the price

   NEVER take the price from:
   ✗ The line ABOVE the item
   ✗ The line BELOW the item
   ✗ A different item's line

   ONLY exception: Multi-line items where 2nd line is INDENTED (starts with spaces)
   ════════════════════════════════════════════════

   TWO TYPES OF ITEMS - extract price differently:

   TYPE 1 - WEIGHTED ITEMS (produce, deli, meat sold by lb/kg):
   Format example:
     Line 1: "Karela per lb               $1.24"     ← Price $1.24 is on THIS line
     Line 2: "  0.69 lb @ 1.79/lb"                   ← Indented breakdown (NOT a new item)
   → Extract price: "1.24" (from Line 1 - the ACTUAL AMOUNT PAID)
   → Extract size: "0.69 lb" (from indented Line 2)
   → Extract quantity: 1
   → IGNORE the unit price (1.79/lb) - this is NOT what customer paid

   TYPE 2 - QUANTITY ITEMS (packaged items sold in multiples):
   Format example:
     Line 1: "Avocado Large Eac           $3.87"     ← Total paid is on THIS line
     Line 2: "  3 @ $1.29"                           ← Indented breakdown showing unit price
   → Extract price: "1.29" (unit price per item from indented Line 2)
   → Extract quantity: 3
   → Note: 3 × $1.29 = $3.87 (total on Line 1)

   VISUAL ALIGNMENT CHECK - DO THIS FOR EVERY ITEM:
   ═════════════════════════════════════════════════
   Before extracting price, ask yourself:
   1. "Is this price on the SAME horizontal line as the item name?"
   2. "Or am I accidentally looking at a price from the line above/below?"
   3. "Is the next line indented? If yes, it's a breakdown. If no, it's a different item."

   Common ERROR pattern to AVOID:
   Receipt shows:
   "Apple Fuji                   $3.99"     ← Different item
   "Banana Organic              $2.99"     ← Extract $2.99 for THIS item
   "Orange Navel                $4.49"     ← Different item

   ✗ WRONG: Extracting $3.99 or $4.49 for Banana (from adjacent lines)
   ✓ CORRECT: Extracting $2.99 for Banana (from SAME line)
   ═════════════════════════════════════════════════

   IMPORTANT GUIDELINES:
   - BE THOROUGH: Scan the ENTIRE receipt carefully. Missing items is NOT acceptable.
   - If no quantity is shown, default quantity to 1.
   - Ignore other numbers (PLU codes, UPC codes, item numbers).
   - "Eac", "Each", "EA" means sold individually - do NOT include this in size.
   - Look for items throughout the ENTIRE receipt (top, middle, bottom).

   CRITICAL - NAME AND SIZE SEPARATION:
   ═════════════════════════════════════════════════
   Keep name and size SEPARATE. Do NOT include size information in the name field.
   The size field should contain ONLY the size/quantity, extracted separately from name.

   ✅ CORRECT EXAMPLES:
   Receipt shows: "Strawberries Org 1 lb"
   → name="Strawberries Org", size="1 lb"

   Receipt shows: "Raspberries 12 oz"
   → name="Raspberries", size="12 oz"

   Receipt shows: "Blackberries 6 oz"
   → name="Blackberries", size="6 oz"

   Receipt shows: "Milk Gallon"
   → name="Milk", size="1 gallon"

   Receipt shows: "Eggs Dozen"
   → name="Eggs", size="12 ct"

   ❌ WRONG - DO NOT DO THIS:
   Receipt shows: "Raspberries 12 oz"
   → name="Raspberries 12 oz", size="" ← WRONG! Size in name field!

   Receipt shows: "Strawberries Org 1 lb"
   → name="Strawberries Org 1 lb", size="" ← WRONG! Size in name field!

   ALWAYS extract size information to the size field, NOT the name field.
   ═════════════════════════════════════════════════

   CRITICAL - PRICE ACCURACY:
   - DOUBLE-CHECK every price carefully. Price accuracy is CRITICAL.
   - Verify each price is from THE SAME LINE as the item name.
   - Be extremely careful with similar-looking digits: **4 vs 9** (most common on thermal receipts), 0 vs 8, 1 vs 7, 5 vs 6, 3 vs 8, 7 vs 9
   - After extraction, mentally verify that item prices roughly add up to the total shown on receipt.

   THERMAL RECEIPT OCR CHALLENGES:
   ═══════════════════════════════════════════════════
   WARNING: This may be a THERMAL RECEIPT (faded print, lower quality).
   Thermal receipts are notorious for poor OCR due to fading over time.

   Pay EXTRA attention to these commonly confused digits:
   - **4 vs 9** (MOST COMMON ERROR - very similar when faded)
     Example: $2.49 can look like $2.99, $3.49 can look like $3.99
   - 0 vs 8 (circles vs figure-8)
   - 1 vs 7 (straight line vs angled)
   - 5 vs 6 (top loop difference)
   - 3 vs 8 (curves vs loops)

   DIGIT-BY-DIGIT VERIFICATION:
   Before recording ANY price, examine each digit individually:
   1. Look at the SHAPE of each digit carefully
   2. If a digit looks ambiguous, use context clues:
      - Does $X.49 or $X.99 make more sense for this item?
      - Are there similar items nearby with .49 or .99 prices?
      - Produce/basic items often end in .49, .79, .99
   3. When in doubt between 4 and 9, examine the digit's TOP:
      - "4" has an OPEN top (looks like ">4")
      - "9" has a CLOSED loop at top (looks like a balloon)
   ═══════════════════════════════════════════════════

   Complete Examples with Visual Spacing:
   ─────────────────────────────────────
   Example 1 - Simple item:
   Receipt: "Banana Organic              $2.99"
   → name: "Banana Organic", price: "2.99", quantity: 1
   (Price $2.99 is on SAME line as item)

   Example 2 - Weighted item:
   Receipt:
   "Karela per lb               $1.24"
   "  0.69 lb @ 1.79/lb"
   → name: "Karela per lb", size: "0.69 lb", price: "1.24", quantity: 1
   (Price $1.24 from first line - actual amount paid, NOT $1.79 unit price)

   Example 3 - Quantity item:
   Receipt:
   "Avocado Large Eac           $3.87"
   "  3 @ $1.29"
   → name: "Avocado Large", price: "1.29", quantity: 3
   (Unit price $1.29 from indented breakdown line, NOT $3.87 total)

   Example 4 - Multiple items (avoid adjacent line confusion):
   Receipt:
   "Apple Fuji                  $3.99"
   "Banana Organic              $2.99"
   "Orange Navel                $4.49"
   → Extract 3 separate items:
     1. "Apple Fuji" with price "3.99"
     2. "Banana Organic" with price "2.99" (NOT 3.99 or 4.49!)
     3. "Orange Navel" with price "4.49"

3. Receipt totals:
   - total_amount: Total amount paid (final total after tax)

MANDATORY SELF-CHECK - DO THIS BEFORE RETURNING JSON:
════════════════════════════════════════════════════
Go through EACH extracted item and verify all 4 checks:

For EVERY item, ask yourself:
1. ✓ SAME LINE CHECK: Is this price from the SAME horizontal line as the item name?
   - NOT from the line above?
   - NOT from the line below?
   - NOT from an indented breakdown line (unless it's a quantity breakdown)?

2. ✓ DIGIT CHECK: Did I carefully examine each digit for 4 vs 9 confusion?
   - Does $X.49 or $X.99 make more sense for this item?
   - Did I look at the TOP of ambiguous digits (4 = open, 9 = closed loop)?

3. ✓ REASONABLENESS CHECK: Does this price make sense?
   - Produce items: Usually $0.50 - $5.00
   - Packaged items: Usually $2.00 - $8.00
   - If price seems too high/low, re-examine

4. ✓ TOTAL CHECK: Do all prices add up to roughly the receipt total?
   - Calculate sum of (price × quantity) for all items
   - Should be close to the total_amount shown on receipt

If ANY check fails for ANY item, STOP and RE-EXAMINE that item!
════════════════════════════════════════════════════

FINAL VERIFICATION - BEFORE RETURNING JSON:
1. Did you extract EVERY item from the receipt?
2. Did you complete the 4-point SELF-CHECK above for EACH item?
3. Did you re-examine any items that failed checks?
4. Are you confident all prices are accurate (no 4↔9 confusion, no adjacent line errors)?

Return ONLY a valid JSON object with this structure:
{
  "store_name": "...",
  "store_location": "...",
  "items": [
    {"name": "...", "brand": "...", "size": "...", "price": "...", "quantity": 1, "category": "..."}
  ],
  "total_amount": "..."
}

If you cannot determine a field, use empty string "" for text fields or 1 for quantity. Do NOT add any other text outside the JSON.
"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o",  # Vision-capable model
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{receipt_image_b64}"
                            }
                        }
                    ]
                }
            ],
            max_tokens=4000,  # Increased for receipts with many items (26+ items need ~3000+ tokens)
            temperature=0.1  # Low temperature for consistency
        )

        # Parse JSON response
        content = response.choices[0].message.content.strip()

        # Remove markdown code blocks if present
        if content.startswith('```json'):
            content = content.replace('```json', '').replace('```', '').strip()
        elif content.startswith('```'):
            content = content.replace('```', '').strip()

        result = json.loads(content)

        store_name = result.get('store_name', 'Unknown Store')
        store_location = result.get('store_location', '')
        items = result.get('items', [])
        total_str = result.get('total_amount', '')

        logger.info(f"OpenAI extracted: store_name='{store_name}', store_location='{store_location}', items={len(items)}")

        # Parse total_amount
        total_amount = None
        if total_str:
            try:
                # Remove $ and commas
                total_clean = total_str.replace('$', '').replace(',', '')
                total_amount = float(total_clean)
            except ValueError:
                logger.warning(f"Could not parse total_amount: {total_str}")

        # Clean up product names and sizes (extract size from name if needed)
        for item in items:
            if 'name' in item:
                original_name = item['name']
                original_size = item.get('size', '')
                cleaned_name, cleaned_size = clean_product_name_and_size(original_name, original_size)

                if cleaned_name != original_name or cleaned_size != original_size:
                    logger.info(f"Cleaned product: '{original_name}' (size: '{original_size}') → '{cleaned_name}' (size: '{cleaned_size}')")
                    item['name'] = cleaned_name
                    item['size'] = cleaned_size

        # Merge duplicate items (same name, brand, size, price)
        items = _merge_duplicate_items(items)

        return store_name, store_location, items, total_amount

    except Exception as e:
        logger.error(f"OpenAI Vision API error: {e}")
        raise


def _get_or_create_user_family(user):
    """
    Get user's primary family, or create one if they don't have any
    """
    # Check if user is already in a family
    membership = FamilyMember.objects.filter(user=user).first()

    if membership:
        return membership.family

    # Create new family
    import random
    import string
    invite_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

    # Ensure unique invite code
    while Family.objects.filter(invite_code=invite_code).exists():
        invite_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

    family = Family.objects.create(
        name=f"{user.username}'s Family",
        invite_code=invite_code
    )

    # Add user as owner
    FamilyMember.objects.create(
        user=user,
        family=family,
        role='owner'
    )

    return family


def _update_grocery_items(items_list, store_name):
    """
    Update store-specific GroceryItem list for autocomplete/search
    Creates or increments purchase count for each item at this store
    """
    for item_data in items_list:
        name = item_data.get('name', '').strip()
        if not name:
            continue

        brand = item_data.get('brand', '').strip()
        size = item_data.get('size', '').strip()
        category = item_data.get('category', '').strip()

        # Clean up name/size separation (safety net for direct calls)
        name, size = clean_product_name_and_size(name, size)

        # Normalize size for weighted items (e.g., "0.69 lb" → "per lb")
        normalized_size = normalize_weighted_item_size(size)

        # Get or create store-specific grocery item (using normalized size)
        grocery_item, created = GroceryItem.objects.get_or_create(
            name=name,
            brand=brand,
            size=normalized_size,  # Use normalized size for matching
            store_name=store_name,
            defaults={'category': category}
        )

        # Increment purchase counter
        grocery_item.times_purchased += 1
        grocery_item.save(update_fields=['times_purchased'])


def _update_shopping_list_from_trip(family, user, store_name, store_location, items_list, trip_date):
    """
    Update shopping list after receipt upload (works for both family and personal lists):
    1. Clear "Already Got" items (checked items from last week - user bought them)
    2. Add new receipt items as "Need to Buy" (unchecked - ready for this week)
    3. Keep items user deliberately unchecked (things they decided not to buy)
    4. Auto-link items to store-specific GroceryItem database

    Workflow:
    Week 1:
    - Upload receipt → items added as "Need to Buy" (unchecked)
    - Family reviews, checks items they don't want
    - During shopping → check off items as you buy them → moves to "Already Got"

    Week 2:
    - Upload new receipt → clears "Already Got" section (last week's items)
    - New items added to "Need to Buy"
    - Keeps any unchecked items (things family decided not to buy)
    """
    from django.db import IntegrityError

    # Get or create shopping list for this store LOCATION (family or personal)
    # Handle race condition where list might be created between check and create
    max_retries = 3
    shopping_list = None
    created = False

    for attempt in range(max_retries):
        try:
            if family:
                shopping_list, created = ShoppingList.objects.get_or_create(
                    family=family,
                    user=None,
                    store_name=store_name,
                    store_location=store_location,
                    defaults={'created_by': family.members.first().user if family.members.exists() else None}
                )
            else:
                shopping_list, created = ShoppingList.objects.get_or_create(
                    family=None,
                    user=user,
                    store_name=store_name,
                    store_location=store_location,
                    defaults={'created_by': user}
                )
            break  # Success - exit retry loop
        except IntegrityError as e:
            if attempt < max_retries - 1:
                # Retry - another request probably created it
                logger.warning(f"Retry {attempt + 1}/{max_retries} for shopping list due to IntegrityError: {e}")
                import time
                time.sleep(0.1)  # Brief pause before retry
                continue
            else:
                # Last attempt failed - try case-insensitive search to retrieve existing
                logger.error(f"Failed get_or_create after {max_retries} attempts, trying case-insensitive search")
                try:
                    if family:
                        shopping_list = ShoppingList.objects.filter(
                            family=family,
                            user__isnull=True,
                            store_name__iexact=store_name,  # Case-insensitive
                            store_location__iexact=store_location  # Case-insensitive
                        ).first()
                    else:
                        shopping_list = ShoppingList.objects.filter(
                            family__isnull=True,
                            user=user,
                            store_name__iexact=store_name,  # Case-insensitive
                            store_location__iexact=store_location  # Case-insensitive
                        ).first()

                    if shopping_list:
                        created = False
                        logger.info(f"✅ Retrieved existing shopping list via case-insensitive search")
                    else:
                        # Shouldn't happen, but log and re-raise original error
                        logger.error(f"❌ Shopping list doesn't exist even after IntegrityError!")
                        raise e
                except Exception as search_error:
                    logger.error(f"❌ Final search failed: {search_error}")
                    raise e

    store_display = f"{store_name} - {store_location}" if store_location else store_name
    logger.info(f"{'Created' if created else 'Found'} shopping list for {store_display}")

    # STEP 1: Clear "Already Got" items (things user bought last week)
    checked_count = shopping_list.list_items.filter(is_checked=True).count()
    if checked_count > 0:
        shopping_list.list_items.filter(is_checked=True).delete()
        logger.info(f"🗑️ Cleared {checked_count} 'Already Got' items from {store_name} list (bought last week)")

    # STEP 2: Process receipt items
    added_count = 0
    updated_count = 0
    linked_count = 0

    for item_data in items_list:
        name = item_data.get('name', '').strip()
        if not name:
            continue

        brand = item_data.get('brand', '').strip()
        size = item_data.get('size', '').strip()
        price = item_data.get('price', '').strip()
        category = item_data.get('category', '').strip()

        # Clean up name/size separation (safety net for direct calls)
        name, size = clean_product_name_and_size(name, size)

        # Normalize size for weighted items when matching GroceryItem
        normalized_size = normalize_weighted_item_size(size)

        # STEP 2A: Find or create store-specific GroceryItem (using normalized size)
        # First try: EXACT match on name+brand+size
        grocery_item = GroceryItem.objects.filter(
            name__iexact=name,
            brand__iexact=brand,
            size__iexact=normalized_size,  # Use normalized size for matching
            store_name=store_name  # Filter by store!
        ).first()

        # Fallback: If brand/size empty AND no exact match, try fuzzy name-only match
        if not grocery_item and (not brand or not normalized_size):
            # Brand or size missing from receipt - do fuzzy name-only match
            # This prevents duplicates when OpenAI doesn't extract complete data
            grocery_item = GroceryItem.objects.filter(
                name__iexact=name,
                store_name=store_name
            ).order_by(
                '-enriched_from_barcode',  # Prioritize items with barcode data (more trustworthy)
                '-times_purchased'          # Then by popularity
            ).first()

            if grocery_item:
                logger.info(f"📍 Fuzzy matched '{name}' to existing item: brand='{grocery_item.brand}', size='{grocery_item.size}' (barcode={bool(grocery_item.barcode)})")

        if not grocery_item:
            # No match found - create new store-specific grocery item (with normalized size)
            grocery_item = GroceryItem.objects.create(
                name=name,
                brand=brand,
                size=normalized_size,  # Store normalized size
                category=category,
                store_name=store_name
            )
            logger.info(f"🆕 Created store-specific GroceryItem: {name} @ {store_name} (size: {normalized_size})")
        else:
            linked_count += 1

        # Increment store-specific purchase counter
        grocery_item.times_purchased += 1
        grocery_item.save(update_fields=['times_purchased'])

        # STEP 2B: Try to find existing item still in "Need to Buy" (not bought yet)
        # Note: We match on NORMALIZED size here too, so weighted items don't duplicate
        # First try: EXACT match on name+brand+size
        existing_item = shopping_list.list_items.filter(
            name=name,
            brand=brand,
            size=normalized_size,  # Use normalized size for matching
            is_checked=False  # Items still in "Need to Buy" from previous weeks
        ).first()

        # Fallback: If brand/size empty AND no exact match, try fuzzy name-only match
        if not existing_item and (not brand or not normalized_size):
            # Brand or size missing from receipt - do fuzzy name-only match
            # This prevents duplicate list items when OpenAI doesn't extract complete data
            existing_item = shopping_list.list_items.filter(
                name__iexact=name,
                is_checked=False
            ).order_by('-purchase_count', '-last_purchased_date').first()

            if existing_item:
                logger.info(f"📍 Fuzzy matched list item '{name}' to existing entry: brand='{existing_item.brand}', size='{existing_item.size}'")

        if existing_item:
            # Item already exists in "Need to Buy" - just update price/tracking info
            existing_item.price = price
            existing_item.last_purchased_date = trip_date
            existing_item.purchase_count += 1
            existing_item.grocery_item = grocery_item  # Link to global item
            existing_item.save(update_fields=['price', 'last_purchased_date', 'purchase_count', 'grocery_item'])
            updated_count += 1
            logger.info(f"📊 Updated existing 'Need to Buy' item: {name}")
        else:
            # New item - add to "Need to Buy" list (unchecked)
            ShoppingListItem.objects.create(
                shopping_list=shopping_list,
                name=name,
                brand=brand,
                size=normalized_size,  # Store normalized size
                price=price,
                category=category,
                quantity=1,
                is_checked=False,  # Start as "Need to Buy" (unchecked)
                last_purchased_date=trip_date,
                purchase_count=1,
                grocery_item=grocery_item  # Link to global item
            )
            added_count += 1

    logger.info(f"✅ Shopping list updated: {store_name} - Added {added_count} new items, Updated {updated_count}, Linked {linked_count} to global database")



# ========================================
# SHOPPING HISTORY API
# ========================================

@require_firebase_auth
def shopping_history_api(request):
    """
    Get user's shopping trip history (most recent first)

    GET /shopright/api/shopping-history/?limit=20

    Returns: {
        "trips": [
            {
                "id": 123,
                "store_name": "Trader Joe's",
                "store_location": "Cupertino, CA",
                "trip_date": "2025-11-01T14:30:00",
                "item_count": 15,
                "total_amount": "45.67"
            },
            ...
        ]
    }
    """
    limit = int(request.GET.get('limit', 20))

    logger.info(f"History request from user: {request.user.username} (id={request.user.id})")

    # Check subscription status for 30-day limit (free tier)
    subscription = SubscriptionService.get_or_create_subscription(request.user)

    # Get user's family (if any)
    membership = FamilyMember.objects.filter(user=request.user).first()

    if membership:
        # User has family - show ALL trips from both user AND family
        trips = ShoppingTrip.objects.filter(
            models.Q(user=request.user) | models.Q(family=membership.family)
        )
    else:
        # User has no family - show only personal trips
        trips = ShoppingTrip.objects.filter(user=request.user)

    # Apply 30-day limit for free users
    if not SubscriptionService.is_premium_user(request.user):
        thirty_days_ago = timezone.now().date() - timedelta(days=30)
        trips = trips.filter(trip_date__gte=thirty_days_ago)
        logger.info(f"Free user - filtering to receipts from last 30 days (since {thirty_days_ago})")

    trips = trips.order_by('-trip_date')[:limit]

    if membership:
        logger.info(f"Found {trips.count()} trips for user + family {membership.family.id}")
    else:
        logger.info(f"Found {trips.count()} personal trips for user (no family)")

    trips_data = [
        {
            'id': trip.id,
            'store_name': trip.store_name,
            'store_location': trip.store_location,
            'trip_date': trip.trip_date.isoformat(),
            'item_count': len(trip.items),
            'total_amount': str(trip.total_amount) if trip.total_amount else None,
            'shopped_by': trip.user.username,
            'receipt_image_url': request.build_absolute_uri(trip.receipt_image.url) if trip.receipt_image else None
        }
        for trip in trips
    ]

    logger.info(f"Returning {len(trips_data)} trips in response")

    return JsonResponse({'trips': trips_data})


@csrf_exempt
@require_firebase_auth
def trip_detail_api(request, trip_id):
    """
    Get or update detailed items from a specific shopping trip

    GET /shopright/api/trip/<trip_id>/
    PUT /shopright/api/trip/<trip_id>/

    Returns: {
        "trip": {
            "id": 123,
            "store_name": "Trader Joe's",
            "trip_date": "2025-11-01T14:30:00",
            "items": [...],
            "total_amount": "45.67"
        }
    }
    """
    try:
        trip = ShoppingTrip.objects.get(id=trip_id)
    except ShoppingTrip.DoesNotExist:
        return JsonResponse({'error': 'Trip not found'}, status=404)

    # Verify user has access (same family or own trip)
    membership = FamilyMember.objects.filter(user=request.user).first()

    if trip.user != request.user:
        # Check if same family
        if not membership or trip.family != membership.family:
            return JsonResponse({'error': 'Access denied'}, status=403)

    if request.method == 'GET':
        # Enrich items with image URLs - exact match for this store only
        enriched_items = []
        for item in trip.items:
            enriched_item = item.copy()
            item_name = item.get('name', '')

            if item_name:
                # Only try exact name match for THIS STORE
                grocery_item = GroceryItem.objects.filter(
                    name__iexact=item_name,
                    store_name=trip.store_name,
                    image_url__isnull=False
                ).exclude(image_url='').first()

                if grocery_item and grocery_item.image_url:
                    # Convert relative URL to absolute URL
                    absolute_url = request.build_absolute_uri(grocery_item.image_url)
                    enriched_item['image_url'] = absolute_url
                else:
                    enriched_item['image_url'] = None
            else:
                enriched_item['image_url'] = None
            enriched_items.append(enriched_item)

        return JsonResponse({
            'trip': {
                'id': trip.id,
                'store_name': trip.store_name,
                'store_location': trip.store_location,
                'trip_date': trip.trip_date.isoformat(),
                'items': enriched_items,
                'total_amount': str(trip.total_amount) if trip.total_amount else None,
                'shopped_by': trip.user.username,
                'receipt_image_url': request.build_absolute_uri(trip.receipt_image.url) if trip.receipt_image else None
            }
        })

    elif request.method == 'PUT':
        try:
            data = json.loads(request.body)
            logger.info(f"PUT request data keys: {data.keys()}")
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)

        # Update fields if provided
        if 'store_name' in data:
            logger.info(f"Updating store_name: '{trip.store_name}' -> '{data['store_name']}'")
            trip.store_name = data['store_name']

        if 'store_location' in data:
            logger.info(f"Updating store_location: '{trip.store_location}' -> '{data['store_location']}'")
            trip.store_location = data['store_location']

        if 'items' in data:
            items_count = len(data['items'])
            logger.info(f"Updating items: {len(trip.items)} -> {items_count} items")
            logger.info(f"First 3 items: {data['items'][:3] if items_count > 0 else []}")
            trip.items = data['items']

        if 'total_amount' in data:
            try:
                total_str = data['total_amount']
                if total_str == '' or total_str is None:
                    trip.total_amount = None
                else:
                    trip.total_amount = float(total_str)
                logger.info(f"Updating total_amount: {trip.total_amount}")
            except (ValueError, TypeError) as e:
                logger.warning(f"Could not parse total_amount: {data['total_amount']}, error: {e}")
                pass

        trip.save()

        logger.info(f"✅ Updated trip {trip_id}: store={trip.store_name}, items={len(trip.items)}")

        return JsonResponse({
            'trip': {
                'id': trip.id,
                'store_name': trip.store_name,
                'store_location': trip.store_location,
                'trip_date': trip.trip_date.isoformat(),
                'items': trip.items,
                'total_amount': str(trip.total_amount) if trip.total_amount else None,
                'shopped_by': trip.user.username,
                'receipt_image_url': request.build_absolute_uri(trip.receipt_image.url) if trip.receipt_image else None
            }
        })

    else:
        return JsonResponse({'error': 'Method not allowed'}, status=405)


# ========================================
# FAMILY MANAGEMENT API
# ========================================

@csrf_exempt
@require_firebase_auth
def create_family_api(request):
    """
    Create a new family group

    POST /shopright/api/family/create/
    Body: {
        "name": "The Smiths"  # optional
    }

    Returns: {
        "family_id": 1,
        "name": "The Smiths",
        "invite_code": "ABC123"
    }
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    # Check if user is already in a family
    existing_membership = FamilyMember.objects.filter(user=request.user).first()
    if existing_membership:
        return JsonResponse({
            'error': f'You are already in a family: {existing_membership.family.name}'
        }, status=400)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        data = {}

    family_name = data.get('name', f"{request.user.username}'s Family")

    # Generate unique invite code
    import random
    import string
    invite_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

    while Family.objects.filter(invite_code=invite_code).exists():
        invite_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

    family = Family.objects.create(
        name=family_name,
        invite_code=invite_code
    )

    # Add creator as owner
    FamilyMember.objects.create(
        user=request.user,
        family=family,
        role='owner'
    )

    logger.info(f"✅ Family created: {family.name} by {request.user.username} (invite code: {invite_code})")

    # Auto-convert all personal data to family data
    trips_converted = ShoppingTrip.objects.filter(user=request.user, family__isnull=True).update(family=family)
    lists_converted = ShoppingList.objects.filter(user=request.user, family__isnull=True).update(family=family)

    logger.info(f"📦 Auto-shared with family: {trips_converted} trips, {lists_converted} shopping lists")

    return JsonResponse({
        'family_id': family.id,
        'name': family.name,
        'invite_code': family.invite_code
    })


@csrf_exempt
@require_firebase_auth
def join_family_api(request):
    """
    Join existing family using invite code

    POST /shopright/api/family/join/
    Body: {
        "invite_code": "ABC123"
    }

    Returns: {
        "family_id": 1,
        "name": "The Smiths",
        "member_count": 2
    }
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    invite_code = data.get('invite_code', '').strip().upper()
    if not invite_code:
        return JsonResponse({'error': 'Missing invite_code'}, status=400)

    try:
        family = Family.objects.get(invite_code=invite_code)
    except Family.DoesNotExist:
        return JsonResponse({'error': 'Invalid invite code'}, status=404)

    # Check if already member
    if FamilyMember.objects.filter(user=request.user, family=family).exists():
        return JsonResponse({'error': 'Already a member of this family'}, status=400)

    # Check family member limit based on owner's subscription
    family_owner = FamilyMember.objects.filter(family=family, role='owner').first()
    if family_owner:
        is_premium = SubscriptionService.is_premium_user(family_owner.user)

        if not is_premium:
            if family.member_count >= 2:
                logger.info(f"🚫 {request.user.username} tried to join '{family.name}' but family is at free tier limit (2 members)")
                return JsonResponse({
                    'error': 'FAMILY_LIMIT_REACHED',
                    'message': 'Free families are limited to 2 members. The family owner needs to upgrade to Premium for unlimited members.',
                    'current_members': family.member_count
                }, status=403)

    # Add as member
    FamilyMember.objects.create(
        user=request.user,
        family=family,
        role='member'
    )

    # Auto-regenerate invite code for security (one-time use)
    import random
    import string
    old_code = family.invite_code
    new_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

    # Ensure uniqueness
    while Family.objects.filter(invite_code=new_code).exists():
        new_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

    family.invite_code = new_code
    family.save()

    logger.info(f"👤 {request.user.username} joined family '{family.name}'")
    logger.info(f"🔄 Invite code auto-regenerated: {old_code} → {new_code}")

    return JsonResponse({
        'family_id': family.id,
        'name': family.name,
        'member_count': family.members.count()
    })


@require_firebase_auth
def family_info_api(request):
    """
    Get current user's family info

    GET /shopright/api/family/info/

    Returns: {
        "family": {
            "id": 1,
            "name": "The Smiths",
            "invite_code": "ABC123",
            "member_count": 2,
            "members": [...]
        }
    }
    """
    membership = FamilyMember.objects.filter(user=request.user).first()

    if not membership:
        return JsonResponse({'family': None})

    family = membership.family
    members = FamilyMember.objects.filter(family=family).select_related('user')

    # Get family owner's subscription status to determine member limit
    family_owner = members.filter(role='owner').first()
    if family_owner:
        is_premium = SubscriptionService.is_premium_user(family_owner.user)
        member_limit = None if is_premium else 2
        can_add_members = family.can_add_member(is_premium)
    else:
        # Fallback if no owner found (shouldn't happen)
        member_limit = 2
        can_add_members = family.member_count < 2

    return JsonResponse({
        'family': {
            'id': family.id,
            'name': family.name,
            'invite_code': family.invite_code,
            'member_count': members.count(),
            'member_limit': member_limit,
            'can_add_members': can_add_members,
            'members': [
                {
                    'username': m.user.username,
                    'role': m.role,
                    'joined_at': m.joined_at.isoformat()
                }
                for m in members
            ]
        }
    })


@csrf_exempt
@require_firebase_auth
def leave_family_api(request):
    """
    Leave current family

    POST /shopright/api/family/leave/

    Returns: {
        "success": true,
        "message": "Left family successfully"
    }
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    # Get user's membership
    membership = FamilyMember.objects.filter(user=request.user).first()

    if not membership:
        return JsonResponse({'error': 'You are not in a family'}, status=400)

    family = membership.family
    family_name = family.name

    # Check if user is the owner and there are other members
    if membership.role == 'owner':
        other_members = FamilyMember.objects.filter(family=family).exclude(user=request.user)
        if other_members.exists():
            return JsonResponse({
                'error': 'You are the owner. Transfer ownership or remove all members before leaving.'
            }, status=400)

    # Delete the membership
    membership.delete()
    logger.info(f"👋 User {request.user.username} left family: {family_name}")

    # If family has no members left, delete the family
    remaining_members = FamilyMember.objects.filter(family=family).count()
    if remaining_members == 0:
        family.delete()
        logger.info(f"🗑️ Empty family deleted: {family_name}")

    return JsonResponse({
        'success': True,
        'message': 'Left family successfully'
    })


@csrf_exempt
@require_firebase_auth
def regenerate_invite_code_api(request):
    """
    Regenerate family invite code (owner only)

    POST /shopright/api/family/regenerate-code/

    Returns: {
        "success": true,
        "new_code": "XYZ789"
    }
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    # Get user's family membership
    membership = FamilyMember.objects.filter(user=request.user).first()

    if not membership:
        return JsonResponse({'error': 'You are not in a family'}, status=400)

    # Only owner can regenerate code
    if membership.role != 'owner':
        return JsonResponse({'error': 'Only family owner can regenerate invite code'}, status=403)

    family = membership.family
    old_code = family.invite_code

    # Generate new unique code
    import random
    import string
    new_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

    while Family.objects.filter(invite_code=new_code).exists():
        new_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

    family.invite_code = new_code
    family.save()

    logger.info(f"🔄 Owner {request.user.username} manually regenerated invite code: {old_code} → {new_code}")

    return JsonResponse({
        'success': True,
        'new_code': new_code
    })


@csrf_exempt
@require_firebase_auth
def remove_family_member_api(request):
    """
    Remove a member from family (owner only)

    POST /shopright/api/family/remove-member/
    Body: {
        "member_username": "+12345678901"
    }

    Returns: {
        "success": true,
        "message": "Member removed successfully"
    }
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    member_username = data.get('member_username')
    if not member_username:
        return JsonResponse({'error': 'Missing member_username'}, status=400)

    # Get requester's family membership
    requester_membership = FamilyMember.objects.filter(user=request.user).first()

    if not requester_membership:
        return JsonResponse({'error': 'You are not in a family'}, status=400)

    # Only owner can remove members
    if requester_membership.role != 'owner':
        return JsonResponse({'error': 'Only family owner can remove members'}, status=403)

    family = requester_membership.family

    # Find the member to remove
    try:
        user_to_remove = User.objects.get(username=member_username)
        member_to_remove = FamilyMember.objects.get(user=user_to_remove, family=family)
    except (User.DoesNotExist, FamilyMember.DoesNotExist):
        return JsonResponse({'error': 'Member not found in this family'}, status=404)

    # Can't remove yourself
    if user_to_remove == request.user:
        return JsonResponse({'error': 'Cannot remove yourself. Use leave family instead.'}, status=400)

    # Can't remove other owners
    if member_to_remove.role == 'owner':
        return JsonResponse({'error': 'Cannot remove other owners'}, status=400)

    # Remove the member
    member_to_remove.delete()
    logger.info(f"🚫 Owner {request.user.username} removed {member_username} from family '{family.name}'")

    return JsonResponse({
        'success': True,
        'message': f'Removed {member_username} from family'
    })


@csrf_exempt
@require_firebase_auth
def transfer_ownership_api(request):
    """
    Transfer family ownership to another member (owner only)

    POST /shopright/api/family/transfer-ownership/
    Body: {
        "new_owner_username": "+12345678901"
    }

    Returns: {
        "success": true,
        "message": "Ownership transferred successfully",
        "new_owner": "+12345678901"
    }
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    new_owner_username = data.get('new_owner_username')
    if not new_owner_username:
        return JsonResponse({'error': 'Missing new_owner_username'}, status=400)

    # Get requester's family membership
    requester_membership = FamilyMember.objects.filter(user=request.user).first()

    if not requester_membership:
        return JsonResponse({'error': 'You are not in a family'}, status=400)

    # Only owner can transfer ownership
    if requester_membership.role != 'owner':
        return JsonResponse({'error': 'Only family owner can transfer ownership'}, status=403)

    family = requester_membership.family

    # Find the new owner
    try:
        new_owner_user = User.objects.get(username=new_owner_username)
        new_owner_membership = FamilyMember.objects.get(user=new_owner_user, family=family)
    except (User.DoesNotExist, FamilyMember.DoesNotExist):
        return JsonResponse({'error': 'Member not found in this family'}, status=404)

    # Can't transfer to yourself
    if new_owner_user == request.user:
        return JsonResponse({'error': 'You are already the owner'}, status=400)

    # Transfer ownership: demote current owner to member, promote new owner to owner
    requester_membership.role = 'member'
    requester_membership.save()

    new_owner_membership.role = 'owner'
    new_owner_membership.save()

    logger.info(f"👑 Ownership transferred in family '{family.name}': {request.user.username} → {new_owner_username}")

    return JsonResponse({
        'success': True,
        'message': f'Ownership transferred to {new_owner_username}',
        'new_owner': new_owner_username
    })


# ========================================
# SHOPPING LISTS API
# ========================================

@require_firebase_auth
def shopping_lists_api(request):
    """
    Get all shopping lists for user (personal or family)

    GET /shopright/api/shopping-lists/

    Returns: {
        "lists": [
            {
                "id": 1,
                "store_name": "Trader Joe's",
                "item_count": 12,
                "checked_count": 8,
                "created_at": "2025-11-03T10:00:00",
                "updated_at": "2025-11-03T14:30:00"
            },
            ...
        ]
    }
    """
    # Get user's family (if any)
    membership = FamilyMember.objects.filter(user=request.user).first()

    # Get ALL active shopping lists (personal + family if they have one)
    if membership:
        # User has family - show both personal AND family lists
        lists = ShoppingList.objects.filter(
            models.Q(user=request.user, family__isnull=True) | models.Q(family=membership.family),
            is_active=True
        ).prefetch_related('list_items')
    else:
        # User has no family - show only personal lists
        lists = ShoppingList.objects.filter(
            user=request.user,
            family__isnull=True,
            is_active=True
        ).prefetch_related('list_items')

    lists_data = [
        {
            'id': lst.id,
            'store_name': lst.store_name,
            'store_location': lst.store_location or '',
            'item_count': lst.total_count,
            'checked_count': lst.checked_count,
            'created_at': lst.created_at.isoformat(),
            'updated_at': lst.updated_at.isoformat()
        }
        for lst in lists
    ]

    return JsonResponse({'lists': lists_data})


@csrf_exempt
@require_firebase_auth
def shopping_list_detail_api(request, list_id):
    """
    Get, update, or delete a specific shopping list

    GET /shopright/api/shopping-list/<list_id>/
    PUT /shopright/api/shopping-list/<list_id>/
    DELETE /shopright/api/shopping-list/<list_id>/

    GET Returns: {
        "list": {
            "id": 1,
            "store_name": "Trader Joe's",
            "items": [
                {
                    "id": 1,
                    "name": "Milk Gallon Whole",
                    "brand": "Horizon",
                    "size": "64oz",
                    "price": "4.49",
                    "category": "Dairy",
                    "quantity": 1,
                    "is_checked": true,
                    "last_purchased_date": "2025-11-01T14:30:00",
                    "purchase_count": 5
                },
                ...
            ]
        }
    }
    """
    try:
        lst = ShoppingList.objects.get(id=list_id)
    except ShoppingList.DoesNotExist:
        return JsonResponse({'error': 'List not found'}, status=404)

    # Verify user has access (same family OR personal list owned by user OR shopper assigned to deliver)
    membership = FamilyMember.objects.filter(user=request.user).first()

    # Allow access if:
    # 1. It's a personal list (no family) owned by this user, OR
    # 2. It's a family list and user is in that family, OR
    # 3. It's a shopper who has an active delivery assignment for this list
    is_personal_list = lst.family is None and lst.user == request.user
    is_family_list = membership and lst.family == membership.family

    # Check if user is a shopper with active delivery assignment
    is_assigned_shopper = False
    try:
        # Check if user is approved shopper (same as @require_approved_shopper decorator)
        if hasattr(request.user, 'profile') and request.user.profile.is_approved_shopper:
            logger.info(f"🛒 Found approved shopper: {request.user.username}")

            # Check if shopper has active delivery for this shopping list
            # Use WeeklyDelivery.shopping_list directly (not subscription.shopping_list)
            active_deliveries = WeeklyDelivery.objects.filter(
                shopper=request.user,
                shopping_list=lst,  # Direct FK on WeeklyDelivery
                status__in=['assigned', 'packing', 'ready', 'out_for_delivery']
            )

            logger.info(f"🛒 Active deliveries for list {lst.id}: {active_deliveries.count()}")
            for delivery in active_deliveries:
                logger.info(f"   - Delivery {delivery.id}, status: {delivery.status}")

            is_assigned_shopper = active_deliveries.exists()
        else:
            logger.info(f"🛒 User {request.user.username} is not an approved shopper")
    except Exception as e:
        logger.error(f"🛒 Error checking shopper status: {e}")

    logger.info(f"🔐 Access check for list {lst.id}: personal={is_personal_list}, family={is_family_list}, shopper={is_assigned_shopper}")

    if not (is_personal_list or is_family_list or is_assigned_shopper):
        return JsonResponse({'error': 'Access denied'}, status=403)

    # Block customer edit access when shopper is actively working (packing or delivering)
    if request.method in ['PUT', 'POST'] and (is_personal_list or is_family_list):
        # Check if any shopper is currently working on this list
        # Use WeeklyDelivery.shopping_list directly (not subscription.shopping_list)
        active_shopper_deliveries = WeeklyDelivery.objects.filter(
            shopping_list=lst,  # Direct FK on WeeklyDelivery
            status__in=['packing', 'ready', 'out_for_delivery']
        ).select_related('shopper')

        if active_shopper_deliveries.exists():
            # Get shopper info for informative error message
            delivery = active_shopper_deliveries.first()
            shopper_name = delivery.shopper.get_full_name() if delivery.shopper else "A shopper"

            # Include delivery date in message for clarity
            delivery_date_str = delivery.delivery_date.strftime('%B %d') if delivery.delivery_date else ""
            date_suffix = f" for your {delivery_date_str} delivery" if delivery_date_str else ""

            status_messages = {
                'packing': f'Shopper is currently shopping{date_suffix}.',
                'ready': f'Shopper has finished shopping{date_suffix}. Your order is ready for delivery.',
                'out_for_delivery': f'Shopper is currently delivering your {delivery_date_str} order.' if delivery_date_str else 'Shopper is currently delivering your order.'
            }

            logger.info(f"🚫 Blocked customer {request.user.username} from editing list {lst.id}: delivery status is {delivery.status} for {delivery.delivery_date}")

            return JsonResponse({
                'error': 'order_in_progress',
                'message': status_messages.get(delivery.status, 'Your order is currently being processed by a shopper.'),
                'delivery_status': delivery.status,
                'delivery_date': delivery.delivery_date.isoformat() if delivery.delivery_date else None,
                'read_only': True
            }, status=423)  # HTTP 423 Locked

    if request.method == 'GET':
        items = lst.list_items.all()

        # For shoppers: Only show items customer marked as "need to buy" (is_checked=False)
        # Shoppers shouldn't see items customer already has
        if is_assigned_shopper:
            items = items.filter(is_checked=False)
            logger.info(f"🛒 Shopper view: Filtered to {items.count()} items customer needs (is_checked=False)")

        # Enrich items with image URLs using fuzzy matching (like trip detail API)
        enriched_items = []
        for item in items:
            # For shopper view: use shopper_collected as is_checked
            # For customer view: use is_checked as is_checked
            if is_assigned_shopper:
                # Shopper sees their own collection state
                checked_state = item.shopper_collected
            else:
                # Customer sees their own checked state
                checked_state = item.is_checked

            item_dict = {
                'id': item.id,
                'name': item.name,
                'brand': item.brand,
                'size': item.size,
                'price': item.price,
                'category': item.category,
                'quantity': item.quantity,
                'is_checked': checked_state,
                'last_purchased_date': item.last_purchased_date.isoformat() if item.last_purchased_date else None,
                'purchase_count': item.purchase_count
            }

            # Only show image if item is directly linked to a grocery_item
            # No fuzzy matching during display - we only want to show images for correctly linked items
            image_url = None
            grocery_item_id = None
            nutrition_data = None

            if item.grocery_item:
                if item.grocery_item.image_url:
                    image_url = item.grocery_item.image_url
                grocery_item_id = item.grocery_item.id

                # Add nutrition data if available
                if item.grocery_item.nutriscore_grade or item.grocery_item.nova_group:
                    nutrition_data = {
                        'nutriscore_grade': item.grocery_item.nutriscore_grade,
                        'nova_group': item.grocery_item.nova_group,
                        'has_nutrition_data': True if item.grocery_item.nutrition_data else False,  # Explicit True/False
                        'nutrients': item.grocery_item.nutrition_data.get('nutrients') if item.grocery_item.nutrition_data else None
                    }

            item_dict['image_url'] = image_url
            item_dict['grocery_item_id'] = grocery_item_id
            item_dict['nutrition'] = nutrition_data
            enriched_items.append(item_dict)

        # Check if list is read-only for customer (shopper actively working)
        read_only_info = None
        if is_personal_list or is_family_list:
            # Use WeeklyDelivery.shopping_list directly (not subscription.shopping_list)
            active_shopper_deliveries = WeeklyDelivery.objects.filter(
                shopping_list=lst,  # Direct FK on WeeklyDelivery
                status__in=['packing', 'ready', 'out_for_delivery']
            ).select_related('shopper')

            # DEBUG: Log ALL deliveries for this list to help diagnose issues
            all_deliveries_for_list = WeeklyDelivery.objects.filter(shopping_list=lst)
            logger.info(f"📋 List {lst.id} ({lst.store_name}) has {all_deliveries_for_list.count()} total deliveries:")
            for d in all_deliveries_for_list:
                logger.info(f"   - Delivery {d.id}: status={d.status}, date={d.delivery_date}, shopping_list_id={d.shopping_list_id}")

            if active_shopper_deliveries.exists():
                delivery = active_shopper_deliveries.first()
                shopper_name = delivery.shopper.get_full_name() if delivery.shopper else "A shopper"

                # Include delivery date in message for clarity (especially for Premium with multiple deliveries)
                delivery_date_str = delivery.delivery_date.strftime('%B %d') if delivery.delivery_date else ""
                date_suffix = f" for your {delivery_date_str} delivery" if delivery_date_str else ""

                status_messages = {
                    'packing': f'Shopper is currently shopping{date_suffix}.',
                    'ready': f'Shopper has finished shopping{date_suffix}. Your order is ready for delivery.',
                    'out_for_delivery': f'Shopper is currently delivering your {delivery_date_str} order.' if delivery_date_str else 'Shopper is currently delivering your order.'
                }

                read_only_info = {
                    'is_read_only': True,
                    'message': status_messages.get(delivery.status, 'Your order is currently being processed by a shopper.'),
                    'delivery_status': delivery.status,
                    'shopper_name': shopper_name,
                    'shopper_phone': delivery.shopper.username if delivery.shopper else None,  # Username is phone
                    'delivery_date': delivery.delivery_date.isoformat() if delivery.delivery_date else None,
                    'delivery_id': delivery.id  # Include delivery ID for debugging
                }
                logger.info(f"📖 LOCKED: List {lst.id} is read-only due to delivery {delivery.id} (status={delivery.status}, date={delivery.delivery_date})")
            else:
                logger.info(f"📖 UNLOCKED: List {lst.id} has no active deliveries in packing/ready/out_for_delivery status")

        # Calculate counts based on filtered items for shoppers
        if is_assigned_shopper:
            # For shoppers: count based on filtered items (only items customer needs)
            item_count = len(enriched_items)
            checked_count = sum(1 for item in enriched_items if item['is_checked'])
        else:
            # For customers: use overall list counts
            item_count = lst.total_count
            checked_count = lst.checked_count

        return JsonResponse({
            'list': {
                'id': lst.id,
                'store_name': lst.store_name,
                'store_location': lst.store_location or '',
                'item_count': item_count,
                'checked_count': checked_count,
                'items': enriched_items,
                'read_only': read_only_info  # NEW: Include read-only status
            }
        })

    elif request.method == 'PUT':
        # Update list items (toggle checked, add/remove items)
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)

        # Handle item updates
        if 'items' in data:
            items_data = data['items']

            for item_data in items_data:
                item_id = item_data.get('id')

                if item_id:
                    # Update existing item
                    try:
                        item = ShoppingListItem.objects.get(id=item_id, shopping_list=lst)
                        if 'is_checked' in item_data:
                            # Shopper updates shopper_collected, customer updates is_checked
                            if is_assigned_shopper:
                                item.shopper_collected = item_data['is_checked']
                                if item_data['is_checked']:
                                    item.collected_at = timezone.now()
                                else:
                                    item.collected_at = None
                            else:
                                item.is_checked = item_data['is_checked']
                        if 'quantity' in item_data:
                            item.quantity = item_data['quantity']
                        item.save()
                    except ShoppingListItem.DoesNotExist:
                        continue
                else:
                    # Add new item (or update if exists due to unique constraint)
                    name = item_data.get('name', '')
                    brand = item_data.get('brand', '')
                    size = item_data.get('size', '')

                    # Check if item already exists (same name/brand/size)
                    existing_item = ShoppingListItem.objects.filter(
                        shopping_list=lst,
                        name=name,
                        brand=brand,
                        size=size
                    ).first()

                    if existing_item:
                        # Item already exists - only act if it was checked (move to "Need to Buy")
                        if existing_item.is_checked:
                            # Item was in "Already Got" section - move to "Need to Buy"
                            existing_item.is_checked = False
                            existing_item.save()
                            logger.info(f"Item '{name}' already exists (checked), moving to Need to Buy")
                        else:
                            # Item already in "Need to Buy" - skip duplicate
                            logger.info(f"Item '{name}' already exists in Need to Buy, skipping")
                    else:
                        # Create new item
                        ShoppingListItem.objects.create(
                            shopping_list=lst,
                            name=name,
                            brand=brand,
                            size=size,
                            price=item_data.get('price', ''),
                            category=item_data.get('category', ''),
                            quantity=item_data.get('quantity', 1),
                            is_checked=item_data.get('is_checked', False),  # Default to unchecked (Need to Buy)
                            added_by=request.user
                        )
                        logger.info(f"Created new item '{name}' in list {list_id}")

        logger.info(f"Updated shopping list {list_id}: {lst.store_name}, items={lst.total_count}")

        # Return updated list
        items = lst.list_items.all()
        items_data = []
        for item in items:
            item_dict = {
                'id': item.id,
                'name': item.name,
                'brand': item.brand,
                'size': item.size,
                'price': item.price,
                'category': item.category,
                'quantity': item.quantity,
                'is_checked': item.is_checked,
                'last_purchased_date': item.last_purchased_date.isoformat() if item.last_purchased_date else None,
                'purchase_count': item.purchase_count
            }

            # Add nutrition data if grocery_item is linked
            if item.grocery_item and (item.grocery_item.nutriscore_grade or item.grocery_item.nova_group):
                item_dict['nutrition'] = {
                    'nutriscore_grade': item.grocery_item.nutriscore_grade,
                    'nova_group': item.grocery_item.nova_group,
                    'has_nutrition_data': True if item.grocery_item.nutrition_data else False,  # Explicit True/False
                    'nutrients': item.grocery_item.nutrition_data.get('nutrients') if item.grocery_item.nutrition_data else None
                }
            else:
                item_dict['nutrition'] = None

            items_data.append(item_dict)

        return JsonResponse({
            'list': {
                'id': lst.id,
                'store_name': lst.store_name,
                'store_location': lst.store_location or '',
                'items': items_data
            }
        })

    elif request.method == 'DELETE':
        lst.delete()
        return JsonResponse({'success': True})

    else:
        return JsonResponse({'error': 'Method not allowed'}, status=405)


@csrf_exempt
@require_firebase_auth
def delete_list_item_api(request, item_id):
    """
    Delete a specific item from a shopping list

    DELETE /shopright/api/shopping-list-item/<item_id>/

    Returns: {"success": true}
    """
    if request.method != 'DELETE':
        return JsonResponse({'error': 'Only DELETE allowed'}, status=405)

    try:
        item = ShoppingListItem.objects.get(id=item_id)
    except ShoppingListItem.DoesNotExist:
        return JsonResponse({'error': 'Item not found'}, status=404)

    # Verify user has access (same family OR personal list owned by user)
    membership = FamilyMember.objects.filter(user=request.user).first()

    shopping_list = item.shopping_list
    is_personal_list = shopping_list.family is None and shopping_list.user == request.user
    is_family_list = membership and shopping_list.family == membership.family

    if not (is_personal_list or is_family_list):
        return JsonResponse({'error': 'Access denied'}, status=403)

    item.delete()
    logger.info(f"Deleted list item {item_id}: {item.name}")

    return JsonResponse({'success': True})


# ========================================
# BARCODE SCANNING API
# ========================================

def lookup_barcode_in_openfoodfacts(upc):
    """
    Lookup product by UPC in Open Food Facts API
    https://world.openfoodfacts.org/api/v0/product/{barcode}.json

    Returns: {
        'found': True/False,
        'product_name': 'Trader Joe\'s Organic Whole Milk',
        'brand': 'Trader Joe\'s',
        'image_url': 'https://...',
        'quantity': '1.89 L (64 oz)',
        'categories': 'Dairy, Milk'
    }
    """
    import requests
    from django.utils import timezone

    try:
        url = f"https://world.openfoodfacts.org/api/v0/product/{upc}.json"
        response = requests.get(url, timeout=5)

        if response.status_code != 200:
            logger.warning(f"Barcode API returned {response.status_code} for {upc}")
            return {'found': False}

        data = response.json()

        if data.get('status') != 1:  # Product not found
            logger.info(f"Product not found in Open Food Facts: {upc}")
            return {'found': False}

        product = data.get('product', {})

        result = {
            'found': True,
            'product_name': product.get('product_name', ''),
            'brand': product.get('brands', ''),
            'image_url': product.get('image_url', ''),
            'quantity': product.get('quantity', ''),
            'categories': product.get('categories', ''),
            'ingredients': product.get('ingredients_text', ''),
            'nutrition_grade': product.get('nutrition_grade_fr', '')  # A, B, C, D, E
        }

        logger.info(f"✅ Barcode lookup success: {upc} -> {result['product_name']}")
        return result

    except Exception as e:
        logger.error(f"Barcode lookup failed for {upc}: {e}")
        return {'found': False}


@csrf_exempt
@require_firebase_auth
@require_nutrition_scan_quota
def scan_barcode_api(request):
    """
    Scan barcode for a shopping list item
    Updates the GLOBAL GroceryItem so all users/families benefit

    POST /shopright/api/scan-barcode/
    Body: {
        "list_item_id": 789,
        "barcode": "012345678901"
    }

    Returns: {
        "success": true,
        "product_data": {...},
        "grocery_item_id": 456,
        "families_helped": 5,
        "already_existed": false
    }
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    list_item_id = data.get('list_item_id')
    barcode = data.get('barcode', '').strip()

    if not list_item_id or not barcode:
        return JsonResponse({'error': 'Missing list_item_id or barcode'}, status=400)

    # Get the list item
    try:
        list_item = ShoppingListItem.objects.get(id=list_item_id)
    except ShoppingListItem.DoesNotExist:
        return JsonResponse({'error': 'List item not found'}, status=404)

    store_name = list_item.shopping_list.store_name

    # Check if barcode already exists for THIS STORE
    grocery_item = GroceryItem.objects.filter(
        barcode=barcode,
        store_name=store_name
    ).first()

    if grocery_item:
        # Barcode already scanned for this store - validate match before linking!
        logger.info(f"✅ Barcode {barcode} already exists for {store_name} (enriched by {grocery_item.first_enriched_by})")

        # VALIDATION: Check if product name matches list item name
        match_result = fuzzy_match_product_names(list_item.name, grocery_item.name)
        logger.info(f"🔍 Fuzzy match: {match_result['confidence']}% - {match_result['reason']}")

        if not match_result['match'] or match_result['confidence'] < 70:
            # LOW CONFIDENCE - Return validation failure for user confirmation
            logger.warning(f"⚠️ Low confidence match ({match_result['confidence']}%) - requiring user confirmation")
            return JsonResponse({
                'success': False,
                'needs_confirmation': True,
                'match_result': match_result,
                'product_data': {
                    'product_name': grocery_item.name,
                    'brand': grocery_item.brand,
                    'image_url': grocery_item.image_url,
                    'quantity': grocery_item.size
                },
                'list_item_name': list_item.name,
                'message': 'Product name mismatch detected. Please confirm if this is correct.',
                # Send barcode back so iOS can call confirm endpoint
                'barcode': barcode,
                'list_item_id': list_item_id
            }, status=400)

        # HIGH CONFIDENCE - Proceed with linking
        list_item.grocery_item = grocery_item
        list_item.save()

        # Count how many families could benefit (same store only)
        potential_matches = ShoppingListItem.objects.filter(
            name__icontains=grocery_item.name[:20],
            grocery_item__isnull=True,
            shopping_list__store_name=store_name
        ).exclude(shopping_list__family=list_item.shopping_list.family)
        families_helped = potential_matches.values('shopping_list__family').distinct().count()

        # Build response
        response_data = {
            'success': True,
            'message': 'Product already in database!',
            'product_data': {
                'product_name': grocery_item.name,
                'brand': grocery_item.brand,
                'image_url': grocery_item.image_url,
                'quantity': grocery_item.size
            },
            'grocery_item_id': grocery_item.id,
            'families_helped': families_helped,
            'already_existed': True,
            'has_image': bool(grocery_item.image_url and grocery_item.image_url.strip()),
            'enriched_by': grocery_item.first_enriched_by.username if grocery_item.first_enriched_by else 'community',
            # Health/Nutrition data
            'nutrition': {
                'nutriscore_grade': grocery_item.nutriscore_grade,  # A-E or None
                'nova_group': grocery_item.nova_group,  # 1-4 or None
                'has_nutrition_data': True if grocery_item.nutrition_data else False,  # Explicit True/False
                'nutrients': grocery_item.nutrition_data.get('nutrients') if grocery_item.nutrition_data else None
            } if (grocery_item.nutriscore_grade or grocery_item.nova_group) else None
        }

        # Return with anti-caching headers (prevent AWS ELB, iOS URLSession, etc. from caching)
        response = JsonResponse(response_data)
        response['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response['Pragma'] = 'no-cache'
        response['Expires'] = '0'
        return response

    # Barcode NOT in database - fetch from API
    product_data = lookup_barcode_in_openfoodfacts(barcode)

    if not product_data.get('found'):
        # OpenFoodFacts API failed/timed out - create PARTIAL grocery item anyway
        # This saves the barcode for the community and lets user add photo manually
        logger.warning(f"⚠️ OpenFoodFacts lookup failed for {barcode} - creating partial item")

        from django.utils import timezone
        from django.db.models import Q

        # Check if item with same name+store exists WITHOUT barcode
        existing_item = GroceryItem.objects.filter(
            name=list_item.name,
            store_name=store_name
        ).filter(
            Q(barcode__isnull=True) | Q(barcode='')
        ).first()

        if existing_item:
            # UPDATE existing item with barcode (but mark as needing enrichment)
            logger.info(f"🔗 Updating existing item with barcode (partial): {existing_item.name} @ {store_name}")
            existing_item.barcode = barcode
            existing_item.enriched_from_barcode = True
            existing_item.needs_enrichment = True  # Flag for retry later
            existing_item.first_enriched_by = request.user
            existing_item.first_enriched_at = timezone.now()
            existing_item.save()
            grocery_item = existing_item
        else:
            # CREATE new partial item (barcode + list item name only)
            grocery_item = GroceryItem.objects.create(
                name=list_item.name,
                brand=list_item.brand or '',
                size=list_item.size or '',
                store_name=store_name,
                category=list_item.category or '',
                barcode=barcode,
                image_url='',  # No image from API
                enriched_from_barcode=True,
                needs_enrichment=True,  # Flag for retry later
                first_enriched_by=request.user,
                first_enriched_at=timezone.now()
            )
            logger.info(f"🎉 NEW partial product (barcode only): {grocery_item.name} (barcode {barcode}) by {request.user.username}")

        # Link this list item to the grocery item
        list_item.grocery_item = grocery_item
        list_item.save()

        # Count families helped (same store only)
        potential_matches = ShoppingListItem.objects.filter(
            name__icontains=grocery_item.name[:20],
            grocery_item__isnull=True,
            shopping_list__store_name=store_name
        ).exclude(shopping_list__family=list_item.shopping_list.family)
        families_helped = potential_matches.values('shopping_list__family').distinct().count()

        # Return success response (but with has_image=False to trigger photo prompt)
        response_data = {
            'success': True,
            'message': f'Product saved! Image not available - you can add a photo. Helped {families_helped} families.' if families_helped > 0 else 'Product saved! Image not available - you can add a photo.',
            'product_data': {
                'product_name': list_item.name,  # Use list item name since API failed
                'brand': list_item.brand or '',
                'image_url': '',  # No image
                'quantity': list_item.size or '',
                'categories': list_item.category or '',
            },
            'grocery_item_id': grocery_item.id,
            'families_helped': families_helped,
            'already_existed': False,
            'has_image': False,  # CRITICAL: This triggers photo prompt in iOS
            'enriched_by': request.user.username,
            'nutrition': None  # No nutrition data from API
        }

        response = JsonResponse(response_data)
        response['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response['Pragma'] = 'no-cache'
        response['Expires'] = '0'
        return response

    # VALIDATION: Check if scanned product matches list item BEFORE creating database entry
    scanned_product_name = product_data.get('product_name', '')
    match_result = fuzzy_match_product_names(list_item.name, scanned_product_name)
    logger.info(f"🔍 New barcode fuzzy match: {match_result['confidence']}% - {match_result['reason']}")

    if not match_result['match'] or match_result['confidence'] < 70:
        # LOW CONFIDENCE - Return validation failure for user confirmation
        logger.warning(f"⚠️ Low confidence match ({match_result['confidence']}%) - requiring user confirmation")
        return JsonResponse({
            'success': False,
            'needs_confirmation': True,
            'match_result': match_result,
            'product_data': {
                'product_name': scanned_product_name,
                'brand': product_data.get('brand', ''),
                'image_url': product_data.get('image_url', ''),
                'quantity': product_data.get('quantity', ''),
                'categories': product_data.get('categories', ''),
            },
            'list_item_name': list_item.name,
            'message': 'Product name mismatch detected. Please confirm if this is correct.',
            # Send barcode back so iOS can call confirm endpoint
            'barcode': barcode,
            'list_item_id': list_item_id
        }, status=400)

    # HIGH CONFIDENCE - Proceed with creating/updating grocery item
    # Create NEW store-specific grocery item (first time this barcode is scanned at this store!)
    # Use list_item.name (receipt name) so receipts can find it via exact match
    from django.utils import timezone

    # Determine brand: prefer barcode API brand, fallback to list item brand
    api_brand = product_data.get('brand', '').strip()
    item_brand = list_item.brand or ''
    final_brand = api_brand if api_brand else item_brand

    # Check if item with same name+store exists WITHOUT barcode (from receipt scan)
    # This prevents duplicates when scanning barcode on items that were manually added
    from django.db.models import Q

    existing_item = GroceryItem.objects.filter(
        name=list_item.name,
        store_name=store_name
    ).filter(
        Q(barcode__isnull=True) | Q(barcode='')  # Match both NULL and empty string
    ).first()

    if existing_item:
        # UPDATE existing item with barcode data (prevents duplicates in autocomplete)
        logger.info(f"🔗 Updating existing item with barcode: {existing_item.name} @ {store_name} (barcode {barcode})")
        existing_item.barcode = barcode
        existing_item.brand = final_brand
        existing_item.size = product_data.get('quantity', list_item.size)
        existing_item.image_url = product_data.get('image_url', '')
        existing_item.category = list_item.category or existing_item.category
        existing_item.enriched_from_barcode = True
        existing_item.first_enriched_by = request.user
        existing_item.first_enriched_at = timezone.now()
        existing_item.save()
        grocery_item = existing_item
        created = False
    else:
        # CREATE new item (different product with different barcode, or first time scanning)
        grocery_item = GroceryItem.objects.create(
            name=list_item.name,  # Use receipt name, not barcode API name!
            brand=final_brand,
            size=product_data.get('quantity', list_item.size),
            store_name=store_name,  # Store-specific!
            category=list_item.category,
            barcode=barcode,
            image_url=product_data.get('image_url', ''),
            enriched_from_barcode=True,
            first_enriched_by=request.user,
            first_enriched_at=timezone.now()
        )
        created = True
        logger.info(f"🎉 NEW product for {store_name}: {grocery_item.name} (brand: {final_brand}, barcode {barcode}) by {request.user.username}")

    # Link this list item to the new grocery item
    list_item.grocery_item = grocery_item

    # Update list item with better data from API
    if product_data.get('brand'):
        list_item.brand = product_data['brand']
    list_item.save()

    # Fetch nutrition data from OpenFoodFacts
    openfoodfacts_service = get_openfoodfacts_service()
    nutrition_enriched = openfoodfacts_service.enrich_grocery_item(grocery_item)

    if nutrition_enriched:
        logger.info(f"✅ Fetched nutrition for {grocery_item.name}: Nutri-Score {grocery_item.nutriscore_grade.upper() if grocery_item.nutriscore_grade else 'N/A'}")

    # Count how many other families will benefit from this scan (same store only)
    potential_matches = ShoppingListItem.objects.filter(
        name__icontains=grocery_item.name[:20],
        grocery_item__isnull=True,
        shopping_list__store_name=store_name
    ).exclude(shopping_list__family=list_item.shopping_list.family)

    families_helped = potential_matches.values('shopping_list__family').distinct().count()

    logger.info(f"📊 Barcode scan will help {families_helped} other families at {store_name}")

    # Build response
    response_data = {
        'success': True,
        'message': f'Product added! You helped {families_helped} other families.' if families_helped > 0 else 'Product added to database!',
        'product_data': {
            'product_name': product_data.get('product_name', ''),
            'brand': product_data.get('brand', ''),
            'image_url': product_data.get('image_url', ''),
            'quantity': product_data.get('quantity', ''),
            'categories': product_data.get('categories', ''),
            'nutrition_grade': product_data.get('nutrition_grade', '')  # Legacy field
        },
        'grocery_item_id': grocery_item.id,
        'families_helped': families_helped,
        'already_existed': False,
        'has_image': bool(grocery_item.image_url and grocery_item.image_url.strip()),
        # NEW: Health/Nutrition data
        'nutrition': {
            'nutriscore_grade': grocery_item.nutriscore_grade,  # A-E or None
            'nova_group': grocery_item.nova_group,  # 1-4 or None
            'has_nutrition_data': True if grocery_item.nutrition_data else False,  # Explicit True/False
            'nutrients': grocery_item.nutrition_data.get('nutrients') if grocery_item.nutrition_data else None
        } if nutrition_enriched else None
    }

    # Return with anti-caching headers (prevent AWS ELB, iOS URLSession, etc. from caching)
    response = JsonResponse(response_data)
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response['Pragma'] = 'no-cache'
    response['Expires'] = '0'
    return response


@csrf_exempt
@require_firebase_auth
@require_nutrition_scan_quota
def confirm_barcode_api(request):
    """
    Confirm and save barcode after user manually approves mismatch
    This endpoint SKIPS fuzzy matching validation since user already confirmed "This is Correct"

    POST /shopright/api/confirm-barcode/
    Body: {
        "list_item_id": 789,
        "barcode": "012345678901"
    }

    Returns: Same as scan_barcode_api (success response)
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    list_item_id = data.get('list_item_id')
    barcode = data.get('barcode', '').strip()

    if not list_item_id or not barcode:
        return JsonResponse({'error': 'Missing list_item_id or barcode'}, status=400)

    # Get the list item
    try:
        list_item = ShoppingListItem.objects.get(id=list_item_id)
    except ShoppingListItem.DoesNotExist:
        return JsonResponse({'error': 'List item not found'}, status=404)

    store_name = list_item.shopping_list.store_name

    # Check if barcode already exists for THIS STORE
    grocery_item = GroceryItem.objects.filter(
        barcode=barcode,
        store_name=store_name
    ).first()

    if grocery_item:
        # Barcode already exists - link it (NO VALIDATION - user confirmed)
        logger.info(f"✅ [CONFIRMED] Linking barcode {barcode} to list item '{list_item.name}' (existing item: '{grocery_item.name}')")

        list_item.grocery_item = grocery_item
        list_item.save()

        # Count how many families could benefit
        potential_matches = ShoppingListItem.objects.filter(
            name__icontains=grocery_item.name[:20],
            grocery_item__isnull=True,
            shopping_list__store_name=store_name
        ).exclude(shopping_list__family=list_item.shopping_list.family)
        families_helped = potential_matches.values('shopping_list__family').distinct().count()

        # Build response
        response_data = {
            'success': True,
            'message': 'Product confirmed and linked!',
            'product_data': {
                'product_name': grocery_item.name,
                'brand': grocery_item.brand,
                'image_url': grocery_item.image_url,
                'quantity': grocery_item.size
            },
            'grocery_item_id': grocery_item.id,
            'families_helped': families_helped,
            'already_existed': True,
            'has_image': bool(grocery_item.image_url and grocery_item.image_url.strip()),
            'enriched_by': grocery_item.first_enriched_by.username if grocery_item.first_enriched_by else 'community',
            'nutrition': {
                'nutriscore_grade': grocery_item.nutriscore_grade,
                'nova_group': grocery_item.nova_group,
                'has_nutrition_data': True if grocery_item.nutrition_data else False,
                'nutrients': grocery_item.nutrition_data.get('nutrients') if grocery_item.nutrition_data else None
            } if (grocery_item.nutriscore_grade or grocery_item.nova_group) else None
        }

        response = JsonResponse(response_data)
        response['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response['Pragma'] = 'no-cache'
        response['Expires'] = '0'
        return response

    # Barcode NOT in database - fetch from API and create (NO VALIDATION - user confirmed)
    product_data = lookup_barcode_in_openfoodfacts(barcode)

    if not product_data.get('found'):
        # OpenFoodFacts API failed - but user CONFIRMED it's correct, so create partial item
        logger.warning(f"⚠️ [CONFIRMED] OpenFoodFacts lookup failed for {barcode} - creating partial item anyway")

        from django.utils import timezone
        from django.db.models import Q

        # Check if item with same name+store exists WITHOUT barcode
        existing_item = GroceryItem.objects.filter(
            name=list_item.name,
            store_name=store_name
        ).filter(
            Q(barcode__isnull=True) | Q(barcode='')
        ).first()

        if existing_item:
            # UPDATE existing item with barcode
            logger.info(f"🔗 [CONFIRMED] Updating existing item with barcode (partial): {existing_item.name} @ {store_name}")
            existing_item.barcode = barcode
            existing_item.enriched_from_barcode = True
            existing_item.needs_enrichment = True
            existing_item.first_enriched_by = request.user
            existing_item.first_enriched_at = timezone.now()
            existing_item.save()
            grocery_item = existing_item
        else:
            # CREATE new partial item
            grocery_item = GroceryItem.objects.create(
                name=list_item.name,
                brand=list_item.brand or '',
                size=list_item.size or '',
                store_name=store_name,
                category=list_item.category or '',
                barcode=barcode,
                image_url='',
                enriched_from_barcode=True,
                needs_enrichment=True,
                first_enriched_by=request.user,
                first_enriched_at=timezone.now()
            )
            logger.info(f"🎉 [CONFIRMED] NEW partial product: {grocery_item.name} (barcode {barcode}) by {request.user.username}")

        # Link this list item to the grocery item
        list_item.grocery_item = grocery_item
        list_item.save()

        # Count families helped
        potential_matches = ShoppingListItem.objects.filter(
            name__icontains=grocery_item.name[:20],
            grocery_item__isnull=True,
            shopping_list__store_name=store_name
        ).exclude(shopping_list__family=list_item.shopping_list.family)
        families_helped = potential_matches.values('shopping_list__family').distinct().count()

        # Return success response (with has_image=False to trigger photo prompt)
        response_data = {
            'success': True,
            'message': f'Product confirmed! Image not available - you can add a photo. Helped {families_helped} families.' if families_helped > 0 else 'Product confirmed! Image not available - you can add a photo.',
            'product_data': {
                'product_name': list_item.name,
                'brand': list_item.brand or '',
                'image_url': '',
                'quantity': list_item.size or '',
                'categories': list_item.category or '',
            },
            'grocery_item_id': grocery_item.id,
            'families_helped': families_helped,
            'already_existed': False,
            'has_image': False,  # Triggers photo prompt
            'nutrition': None
        }

        response = JsonResponse(response_data)
        response['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response['Pragma'] = 'no-cache'
        response['Expires'] = '0'
        return response

    logger.info(f"✅ [CONFIRMED] Creating new barcode item '{list_item.name}' (scanned: '{product_data.get('product_name')}')")

    # Create NEW store-specific grocery item (user confirmed it's correct)
    from django.utils import timezone

    # Determine brand
    api_brand = product_data.get('brand', '').strip()
    item_brand = list_item.brand or ''
    final_brand = api_brand if api_brand else item_brand

    # Check if item with same name+store exists WITHOUT barcode
    from django.db.models import Q
    existing_item = GroceryItem.objects.filter(
        name=list_item.name,
        store_name=store_name
    ).filter(
        Q(barcode__isnull=True) | Q(barcode='')
    ).first()

    if existing_item:
        # UPDATE existing item with barcode data
        logger.info(f"🔗 [CONFIRMED] Updating existing item with barcode: {existing_item.name} @ {store_name}")
        existing_item.barcode = barcode
        existing_item.brand = final_brand
        existing_item.size = product_data.get('quantity', list_item.size)
        existing_item.image_url = product_data.get('image_url', '')
        existing_item.category = list_item.category or existing_item.category
        existing_item.enriched_from_barcode = True
        existing_item.first_enriched_by = request.user
        existing_item.first_enriched_at = timezone.now()
        existing_item.save()
        grocery_item = existing_item
    else:
        # CREATE new item
        grocery_item = GroceryItem.objects.create(
            name=list_item.name,
            brand=final_brand,
            size=product_data.get('quantity', list_item.size),
            store_name=store_name,
            category=list_item.category,
            barcode=barcode,
            image_url=product_data.get('image_url', ''),
            enriched_from_barcode=True,
            first_enriched_by=request.user,
            first_enriched_at=timezone.now()
        )
        logger.info(f"🎉 [CONFIRMED] NEW product: {grocery_item.name} (barcode {barcode}) by {request.user.username}")

    # Link this list item to the grocery item
    list_item.grocery_item = grocery_item
    if product_data.get('brand'):
        list_item.brand = product_data['brand']
    list_item.save()

    # Fetch nutrition data
    openfoodfacts_service = get_openfoodfacts_service()
    nutrition_enriched = openfoodfacts_service.enrich_grocery_item(grocery_item)

    if nutrition_enriched:
        logger.info(f"✅ Fetched nutrition for {grocery_item.name}: Nutri-Score {grocery_item.nutriscore_grade.upper() if grocery_item.nutriscore_grade else 'N/A'}")

    # Count families helped
    potential_matches = ShoppingListItem.objects.filter(
        name__icontains=grocery_item.name[:20],
        grocery_item__isnull=True,
        shopping_list__store_name=store_name
    ).exclude(shopping_list__family=list_item.shopping_list.family)
    families_helped = potential_matches.values('shopping_list__family').distinct().count()

    # Build response
    response_data = {
        'success': True,
        'message': f'Product confirmed! You helped {families_helped} other families.' if families_helped > 0 else 'Product confirmed and added to database!',
        'product_data': {
            'product_name': product_data.get('product_name', ''),
            'brand': product_data.get('brand', ''),
            'image_url': product_data.get('image_url', ''),
            'quantity': product_data.get('quantity', ''),
            'categories': product_data.get('categories', ''),
        },
        'grocery_item_id': grocery_item.id,
        'families_helped': families_helped,
        'already_existed': False,
        'has_image': bool(grocery_item.image_url and grocery_item.image_url.strip()),
        'nutrition': {
            'nutriscore_grade': grocery_item.nutriscore_grade,
            'nova_group': grocery_item.nova_group,
            'has_nutrition_data': True if grocery_item.nutrition_data else False,
            'nutrients': grocery_item.nutrition_data.get('nutrients') if grocery_item.nutrition_data else None
        } if nutrition_enriched else None
    }

    response = JsonResponse(response_data)
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response['Pragma'] = 'no-cache'
    response['Expires'] = '0'
    return response


@csrf_exempt
@require_firebase_auth
@require_nutrition_scan_quota
def lookup_barcode_api(request):
    """
    Standalone barcode lookup for nutrition info (no list_item_id required)
    Used by NutritionBarcodeScannerView for exploratory scanning

    POST /shopright/api/lookup-barcode/
    Body: {
        "barcode": "012345678901"
    }

    Returns: {
        "success": true,
        "product_name": "Organic Whole Milk",
        "brand": "Horizon",
        "image_url": "https://...",
        "nutrition": {
            "nutriscore_grade": "a",
            "nova_group": 1,
            "has_nutrition_data": true,
            "nutrients": {...}
        },
        "grocery_item_id": 456  // If exists in DB for any store
    }
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    barcode = data.get('barcode', '').strip()

    if not barcode:
        return JsonResponse({'error': 'Missing barcode'}, status=400)

    logger.info(f"🔍 Standalone barcode lookup: {barcode} by {request.user.username}")

    # Check if barcode exists in our database (any store)
    grocery_item = GroceryItem.objects.filter(barcode=barcode).first()

    if grocery_item and (grocery_item.nutriscore_grade or grocery_item.nova_group or grocery_item.nutrition_data):
        # We have nutrition data cached!
        logger.info(f"✅ Found cached nutrition for {barcode}: {grocery_item.name}")

        response_data = {
            'success': True,
            'product_name': grocery_item.name,
            'brand': grocery_item.brand or '',
            'image_url': grocery_item.image_url or '',
            'nutrition': {
                'nutriscore_grade': grocery_item.nutriscore_grade,
                'nova_group': grocery_item.nova_group,
                'has_nutrition_data': True if grocery_item.nutrition_data else False,
                'nutrients': grocery_item.nutrition_data.get('nutrients') if grocery_item.nutrition_data else None
            },
            'grocery_item_id': grocery_item.id,
            'from_cache': True
        }

        response = JsonResponse(response_data)
        response['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response['Pragma'] = 'no-cache'
        response['Expires'] = '0'
        return response

    # Not in cache or no nutrition data - fetch from OpenFoodFacts
    openfoodfacts_service = get_openfoodfacts_service()
    nutrition_data = openfoodfacts_service.fetch_product_by_barcode(barcode)

    if not nutrition_data:
        return JsonResponse({
            'success': False,
            'error': 'Product not found in nutrition database'
        }, status=404)

    logger.info(f"✅ Fetched nutrition from OpenFoodFacts for {barcode}: {nutrition_data.get('product_name', 'Unknown')}")

    # Build response
    response_data = {
        'success': True,
        'product_name': nutrition_data.get('product_name', 'Unknown Product'),
        'brand': nutrition_data.get('brands', ''),
        'image_url': nutrition_data.get('image_url', ''),
        'nutrition': {
            'nutriscore_grade': nutrition_data.get('nutriscore_grade'),
            'nova_group': nutrition_data.get('nova_group'),
            'has_nutrition_data': bool(nutrition_data.get('nutrients')),
            'nutrients': nutrition_data.get('nutrients')
        },
        'grocery_item_id': grocery_item.id if grocery_item else None,
        'from_cache': False
    }

    # If we found a grocery_item but it lacked nutrition, update it now
    if grocery_item:
        if nutrition_data.get('nutriscore_grade'):
            grocery_item.nutriscore_grade = nutrition_data['nutriscore_grade']
        if nutrition_data.get('nova_group'):
            grocery_item.nova_group = nutrition_data['nova_group']
        if nutrition_data.get('nutrients'):
            grocery_item.nutrition_data = nutrition_data
        from django.utils import timezone
        grocery_item.last_nutrition_fetch = timezone.now()
        grocery_item.save()
        logger.info(f"💾 Updated cached grocery item {grocery_item.id} with fresh nutrition data")

    response = JsonResponse(response_data)
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response['Pragma'] = 'no-cache'
    response['Expires'] = '0'
    return response


# ========================================
# SUBSCRIPTION & PAYMENT ENDPOINTS
# ========================================

@csrf_exempt
@require_firebase_auth
def verify_subscription_api(request):
    """
    Verify Apple In-App Purchase receipt and update subscription status

    POST /shopright/api/verify-subscription/
    Body: {
        "receipt_data": "base64_encoded_receipt_string",
        "transaction_id": "1000000123456789"
    }

    Returns: {
        "success": true,
        "subscription": {
            "is_premium": true,
            "subscription_type": "monthly",
            "scans_remaining": 999,
            "premium_expires_at": "2024-12-25T10:00:00Z"
        }
    }
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    receipt_data = data.get('receipt_data', '').strip()
    transaction_id = data.get('transaction_id', '').strip()

    if not receipt_data or not transaction_id:
        return JsonResponse({'error': 'Missing receipt_data or transaction_id'}, status=400)

    user = request.user
    logger.info(f"💳 Verifying subscription for user {user.username}")

    # PRODUCTION - Verify receipt with Apple's verification servers

    is_valid, receipt_info = verify_subscription_receipt(
        receipt_data=receipt_data,
        bundle_id=settings.SHOPRIGHT_BUNDLE_ID,
        shared_secret=settings.APPLE_SHARED_SECRET
    )

    if not is_valid:
        logger.error(f"❌ Receipt verification failed for user {user.username}")
        error_message = receipt_info.get('error', 'Invalid receipt')
        return JsonResponse({
            'error': f'Receipt verification failed: {error_message}'
        }, status=400)

    # Receipt is valid - extract subscription info from Apple's response
    product_id = receipt_info.get('product_id', '')
    transaction_id_from_receipt = receipt_info.get('transaction_id', transaction_id)
    subscription_type = receipt_info.get('subscription_type', 'monthly')
    expires_date = receipt_info.get('expires_date')
    is_active = receipt_info.get('is_active', False)

    if not is_active:
        logger.warning(f"⚠️ Subscription expired for user {user.username}")
        return JsonResponse({
            'error': 'Subscription has expired'
        }, status=400)

    logger.info(f"✅ Receipt verified by Apple - Product: {product_id}, Active: {is_active}")

    # Get or create subscription
    from shopright.services.subscription_service import SubscriptionService
    subscription = SubscriptionService.get_or_create_subscription(user)

    # Update subscription with verified data from Apple
    subscription.is_premium = True
    subscription.subscription_type = subscription_type
    subscription.apple_receipt_data = receipt_data
    subscription.apple_transaction_id = transaction_id_from_receipt

    # Set expiration from Apple's response
    from django.utils import timezone

    if expires_date:
        subscription.premium_expires_at = expires_date
    else:
        # Fallback to calculated expiration if Apple doesn't provide it
        from datetime import timedelta
        if subscription_type == 'lifetime':
            subscription.premium_expires_at = None  # Never expires
        elif subscription_type == 'annual':
            subscription.premium_expires_at = timezone.now() + timedelta(days=365)
        else:  # monthly
            subscription.premium_expires_at = timezone.now() + timedelta(days=30)

    subscription.save()

    logger.info(f"✅ Subscription updated for user {user.username}: {subscription_type}, expires {subscription.premium_expires_at}")

    # Return updated subscription status
    status = SubscriptionService.get_subscription_status(user)

    return JsonResponse({
        'success': True,
        'subscription': status
    })



@csrf_exempt
@require_firebase_auth
def get_subscription_status_api(request):
    """
    Get current subscription status for user

    GET /shopright/api/subscription-status/

    Returns: {
        "subscription": {
            "is_premium": false,
            "subscription_type": "free",
            "scans_remaining": 5,
            "scans_used_today": 0,
            "premium_expires_at": null
        }
    }
    """
    if request.method != 'GET':
        return JsonResponse({'error': 'Only GET allowed'}, status=405)

    user = request.user
    from shopright.services.subscription_service import SubscriptionService

    status = SubscriptionService.get_subscription_status(user)

    return JsonResponse({
        'subscription': status
    })


@csrf_exempt
@require_firebase_auth
def upload_product_photo_api(request):
    """
    Upload photo for a product that has no image

    POST /shopright/api/upload-product-photo/
    Body (multipart/form-data): {
        "list_item_id": 789,
        "photo": <file>
    }

    Returns: {
        "success": true,
        "image_url": "/media/products/...",
        "verification": {
            "match": true,
            "confidence": 100,
            "reason": "Matching words: milk"
        },
        "families_helped": 5
    }
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    list_item_id = request.POST.get('list_item_id')
    photo = request.FILES.get('photo')

    if not list_item_id or not photo:
        return JsonResponse({'error': 'Missing list_item_id or photo'}, status=400)

    # Get the list item
    try:
        list_item = ShoppingListItem.objects.get(id=list_item_id)
    except ShoppingListItem.DoesNotExist:
        return JsonResponse({'error': 'List item not found'}, status=404)

    store_name = list_item.shopping_list.store_name

    # Get the linked grocery item (or create for this store if not linked yet)
    grocery_item = list_item.grocery_item

    if not grocery_item:
        # List item not linked yet - try to find or create for THIS STORE
        logger.info(f"📸 List item '{list_item.name}' not linked, searching for grocery_item at {store_name}")

        # Normalize size for matching
        normalized_size = normalize_weighted_item_size(list_item.size or '')

        # Try exact match for this store first (with normalized size)
        grocery_item = GroceryItem.objects.filter(
            name__iexact=list_item.name,
            size__iexact=normalized_size,
            store_name=store_name
        ).first()

        if not grocery_item:
            # Not found - create new store-specific grocery item (with normalized size)
            logger.info(f"📸 Creating new grocery_item for '{list_item.name}' at {store_name} (size: {normalized_size})")
            grocery_item = GroceryItem.objects.create(
                name=list_item.name,
                brand=list_item.brand or '',
                size=normalized_size,  # Store normalized size
                category=list_item.category or '',
                store_name=store_name
            )

        # Link the list_item to this grocery_item
        list_item.grocery_item = grocery_item
        list_item.save()

    # Save photo to media folder
    import os
    from django.core.files.storage import default_storage
    from django.core.files.base import ContentFile

    # Generate unique filename
    ext = os.path.splitext(photo.name)[1]
    filename = f'products/{grocery_item.id}_{datetime.now().strftime("%Y%m%d_%H%M%S")}{ext}'

    # Save file
    path = default_storage.save(filename, ContentFile(photo.read()))

    # Update grocery item with image URL
    grocery_item.image_url = f'/media/{path}'
    grocery_item.save()

    logger.info(f"📸 Photo uploaded for {grocery_item.name} by {request.user.username}")

    # Count families helped
    potential_matches = ShoppingListItem.objects.filter(
        name__icontains=grocery_item.name[:20],
        grocery_item__isnull=True,
        shopping_list__store_name=store_name
    ).exclude(shopping_list__family=list_item.shopping_list.family)

    families_helped = potential_matches.values('shopping_list__family').distinct().count()

    # Verify product name matches (simple text matching)
    verification = fuzzy_match_product_names(list_item.name, grocery_item.name)

    logger.info(f"🔍 Product verification for upload:")
    logger.info(f"   List item name: '{list_item.name}'")
    logger.info(f"   Grocery item name: '{grocery_item.name}'")
    logger.info(f"   Match result: {verification['match']} ({verification['confidence']}%) - {verification['reason']}")

    return JsonResponse({
        'success': True,
        'image_url': grocery_item.image_url,
        'verification': verification,
        'families_helped': families_helped,
        'message': f'Photo uploaded! You helped {families_helped} other families.' if families_helped > 0 else 'Photo uploaded!'
    })


@csrf_exempt
@require_firebase_auth
def report_wrong_image_api(request):
    """
    Report a wrong/incorrect product image

    POST /shopright/api/report-wrong-image/
    Body: {
        "grocery_item_id": 123
    }

    After 3 reports, image gets auto-flagged and hidden

    Returns: {
        "success": true,
        "report_count": 2,
        "flagged": false
    }
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    grocery_item_id = data.get('grocery_item_id')

    if not grocery_item_id:
        return JsonResponse({'error': 'Missing grocery_item_id'}, status=400)

    try:
        grocery_item = GroceryItem.objects.get(id=grocery_item_id)
    except GroceryItem.DoesNotExist:
        return JsonResponse({'error': 'Grocery item not found'}, status=404)

    # Increment report counter
    grocery_item.image_report_count += 1

    # Auto-flag after 3 reports
    REPORT_THRESHOLD = 3
    if grocery_item.image_report_count >= REPORT_THRESHOLD and not grocery_item.image_flagged:
        grocery_item.image_flagged = True
        grocery_item.image_url = ''  # Hide the image
        logger.warning(f"🚩 Image flagged for {grocery_item.name} after {grocery_item.image_report_count} reports")

    grocery_item.save()

    logger.info(f"⚠️ User {request.user.username} reported wrong image for {grocery_item.name} (report #{grocery_item.image_report_count})")

    return JsonResponse({
        'success': True,
        'report_count': grocery_item.image_report_count,
        'flagged': grocery_item.image_flagged,
        'message': 'Thank you for reporting. We\'ll review this image.' if not grocery_item.image_flagged else 'Image has been hidden due to multiple reports.'
    })


@require_firebase_auth
def flagged_images_api(request):
    """
    Get all grocery items with reported/flagged images (admin only)

    GET /shopright/api/flagged-images/?status=all

    Query params:
    - status: 'flagged' (3+ reports), 'reported' (1-2 reports), 'all' (default)

    Returns: {
        "items": [
            {
                "id": 123,
                "name": "Milk Gallon Whole",
                "brand": "Horizon",
                "image_url": "/media/products/...",
                "report_count": 2,
                "flagged": false,
                "first_enriched_by": "username"
            },
            ...
        ],
        "total_flagged": 5,
        "total_reported": 12
    }
    """
    # Optional: Add admin check here if you want to restrict access
    # if not request.user.is_staff:
    #     return JsonResponse({'error': 'Admin access required'}, status=403)

    status_filter = request.GET.get('status', 'all')

    # Get items based on filter
    if status_filter == 'flagged':
        items = GroceryItem.objects.filter(image_flagged=True)
    elif status_filter == 'reported':
        items = GroceryItem.objects.filter(image_report_count__gte=1, image_flagged=False)
    else:  # 'all'
        items = GroceryItem.objects.filter(image_report_count__gte=1)

    items = items.order_by('-image_report_count', '-updated_at')

    items_data = [
        {
            'id': item.id,
            'name': item.name,
            'brand': item.brand,
            'size': item.size,
            'category': item.category,
            'image_url': item.image_url,
            'report_count': item.image_report_count,
            'flagged': item.image_flagged,
            'first_enriched_by': item.first_enriched_by.username if item.first_enriched_by else None,
            'first_enriched_at': item.first_enriched_at.isoformat() if item.first_enriched_at else None,
            'updated_at': item.updated_at.isoformat()
        }
        for item in items
    ]

    # Summary stats
    total_flagged = GroceryItem.objects.filter(image_flagged=True).count()
    total_reported = GroceryItem.objects.filter(image_report_count__gte=1, image_flagged=False).count()

    return JsonResponse({
        'items': items_data,
        'total_flagged': total_flagged,
        'total_reported': total_reported,
        'status_filter': status_filter
    })


# ========================================
# AISLE LOCATION API
# ========================================

@csrf_exempt
@require_firebase_auth
def add_location_api(request):
    """
    Add location for a grocery item at a specific store location

    POST /shopright/api/location/add/
    Body: {
        "grocery_item_id": 123,
        "store_location": "123 Main St, SF",  # specific physical store address
        "location_type": "aisle",  # or "relative", "category"
        "aisle_number": "10",  # for aisle type
        "bay_number": "3",  # optional, for aisle type
        "location_description": "Behind fruit section"  # for relative/category types
    }

    Returns: {
        "success": true,
        "location_id": 456,
        "location": "Aisle 10 Bay 3",
        "families_helped": 5
    }
    """
    if request.method != 'POST':
        return JsonResponse({
            'success': False,
            'error': 'Only POST allowed',
            'message': 'Only POST allowed'
        }, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({
            'success': False,
            'error': 'Invalid JSON',
            'message': 'Invalid JSON'
        }, status=400)

    grocery_item_id = data.get('grocery_item_id')
    location_type = data.get('location_type', 'aisle')
    store_location = data.get('store_location', '').strip()

    # Normalize store location for consistent matching
    store_location = normalize_store_location(store_location)

    if not grocery_item_id:
        return JsonResponse({
            'success': False,
            'error': 'Missing grocery_item_id',
            'message': 'Missing grocery_item_id'
        }, status=400)

    if not store_location:
        return JsonResponse({
            'success': False,
            'error': 'Missing store_location',
            'message': 'Missing store_location - need specific store address'
        }, status=400)

    try:
        grocery_item = GroceryItem.objects.get(id=grocery_item_id)
    except GroceryItem.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Grocery item not found',
            'message': 'Grocery item not found'
        }, status=404)

    store_name = grocery_item.store_name

    # Validate input based on location type
    if location_type == 'aisle':
        aisle_number = data.get('aisle_number', '').strip()
        if not aisle_number:
            return JsonResponse({
                'success': False,
                'error': 'Missing aisle_number for aisle-type location',
                'message': 'Missing aisle_number for aisle-type location'
            }, status=400)
        bay_number = data.get('bay_number', '').strip()
    else:  # relative or category
        location_description = data.get('location_description', '').strip()
        if not location_description:
            return JsonResponse({
                'success': False,
                'error': 'Missing location_description for relative/category-type location',
                'message': 'Missing location_description for relative/category-type location'
            }, status=400)
        aisle_number = ''
        bay_number = ''

    # Check if this EXACT location already exists for this item at this store LOCATION
    # (Allow multiple different locations, but prevent exact duplicates)
    if location_type == 'aisle':
        existing_location = AisleLocation.objects.filter(
            grocery_item=grocery_item,
            store_name=store_name,
            store_location=store_location,
            location_type='aisle',
            aisle_number=aisle_number,
            bay_number=bay_number
        ).first()
    else:
        existing_location = AisleLocation.objects.filter(
            grocery_item=grocery_item,
            store_name=store_name,
            store_location=store_location,
            location_type=location_type,
            location_description=data.get('location_description', '')
        ).first()

    if existing_location:
        # This exact location already exists - just upvote it instead
        existing_location.change_vote(request.user, 'up')
        logger.info(f"📍 User {request.user.username} upvoted existing location for {grocery_item.name}")

        return JsonResponse({
            'success': True,
            'location_id': existing_location.id,
            'location': existing_location.get_display_location(),
            'message': 'Location already exists - upvoted!',
            'confidence_score': existing_location.confidence_score
        })

    # Create new location
    location = AisleLocation.objects.create(
        grocery_item=grocery_item,
        store_name=store_name,
        store_location=store_location,
        location_type=location_type,
        aisle_number=aisle_number if location_type == 'aisle' else '',
        bay_number=bay_number if location_type == 'aisle' else '',
        location_description=data.get('location_description', '') if location_type != 'aisle' else '',
        added_by=request.user,
        upvotes=1  # Creator automatically upvotes
    )

    # Creator automatically upvotes their own location
    LocationVote.objects.create(location=location, user=request.user, vote_type='up')

    logger.info(f"📍 NEW location added: {grocery_item.name} → {location.get_display_location()} at {store_name} - {store_location} by {request.user.username}")

    # Count how many families will benefit (same item at same store LOCATION)
    potential_matches = ShoppingListItem.objects.filter(
        grocery_item=grocery_item,
        shopping_list__store_name=store_name,
        shopping_list__store_location=store_location
    ).exclude(shopping_list__family__isnull=True)

    families_helped = potential_matches.values('shopping_list__family').distinct().count()

    return JsonResponse({
        'success': True,
        'location_id': location.id,
        'location': location.get_display_location(),
        'families_helped': families_helped,
        'message': f'Location added! You helped {families_helped} families.' if families_helped > 0 else 'Location added!'
    })


@csrf_exempt
@require_firebase_auth
def update_location_api(request, location_id):
    """
    Update an existing location (edit wrong information)

    PUT /shopright/api/location/update/<location_id>/
    Body: {
        "location_type": "aisle",
        "aisle_number": "12",
        "bay_number": "5",
        "location_description": ""
    }

    Returns: {
        "success": true,
        "location": "Aisle 12 Bay 5"
    }
    """
    if request.method != 'PUT':
        return JsonResponse({
            'success': False,
            'error': 'Only PUT allowed',
            'message': 'Only PUT allowed'
        }, status=405)

    try:
        location = AisleLocation.objects.get(id=location_id)
    except AisleLocation.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Location not found',
            'message': 'Location not found'
        }, status=404)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({
            'success': False,
            'error': 'Invalid JSON',
            'message': 'Invalid JSON'
        }, status=400)

    # Update fields if provided
    if 'location_type' in data:
        location.location_type = data['location_type']

    if location.location_type == 'aisle':
        if 'aisle_number' in data:
            location.aisle_number = data['aisle_number']
        if 'bay_number' in data:
            location.bay_number = data['bay_number']
        location.location_description = ''
    else:
        if 'location_description' in data:
            location.location_description = data['location_description']
        location.aisle_number = ''
        location.bay_number = ''

    location.save()

    # User who updates the location automatically upvotes it
    location.change_vote(request.user, 'up')

    logger.info(f"📍 Location updated: {location.grocery_item.name} → {location.get_display_location()} by {request.user.username}")

    return JsonResponse({
        'success': True,
        'location': location.get_display_location(),
        'message': 'Location updated successfully'
    })


@csrf_exempt
@require_firebase_auth
def vote_location_api(request):
    """
    Vote on a location (upvote/downvote) or change existing vote

    POST /shopright/api/location/vote/
    Body: {
        "location_id": 456,
        "vote_type": "up"  # "up", "down", or null to remove vote
    }

    Returns: {
        "success": true,
        "upvotes": 10,
        "downvotes": 2,
        "confidence_score": 83,
        "net_score": 8,
        "user_vote": "up",  # or "down" or null
        "message": "Vote changed to upvote"
    }
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    location_id = data.get('location_id')
    vote_type = data.get('vote_type')  # Can be 'up', 'down', or None

    if not location_id:
        return JsonResponse({'error': 'Missing location_id'}, status=400)

    if vote_type not in ['up', 'down', None]:
        return JsonResponse({'error': 'Invalid vote_type (must be "up", "down", or null)'}, status=400)

    try:
        location = AisleLocation.objects.get(id=location_id)
    except AisleLocation.DoesNotExist:
        return JsonResponse({'error': 'Location not found'}, status=404)

    # Get user's current vote
    current_vote = location.get_user_vote(request.user)

    # Change the vote
    location.change_vote(request.user, vote_type)

    # Determine action message
    if current_vote is None and vote_type == 'up':
        action = "Upvoted successfully"
    elif current_vote is None and vote_type == 'down':
        action = "Downvoted successfully"
    elif current_vote == 'up' and vote_type == 'down':
        action = "Vote changed to downvote"
    elif current_vote == 'down' and vote_type == 'up':
        action = "Vote changed to upvote"
    elif vote_type is None:
        action = "Vote removed"
    elif current_vote == vote_type:
        action = f"Already {vote_type}voted"
    else:
        action = "Vote updated"

    logger.info(f"👍 User {request.user.username}: {current_vote} → {vote_type} for {location.grocery_item.name}")

    return JsonResponse({
        'success': True,
        'upvotes': location.upvotes,
        'downvotes': location.downvotes,
        'confidence_score': location.confidence_score,
        'net_score': location.net_score,
        'user_vote': vote_type,
        'message': action
    })


@require_firebase_auth
def get_location_api(request, grocery_item_id):
    """
    Get best location for a grocery item at a specific store location

    GET /shopright/api/location/<grocery_item_id>/?store_name=Trader+Joe's&store_location=123+Main+St

    Returns: {
        "location": {
            "id": 456,
            "location": "Aisle 10 Bay 3",
            "location_type": "aisle",
            "upvotes": 10,
            "downvotes": 2,
            "confidence_score": 83,
            "last_verified": "2025-11-05T10:30:00"
        }
    }
    or {"location": null} if no location found
    """
    store_name = request.GET.get('store_name')
    store_location = request.GET.get('store_location', '')

    # Normalize store location for consistent matching
    store_location = normalize_store_location(store_location)

    if not store_name:
        return JsonResponse({'error': 'Missing store_name query parameter'}, status=400)

    try:
        grocery_item = GroceryItem.objects.get(id=grocery_item_id)
    except GroceryItem.DoesNotExist:
        return JsonResponse({'error': 'Grocery item not found'}, status=404)

    # Get best location (highest net score) for this specific store location
    location = AisleLocation.objects.filter(
        grocery_item=grocery_item,
        store_name=store_name,
        store_location=store_location,
        is_flagged=False  # Don't show flagged locations
    ).order_by('-upvotes', '-last_verified').first()

    if not location:
        return JsonResponse({'location': None})

    # Get user's current vote on this location
    user_vote = location.get_user_vote(request.user)

    return JsonResponse({
        'location': {
            'id': location.id,
            'location': location.get_display_location(),
            'location_type': location.location_type,
            'aisle_number': location.aisle_number,
            'bay_number': location.bay_number,
            'location_description': location.location_description,
            'upvotes': location.upvotes,
            'downvotes': location.downvotes,
            'confidence_score': location.confidence_score,
            'net_score': location.net_score,
            'last_verified': location.last_verified.isoformat(),
            'has_voted': user_vote is not None,
            'user_vote': user_vote  # 'up', 'down', or null
        }
    })


@require_firebase_auth
def get_all_locations_api(request, grocery_item_id):
    """
    Get ALL locations for a grocery item at a specific store location (not just the best one)

    GET /shopright/api/location/<grocery_item_id>/all/?store_name=Trader+Joe's&store_location=123+Main+St

    Returns: {
        "locations": [
            {
                "id": 456,
                "location": "Dairy Section - Back wall",
                "upvotes": 5,
                "downvotes": 1,
                ...
            }
        ]
    }
    """
    store_name = request.GET.get('store_name')
    store_location = request.GET.get('store_location', '')

    # Normalize store location for consistent matching
    store_location = normalize_store_location(store_location)

    if not store_name:
        return JsonResponse({'error': 'Missing store_name query parameter'}, status=400)

    try:
        grocery_item = GroceryItem.objects.get(id=grocery_item_id)
    except GroceryItem.DoesNotExist:
        return JsonResponse({'error': 'Grocery item not found'}, status=404)

    # Get ALL locations for this item at this specific store location (not sorted yet)
    locations = AisleLocation.objects.filter(
        grocery_item=grocery_item,
        store_name=store_name,
        store_location=store_location,
        is_flagged=False  # Don't show flagged locations
    )

    # Serialize all locations with net_score calculated
    location_list = []
    for location in locations:
        user_vote = location.get_user_vote(request.user)
        location_list.append({
            'id': location.id,
            'location': location.get_display_location(),
            'location_type': location.location_type,
            'aisle_number': location.aisle_number,
            'bay_number': location.bay_number,
            'location_description': location.location_description,
            'upvotes': location.upvotes,
            'downvotes': location.downvotes,
            'confidence_score': location.confidence_score,
            'net_score': location.net_score,
            'last_verified': location.last_verified.isoformat(),
            'has_voted': user_vote is not None,
            'user_vote': user_vote
        })

    # Sort by net_score (descending), then upvotes (descending)
    location_list.sort(key=lambda x: (x['net_score'], x['upvotes']), reverse=True)

    return JsonResponse({
        'locations': location_list,
        'count': len(location_list)
    })


@csrf_exempt
@require_firebase_auth
def report_wrong_location_api(request):
    """
    Report a wrong/incorrect location

    POST /shopright/api/location/report/
    Body: {
        "location_id": 456
    }

    After 3 reports, location gets auto-flagged and hidden

    Returns: {
        "success": true,
        "flag_count": 2,
        "flagged": false
    }
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    location_id = data.get('location_id')

    if not location_id:
        return JsonResponse({'error': 'Missing location_id'}, status=400)

    try:
        location = AisleLocation.objects.get(id=location_id)
    except AisleLocation.DoesNotExist:
        return JsonResponse({'error': 'Location not found'}, status=404)

    # Increment flag counter
    location.flag_count += 1

    # Auto-flag after 3 reports
    FLAG_THRESHOLD = 3
    if location.flag_count >= FLAG_THRESHOLD and not location.is_flagged:
        location.is_flagged = True
        logger.warning(f"🚩 Location flagged: {location.grocery_item.name} → {location.get_display_location()} after {location.flag_count} reports")

    location.save()

    logger.info(f"⚠️ User {request.user.username} reported wrong location for {location.grocery_item.name} (report #{location.flag_count})")

    return JsonResponse({
        'success': True,
        'flag_count': location.flag_count,
        'flagged': location.is_flagged,
        'message': 'Thank you for reporting. We\'ll review this location.' if not location.is_flagged else 'Location has been hidden due to multiple reports.'
    })


# ========================================
# SEARCH GROCERY ITEMS API
# ========================================

@require_firebase_auth
def search_grocery_items_api(request):
    """
    Search GroceryItem database for autocomplete when adding new items
    Returns products from past receipts across ALL locations of the specified store chain

    GET /shopright/api/search-items/?store_name=Trader+Joe's&query=milk&limit=10

    Query params:
    - store_name: Store chain name (e.g., "Trader Joe's")
    - query: Search text (minimum 2 characters)
    - limit: Max results to return (default: 10, max: 20)

    Returns: {
        "items": [
            {
                "id": 123,
                "name": "Organic Whole Milk",
                "brand": "Horizon",
                "size": "64oz",
                "category": "Dairy",
                "price": null,
                "image_url": "/media/products/...",
                "times_purchased": 15
            },
            ...
        ],
        "count": 5,
        "query": "milk",
        "store_name": "Trader Joe's"
    }
    """
    store_name = request.GET.get('store_name', '').strip()
    query = request.GET.get('query', '').strip()
    limit = min(int(request.GET.get('limit', 10)), 20)  # Cap at 20 results

    # Validate input
    if not store_name:
        return JsonResponse({
            'items': [],
            'count': 0,
            'error': 'Missing store_name parameter'
        }, status=400)

    if not query or len(query) < 2:
        return JsonResponse({
            'items': [],
            'count': 0,
            'query': query,
            'store_name': store_name,
            'message': 'Query must be at least 2 characters' if query else 'No query provided'
        })

    # Search across ALL locations of this store chain
    # Order by popularity (times_purchased) to show most common items first
    items = GroceryItem.objects.filter(
        store_name__iexact=store_name,  # Case-insensitive store name match
        name__icontains=query  # Case-insensitive name search
    ).order_by('-times_purchased', 'name')[:limit]

    logger.info(f"🔍 Search: store='{store_name}', query='{query}', found {items.count()} items")

    # Serialize results
    items_data = []
    for item in items:
        item_dict = {
            'id': item.id,
            'name': item.name,
            'brand': item.brand or '',
            'size': item.size or '',
            'category': item.category or '',
            'price': None,  # Don't include price in autocomplete (varies by time/location)
            'image_url': request.build_absolute_uri(item.image_url) if item.image_url else '',
            'times_purchased': item.times_purchased
        }

        # Add nutrition data if available
        if item.nutriscore_grade or item.nova_group:
            item_dict['nutrition'] = {
                'nutriscore_grade': item.nutriscore_grade,
                'nova_group': item.nova_group,
                'has_nutrition_data': True if item.nutrition_data else False,
                'nutrients': item.nutrition_data.get('nutrients') if item.nutrition_data else None
            }
        else:
            item_dict['nutrition'] = None

        items_data.append(item_dict)

    return JsonResponse({
        'items': items_data,
        'count': len(items_data),
        'query': query,
        'store_name': store_name
    })


# ========================================
# RECALL ALERT SYSTEM API
# ========================================

@csrf_exempt
@require_firebase_auth
def recall_matches_api(request):
    """
    Get user's recall matches (products they bought that were recalled)

    GET /shopright/api/recalls/matches/
    Query params:
        - status: 'all' | 'unverified' | 'confirmed' | 'dismissed' (default: 'unverified')
        - classification: 'Class I' | 'Class II' | 'Class III' (optional filter)

    Returns: {
        'matches': [
            {
                'id': 123,
                'recall': {
                    'id': 456,
                    'recall_number': 'F-0234-2024',
                    'source': 'FDA',
                    'classification': 'Class I',
                    'product_name': 'Milk...',
                    'reason': 'Undeclared allergen',
                    'recall_date': '2024-01-15',
                    'is_critical': true
                },
                'purchased_product_name': 'Whole Milk',
                'purchased_at_store': 'Trader Joe\'s',
                'purchased_date': '2024-01-10',
                'confidence_score': 95,
                'match_reason': 'Exact product+brand match',
                'user_response': 'unverified',
                'notified_at': '2024-01-15T08:00:00Z',
                'created_at': '2024-01-15T02:00:00Z'
            }
        ],
        'count': 1,
        'critical_count': 1
    }
    """
    from shopright.models import RecallMatch

    # Check if user is premium (PREMIUM ONLY FEATURE)
    if not SubscriptionService.is_premium_user(request.user):
        logger.info(f"🚫 {request.user.username} tried to access recalls but is not premium")
        return JsonResponse({
            'premium_required': True,
            'message': 'Recall alerts are a Premium feature. Upgrade to monitor your purchases for recalls.',
            'matches': [],
            'count': 0,
            'critical_count': 0
        }, status=403)

    # Get filter parameters
    status = request.GET.get('status', 'unverified')
    classification = request.GET.get('classification', None)

    # Base query: user's matches
    matches = RecallMatch.objects.filter(user=request.user).select_related('recall')

    # Filter by status
    if status != 'all':
        matches = matches.filter(user_response=status)

    # Filter by classification
    if classification:
        matches = matches.filter(recall__classification=classification)

    # Order by severity and date
    matches = matches.order_by(
        '-recall__classification',  # Class I first
        '-recall__recall_posted_date'
    )

    # Serialize matches
    matches_data = []
    critical_count = 0

    for match in matches:
        recall = match.recall

        # Count critical matches
        if recall.is_critical and match.user_response == 'unverified':
            critical_count += 1

        match_dict = {
            'id': match.id,
            'recall': {
                'id': recall.id,
                'recall_number': recall.recall_number,
                'source': recall.source,
                'classification': recall.classification,
                'product_name': recall.product_name,
                'reason': recall.reason_for_recall[:100],  # Truncate for list view
                'recall_date': recall.recall_posted_date.isoformat(),
                'is_critical': recall.is_critical
            },
            'purchased_product_name': match.purchased_product_name,
            'purchased_at_store': match.purchased_at_store,
            'purchased_date': match.purchased_date.isoformat(),
            'shopping_trip_id': match.shopping_trip.id if match.shopping_trip else None,
            'confidence_score': match.confidence_score,
            'match_reason': match.match_reason,
            'user_response': match.user_response,
            'notified_at': match.notified_at.isoformat() if match.notified_at else None,
            'created_at': match.matched_at.isoformat()
        }
        matches_data.append(match_dict)

    logger.info(f"📋 Recall matches for {request.user.username}: {len(matches_data)} matches ({critical_count} critical)")

    return JsonResponse({
        'matches': matches_data,
        'count': len(matches_data),
        'critical_count': critical_count
    })


@csrf_exempt
@require_firebase_auth
def recall_detail_api(request, recall_id):
    """
    Get full details for a specific recall

    GET /shopright/api/recalls/{id}/detail/

    Returns: {
        'recall': {
            'id': 456,
            'recall_number': 'F-0234-2024',
            'source': 'FDA',
            'classification': 'Class I',
            'status': 'Active',
            'product_name': 'Whole Milk...',
            'product_description': 'Full description...',
            'recalling_firm': 'Company Name',
            'reason_for_recall': 'Undeclared allergen: milk',
            'health_hazard': 'May cause severe allergic reaction...',
            'distribution_pattern': 'Nationwide',
            'stores': ['Walmart', 'Target'],
            'remedy': 'Return to store for refund',
            'contact_info': '1-800-XXX-XXXX',
            'recall_initiation_date': '2024-01-10',
            'recall_posted_date': '2024-01-15',
            'is_critical': true
        }
    }
    """
    from shopright.models import ProductRecall

    # Check if user is premium (PREMIUM ONLY FEATURE)
    if not SubscriptionService.is_premium_user(request.user):
        logger.info(f"🚫 {request.user.username} tried to view recall detail but is not premium")
        return JsonResponse({
            'error': 'PREMIUM_REQUIRED',
            'message': 'Recall alerts are a Premium feature'
        }, status=403)

    try:
        recall = ProductRecall.objects.get(id=recall_id)
    except ProductRecall.DoesNotExist:
        return JsonResponse({'error': 'Recall not found'}, status=404)

    recall_dict = {
        'id': recall.id,
        'recall_number': recall.recall_number,
        'source': recall.source,
        'classification': recall.classification,
        'status': recall.status,
        'product_name': recall.product_name,
        'product_description': recall.product_description,
        'recalling_firm': recall.recalling_firm,
        'reason_for_recall': recall.reason_for_recall,
        'health_hazard': recall.health_hazard_evaluation,
        'distribution_pattern': recall.distribution_pattern,
        'stores': recall.stores,
        'remedy': recall.remedy,
        'contact_info': recall.contact_info,
        'upc_codes': recall.upc_codes,
        'lot_numbers': recall.lot_numbers,
        'recall_initiation_date': recall.recall_initiation_date.isoformat(),
        'recall_posted_date': recall.recall_posted_date.isoformat(),
        'is_critical': recall.is_critical
    }

    logger.info(f"📄 Recall detail viewed: {recall.recall_number} by {request.user.username}")

    return JsonResponse({'recall': recall_dict})


@csrf_exempt
@require_firebase_auth
def confirm_recall_match_api(request, match_id):
    """
    User confirms this is their product (they bought the recalled item)

    POST /shopright/api/recalls/{id}/confirm/
    Body: {
        'feedback': 'Yes, I bought this at Walmart' (optional)
    }

    Returns: {
        'success': true,
        'match': {...}
    }
    """
    from shopright.models import RecallMatch

    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    # Check if user is premium (PREMIUM ONLY FEATURE)
    # Check premium status
    if not SubscriptionService.is_premium_user(request.user):
        logger.info(f"🚫 {request.user.username} tried to confirm recall but is not premium")
        return JsonResponse({
            'error': 'PREMIUM_REQUIRED',
            'message': 'This feature requires Premium'
        }, status=403)

    try:
        match = RecallMatch.objects.get(id=match_id, user=request.user)
    except RecallMatch.DoesNotExist:
        return JsonResponse({'error': 'Match not found'}, status=404)

    # Parse request body
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        data = {}

    feedback = data.get('feedback', '')

    # Mark as confirmed
    match.mark_confirmed(feedback=feedback)

    logger.info(f"✅ {request.user.username} CONFIRMED recall match for {match.purchased_product_name}")

    return JsonResponse({
        'success': True,
        'match': {
            'id': match.id,
            'user_response': match.user_response,
            'user_response_at': match.user_response_at.isoformat(),
            'recall_number': match.recall.recall_number
        }
    })


@csrf_exempt
@require_firebase_auth
def dismiss_recall_match_api(request, match_id):
    """
    User dismisses false positive (this is NOT their product)

    POST /shopright/api/recalls/{id}/dismiss/
    Body: {
        'reason': 'I didn\'t buy this brand' (optional)
    }

    Returns: {
        'success': true,
        'match': {...}
    }
    """
    from shopright.models import RecallMatch

    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    # Check if user is premium (PREMIUM ONLY FEATURE)
    subscription = SubscriptionService.get_or_create_subscription(request.user)

    if not SubscriptionService.is_premium_user(request.user):
        logger.info(f"🚫 {request.user.username} tried to dismiss recall but is not premium")
        return JsonResponse({
            'error': 'PREMIUM_REQUIRED',
            'message': 'This feature requires Premium'
        }, status=403)

    try:
        match = RecallMatch.objects.get(id=match_id, user=request.user)
    except RecallMatch.DoesNotExist:
        return JsonResponse({'error': 'Match not found'}, status=404)

    # Parse request body
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        data = {}

    reason = data.get('reason', '')

    # Mark as dismissed
    match.mark_dismissed(reason=reason)

    logger.info(f"⏭️ {request.user.username} DISMISSED recall match for {match.purchased_product_name}: {reason}")

    return JsonResponse({
        'success': True,
        'match': {
            'id': match.id,
            'user_response': match.user_response,
            'user_response_at': match.user_response_at.isoformat(),
            'resolved': match.resolved
        }
    })


@csrf_exempt
@require_firebase_auth
def mark_recall_notified_api(request, match_id):
    """
    Mark a recall match as notified (notification sent to user).
    Called by iOS app after scheduling local notification.

    POST /api/recalls/match/{match_id}/mark-notified/

    Response:
    {
        'success': true,
        'match': {
            'id': 1,
            'notification_sent': true,
            'notified_at': '2025-01-10T09:00:00Z'
        }
    }
    """
    from shopright.models import RecallMatch
    from django.utils import timezone

    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    try:
        match = RecallMatch.objects.get(id=match_id, user=request.user)
    except RecallMatch.DoesNotExist:
        return JsonResponse({'error': 'Match not found'}, status=404)

    # Mark as notified
    match.notification_sent = True
    match.notified_at = timezone.now()
    match.save()

    logger.info(f"🔔 Marked recall match {match_id} as notified for {request.user.username}")

    return JsonResponse({
        'success': True,
        'match': {
            'id': match.id,
            'notification_sent': match.notification_sent,
            'notified_at': match.notified_at.isoformat() if match.notified_at else None
        }
    })


@require_http_methods(["GET"])
@require_firebase_auth
def monthly_spending_api(request):
    """
    Get spending analytics for a given month

    GET /shopright/api/spending/monthly/?year=2024&month=11

    Returns:
    {
        "year": 2024,
        "month": 11,
        "total_spent": 423.18,
        "trip_count": 5,
        "by_store": {
            "Trader Joe's": 256.00,
            "Whole Foods": 167.18
        },
        "by_category": {
            "Produce": 145.00,
            "Dairy": 89.00,
            ...
        },
        "by_nutrition": {
            "A": 89.00,
            "B": 167.00,
            "C": 89.00,
            "D": 56.00,
            "E": 22.18,
            "unknown": 0.00
        }
    }
    """
    from datetime import datetime
    from decimal import Decimal
    from collections import defaultdict

    # Get parameters with proper defaults
    now = datetime.now()
    year_param = request.GET.get('year')
    month_param = request.GET.get('month')

    try:
        year = int(year_param) if year_param else now.year
        month = int(month_param) if month_param else now.month
    except (ValueError, TypeError):
        return JsonResponse({'error': 'Invalid year or month'}, status=400)

    if not (1 <= month <= 12):
        return JsonResponse({'error': 'Month must be 1-12'}, status=400)

    # Check premium access for historical spending data (Basic: last 3 months only)
    if not SubscriptionService.is_premium_user(request.user):
        # Calculate 3 months ago from current date
        three_months_ago = now.date() - timedelta(days=90)
        requested_date = datetime(year, month, 1).date()

        if requested_date < three_months_ago:
            logger.info(f"🚫 {request.user.username} (Basic) tried to access spending data from {year}-{month:02d} but Basic users limited to last 3 months")
            return JsonResponse({
                'premium_required': True,
                'message': 'Spending analytics older than 3 months requires Premium. Upgrade for unlimited history.',
                'error': 'PREMIUM_REQUIRED_FOR_HISTORICAL_DATA'
            }, status=403)

    # Get user's family
    try:
        family_member = FamilyMember.objects.get(user=request.user)
        family = family_member.family
    except FamilyMember.DoesNotExist:
        return JsonResponse({'error': 'User not in a family'}, status=404)

    # Get all trips for this family in the specified month
    trips = ShoppingTrip.objects.filter(
        family=family,
        trip_date__year=year,
        trip_date__month=month
    )

    # Calculate totals
    total_spent = Decimal('0.00')
    by_store = defaultdict(Decimal)
    by_category = defaultdict(Decimal)
    by_nutrition = {
        'A': Decimal('0.00'),
        'B': Decimal('0.00'),
        'C': Decimal('0.00'),
        'D': Decimal('0.00'),
        'E': Decimal('0.00'),
        'unknown': Decimal('0.00')
    }

    for trip in trips:
        # Add to total
        if trip.total_amount:
            trip_total = Decimal(str(trip.total_amount))
            total_spent += trip_total

            # By store
            by_store[trip.store_name] += trip_total

        # Process items for category and nutrition breakdown
        for item in trip.items:
            item_name = item.get('name', '')
            item_category = item.get('category', 'Other')
            item_price = item.get('price')
            item_quantity = item.get('quantity', 1)

            if item_price:
                try:
                    unit_price = Decimal(str(item_price))
                    line_total = unit_price * Decimal(str(item_quantity))

                    # By category
                    by_category[item_category] += line_total

                    # By nutrition - look up GroceryItem to get grade
                    grocery_item = GroceryItem.objects.filter(
                        name=item_name,
                        store_name=trip.store_name
                    ).first()

                    if grocery_item and grocery_item.nutriscore_grade:
                        grade = grocery_item.nutriscore_grade.upper()
                        if grade in by_nutrition:
                            by_nutrition[grade] += line_total
                        else:
                            by_nutrition['unknown'] += line_total
                    else:
                        by_nutrition['unknown'] += line_total

                except (ValueError, TypeError, Decimal.InvalidOperation):
                    continue

    # Convert Decimal to float for JSON
    response_data = {
        'year': year,
        'month': month,
        'total_spent': float(total_spent),
        'trip_count': trips.count(),
        'by_store': {store: float(amount) for store, amount in by_store.items()},
        'by_category': {cat: float(amount) for cat, amount in by_category.items()},
        'by_nutrition': {grade: float(amount) for grade, amount in by_nutrition.items()}
    }

    logger.info(f"📊 Spending analytics for {family.name}: {year}-{month:02d} = ${total_spent}")

    return JsonResponse(response_data)


@require_http_methods(["GET"])
@require_firebase_auth
def spending_trend_api(request):
    """
    Get 6-month spending trend

    GET /shopright/api/spending/trend/

    Returns:
    {
        "months": [
            {"year": 2025, "month": 10, "label": "Oct", "total": 450.00},
            {"year": 2025, "month": 11, "label": "Nov", "total": 520.00},
            ...
        ]
    }
    """
    from datetime import datetime
    from decimal import Decimal
    import calendar

    # Get user's family
    try:
        family_member = FamilyMember.objects.get(user=request.user)
        family = family_member.family
    except FamilyMember.DoesNotExist:
        return JsonResponse({'error': 'User not in a family'}, status=404)

    # Calculate last 6 months (including current month)
    now = datetime.now()
    months_data = []

    # Check if user is premium for historical data access
    is_premium = SubscriptionService.is_premium_user(request.user)

    for i in range(5, -1, -1):  # 6 months ago to now
        # Calculate target month
        target_month = now.month - i
        target_year = now.year

        # Handle year rollover
        while target_month <= 0:
            target_month += 12
            target_year -= 1
        while target_month > 12:
            target_month -= 12
            target_year += 1

        # Skip months older than 3 months for Basic users
        if not is_premium:
            target_date = datetime(target_year, target_month, 1).date()
            three_months_ago = now.date() - timedelta(days=90)
            if target_date < three_months_ago:
                logger.info(f"🚫 Skipping {target_year}-{target_month:02d} for Basic user (older than 3 months)")
                continue

        # Get all trips for this family in this month
        trips = ShoppingTrip.objects.filter(
            family=family,
            trip_date__year=target_year,
            trip_date__month=target_month
        )

        # Calculate total for this month
        total_spent = Decimal('0.00')
        for trip in trips:
            if trip.total_amount:
                total_spent += Decimal(str(trip.total_amount))

        # Get month abbreviation
        month_label = calendar.month_abbr[target_month]

        months_data.append({
            'year': target_year,
            'month': target_month,
            'label': month_label,
            'total': float(total_spent)
        })

    logger.info(f"📈 Spending trend for {family.name}: {len(months_data)} months")

    return JsonResponse({'months': months_data})


# ========================================
# PRICE COMPARISON API (Barcode-First Matching)
# ========================================

@csrf_exempt
@require_firebase_auth
def batch_price_comparison_api(request):
    """
    Compare prices for multiple items across stores using barcode matching

    POST /shopright/api/price-comparison/batch/
    Body: {
        "grocery_item_ids": [123, 456, 789, ...]
    }

    Returns: {
        "comparisons": [
            {
                "grocery_item_id": 123,
                "current_price": 4.99,
                "current_store": "Trader Joe's",
                "cheapest_store": "Walmart",
                "cheapest_price": 2.99,
                "savings": 2.00,
                "match_method": "barcode",  # or "name"
                "has_barcode": true
            },
            ...
        ],
        "total_savings": 12.50,
        "items_compared": 8,
        "items_with_savings": 3
    }
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    grocery_item_ids = data.get('grocery_item_ids', [])

    if not grocery_item_ids:
        return JsonResponse({'error': 'Missing grocery_item_ids'}, status=400)

    from datetime import timedelta
    from decimal import Decimal

    # Fetch all items in one query
    items = GroceryItem.objects.filter(id__in=grocery_item_ids)

    comparisons = []
    total_savings = Decimal('0.00')
    items_compared = 0
    items_with_savings = 0

    # Time window for recent prices (30 days)
    thirty_days_ago = datetime.now() - timedelta(days=30)

    for item in items:
        comparison = _compare_single_item_price(item, thirty_days_ago)

        if comparison:
            comparisons.append(comparison)
            items_compared += 1

            if comparison['savings'] > 0:
                total_savings += Decimal(str(comparison['savings']))
                items_with_savings += 1

    logger.info(f"💰 Price comparison for {request.user.username}: {items_compared} items, {items_with_savings} with savings, total savings ${total_savings}")

    return JsonResponse({
        'comparisons': comparisons,
        'total_savings': float(total_savings),
        'items_compared': items_compared,
        'items_with_savings': items_with_savings
    })


def _compare_single_item_price(reference_item, thirty_days_ago):
    """
    Helper function to compare price for a single grocery item
    Uses barcode matching first, falls back to name matching

    Returns: {
        'grocery_item_id': 123,
        'current_price': 4.99,
        'current_store': 'Trader Joe\'s',
        'cheapest_store': 'Walmart',
        'cheapest_price': 2.99,
        'savings': 2.00,
        'match_method': 'barcode',
        'has_barcode': True
    }
    or None if no comparison data available
    """
    # STEP 1: Try barcode matching first (most accurate)
    if reference_item.barcode:
        same_product_all_stores = GroceryItem.objects.filter(
            barcode=reference_item.barcode
        ).exclude(
            store_name=reference_item.store_name  # Exclude current store
        )
        match_method = 'barcode'

        logger.debug(f"🔍 Barcode match: {reference_item.name} found at {same_product_all_stores.count()} other stores")

    # STEP 2: Fallback to name+brand+size matching (less accurate, for items without barcodes)
    else:
        same_product_all_stores = GroceryItem.objects.filter(
            name__iexact=reference_item.name,
            brand__iexact=reference_item.brand,
            size__iexact=reference_item.size
        ).exclude(store_name=reference_item.store_name)
        match_method = 'name'

        logger.debug(f"📝 Name match: {reference_item.name} found at {same_product_all_stores.count()} other stores")

    # No other stores selling this item
    if not same_product_all_stores.exists():
        return None

    # Extract prices from recent ShoppingTrip receipts
    price_data = []

    for other_store_item in same_product_all_stores:
        # Find most recent purchase of this item at this store
        recent_trips = ShoppingTrip.objects.filter(
            store_name=other_store_item.store_name,
            trip_date__gte=thirty_days_ago
        ).order_by('-trip_date')

        # Search for this item in trip.items JSONField
        for trip in recent_trips:
            found_price = None

            for receipt_item in trip.items:
                # Match by name (barcode not stored in receipt items currently)
                if receipt_item.get('name', '').lower() == reference_item.name.lower():
                    price_str = receipt_item.get('price', '')
                    if price_str:
                        try:
                            found_price = float(price_str.replace('$', '').replace(',', ''))
                            break
                        except ValueError:
                            continue

            if found_price is not None:
                price_data.append({
                    'store_name': other_store_item.store_name,
                    'price': found_price,
                    'last_seen': trip.trip_date.isoformat()
                })
                break  # Found price at this store, move to next store

    # No price data found from recent receipts
    if not price_data:
        return None

    # Find cheapest price
    cheapest = min(price_data, key=lambda x: x['price'])

    # Get current item's price from recent receipts
    # NOTE: GroceryItem doesn't have a price field - prices are only in receipts
    current_price = None

    recent_trip = ShoppingTrip.objects.filter(
        store_name=reference_item.store_name,
        trip_date__gte=thirty_days_ago
    ).order_by('-trip_date').first()

    if recent_trip:
        for receipt_item in recent_trip.items:
            if receipt_item.get('name', '').lower() == reference_item.name.lower():
                price_str = receipt_item.get('price', '')
                if price_str:
                    try:
                        current_price = float(price_str.replace('$', '').replace(',', ''))
                        break
                    except ValueError:
                        continue

    # Can't compare without current price
    if current_price is None:
        return None

    # Calculate savings
    savings = max(0, current_price - cheapest['price'])  # Only positive savings

    # Only return comparison if there are actual savings (threshold: $0.10 to avoid noise)
    if savings < 0.10:
        return None

    return {
        'grocery_item_id': reference_item.id,
        'current_price': round(current_price, 2),
        'current_store': reference_item.store_name,
        'cheapest_store': cheapest['store_name'],
        'cheapest_price': round(cheapest['price'], 2),
        'savings': round(savings, 2),
        'match_method': match_method,
        'has_barcode': bool(reference_item.barcode)
    }


# ========================================
# SHOPPER/DELIVERY SERVICE API
# ========================================

@require_firebase_auth
def user_profile_api(request):
    """
    Get current user's profile information

    GET /shopright/api/user-profile/

    Returns: {
        "user": {
            "username": "+12193089382",
            "first_name": "John",
            "last_name": "Doe"
        },
        "profile": {
            "account_type": "customer",
            "is_approved_shopper": true,
            "has_fcm_token": true
        }
    }
    """
    if request.method != 'GET':
        return JsonResponse({'error': 'Only GET allowed'}, status=405)

    user = request.user

    # Get or create profile
    profile, created = UserProfile.objects.get_or_create(user=user)

    return JsonResponse({
        'user': {
            'username': user.username,
            'first_name': user.first_name,
            'last_name': user.last_name
        },
        'profile': {
            'account_type': profile.account_type,
            'is_approved_shopper': profile.is_approved_shopper,
            'has_fcm_token': bool(profile.fcm_token)
        }
    })


@require_firebase_auth
@require_approved_shopper
def get_available_deliveries_api(request):
    """
    Get all deliveries available for shopper assignment
    (Collaborative team workspace - ALL approved shoppers see ALL deliveries)

    GET /shopright/api/shopper/available-deliveries/

    Returns: {
        "deliveries_by_store": [
            {
                "store_name": "Trader Joe's",
                "store_location": "Sunnyvale, CA",
                "delivery_date": "2025-12-14",
                "deliveries": [
                    {
                        "id": 123,
                        "customer_name": "John D.",
                        "delivery_address": "123 Main St, Sunnyvale, CA",
                        "estimated_amount": "85.00",
                        "delivery_time": "2pm",
                        "item_count": 15,
                        "status": "pending_shopper",
                        "shopping_list_id": 456
                    }
                ]
            }
        ]
    }
    """
    if request.method != 'GET':
        return JsonResponse({'error': 'Only GET allowed'}, status=405)

    # Get all weekly deliveries that need shopper assignment (from active subscriptions only)
    deliveries = WeeklyDelivery.objects.filter(
        status__in=['pending_shopper', 'scheduled'],
        shopper__isnull=True,  # Only deliveries without assigned shopper
        subscription__status__in=['active', 'pending_confirmation', 'setup_complete']  # Include setup_complete for add_delivery flow
    ).select_related('subscription', 'shopping_list', 'shopper').order_by('delivery_date', 'subscription__store__name')

    # Group by store and date
    from collections import defaultdict
    grouped = defaultdict(lambda: defaultdict(list))

    for delivery in deliveries:
        # Get store info from shopping list (primary) or subscription.store (fallback for partners)
        if delivery.shopping_list:
            store_name = delivery.shopping_list.store_name
            store_location = delivery.shopping_list.store_location
        elif delivery.subscription and delivery.subscription.store:
            store_name = delivery.subscription.store.name
            store_location = delivery.subscription.store.address
        else:
            store_name = "Unknown Store"
            store_location = ""

        delivery_date = delivery.delivery_date.isoformat()

        # Get customer name (first name + last initial)
        customer = delivery.subscription.customer if delivery.subscription else None
        if customer:
            customer_display = f"{customer.first_name or customer.username} {customer.last_name[0] if customer.last_name else ''}."
        else:
            customer_display = "Unknown"

        # Get item count from shopping list (only "Need to Buy" items, not "Already Got")
        item_count = delivery.shopping_list.list_items.filter(is_checked=False).count() if delivery.shopping_list else 0

        # Build delivery dict
        delivery_dict = {
            'id': delivery.id,
            'customer_name': customer_display,
            'delivery_address': delivery.subscription.delivery_address if delivery.subscription else '',
            'delivery_instructions': delivery.subscription.delivery_instructions if delivery.subscription else '',
            'estimated_amount': str(delivery.actual_cost) if delivery.actual_cost else '0.00',
            'delivery_time': delivery.delivery_window or (delivery.subscription.delivery_window if delivery.subscription else ''),
            'item_count': item_count,
            'status': delivery.status,
            'shopping_list_id': delivery.shopping_list.id if delivery.shopping_list else None,
            'shopper': delivery.shopper.username if delivery.shopper else None
        }

        grouped[(store_name, store_location)][delivery_date].append(delivery_dict)

    # Convert to list format
    deliveries_by_store = []
    for (store_name, store_location) in sorted(grouped.keys()):
        for delivery_date in sorted(grouped[(store_name, store_location)].keys()):
            deliveries_by_store.append({
                'store_name': store_name,
                'store_location': store_location,
                'delivery_date': delivery_date,
                'deliveries': grouped[(store_name, store_location)][delivery_date]
            })

    logger.info(f"📦 Shopper {request.user.username} viewing {len(deliveries)} available deliveries")

    return JsonResponse({
        'deliveries_by_store': deliveries_by_store,
        'total_count': len(deliveries)
    })


@csrf_exempt
@require_firebase_auth
@require_approved_shopper
def assign_delivery_api(request):
    """
    Assign a delivery to yourself (shopper accepts the job)

    POST /shopright/api/shopper/assign-delivery/
    Body: {
        "delivery_id": 123
    }

    Returns: {
        "success": true,
        "message": "Delivery assigned successfully",
        "delivery": {...}
    }
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    delivery_id = data.get('delivery_id')

    if not delivery_id:
        return JsonResponse({'error': 'Missing delivery_id'}, status=400)

    try:
        delivery = WeeklyDelivery.objects.get(id=delivery_id)
    except WeeklyDelivery.DoesNotExist:
        return JsonResponse({'error': 'Delivery not found'}, status=404)

    # Check if delivery was cancelled
    if delivery.status == 'cancelled':
        return JsonResponse({
            'success': False,
            'error': 'This delivery was cancelled by the customer',
            'status': 'cancelled'
        }, status=410)  # 410 Gone

    # Check if already assigned
    if delivery.shopper:
        return JsonResponse({
            'success': False,
            'error': 'This delivery was already assigned to another shopper',
            'assigned_to': delivery.shopper.username
        }, status=409)  # 409 Conflict

    # Get subscription and customer info
    subscription = delivery.subscription
    if not subscription:
        return JsonResponse({'error': 'No subscription found for this delivery'}, status=400)

    customer = subscription.customer
    if not customer:
        return JsonResponse({'error': 'No customer found for this delivery'}, status=400)

    # Get customer's payment info
    customer_profile = UserProfile.objects.filter(user=customer).first()
    if not customer_profile:
        return JsonResponse({'error': 'Customer profile not found'}, status=400)

    if not customer_profile.stripe_customer_id or not customer_profile.default_payment_method:
        logger.error(f"❌ Customer {customer.username} has no payment method on file")
        return JsonResponse({
            'success': False,
            'error': 'Customer has no payment method on file',
            'payment_required': True
        }, status=402)  # 402 Payment Required

    # Check if subscription was already charged this week
    # Look for any delivery from this subscription this week that has payment_authorization_id
    # (payment_authorization_id exists = customer was charged)
    from datetime import timedelta
    week_start = delivery.delivery_date - timedelta(days=delivery.delivery_date.weekday())
    week_end = week_start + timedelta(days=6)

    already_charged_this_week = WeeklyDelivery.objects.filter(
        subscription=subscription,
        delivery_date__gte=week_start,
        delivery_date__lte=week_end,
        payment_authorization_id__isnull=False  # Has payment ID = was charged
    ).exclude(id=delivery.id).exists()

    should_charge = not already_charged_this_week
    subscription_amount = 30.00 if subscription.subscription_tier == 'premium' else 15.00

    if should_charge:
        # First delivery this week - charge subscription fee
        from .services.stripe_service import StripeService
        success, charge_id, error_msg = StripeService.charge_subscription_fee(
            customer_id=customer_profile.stripe_customer_id,
            payment_method_id=customer_profile.default_payment_method,
            amount=subscription_amount,
            description=f"ShopRight Weekly Delivery - {delivery.delivery_date.strftime('%b %d')}",
            metadata={
                'delivery_id': str(delivery.id),
                'subscription_id': str(subscription.id),
                'customer_username': customer.username,
                'shopper_username': request.user.username
            }
        )

        if not success:
            # Payment failed - don't assign delivery
            logger.error(f"❌ Payment failed for customer {customer.username}: {error_msg}")

            # Send payment failed notification to customer
            from .services.notification_service import NotificationService
            NotificationService.send_subscription_payment_failed(
                user=customer,
                delivery_date=delivery.delivery_date
            )

            return JsonResponse({
                'success': False,
                'error': 'Customer payment failed',
                'error_detail': error_msg,
                'payment_failed': True
            }, status=402)  # 402 Payment Required

        logger.info(f"✅ Subscription fee ${subscription_amount} charged for delivery {delivery_id}")

        # Store payment ID to prevent duplicate charges
        delivery.payment_authorization_id = charge_id
        delivery.save()
    else:
        logger.info(f"ℹ️ Subscription already charged this week, skipping charge for delivery {delivery_id}")

    # Payment successful! Now assign delivery
    delivery.shopper = request.user
    delivery.status = 'assigned'  # Shopper accepted, but hasn't started packing yet
    delivery.save()

    # RESET: Clear shopper_collected for all items (fresh start for new shopper)
    # This ensures previous delivery's collected state doesn't carry over
    if delivery.shopping_list:
        reset_count = delivery.shopping_list.list_items.filter(shopper_collected=True).update(shopper_collected=False)
        if reset_count > 0:
            logger.info(f"🔄 Reset {reset_count} items' shopper_collected to False for new shopper assignment")

    # Update subscription status to active
    subscription.status = 'active'
    subscription.save()

    if should_charge:
        logger.info(f"✅ Delivery {delivery_id} assigned to shopper {request.user.username} and ${subscription_amount} charged")

        # Send confirmation notification to customer (only when charged)
        from .services.notification_service import NotificationService
        NotificationService.send_delivery_confirmed(
            user=customer,
            delivery_date=delivery.delivery_date,
            amount=subscription_amount
        )

        return JsonResponse({
            'success': True,
            'message': f'Delivery assigned and customer charged ${subscription_amount}',
            'delivery': {
                'id': delivery.id,
                'status': delivery.status,
                'shopper': request.user.username,
                'delivery_date': delivery.delivery_date.isoformat(),
                'amount_charged': subscription_amount
            }
        })
    else:
        logger.info(f"✅ Delivery {delivery_id} assigned to shopper {request.user.username} (no charge - already paid this week)")

        # Send simpler notification for 2nd delivery (no charge mentioned)
        from .services.notification_service import NotificationService
        NotificationService.send_notification(
            user=customer,
            title="✅ Delivery Confirmed",
            body=f"Your delivery on {delivery.delivery_date.strftime('%a, %b %d')} is confirmed!",
            data={
                'delivery_date': delivery.delivery_date.isoformat(),
                'action': 'open_deliveries'
            },
            notification_type='delivery_confirmed'
        )

        return JsonResponse({
            'success': True,
            'message': 'Delivery assigned (no additional charge - already paid this week)',
            'delivery': {
                'id': delivery.id,
                'status': delivery.status,
                'shopper': request.user.username,
                'delivery_date': delivery.delivery_date.isoformat(),
                'amount_charged': 0  # Already paid
            }
        })


@csrf_exempt
@require_firebase_auth
@require_approved_shopper
def deny_delivery_api(request):
    """
    Deny/reject a delivery (shopper cannot fulfill it)

    POST /shopright/api/shopper/deny-delivery/
    Body: {
        "delivery_id": 123,
        "reason": "Outside service area"  // Optional
    }

    Returns: {
        "success": true,
        "message": "Delivery cancelled. Customer was not charged."
    }
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    delivery_id = data.get('delivery_id')
    reason = data.get('reason', 'No shopper available')

    if not delivery_id:
        return JsonResponse({'error': 'Missing delivery_id'}, status=400)

    try:
        delivery = WeeklyDelivery.objects.get(id=delivery_id)
    except WeeklyDelivery.DoesNotExist:
        return JsonResponse({'error': 'Delivery not found'}, status=404)

    # Only allow denying deliveries that haven't been accepted yet
    if delivery.status not in ['scheduled', 'pending_shopper']:
        return JsonResponse({
            'error': 'Cannot deny this delivery',
            'message': f'Delivery is already {delivery.status}. Only available deliveries can be denied.'
        }, status=400)

    # Get subscription and customer info
    subscription = delivery.subscription
    customer = subscription.customer if subscription else None

    if not customer:
        return JsonResponse({'error': 'No customer found for this delivery'}, status=400)

    # Cancel THIS delivery only (no charge) - do NOT cancel the subscription!
    # The subscription stays active for future deliveries
    delivery.status = 'cancelled'
    delivery.save()

    # NOTE: We do NOT cancel the subscription here!
    # Denying one delivery should not affect other deliveries or the subscription itself

    logger.info(f"🚫 Delivery {delivery_id} denied by shopper {request.user.username}. Reason: {reason}")

    # Notify customer that delivery is unavailable
    from .services.notification_service import NotificationService
    NotificationService.send_delivery_unavailable(
        user=customer,
        delivery_date=delivery.delivery_date,
        reason=reason
    )

    return JsonResponse({
        'success': True,
        'message': 'Delivery denied. Customer was not charged.',
        'delivery': {
            'id': delivery.id,
            'status': delivery.status,
            'reason': reason
        }
    })


@require_firebase_auth
@require_approved_shopper
def my_deliveries_api(request):
    """
    Get deliveries assigned to me (current shopper)

    GET /shopright/api/shopper/my-deliveries/

    Returns: {
        "deliveries": [
            {
                "id": 123,
                "customer_name": "John D.",
                "delivery_address": "123 Main St, Sunnyvale, CA",
                "shopping_list": {...},
                "status": "assigned",
                "delivery_date": "2025-12-14",
                "assigned_at": "2025-12-10T10:00:00Z"
            }
        ],
        "count": 5
    }
    """
    if request.method != 'GET':
        return JsonResponse({'error': 'Only GET allowed'}, status=405)

    # Get all deliveries assigned to me with active subscriptions only
    deliveries = WeeklyDelivery.objects.filter(
        shopper=request.user,
        subscription__status__in=['active', 'pending_confirmation', 'setup_complete']  # Include setup_complete for add_delivery flow
    ).exclude(
        status='cancelled'  # Also exclude cancelled deliveries
    ).select_related('subscription', 'shopping_list').order_by('delivery_date')

    deliveries_data = []
    for delivery in deliveries:
        # Skip delivered deliveries - they should only appear in History tab
        if delivery.status == 'delivered':
            continue  # Don't show delivered deliveries in Active tab

        customer = delivery.subscription.customer if delivery.subscription else None
        customer_display = f"{customer.first_name or customer.username} {customer.last_name[0] if customer.last_name else ''}." if customer else "Unknown"

        # Get shopping list items
        shopping_list_data = None
        if delivery.shopping_list:
            items = delivery.shopping_list.list_items.all()
            unchecked_count = items.filter(is_checked=False).count()
            checked_count = items.filter(is_checked=True).count()
            logger.info(f"📦 Delivery {delivery.id} → List {delivery.shopping_list.id}: total={items.count()}, need_to_buy={unchecked_count}, already_got={checked_count}")
            shopping_list_data = {
                'id': delivery.shopping_list.id,
                'store_name': delivery.shopping_list.store_name,
                'item_count': unchecked_count,  # FIX: Show only "Need to Buy" count, not total
                'items': [
                    {
                        'id': item.id,
                        'name': item.name,
                        'brand': item.brand,
                        'size': item.size,
                        'quantity': item.quantity,
                        'is_checked': item.is_checked,
                        'shopper_collected': item.shopper_collected
                    }
                    for item in items.filter(is_checked=False)  # FIX: Only include "Need to Buy" items
                ]
            }

        # Get shopping trip if receipt has been scanned
        shopping_trip_data = None
        if delivery.shopping_trip:
            shopping_trip_data = {
                'id': delivery.shopping_trip.id,
                'store_name': delivery.shopping_trip.store_name,
                'store_location': delivery.shopping_trip.store_location or '',
                'trip_date': delivery.shopping_trip.trip_date.isoformat(),
                'total_amount': str(delivery.shopping_trip.total_amount) if delivery.shopping_trip.total_amount else None,
                'item_count': len(delivery.shopping_trip.items) if delivery.shopping_trip.items else 0
            }

        delivery_dict = {
            'id': delivery.id,
            'customer_name': customer_display,
            'delivery_address': delivery.subscription.delivery_address if delivery.subscription else '',
            'delivery_instructions': delivery.subscription.delivery_instructions if delivery.subscription else '',
            'shopping_list': shopping_list_data,
            'shopping_trip': shopping_trip_data,
            'status': delivery.status,
            'delivery_date': delivery.delivery_date.isoformat(),
            'delivery_time': delivery.delivery_window or (delivery.subscription.delivery_window if delivery.subscription else ''),
            'estimated_amount': str(delivery.estimated_cost or delivery.actual_cost) if (delivery.estimated_cost or delivery.actual_cost) else '0.00'
        }
        deliveries_data.append(delivery_dict)

    logger.info(f"📋 Shopper {request.user.username} has {len(deliveries_data)} assigned deliveries")

    return JsonResponse({
        'deliveries': deliveries_data,
        'count': len(deliveries_data)
    })


@csrf_exempt
@require_firebase_auth
@require_approved_shopper
def my_past_deliveries_api(request):
    """
    Get past deliveries completed by me (current shopper) - for History tab

    GET /shopright/api/shopper/my-past-deliveries/

    Returns: {
        "deliveries": [
            {
                "id": 123,
                "customer_name": "John D.",
                "delivery_address": "123 Main St, Sunnyvale, CA",
                "shopping_list": {...},
                "status": "delivered",
                "delivery_date": "2025-12-14",
                "delivered_at": "2025-12-14T15:30:00Z",
                "actual_cost": "67.89"
            }
        ],
        "count": 12
    }
    """
    if request.method != 'GET':
        return JsonResponse({'error': 'Only GET allowed'}, status=405)

    # Get past deliveries (delivered with inactive subscriptions OR cancelled)
    past_deliveries = WeeklyDelivery.objects.filter(
        shopper=request.user
    ).select_related('subscription', 'shopping_list')

    # Filter to only show truly "past" deliveries
    filtered_past_deliveries = []
    for delivery in past_deliveries:
        if delivery.status == 'cancelled':
            filtered_past_deliveries.append(delivery)
        elif delivery.status == 'delivered':
            # Show all delivered deliveries in History tab
            filtered_past_deliveries.append(delivery)

    # Sort by delivered date, then delivery date (most recent first)
    # Convert delivery_date to timezone-aware datetime for consistent comparison
    filtered_past_deliveries.sort(key=lambda d: d.delivered_at or timezone.make_aware(datetime.combine(d.delivery_date, datetime.min.time())), reverse=True)
    past_deliveries = filtered_past_deliveries[:50]  # Limit to last 50

    deliveries_data = []
    for delivery in past_deliveries:
        customer = delivery.subscription.customer if delivery.subscription else None
        customer_display = f"{customer.first_name or customer.username} {customer.last_name[0] if customer.last_name else ''}." if customer else "Unknown"

        # Get shopping list items
        shopping_list_data = None
        if delivery.shopping_list:
            items = delivery.shopping_list.list_items.all()
            shopping_list_data = {
                'id': delivery.shopping_list.id,
                'store_name': delivery.shopping_list.store_name,
                'item_count': items.count(),
                'items': [
                    {
                        'id': item.id,
                        'name': item.name,
                        'brand': item.brand,
                        'size': item.size,
                        'quantity': item.quantity,
                        'is_checked': item.is_checked,
                        'shopper_collected': item.shopper_collected
                    }
                    for item in items
                ]
            }

        # Get shopping trip if receipt has been scanned (for past deliveries)
        shopping_trip_data = None
        if delivery.shopping_trip:
            shopping_trip_data = {
                'id': delivery.shopping_trip.id,
                'store_name': delivery.shopping_trip.store_name,
                'store_location': delivery.shopping_trip.store_location or '',
                'trip_date': delivery.shopping_trip.trip_date.isoformat(),
                'total_amount': str(delivery.shopping_trip.total_amount) if delivery.shopping_trip.total_amount else None,
                'item_count': len(delivery.shopping_trip.items) if delivery.shopping_trip.items else 0
            }

        delivery_dict = {
            'id': delivery.id,
            'customer_name': customer_display,
            'delivery_address': delivery.subscription.delivery_address if delivery.subscription else '',
            'delivery_instructions': delivery.subscription.delivery_instructions if delivery.subscription else '',
            'shopping_list': shopping_list_data,
            'shopping_trip': shopping_trip_data,
            'status': delivery.status,
            'delivery_date': delivery.delivery_date.isoformat(),
            'delivery_time': delivery.delivery_window or (delivery.subscription.delivery_window if delivery.subscription else ''),
            'delivered_at': delivery.delivered_at.isoformat() if delivery.delivered_at else None,
            'estimated_amount': str(delivery.actual_cost) if delivery.actual_cost else '0.00'  # For past deliveries, use actual_cost as estimated_amount
        }
        deliveries_data.append(delivery_dict)

    logger.info(f"📋 Shopper {request.user.username} has {len(deliveries_data)} past deliveries")

    return JsonResponse({
        'deliveries': deliveries_data,
        'count': len(deliveries_data)
    })


@csrf_exempt
@require_firebase_auth
@require_approved_shopper
def start_packing_api(request):
    """
    Mark delivery as started packing and notify customer

    POST /shopright/api/shopper/start-packing/
    Body: {
        "delivery_id": 123
    }

    Returns: {
        "success": true,
        "message": "Packing started successfully"
    }
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    delivery_id = data.get('delivery_id')
    if not delivery_id:
        return JsonResponse({'error': 'Missing delivery_id'}, status=400)

    try:
        delivery = WeeklyDelivery.objects.get(id=delivery_id, shopper=request.user)
    except WeeklyDelivery.DoesNotExist:
        return JsonResponse({'error': 'Delivery not found or not assigned to you'}, status=404)

    # Check if subscription is still active before allowing packing
    if not delivery.subscription or delivery.subscription.status != 'active':
        return JsonResponse({
            'success': False,
            'error': 'Cannot start packing. Customer subscription is no longer active.'
        }, status=400)

    # Check if delivery is in correct status
    if delivery.status != 'assigned':
        return JsonResponse({
            'success': False,
            'error': f'Cannot start packing. Delivery status is "{delivery.status}"'
        }, status=400)

    # Calculate estimated total and pre-authorize payment
    estimated_total = 0.00
    shopping_list = delivery.shopping_list
    if shopping_list:
        # RESET: Clear shopper_collected for all items (fresh start for new delivery)
        # This ensures previous delivery's collected state doesn't carry over
        reset_count = shopping_list.list_items.filter(shopper_collected=True).update(shopper_collected=False)
        if reset_count > 0:
            logger.info(f"🔄 Reset {reset_count} items' shopper_collected to False for fresh packing start")

        # Calculate based only on UNCHECKED items (items still "Need to Buy")
        # Don't charge for items already marked as "Already Got" (is_checked=True)
        unchecked_items = shopping_list.list_items.filter(is_checked=False)

        for item in unchecked_items:
            try:
                # Parse price string (e.g., "$3.99" -> 3.99)
                price_str = item.price.replace('$', '').replace(',', '') if item.price else '0'
                item_price = float(price_str) * item.quantity
                estimated_total += item_price
                logger.debug(f"💰 Unchecked Item: {item.name}, Price: ${price_str}, Qty: {item.quantity}, Subtotal: ${item_price}")
            except (ValueError, TypeError):
                # Skip items with invalid prices
                logger.warning(f"⚠️ Skipping item with invalid price: {item.name}, price='{item.price}'")
                continue

    total_items = shopping_list.list_items.count() if shopping_list else 0
    unchecked_count = shopping_list.list_items.filter(is_checked=False).count() if shopping_list else 0
    logger.info(f"💰 Calculated estimated total: ${estimated_total} for {unchecked_count}/{total_items} unchecked items (excluding already purchased items)")

    # Add 20% buffer for price changes/tax/fees
    hold_amount = round(estimated_total * 1.2, 2)

    # Pre-authorize payment with customer
    try:
        from .services.stripe_service import StripeService
        customer = delivery.subscription.customer if delivery.subscription else None
        if customer and hold_amount > 0:
            # Convert dollars to cents for Stripe (Stripe expects integer cents)
            hold_amount_cents = int(round(hold_amount * 100))

            # Attempt to pre-authorize the hold amount
            authorization_result = StripeService.pre_authorize_payment(
                customer=customer,
                amount=hold_amount_cents,  # Pass amount in cents
                delivery_id=delivery.id
            )
            if not authorization_result.get('success'):
                return JsonResponse({
                    'success': False,
                    'error': 'Customer payment authorization failed. Please ask customer to update their payment method.',
                    'payment_error': True
                }, status=402)  # Payment Required

            # Store authorization ID and estimated cost for later capture
            delivery.payment_authorization_id = authorization_result.get('authorization_id')
            delivery.estimated_cost = estimated_total  # Store original estimated cost (before 20% buffer)

    except Exception as e:
        logger.error(f"Payment authorization failed for delivery {delivery_id}: {e}")
        return JsonResponse({
            'success': False,
            'error': 'Payment system temporarily unavailable. Please try again.',
            'payment_error': True
        }, status=503)

    # Update delivery status (only after successful payment authorization)
    from django.utils import timezone
    delivery.status = 'packing'
    delivery.packing_started_at = timezone.now()
    delivery.packed_by = request.user
    delivery.save()

    # Lock the shopping list for customer edits
    shopping_list = delivery.shopping_list
    if shopping_list:
        shopping_list.locked_for_shopping = True
        shopping_list.locked_at = timezone.now()
        shopping_list.save()
        logger.info(f"🔒 Shopping list {shopping_list.id} locked for delivery {delivery_id}")

    # Send notification to customer with estimated hold amount
    from .services.notification_service import NotificationService
    customer = delivery.subscription.customer if delivery.subscription else None
    if customer:
        # Use the already calculated hold_amount from above (no need to recalculate)
        NotificationService.send_shopping_started(
            user=customer,
            delivery_id=delivery.id,
            hold_amount=hold_amount
        )

    logger.info(f"✅ Shopper {request.user.username} started packing delivery {delivery_id}")

    return JsonResponse({
        'success': True,
        'message': 'Packing started successfully',
        'delivery': {
            'id': delivery.id,
            'status': delivery.status,
            'shopper': request.user.get_full_name() or request.user.username,
            'delivery_date': delivery.delivery_date.isoformat(),
            'packing_started_at': delivery.packing_started_at.isoformat()
        }
    })


@csrf_exempt
@require_firebase_auth
@require_approved_shopper
def mark_ready_api(request):
    """
    Mark delivery as ready for pickup and notify customer

    POST /shopright/api/shopper/mark-ready/
    Body: {
        "delivery_id": 123
    }

    Returns: {
        "success": true,
        "message": "Delivery marked ready for pickup"
    }
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    delivery_id = data.get('delivery_id')
    if not delivery_id:
        return JsonResponse({'error': 'Missing delivery_id'}, status=400)

    try:
        delivery = WeeklyDelivery.objects.get(id=delivery_id, shopper=request.user)
    except WeeklyDelivery.DoesNotExist:
        return JsonResponse({'error': 'Delivery not found or not assigned to you'}, status=404)

    # Check if delivery is in correct status
    if delivery.status != 'packing':
        return JsonResponse({
            'success': False,
            'error': f'Cannot mark ready. Delivery status is "{delivery.status}"'
        }, status=400)

    # Update delivery status
    from django.utils import timezone
    delivery.status = 'ready'
    delivery.packing_completed_at = timezone.now()
    delivery.save()

    # Skip notification for "ready" status - customer will be notified when delivery starts
    logger.info(f"✅ Shopper {request.user.username} marked delivery {delivery_id} ready for pickup (no customer notification)")

    return JsonResponse({
        'success': True,
        'message': 'Delivery marked ready for pickup',
        'delivery': {
            'id': delivery.id,
            'status': delivery.status,
            'shopper': request.user.get_full_name() or request.user.username,
            'delivery_date': delivery.delivery_date.isoformat(),
            'packing_completed_at': delivery.packing_completed_at.isoformat()
        }
    })


@csrf_exempt
@require_firebase_auth
@require_approved_shopper
def start_delivery_api(request):
    """
    API endpoint for shoppers to start delivery (out for delivery)
    Changes status from 'ready' to 'out_for_delivery'
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    delivery_id = data.get('delivery_id')
    if not delivery_id:
        return JsonResponse({'error': 'Missing delivery_id'}, status=400)

    try:
        delivery = WeeklyDelivery.objects.get(id=delivery_id, shopper=request.user)
    except WeeklyDelivery.DoesNotExist:
        return JsonResponse({'error': 'Delivery not found or not assigned to you'}, status=404)

    # Check if delivery is in correct status
    if delivery.status != 'ready':
        return JsonResponse({
            'success': False,
            'error': f'Cannot start delivery. Delivery status is "{delivery.status}"'
        }, status=400)

    # Update delivery status
    from django.utils import timezone
    delivery.status = 'out_for_delivery'
    delivery.picked_up_at = timezone.now()
    delivery.save()

    # Send notification to customer
    from .services.notification_service import NotificationService
    customer = delivery.subscription.customer if delivery.subscription else None
    if customer:
        NotificationService.send_delivery_status_update(
            user=customer,
            delivery_id=delivery.id,
            status='out_for_delivery'
        )

    logger.info(f"✅ Shopper {request.user.username} started delivery {delivery_id}")

    return JsonResponse({
        'success': True,
        'message': 'Delivery started - out for delivery',
        'delivery': {
            'id': delivery.id,
            'status': delivery.status,
            'shopper': request.user.get_full_name() or request.user.username,
            'delivery_date': delivery.delivery_date.isoformat(),
            'picked_up_at': delivery.picked_up_at.isoformat()
        }
    })


@csrf_exempt
@require_firebase_auth
@require_approved_shopper
def mark_delivered_api(request):
    """
    API endpoint for shoppers to mark delivery as delivered
    Changes status from 'out_for_delivery' to 'delivered'
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    delivery_id = data.get('delivery_id')
    if not delivery_id:
        return JsonResponse({'error': 'Missing delivery_id'}, status=400)

    try:
        delivery = WeeklyDelivery.objects.get(id=delivery_id, shopper=request.user)
    except WeeklyDelivery.DoesNotExist:
        return JsonResponse({'error': 'Delivery not found or not assigned to you'}, status=404)

    # Check if delivery is in correct status
    if delivery.status != 'out_for_delivery':
        return JsonResponse({
            'success': False,
            'error': f'Cannot mark delivered. Delivery status is "{delivery.status}"'
        }, status=400)

    # Update delivery status
    from django.utils import timezone
    delivery.status = 'delivered'
    delivery.delivered_at = timezone.now()
    delivery.save()

    # Auto-create next week's delivery for weekly subscription (before notifications)
    next_delivery_created = False
    if delivery.subscription and delivery.subscription.status == 'active':
        try:
            from datetime import timedelta

            # Double-check subscription is still active and not cancelled
            subscription = delivery.subscription
            subscription.refresh_from_db()  # Get latest status from database

            if subscription.status != 'active':
                logger.info(f"Subscription {subscription.id} is no longer active, skipping auto-creation")
            else:
                # Calculate next delivery date (add 7 days)
                current_delivery_date = delivery.delivery_date
                next_delivery_date = current_delivery_date + timedelta(days=7)

                logger.info(f"🔄 Attempting to create next week's delivery for {next_delivery_date}")

                # Check if next delivery doesn't already exist (prevent duplicates)
                existing_next_delivery = WeeklyDelivery.objects.filter(
                    subscription=subscription,
                    delivery_date=next_delivery_date
                ).first()

                if existing_next_delivery:
                    logger.info(f"Next delivery already exists for {next_delivery_date}, skipping auto-creation")
                else:
                    # Create next week's delivery
                    next_delivery = WeeklyDelivery.objects.create(
                        subscription=subscription,
                        delivery_date=next_delivery_date,
                        status='scheduled',  # New status for upcoming deliveries
                        shopping_list=delivery.shopping_list,  # Reuse same shopping list
                        shopper=request.user,  # Auto-assign to same shopper (employee)
                    )

                    logger.info(f"✅ Auto-created next week's delivery {next_delivery.id} for {next_delivery_date}")
                    next_delivery_created = True

        except Exception as e:
            logger.error(f"Failed to auto-create next delivery: {e}")
            # Don't fail the main delivery completion if auto-creation fails

    # Send notification to customer (after auto-creation)
    from .services.notification_service import NotificationService
    customer = delivery.subscription.customer if delivery.subscription else None
    if customer:
        NotificationService.send_delivery_status_update(
            user=customer,
            delivery_id=delivery.id,
            status='delivered'
        )

    logger.info(f"✅ Shopper {request.user.username} delivered delivery {delivery_id}")

    response_data = {
        'success': True,
        'message': 'Delivery completed successfully!',
        'delivery': {
            'id': delivery.id,
            'status': delivery.status,
            'shopper': request.user.get_full_name() or request.user.username,
            'delivery_date': delivery.delivery_date.isoformat(),
            'delivered_at': delivery.delivered_at.isoformat()
        }
    }

    # Add info about next week's delivery creation
    if next_delivery_created:
        response_data['next_delivery_created'] = True
        response_data['message'] = 'Delivery completed successfully! Next week\'s delivery has been scheduled.'

    return JsonResponse(response_data)


# ========================================
# LEGAL PAGES
# ========================================

def terms_of_service(request):
    """
    Display Terms of Service page.
    Required by Apple App Store for subscription apps.
    """
    return render(request, 'shopright/terms.html')


def privacy_policy(request):
    """
    Display Privacy Policy page.
    Required by Apple App Store for subscription apps.
    """
    return render(request, 'shopright/privacy.html')


def support(request):
    """
    Display Support/Help page.
    Required by Apple App Store - provides user support and FAQ.
    """
    return render(request, 'shopright/support.html')


# ========================================
# ACCOUNT MANAGEMENT
# ========================================

@csrf_exempt
@require_firebase_auth
def delete_account_api(request):
    """
    Delete user account and personal data

    DELETE /shopright/api/account/delete/

    Required by Apple App Store Guidelines 5.1.1(v)

    DATA RETENTION POLICY:
    - Personal trips (no family): DELETED
    - Family-shared trips: KEPT for family (user anonymized)
    - Personal lists (no family): DELETED
    - Family-shared lists: KEPT for family (user anonymized)
    - Subscriptions, recalls, votes: DELETED
    - User account and phone number: DELETED

    Returns: {
        "success": true,
        "message": "Account deleted successfully",
        "stats": {
            "personal_trips_deleted": 5,
            "family_trips_kept": 3,
            "personal_lists_deleted": 2,
            "family_lists_kept": 1
        }
    }
    """
    if request.method != 'DELETE':
        return JsonResponse({'error': 'Only DELETE allowed'}, status=405)

    user = request.user
    username = user.username

    logger.info(f"🗑️ Account deletion requested for user: {username}")

    try:
        stats = {
            'personal_trips_deleted': 0,
            'family_trips_kept': 0,
            'personal_lists_deleted': 0,
            'family_lists_kept': 0,
            'families_left': 0
        }

        # 1. Handle shopping trips (keep family trips, delete personal trips)
        all_trips = ShoppingTrip.objects.filter(user=user)
        for trip in all_trips:
            if trip.family:
                # Trip belongs to family - anonymize but keep it
                trip.user = None  # Remove user reference
                trip.save()
                stats['family_trips_kept'] += 1
                logger.debug(f"   Kept family trip: {trip.store_name} (anonymized)")
            else:
                # Personal trip - delete it
                trip.items.all().delete()  # Delete associated items first
                trip.delete()
                stats['personal_trips_deleted'] += 1
                logger.debug(f"   Deleted personal trip: {trip.store_name}")

        # 2. Handle shopping lists (keep family lists, delete personal lists)
        all_lists = ShoppingList.objects.filter(user=user)
        for shopping_list in all_lists:
            if shopping_list.family:
                # List belongs to family - anonymize but keep it
                shopping_list.user = None
                shopping_list.created_by = None
                shopping_list.save()
                stats['family_lists_kept'] += 1
                logger.debug(f"   Kept family list: {shopping_list.store_name} (anonymized)")
            else:
                # Personal list - delete it
                shopping_list.list_items.all().delete()  # Delete items first
                shopping_list.delete()
                stats['personal_lists_deleted'] += 1
                logger.debug(f"   Deleted personal list: {shopping_list.store_name}")

        # 3. Leave all families
        family_memberships = FamilyMember.objects.filter(user=user)
        for membership in family_memberships:
            family = membership.family
            was_owner = membership.role == 'owner'
            membership.delete()
            stats['families_left'] += 1
            logger.debug(f"   Left family: {family.name}")

            # If family is now empty, delete it
            if family.members.count() == 0:
                family_name = family.name
                family.delete()
                logger.debug(f"   Deleted empty family: {family_name}")
            # If user was owner, transfer ownership to another member
            elif was_owner and family.members.count() > 0:
                new_owner = family.members.first()
                new_owner.role = 'owner'
                new_owner.save()
                logger.debug(f"   Transferred ownership to: {new_owner.user.username}")

        # 4. Delete subscription data
        try:
            subscription = UserSubscription.objects.get(user=user)
            subscription.delete()
            logger.debug(f"   Deleted subscription data")
        except UserSubscription.DoesNotExist:
            pass

        # 5. Delete recall matches
        from shopright.models import RecallMatch
        recall_matches = RecallMatch.objects.filter(user=user)
        recall_count = recall_matches.count()
        recall_matches.delete()
        logger.debug(f"   Deleted {recall_count} recall matches")

        # 6. Delete aisle location votes
        location_votes = LocationVote.objects.filter(user=user)
        vote_count = location_votes.count()
        location_votes.delete()
        logger.debug(f"   Deleted {vote_count} location votes")

        # 7. Finally, delete the user account
        user.delete()

        logger.info(f"✅ Account deleted successfully: {username}")
        logger.info(f"   - {stats['personal_trips_deleted']} personal trips deleted")
        logger.info(f"   - {stats['family_trips_kept']} family trips kept (anonymized)")
        logger.info(f"   - {stats['personal_lists_deleted']} personal lists deleted")
        logger.info(f"   - {stats['family_lists_kept']} family lists kept (anonymized)")
        logger.info(f"   - Left {stats['families_left']} families")
        logger.info(f"   - {recall_count} recall matches deleted")
        logger.info(f"   - {vote_count} location votes deleted")

        return JsonResponse({
            'success': True,
            'message': 'Account and personal data deleted successfully',
            'stats': stats
        })

    except Exception as e:
        logger.error(f"❌ Error deleting account for {username}: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return JsonResponse({
            'error': 'Failed to delete account',
            'details': str(e)
        }, status=500)
