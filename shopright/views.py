import logging
import json
import base64
from datetime import datetime
from functools import wraps

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.models import User
from django.db import models
from firebase_admin import auth as firebase_auth
from openai import OpenAI
from django.conf import settings

from .models import (
    Family, FamilyMember, ShoppingTrip, GroceryItem,
    ShoppingList, ShoppingListItem, AisleLocation, LocationVote
)

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
    trip.receipt_image.save(
        f'receipt_{trip.id}.jpg',
        ContentFile(image_data),
        save=True
    )

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

    # Get user's family
    membership = FamilyMember.objects.filter(user=request.user).first()
    family = membership.family if membership else None

    # Save receipt image
    from django.core.files.base import ContentFile
    image_data = base64.b64decode(receipt_image_b64)

    # Create ShoppingTrip
    trip = ShoppingTrip.objects.create(
        user=request.user,
        family=family,
        store_name=store_name,
        store_location=store_location,
        items=parsed_items,
        total_amount=total_amount,
        trip_date=trip_date
    )

    # Save image with trip ID
    trip.receipt_image.save(
        f'receipt_{trip.id}.jpg',
        ContentFile(image_data),
        save=True
    )

    # Update GroceryItem master list
    _update_grocery_items(parsed_items, store_name)

    # Update shopping list
    if family:
        _update_shopping_list_from_trip(family, None, store_name, store_location, parsed_items, trip_date)
        logger.info(f"Updated shopping list for family: {family.name}")
    else:
        _update_shopping_list_from_trip(None, request.user, store_name, store_location, parsed_items, trip_date)
        logger.info(f"Updated personal shopping list for user: {request.user.username}")

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
        'shopped_by': request.user.username,
        'receipt_image_url': receipt_image_url
    }

    logger.info(f"Receipt saved: trip_id={trip.id}, store={trip.store_name}, items={len(parsed_items)}")

    return JsonResponse(response_data)


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
   Extract EACH LINE ITEM as it appears on the receipt.
   If the same product appears on multiple separate lines, include it multiple times.
   If one line shows a quantity (e.g., "3 @ $1.29"), extract it as ONE item with that quantity.

   For each line item, provide:
   - name: Product name WITHOUT size/quantity (e.g., "Horizon Organic Whole Milk" NOT "Horizon Organic Whole Milk 64oz")
   - brand: Brand name if visible (e.g., "Horizon")
   - size: Size/quantity ONLY, extracted separately from name (e.g., "64oz", "1 lb", "12 ct")
   - price: Individual UNIT price (e.g., "1.29" for an item showing "3 @ $1.29")
   - quantity: Number of units purchased (default is 1 if not specified, e.g., 3 for "3 @ $1.29")
   - category: Best-guess category (e.g., "Dairy", "Produce", "Meat", "Snacks", "Beverages", "Bakery", "Frozen", "Pantry")

   IMPORTANT GUIDELINES:
   - Keep name and size SEPARATE. Do NOT include size information in the name field.
   - If an item shows "X @ $Y.YY" on the next line, extract quantity as X and price as Y.YY (the unit price).
   - If no quantity is shown, default quantity to 1.
   - Ignore other numbers below the item line (PLU codes, UPC codes, item numbers).
   - "Eac", "Each", "EA" means the item is sold individually - do NOT include this in size.

   Examples:
   - Receipt shows "Bananas 1 lb    $2.99" -> name: "Bananas", size: "1 lb", price: "2.99", quantity: 1
   - Receipt shows "Milk Gallon Whole 64oz    $4.99" -> name: "Milk Gallon Whole", size: "64oz", price: "4.99", quantity: 1
   - Receipt shows "Avocado Large Eac    $3.87" with "3 @ $1.29" on next line:
     -> name: "Avocado Large", size: "", price: "1.29", quantity: 3
   - If receipt shows "Carrots $1.99" on two separate lines, extract as TWO separate items each with quantity: 1

3. Receipt totals:
   - total_amount: Total amount paid (final total after tax)

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
            max_tokens=2000,
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

        # Get or create store-specific grocery item
        grocery_item, created = GroceryItem.objects.get_or_create(
            name=name,
            brand=brand,
            size=size,
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

        # STEP 2A: Find or create store-specific GroceryItem
        grocery_item = GroceryItem.objects.filter(
            name__iexact=name,
            brand__iexact=brand,
            size__iexact=size,
            store_name=store_name  # Filter by store!
        ).first()

        if not grocery_item:
            # Create new store-specific grocery item
            grocery_item = GroceryItem.objects.create(
                name=name,
                brand=brand,
                size=size,
                category=category,
                store_name=store_name
            )
            logger.info(f"🆕 Created store-specific GroceryItem: {name} @ {store_name}")
        else:
            linked_count += 1

        # Increment store-specific purchase counter
        grocery_item.times_purchased += 1
        grocery_item.save(update_fields=['times_purchased'])

        # STEP 2B: Try to find existing item still in "Need to Buy" (not bought yet)
        existing_item = shopping_list.list_items.filter(
            name=name,
            brand=brand,
            size=size,
            is_checked=False  # Items still in "Need to Buy" from previous weeks
        ).first()

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
                size=size,
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

    # Get user's family (if any)
    membership = FamilyMember.objects.filter(user=request.user).first()

    if membership:
        # User has family - show ALL trips from both user AND family
        trips = ShoppingTrip.objects.filter(
            models.Q(user=request.user) | models.Q(family=membership.family)
        ).order_by('-trip_date')[:limit]
        logger.info(f"Found {trips.count()} trips for user + family {membership.family.id}")
    else:
        # User has no family - show only personal trips
        trips = ShoppingTrip.objects.filter(user=request.user).order_by('-trip_date')[:limit]
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

    return JsonResponse({
        'family': {
            'id': family.id,
            'name': family.name,
            'invite_code': family.invite_code,
            'member_count': members.count(),
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

    # Verify user has access (same family OR personal list owned by user)
    membership = FamilyMember.objects.filter(user=request.user).first()

    # Allow access if:
    # 1. It's a personal list (no family) owned by this user, OR
    # 2. It's a family list and user is in that family
    is_personal_list = lst.family is None and lst.user == request.user
    is_family_list = membership and lst.family == membership.family

    if not (is_personal_list or is_family_list):
        return JsonResponse({'error': 'Access denied'}, status=403)

    if request.method == 'GET':
        items = lst.list_items.all()

        # Enrich items with image URLs using fuzzy matching (like trip detail API)
        enriched_items = []
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

            # Only show image if item is directly linked to a grocery_item
            # No fuzzy matching during display - we only want to show images for correctly linked items
            image_url = None
            grocery_item_id = None

            if item.grocery_item and item.grocery_item.image_url:
                image_url = item.grocery_item.image_url
                grocery_item_id = item.grocery_item.id

            item_dict['image_url'] = image_url
            item_dict['grocery_item_id'] = grocery_item_id
            enriched_items.append(item_dict)

        return JsonResponse({
            'list': {
                'id': lst.id,
                'store_name': lst.store_name,
                'store_location': lst.store_location or '',
                'item_count': lst.total_count,
                'checked_count': lst.checked_count,
                'items': enriched_items
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
                            item.is_checked = item_data['is_checked']
                        if 'quantity' in item_data:
                            item.quantity = item_data['quantity']
                        item.save()
                    except ShoppingListItem.DoesNotExist:
                        continue
                else:
                    # Add new item
                    ShoppingListItem.objects.create(
                        shopping_list=lst,
                        name=item_data.get('name', ''),
                        brand=item_data.get('brand', ''),
                        size=item_data.get('size', ''),
                        price=item_data.get('price', ''),
                        category=item_data.get('category', ''),
                        quantity=item_data.get('quantity', 1),
                        is_checked=item_data.get('is_checked', True),
                        added_by=request.user
                    )

        logger.info(f"Updated shopping list {list_id}: {lst.store_name}, items={lst.total_count}")

        # Return updated list
        items = lst.list_items.all()
        return JsonResponse({
            'list': {
                'id': lst.id,
                'store_name': lst.store_name,
                'store_location': lst.store_location or '',
                'items': [
                    {
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
                    for item in items
                ]
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
        # Barcode already scanned for this store - just link it!
        logger.info(f"✅ Barcode {barcode} already exists for {store_name} (enriched by {grocery_item.first_enriched_by})")

        # Link this user's list item to existing item
        list_item.grocery_item = grocery_item
        list_item.save()

        # Count how many families could benefit (same store only)
        potential_matches = ShoppingListItem.objects.filter(
            name__icontains=grocery_item.name[:20],
            grocery_item__isnull=True,
            shopping_list__store_name=store_name
        ).exclude(shopping_list__family=list_item.shopping_list.family)
        families_helped = potential_matches.values('shopping_list__family').distinct().count()

        return JsonResponse({
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
            'enriched_by': grocery_item.first_enriched_by.username if grocery_item.first_enriched_by else 'community'
        })

    # Barcode NOT in database - fetch from API
    product_data = lookup_barcode_in_openfoodfacts(barcode)

    if not product_data.get('found'):
        return JsonResponse({
            'success': False,
            'error': 'Product not found in database. Try taking a photo instead.'
        }, status=404)

    # Create NEW store-specific grocery item (first time this barcode is scanned at this store!)
    # Use list_item.name (receipt name) so receipts can find it via exact match
    from django.utils import timezone

    # Determine brand: prefer barcode API brand, fallback to list item brand
    api_brand = product_data.get('brand', '').strip()
    item_brand = list_item.brand or ''
    final_brand = api_brand if api_brand else item_brand

    # Use get_or_create to handle case where item exists but barcode wasn't linked
    grocery_item, created = GroceryItem.objects.get_or_create(
        name=list_item.name,  # Use receipt name, not barcode API name!
        brand=final_brand,
        size=product_data.get('quantity', list_item.size),
        store_name=store_name,  # Store-specific!
        defaults={
            'category': list_item.category,
            'barcode': barcode,
            'image_url': product_data.get('image_url', ''),
            'enriched_from_barcode': True,
            'first_enriched_by': request.user,
            'first_enriched_at': timezone.now()
        }
    )

    if created:
        logger.info(f"🎉 NEW product for {store_name}: {grocery_item.name} (brand: {final_brand}, barcode {barcode}) by {request.user.username}")
    else:
        # Item existed but didn't have barcode - update it
        logger.info(f"🔗 Linking existing item to barcode: {grocery_item.name} @ {store_name} (barcode {barcode})")
        grocery_item.barcode = barcode
        if not grocery_item.image_url and product_data.get('image_url'):
            grocery_item.image_url = product_data.get('image_url', '')
        if not grocery_item.enriched_from_barcode:
            grocery_item.enriched_from_barcode = True
            grocery_item.first_enriched_by = request.user
            grocery_item.first_enriched_at = timezone.now()
        grocery_item.save()

    # Link this list item to the new grocery item
    list_item.grocery_item = grocery_item

    # Update list item with better data from API
    if product_data.get('brand'):
        list_item.brand = product_data['brand']
    list_item.save()

    # Count how many other families will benefit from this scan (same store only)
    potential_matches = ShoppingListItem.objects.filter(
        name__icontains=grocery_item.name[:20],
        grocery_item__isnull=True,
        shopping_list__store_name=store_name
    ).exclude(shopping_list__family=list_item.shopping_list.family)

    families_helped = potential_matches.values('shopping_list__family').distinct().count()

    logger.info(f"📊 Barcode scan will help {families_helped} other families at {store_name}")

    return JsonResponse({
        'success': True,
        'message': f'Product added! You helped {families_helped} other families.' if families_helped > 0 else 'Product added to database!',
        'product_data': {
            'product_name': product_data.get('product_name', ''),
            'brand': product_data.get('brand', ''),
            'image_url': product_data.get('image_url', ''),
            'quantity': product_data.get('quantity', ''),
            'categories': product_data.get('categories', ''),
            'nutrition_grade': product_data.get('nutrition_grade', '')
        },
        'grocery_item_id': grocery_item.id,
        'families_helped': families_helped,
        'already_existed': False,
        'has_image': bool(grocery_item.image_url and grocery_item.image_url.strip())
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

        # Try exact match for this store first
        grocery_item = GroceryItem.objects.filter(
            name__iexact=list_item.name,
            store_name=store_name
        ).first()

        if not grocery_item:
            # Not found - create new store-specific grocery item
            logger.info(f"📸 Creating new grocery_item for '{list_item.name}' at {store_name}")
            grocery_item = GroceryItem.objects.create(
                name=list_item.name,
                brand=list_item.brand or '',
                size=list_item.size or '',
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
