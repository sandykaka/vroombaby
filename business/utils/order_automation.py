"""
Order Automation System

Uses Playwright + cached selectors to automatically:
1. Load ordering URL and display menu
2. Add dishes to cart with user's customization selections
3. Fill delivery address popup (appears after adding items on delivery URLs)
4. Navigate to checkout page (View Cart → Checkout)
5. Auto-fill user contact/delivery information on checkout (best effort)
6. Return checkout URL with session data
7. User completes payment in iOS WebView
"""

import logging
import asyncio
import re
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime

from playwright.async_api import async_playwright, Page
from django.conf import settings

from .dish_customization import (
    DishCustomizationDiscovery,
    ai_diagnose_blocker,
    increment_blocker_success,
    load_blocker_cache
)

logger = logging.getLogger(__name__)

# Playwright timeouts
PAGE_LOAD_TIMEOUT = 15_000
ELEMENT_WAIT_TIMEOUT = 10_000
BRIEF_PAUSE = 500


async def automate_restaurant_order(
    restaurant_id: str,
    restaurant_name: str,
    ordering_url: str,
    dish_selections: List[Dict],
    user_info: Dict
) -> Dict:
    """
    Automate order placement up to checkout page using Playwright and cached selectors

    Args:
        restaurant_id: Place ID
        restaurant_name: Restaurant name (for DoorDash search)
        ordering_url: Restaurant's ordering URL
        dish_selections: List of dishes with user's customization choices
        user_info: User's delivery/contact information (auto-filled if fields are visible)

    Returns:
        {
            "success": True/False,
            "order_id": "auto_order_123",
            "dishes_added": 2,
            "checkout_url": "https://...",
            "session_cookies": [...],
            "local_storage": {...},
            "message": "Successfully navigated to checkout"
        }
    """
    order_id = f"auto_order_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    logger.info(f"🤖 Starting order automation: {order_id}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)  # Set to False to see automation
        context = await browser.new_context(
            viewport={'width': 1280, 'height': 800},
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        )
        page = await context.new_page()

        try:
            # DoorDash: Return URL for WebView (too many automation hurdles)
            if 'doordash.com' in ordering_url.lower():
                logger.info(f"🔍 DoorDash detected - returning URL for WebView")
                return {
                    "success": True,
                    "order_id": order_id,
                    "use_webview": True,
                    "checkout_url": ordering_url,
                    "message": "DoorDash requires WebView - please complete order in app"
                }

            # Load ordering page for non-DoorDash restaurants
            logger.info(f"📄 Loading ordering page: {ordering_url}")
            await page.goto(ordering_url, timeout=PAGE_LOAD_TIMEOUT, wait_until='domcontentloaded')
            await page.wait_for_timeout(BRIEF_PAUSE * 6)  # 3 seconds

            # PROACTIVE BLOCKER HANDLING: Check platform cache for known blockers
            # If we have cached address_modal solution, fill it proactively (before clicking dishes)
            logger.info(f"🔍 Checking platform cache for known blockers...")
            cached_blockers = load_blocker_cache(restaurant_id, ordering_url)

            for blocker in cached_blockers:
                if blocker.blocker_type == "address_modal" and blocker.success_count > 0:
                    logger.info(f"📦 Found cached address_modal handler (success_count={blocker.success_count}), filling proactively...")
                    filled = await fill_blocker_form(page, blocker.selectors, user_info)

                    if filled:
                        logger.info(f"✅ Proactively filled address form, waiting for menu...")
                        await page.wait_for_timeout(3000)

                        # Verify menu loaded
                        try:
                            await page.wait_for_selector(
                                "[data-testid*='menu'], [class*='menu-item'], [class*='store-item'], button:has-text('Quick Add')",
                                timeout=8000
                            )
                            logger.info(f"✅ Menu items detected after proactive fill")
                        except Exception as e:
                            logger.warning(f"⚠️  Menu items not detected after 8s: {e}")

                        await page.wait_for_timeout(2000)  # Extra pause for animations
                        logger.info(f"✅ Ready to search for dishes")
                        break  # Only fill once

            dishes_added = 0
            delivery_popup_handled = False  # Track if we've already handled the popup
            modal_mismatch_detected = False  # Track if we hit a blocking modal (restaurant closed, etc.)
            original_url = page.url  # Save original restaurant page URL BEFORE clicking anything

            # Process each dish sequentially
            for idx, dish_selection in enumerate(dish_selections):
                logger.info(f"🍔 Processing dish {idx + 1}/{len(dish_selections)}: {dish_selection['dish_name']}")

                success = await add_dish_to_cart(
                    page=page,
                    restaurant_id=restaurant_id,
                    dish_selection=dish_selection,
                    user_info=user_info  # Pass user info for address modal handling
                )

                if success:
                    dishes_added += 1
                    logger.info(f"✅ Successfully added {dish_selection['dish_name']}")

                    # Check for delivery popup after FIRST dish only (blocks subsequent dishes)
                    if not delivery_popup_handled:
                        logger.info(f"🔍 Checking for delivery address popup after first dish...")
                        await page.wait_for_timeout(BRIEF_PAUSE * 4)  # Wait for popup to appear
                        popup_filled = await fill_initial_delivery_address(page, user_info)
                        if popup_filled:
                            logger.info(f"✅ Filled delivery address popup, continuing with remaining dishes...")
                            delivery_popup_handled = True
                            await page.wait_for_timeout(BRIEF_PAUSE * 4)  # Wait after submitting address
                        else:
                            delivery_popup_handled = True  # Mark as handled even if not found

                else:
                    logger.warning(f"⚠️ Failed to add {dish_selection['dish_name']}")

                    # Check if this is a blocking modal (restaurant closed, item unavailable, etc.)
                    # If modal verification failed on FIRST dish, it's likely a blocking error
                    if idx == 0:
                        logger.error("🛑 First dish failed - likely blocking modal (restaurant closed/unavailable)")
                        logger.info(f"📍 Returning to original restaurant page: {original_url}")
                        modal_mismatch_detected = True
                        break  # Stop immediately - don't try other dishes

            if dishes_added == 0:
                logger.warning("❌ Could not add any dishes - returning current page for manual completion")

                # Extract session data so user can continue manually in WebView
                cookies = await context.cookies()
                serializable_cookies = []
                for cookie in cookies:
                    serializable_cookie = {
                        "name": cookie.get("name", ""),
                        "value": cookie.get("value", ""),
                        "domain": cookie.get("domain", ""),
                        "path": cookie.get("path", "/"),
                        "expires": cookie.get("expires", -1),
                        "httpOnly": cookie.get("httpOnly", False),
                        "secure": cookie.get("secure", False),
                        "sameSite": cookie.get("sameSite", "Lax")
                    }
                    serializable_cookies.append(serializable_cookie)

                # Get local storage
                local_storage = {}
                try:
                    local_storage_raw = await page.evaluate("() => Object.assign({}, window.localStorage)")
                    # Filter out null/None values - iOS expects [String: String]
                    local_storage = {k: str(v) for k, v in local_storage_raw.items() if v is not None}
                except Exception as e:
                    logger.warning(f"Could not extract localStorage: {e}")

                # Use original_url if we hit a modal mismatch (restaurant closed), otherwise use current page.url
                checkout_url = original_url if modal_mismatch_detected else page.url
                logger.info(f"📍 Checkout URL: {checkout_url} (original: {modal_mismatch_detected})")

                # Return current page state for manual completion
                return {
                    "success": False,
                    "order_id": order_id,
                    "dishes_added": 0,  # REQUIRED: iOS decoder needs this field
                    "message": "Some items may be unavailable. Please review and complete your order manually.",
                    "fallback_to_manual": True,
                    "checkout_url": checkout_url,
                    "session_cookies": serializable_cookies,
                    "local_storage": local_storage
                }

            # Navigate to checkout page (cart → checkout)
            logger.info(f"💳 Navigating to checkout...")
            checkout_success = await navigate_to_checkout(page)

            if not checkout_success:
                logger.warning("Failed to navigate to checkout, but continuing with current page")

            # Dismiss any popups/modals that might appear on checkout page
            await dismiss_popups(page)

            # Wait a bit after dismissing modals for page to settle
            await page.wait_for_timeout(BRIEF_PAUSE * 4)  # 2 seconds to let page settle
            logger.info("Page settled after dismissing modals")

            # Fill user contact/delivery info on checkout page (skip if login page appears)
            logger.info(f"📝 Auto-filling user information...")
            filled = await fill_user_info(page, user_info)
            if filled:
                logger.info("✅ User info auto-filled successfully")
            else:
                logger.info("ℹ️  Could not auto-fill (may be on login/CAPTCHA page - user will complete in WebView)")

            await page.wait_for_timeout(BRIEF_PAUSE * 2)

            # Extract session cookies and storage for iOS WebView
            logger.info(f"📦 Extracting session data for WebView transfer...")
            cookies = await context.cookies()
            logger.info(f"   Extracted {len(cookies)} cookies from browser")

            # Convert Playwright cookies to serializable format
            serializable_cookies = []
            for cookie in cookies:
                serializable_cookie = {
                    "name": cookie.get("name", ""),
                    "value": cookie.get("value", ""),
                    "domain": cookie.get("domain", ""),
                    "path": cookie.get("path", "/"),
                    "expires": cookie.get("expires", -1),
                    "httpOnly": cookie.get("httpOnly", False),
                    "secure": cookie.get("secure", False),
                    "sameSite": cookie.get("sameSite", "Lax")
                }
                serializable_cookies.append(serializable_cookie)
                logger.info(f"   Cookie: {serializable_cookie['name']} (domain: {serializable_cookie['domain']})")

            # Get local storage data
            local_storage = {}
            try:
                local_storage_raw = await page.evaluate("() => Object.assign({}, window.localStorage)")
                # Filter out null/None values - iOS expects [String: String]
                local_storage = {k: str(v) for k, v in local_storage_raw.items() if v is not None}
                logger.info(f"   Extracted {len(local_storage)} localStorage items (filtered nulls)")
            except Exception as e:
                logger.warning(f"Could not extract localStorage: {e}")

            # Get current URL (should be on checkout page with contact/payment form)
            checkout_url = page.url
            logger.info(f"✅ Checkout page URL: {checkout_url}")

            return {
                "success": True,
                "order_id": order_id,
                "dishes_added": dishes_added,
                "checkout_url": checkout_url,
                "session_cookies": serializable_cookies,
                "local_storage": local_storage,
                "message": f"Successfully added {dishes_added}/{len(dish_selections)} dishes and navigated to checkout!"
            }

        except Exception as e:
            logger.error(f"❌ Order automation failed: {e}", exc_info=True)
            return {
                "success": False,
                "order_id": order_id,
                "dishes_added": 0,  # REQUIRED: iOS decoder needs this field
                "message": f"Automation error: {str(e)}",
                "details": "Automation encountered an unexpected error"
            }

        finally:
            # Close browser - iOS app will handle checkout in WebView
            logger.info("🔄 Closing browser, session data transferred to iOS...")
            await browser.close()


async def fill_blocker_form(page: Page, selectors: Dict[str, str], user_info: Dict) -> bool:
    """
    Fill a blocker form (e.g., address modal) using AI-discovered selectors

    Supports two flows:
    1. Uber Eats style: address_input + autocomplete_button (smart matching)
    2. Generic forms: individual fields + submit button

    Args:
        page: Playwright page object
        selectors: Dict of field_name -> CSS selector
        user_info: User's data to fill

    Returns:
        True if form filled and submitted successfully, False otherwise
    """
    try:
        logger.info(f"📝 Filling blocker form with {len(selectors)} fields...")

        # Detect if this is Uber Eats style (autocomplete) or generic form
        is_ubereats_style = "address_input" in selectors and "autocomplete_button" in selectors

        if is_ubereats_style:
            logger.info(f"🏠 Detected Uber Eats autocomplete style")
            return await _fill_ubereats_address(page, selectors, user_info)
        else:
            logger.info(f"📋 Detected generic form style")
            return await _fill_generic_form(page, selectors, user_info)

    except Exception as e:
        logger.error(f"❌ Failed to fill blocker form: {e}")
        return False


async def _fill_ubereats_address(page: Page, selectors: Dict[str, str], user_info: Dict) -> bool:
    """Handle Uber Eats autocomplete address input"""
    try:
        # Build full address
        full_address = f"{user_info.get('address', '')}, {user_info.get('city', '')}, {user_info.get('state', '')} {user_info.get('zip', '')}"

        # Find and fill address input
        address_input = page.locator(selectors["address_input"]).first
        if await address_input.count() == 0:
            logger.warning(f"Address input not found: {selectors['address_input']}")
            return False

        await address_input.click()
        await address_input.fill(full_address)
        await address_input.type(" ")  # Trigger autocomplete
        logger.info(f"📍 Entered address: {full_address}")

        await page.wait_for_timeout(2000)  # Wait for autocomplete dropdown

        # Extract street number + name for matching (e.g., "10221 Vicksburg")
        import re
        address_parts = re.split(r'[,\s]+', full_address.strip())
        match_pattern = ' '.join(address_parts[:2]) if len(address_parts) >= 2 else address_parts[0]
        logger.info(f"🔍 Looking for autocomplete suggestion: '{match_pattern}'")

        # Try to find matching autocomplete button
        try:
            all_buttons = await page.locator(selectors["autocomplete_button"]).all()

            for button in all_buttons:
                aria_label = await button.get_attribute('aria-label')
                if aria_label and match_pattern.lower() in aria_label.lower():
                    logger.info(f"🎯 Found match: '{aria_label}'")
                    await button.click(timeout=3000)
                    await page.wait_for_timeout(5000)  # Wait for menu to reload
                    logger.info(f"✅ Clicked autocomplete suggestion")
                    return True

        except Exception as e:
            logger.warning(f"Autocomplete matching failed: {e}")

        # Fallback: Press Enter
        logger.info(f"⚠️ No matching suggestion, pressing Enter")
        await address_input.press('Enter')
        await page.wait_for_timeout(5000)
        return True

    except Exception as e:
        logger.error(f"Failed Uber Eats address fill: {e}")
        return False


async def _fill_generic_form(page: Page, selectors: Dict[str, str], user_info: Dict) -> bool:
    """Handle generic form with individual fields"""
    try:
        # Map user_info keys to common field names
        field_mapping = {
            "street_address": user_info.get("address", ""),
            "address": user_info.get("address", ""),
            "address_line_1": user_info.get("address", ""),
            "city": user_info.get("city", ""),
            "locality": user_info.get("city", ""),
            "state": user_info.get("state", ""),
            "administrative_area": user_info.get("state", ""),
            "zip": user_info.get("zip", ""),
            "postal_code": user_info.get("zip", ""),
            "zipcode": user_info.get("zip", ""),
        }

        # Fill each field
        filled_count = 0
        submit_selector = None

        for field_name, selector in selectors.items():
            # Skip submit button for now
            if "submit" in field_name.lower() or "button" in field_name.lower():
                submit_selector = selector
                continue

            # Get value to fill
            value = field_mapping.get(field_name, "")
            if not value:
                logger.warning(f"  No value for field: {field_name}")
                continue

            # Try to fill the field
            try:
                element = page.locator(selector).first
                if await element.count() > 0:
                    # Check if it's a select dropdown
                    if await element.evaluate("el => el.tagName") == "SELECT":
                        await element.select_option(value, timeout=3000)
                    else:
                        await element.fill(value, timeout=3000)

                    logger.info(f"  ✓ Filled {field_name}: {value}")
                    filled_count += 1
                else:
                    logger.warning(f"  Field not found: {selector}")
            except Exception as e:
                logger.warning(f"  Failed to fill {field_name}: {e}")

        # Click submit button if we filled at least one field
        if filled_count > 0 and submit_selector:
            try:
                submit_btn = page.locator(submit_selector).first
                if await submit_btn.count() > 0:
                    await submit_btn.click(timeout=5000)
                    logger.info(f"  ✓ Clicked submit button")
                    await page.wait_for_timeout(2000)  # Wait for form submission
                    return True
            except Exception as e:
                logger.error(f"  Failed to click submit: {e}")
                return False

        logger.warning(f"  Filled {filled_count} fields but no submit button found")
        return filled_count > 0

    except Exception as e:
        logger.error(f"Failed generic form fill: {e}")
        return False


async def _try_fill_address_modal_generic(page: Page, user_info: Dict) -> bool:
    """
    Generic function to fill address modal across different platforms

    IMPORTANT: Scopes search to modal element first to avoid clicking inputs behind the modal

    Tries multiple strategies in order:
    1. Find the modal container
    2. Find address input WITHIN the modal
    3. Fill with full address
    4. Look for autocomplete suggestions and click first match
    5. If no autocomplete, press Enter
    6. Wait for page to process

    Args:
        page: Playwright page object
        user_info: User's address data

    Returns:
        True if successfully filled address, False otherwise
    """
    try:
        # Build full address string
        full_address = f"{user_info.get('address', '')}, {user_info.get('city', '')}, {user_info.get('state', '')} {user_info.get('zip', '')}"
        logger.info(f"📍 Attempting to fill address: {full_address}")

        # CRITICAL FIX: First, find the modal container to scope our search
        modal_selectors = [
            "[role='dialog']:visible",
            ".modal:visible",
            "[class*='Modal']:visible",
            "[class*='modal']:visible",
            "[class*='dialog']:visible",
            "[class*='Dialog']:visible",
            "div[class*='overlay']:visible",
        ]

        modal_container = None
        for modal_selector in modal_selectors:
            try:
                element = page.locator(modal_selector).first
                if await element.count() > 0 and await element.is_visible():
                    modal_container = element
                    logger.info(f"✅ Found modal container: {modal_selector}")
                    break
            except:
                continue

        # If we found a modal, search WITHIN it. Otherwise, search entire page.
        search_scope = modal_container if modal_container else page

        # Strategy 1: Find any visible address input field (generic selectors)
        generic_address_selectors = [
            "input[placeholder*='address' i]",
            "input[placeholder*='location' i]",
            "input[placeholder*='deliver' i]",
            "input[aria-label*='address' i]",
            "input[name*='address' i]",
            "input[id*='address' i]",
            "input[type='text']:visible",  # Generic fallback
        ]

        address_input = None
        for selector in generic_address_selectors:
            try:
                # Search within modal container (or page if no modal found)
                element = search_scope.locator(selector).first
                if await element.count() > 0 and await element.is_visible():
                    address_input = element
                    logger.info(f"✅ Found address input in modal: {selector}")
                    break
            except:
                continue

        if not address_input:
            logger.warning(f"❌ No address input found in modal")
            return False

        # Strategy 2: Fill the address field
        await address_input.click()
        await address_input.fill(full_address)
        await address_input.type(" ")  # Trigger autocomplete
        logger.info(f"✅ Filled address field")

        # Wait for autocomplete dropdown to appear
        await page.wait_for_timeout(2000)

        # Strategy 3: Try to click autocomplete suggestion
        generic_autocomplete_selectors = [
            "[role='option']:visible",
            "li[role='option']:visible",
            "div[role='option']:visible",
            "button[aria-label*='address' i]:visible",
            "button[aria-label*='search result' i]:visible",
            "[class*='suggestion']:visible",
            "[class*='autocomplete']:visible",
            "[data-testid*='address']:visible",
            "[data-testid*='suggestion']:visible",
        ]

        clicked_suggestion = False
        for selector in generic_autocomplete_selectors:
            try:
                suggestion = page.locator(selector).first
                if await suggestion.count() > 0 and await suggestion.is_visible():
                    suggestion_text = await suggestion.inner_text()
                    logger.info(f"🎯 Clicking autocomplete suggestion: '{suggestion_text[:50]}...'")
                    await suggestion.click(timeout=3000)
                    clicked_suggestion = True
                    break
            except:
                continue

        # Strategy 4: If no autocomplete clicked, press Enter
        if not clicked_suggestion:
            logger.info(f"⚠️ No autocomplete found, pressing Enter")
            await address_input.press('Enter')

        # Strategy 5: Wait for page to process address (network idle)
        logger.info(f"⏳ Waiting for page to process address...")
        await page.wait_for_timeout(3000)

        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
            logger.info(f"✅ Page loaded successfully")
        except:
            logger.warning(f"⚠️ Network idle timeout, but continuing...")

        # Look for submit/continue button if modal is still open
        submit_selectors = [
            "button:has-text('Continue')",
            "button:has-text('Submit')",
            "button:has-text('Deliver here')",
            "button:has-text('Confirm')",
            "button:has-text('Next')",
            "button[type='submit']:visible",
        ]

        for submit_selector in submit_selectors:
            try:
                submit_btn = page.locator(submit_selector).first
                if await submit_btn.count() > 0 and await submit_btn.is_visible():
                    await submit_btn.click(timeout=3000)
                    logger.info(f"✅ Clicked submit button: {submit_selector}")
                    await page.wait_for_timeout(2000)
                    break
            except:
                continue

        logger.info(f"✅ Generic address fill completed")
        return True

    except Exception as e:
        logger.error(f"❌ Generic address fill failed: {e}")
        return False


async def add_dish_to_cart(
    page: Page,
    restaurant_id: str,
    dish_selection: Dict,
    user_info: Dict = None
) -> bool:
    """
    Add a single dish to cart with user's customization selections

    NEW: Uses adaptive retry with AI blocker diagnosis for any unexpected modals

    Steps:
    1. Find and click the dish
    2. Wait for customization modal
    3. If wrong modal appears, diagnose with AI and handle blocker
    4. Apply user's selections using cached selectors
    5. Click "Add to Cart"
    """
    dish_name = dish_selection['dish_name']
    group_selections = dish_selection.get('group_selections', [])
    special_instructions = dish_selection.get('special_instructions')
    quantity = dish_selection.get('quantity', 1)

    # Allow up to 2 retries (1 initial + 1 after handling blocker)
    max_retries = 2

    for attempt in range(max_retries):
        try:
            # Step 1: Load cached customization data (includes selectors)
            discovery = DishCustomizationDiscovery()
            customization = discovery.get_cached_customization(restaurant_id, dish_name)

            selector_cache = None
            if customization and customization.selector_cache:
                selector_cache = customization.selector_cache
                logger.info(f"📦 Loaded selector cache for {dish_name}")
            else:
                logger.warning(f"⚠️  No cached selectors for {dish_name}, will use live search/click")

            # Step 2: Find and click the dish (reuse from DishCustomizationDiscovery)
            discovery_temp = DishCustomizationDiscovery()
            clicked = await discovery_temp._find_and_click_dish(page, dish_name)
            if not clicked:
                logger.error(f"Could not find/click {dish_name}")
                return False

            await page.wait_for_timeout(BRIEF_PAUSE * 4)  # Wait for modal to load

            # Step 2b: Verify the modal is for the correct dish
            modal_verified = await discovery_temp._verify_customization_modal(page, dish_name)
            if not modal_verified:
                logger.warning(f"⚠️ Modal opened but doesn't match {dish_name}")

                # STRATEGY: When modal doesn't match, it's likely a blocker
                # Try to handle common blockers generically:
                # 1. Check for address modal → try to fill OR dismiss
                # 2. Check for login modal → dismiss (can't automate)
                # 3. Otherwise → dismiss and retry

                html = await page.content()
                current_url = page.url

                # Quick heuristic: Check for address modal keywords
                html_lower = html.lower()
                address_keywords = [
                    'enter your address',
                    'delivery address',
                    'enter address to',
                    'check availability',
                    'add an address',
                    'where would you like this delivered',
                    'location-typeahead'  # Uber Eats specific
                ]
                is_address_modal = any(keyword in html_lower for keyword in address_keywords)

                if is_address_modal:
                    logger.info(f"🎯 Detected address blocker modal")

                    # Check if user has address info
                    has_address = (
                        user_info and
                        user_info.get('address') and
                        user_info.get('city') and
                        user_info.get('state') and
                        user_info.get('zip')
                    )

                    if has_address:
                        logger.info(f"📍 Attempting to fill address modal...")

                        # Try generic address fill approaches
                        filled = await _try_fill_address_modal_generic(page, user_info)

                        if filled:
                            logger.info(f"✅ Address filled, waiting for page reload...")

                            # Wait for page to reload with new address
                            await page.wait_for_timeout(5000)

                            # Wait for network idle
                            try:
                                await page.wait_for_load_state("networkidle", timeout=10000)
                                logger.info(f"✅ Page reloaded - ready to retry")
                            except Exception:
                                pass

                            continue  # Retry dish addition
                        else:
                            logger.warning(f"⚠️ Could not fill address modal - dismissing")
                    else:
                        logger.warning(f"⚠️ Address modal detected but user_info incomplete")
                        if not user_info:
                            logger.warning(f"   No user_info provided")
                        else:
                            missing = []
                            if not user_info.get('address'): missing.append('address')
                            if not user_info.get('city'): missing.append('city')
                            if not user_info.get('state'): missing.append('state')
                            if not user_info.get('zip'): missing.append('zip')
                            logger.warning(f"   Missing fields: {', '.join(missing)}")

                # Try to dismiss modal (press Escape or click close button)
                logger.info(f"🚫 Dismissing blocker modal...")
                try:
                    # Try escape key first
                    await page.keyboard.press('Escape')
                    await page.wait_for_timeout(500)

                    # If modal still visible, try close button
                    close_selectors = [
                        "button[aria-label*='close' i]",
                        "button[aria-label*='dismiss' i]",
                        "button:has-text('×')",
                        "button:has-text('✕')",
                        "[role='dialog'] button[aria-label*='close' i]"
                    ]
                    for selector in close_selectors:
                        try:
                            close_btn = page.locator(selector).first
                            if await close_btn.count() > 0:
                                await close_btn.click(timeout=1000)
                                logger.info(f"✅ Clicked close button: {selector}")
                                break
                        except:
                            pass
                except Exception as e:
                    logger.warning(f"⚠️ Could not dismiss modal: {e}")

                await page.wait_for_timeout(BRIEF_PAUSE)
                return False

            # Modal verified! Proceed with customization

            # Step 3: Set quantity if needed
            if quantity > 1:
                logger.info(f"Setting quantity to {quantity}...")

                # Try different quantity selector patterns
                quantity_set = False

                # Method 1: Click + button (quantity - 1) times
                plus_button_selectors = [
                    "button:has(img[alt*='Increase' i])",
                    "button:has(img[alt*='quantity' i])",
                    "button[aria-label*='increase' i]",
                    "button[aria-label*='plus' i]",
                    "button:has-text('+')",
                    "[class*='increment']",
                    "[class*='plus']",
                    "[data-testid*='increase']",
                    "[data-testid*='plus']"
                ]

                for plus_selector in plus_button_selectors:
                    try:
                        plus_btn = page.locator(plus_selector).first
                        if await plus_btn.count() > 0 and await plus_btn.is_visible():
                            for i in range(quantity - 1):
                                await plus_btn.click(timeout=2000)
                                await page.wait_for_timeout(300)
                            logger.info(f"✓ Set quantity to {quantity} using + button")
                            quantity_set = True
                            break
                    except:
                        continue

                # Method 2: Select from dropdown
                if not quantity_set and selector_cache and 'form_fields' in selector_cache:
                    quantity_selector = selector_cache['form_fields'].get('quantity')
                    if quantity_selector:
                        try:
                            await page.select_option(quantity_selector, str(quantity), timeout=3000)
                            logger.info(f"✓ Set quantity to {quantity} using dropdown")
                            quantity_set = True
                        except:
                            pass

                # Method 3: Fill text input (fallback)
                if not quantity_set and selector_cache and 'form_fields' in selector_cache:
                    quantity_selector = selector_cache['form_fields'].get('quantity')
                    if quantity_selector:
                        try:
                            await page.fill(quantity_selector, str(quantity), timeout=3000)
                            logger.info(f"✓ Set quantity to {quantity} using text field")
                            quantity_set = True
                        except:
                            pass

                if not quantity_set:
                    logger.warning(f"⚠️ Could not set quantity to {quantity}, using default")

            # Step 4: Apply user's customization selections
            if selector_cache and group_selections:
                for group_selection in group_selections:
                    label = group_selection['label']
                    selected_options = group_selection['selected_options']

                    for option_name in selected_options:
                        selector = selector_cache.get('option_selectors', {}).get(option_name)
                        if selector:
                            if '[...]' in selector or selector.count('[') != selector.count(']'):
                                logger.warning(f"Skipping invalid selector for '{option_name}': {selector}")
                                continue

                            try:
                                await page.click(selector, timeout=3000)
                                logger.info(f"✓ Selected '{option_name}' for '{label}'")
                                await page.wait_for_timeout(BRIEF_PAUSE)
                            except Exception as e:
                                logger.warning(f"Could not click '{option_name}': {e}")
                        else:
                            logger.warning(f"No selector found for option '{option_name}'")
            elif group_selections:
                logger.info(f"ℹ️  Skipping customization selections (no cached selectors)")

            # Step 5: Fill special instructions
            if special_instructions and selector_cache and 'form_fields' in selector_cache:
                instructions_selector = selector_cache['form_fields'].get('special_instructions')
                if instructions_selector:
                    try:
                        await page.fill(instructions_selector, special_instructions, timeout=3000)
                        logger.info(f"✓ Added special instructions")
                    except:
                        logger.warning("Could not fill special instructions")

            # Step 6: Click "Add to Cart"
            add_to_cart_clicked = False

            # Try cached selector first
            if selector_cache:
                add_to_cart_selector = selector_cache.get('action_buttons', {}).get('add_to_cart')
                if add_to_cart_selector:
                    try:
                        # Try normal click first
                        await page.click(add_to_cart_selector, timeout=5000)
                        logger.info(f"✓ Clicked 'Add to Cart' (cached selector)")
                        await page.wait_for_timeout(BRIEF_PAUSE * 3)
                        add_to_cart_clicked = True
                    except Exception as e:
                        # If blocked by overlay, try force click
                        if "intercepts pointer events" in str(e):
                            logger.info(f"🔨 Overlay detected, forcing click through...")
                            try:
                                await page.click(add_to_cart_selector, timeout=5000, force=True)
                                logger.info(f"✓ Force-clicked 'Add to Cart' (bypassed overlay)")
                                await page.wait_for_timeout(BRIEF_PAUSE * 3)
                                add_to_cart_clicked = True
                            except Exception as force_error:
                                logger.warning(f"Force click also failed: {force_error}, trying generic selectors...")
                        else:
                            logger.warning(f"Cached selector failed: {e}, trying generic selectors...")

            # Fallback: Try generic selectors
            if not add_to_cart_clicked:
                generic_add_to_cart_selectors = [
                    "button:has-text('Add to Cart')",
                    "button:has-text('Add to cart')",
                    "button:has-text('Add')",
                    "button[data-testid*='add-to-cart']",
                    "button[aria-label*='Add to cart' i]",
                    ".add-to-cart",
                    "button:has-text('Submit')",
                    "button:has-text('Done')"
                ]

                for selector in generic_add_to_cart_selectors:
                    try:
                        button = page.locator(selector).first
                        if await button.count() > 0 and await button.is_visible():
                            try:
                                # Try normal click first
                                await button.click(timeout=5000)
                                logger.info(f"✓ Clicked 'Add to Cart' (generic selector: {selector})")
                                await page.wait_for_timeout(BRIEF_PAUSE * 3)
                                add_to_cart_clicked = True
                                break
                            except Exception as click_error:
                                # If blocked by overlay, try force click
                                if "intercepts pointer events" in str(click_error):
                                    logger.info(f"🔨 Overlay detected on generic selector, forcing click...")
                                    await button.click(timeout=5000, force=True)
                                    logger.info(f"✓ Force-clicked 'Add to Cart' (generic: {selector})")
                                    await page.wait_for_timeout(BRIEF_PAUSE * 3)
                                    add_to_cart_clicked = True
                                    break
                                else:
                                    raise  # Re-raise if not an overlay issue
                    except:
                        continue

            if not add_to_cart_clicked:
                logger.error("Could not find/click 'Add to Cart' button")
                return False

            # Step 7: Check if delivery address modal appeared after clicking "Add to Cart"
            # Many restaurants ask for delivery address AFTER user clicks add to cart (not before)
            logger.info(f"⏳ Waiting for possible post-add delivery address modal...")
            await page.wait_for_timeout(BRIEF_PAUSE * 4)  # 2 seconds for modal to appear

            # Check if address modal appeared using platform cache
            cached_blockers = load_blocker_cache(restaurant_id, page.url)
            for blocker in cached_blockers:
                if blocker.blocker_type == "address_modal" and blocker.success_count > 0:
                    # Check if modal is actually visible
                    try:
                        # Look for common address input patterns
                        address_inputs = [
                            "input[placeholder*='address' i]",
                            "input[aria-label*='address' i]",
                            "input[name*='address' i]"
                        ]

                        modal_visible = False
                        for input_selector in address_inputs:
                            input_count = await page.locator(input_selector).count()
                            if input_count > 0:
                                modal_visible = True
                                break

                        if modal_visible and user_info:
                            logger.info(f"📍 Post-add delivery address modal detected, filling...")
                            filled = await fill_blocker_form(page, blocker.selectors, user_info)
                            if filled:
                                logger.info(f"✅ Filled post-add address modal")
                                await page.wait_for_timeout(BRIEF_PAUSE * 4)  # Wait for item to be added
                                break
                        else:
                            logger.info(f"ℹ️ No address modal detected after add to cart")
                    except Exception as modal_error:
                        logger.warning(f"Error checking post-add modal: {modal_error}")

            # Success! Break out of retry loop
            return True

        except Exception as e:
            logger.error(f"Error in attempt {attempt + 1}: {e}", exc_info=True)
            if attempt < max_retries - 1:
                logger.info(f"Retrying...")
                continue
            else:
                return False

    # If we get here, all retries failed
    return False


async def navigate_to_checkout(page: Page) -> bool:
    """Navigate from menu → cart → checkout page"""
    try:
        # Step 1: Click "View Cart" button (opens cart sidebar/page)
        cart_selectors = [
            "button:has-text('View Cart')",
            "button:has-text('Cart')",
            "a:has-text('View Cart')",
            "a:has-text('Cart')",
            "[data-testid='cart-button']",
            ".cart-button",
            "[class*='cart']",
            "[aria-label*='cart' i]"
        ]

        cart_clicked = False
        for selector in cart_selectors:
            try:
                button = page.locator(selector).first
                if await button.count() > 0:
                    await button.click(timeout=5000)
                    # No wait needed here - just click and move on
                    logger.info(f"✓ Clicked cart button: {selector}")
                    cart_clicked = True
                    break
            except:
                continue

        if not cart_clicked:
            logger.info("No cart button found, assuming already on cart page")

        # Step 2: Click "Checkout" button (navigates to checkout page)
        checkout_selectors = [
            "button:has-text('Checkout')",
            "button:has-text('Check out')",
            "a:has-text('Checkout')",
            "a:has-text('Check out')",
            "button:has-text('Continue')",
            "button:has-text('Proceed to Checkout')",
            "[data-testid='checkout-button']",
            ".checkout-button",
            "[class*='checkout']",
            "[aria-label*='checkout' i]"
        ]

        for selector in checkout_selectors:
            try:
                button = page.locator(selector).first
                if await button.count() > 0:
                    # Click and wait for navigation to complete
                    logger.info(f"Clicking checkout button: {selector}")
                    await button.click(timeout=5000)

                    # Wait for page to navigate to checkout URL
                    await page.wait_for_timeout(BRIEF_PAUSE * 6)  # 3 seconds for navigation

                    # Additional wait for checkout page to load
                    try:
                        await page.wait_for_load_state('networkidle', timeout=5000)
                    except:
                        # If networkidle times out, still proceed
                        pass

                    logger.info(f"✓ Navigated to checkout page")
                    return True
            except:
                continue

        logger.warning("Could not find checkout button")
        return False

    except Exception as e:
        logger.error(f"Error navigating to checkout: {e}")
        return False


async def dismiss_popups(page: Page):
    """Dismiss any popups or modals that appear (email signup, tips, offers, etc.)"""
    try:
        # Common dismissal button selectors
        dismissal_selectors = [
            "button:has-text('No thanks')",
            "button:has-text('No Thanks')",
            "button:has-text('Not now')",
            "button:has-text('Maybe later')",
            "button:has-text('Skip')",
            "button:has-text('Close')",
            "button:has-text('Continue')",
            "button[aria-label='Close']",
            "button[aria-label='close' i]",
            "[class*='close']",
            "[class*='dismiss']",
            "button.close",
            "[data-testid='close-button']",
            "[data-testid='dismiss-button']"
        ]

        # Wait a moment for any popup to appear
        await page.wait_for_timeout(BRIEF_PAUSE * 2)

        # Try to dismiss any visible popups
        dismissed_count = 0
        for selector in dismissal_selectors:
            try:
                button = page.locator(selector).first
                if await button.count() > 0 and await button.is_visible():
                    await button.click(timeout=3000)
                    await page.wait_for_timeout(BRIEF_PAUSE)
                    logger.info(f"✓ Dismissed popup with: {selector}")
                    dismissed_count += 1
                    break  # Only dismiss one popup at a time
            except:
                continue

        if dismissed_count == 0:
            logger.info("No popups to dismiss")

    except Exception as e:
        logger.warning(f"Error dismissing popups: {e}")


async def fill_initial_delivery_address(page: Page, user_info: Dict) -> bool:
    """
    Fill delivery address popup that appears AFTER adding items to cart
    (Common on delivery URLs - popup appears before checkout)
    """
    try:
        # Common address popup selectors (appears on delivery URLs)
        address_popup_selectors = [
            "input[aria-label='Search for address']",
            "input[placeholder*='Enter your address' i]",
            "input[placeholder*='delivery address' i]",
            "input[placeholder*='Where should we deliver' i]",
            "input[placeholder*='Enter address' i]",
            "input[name='deliveryAddress']",
            "input[id*='delivery-address' i]"
        ]

        full_address = f"{user_info.get('address', '')}, {user_info.get('city', '')}, {user_info.get('state', '')} {user_info.get('zip', '')}"

        # Try to find and fill the initial address popup
        for selector in address_popup_selectors:
            try:
                field = page.locator(selector).first
                if await field.count() > 0 and await field.is_visible():
                    logger.info(f"📍 Found initial address popup: {selector}")
                    await field.fill(full_address, timeout=3000)
                    await page.wait_for_timeout(BRIEF_PAUSE * 2)  # Wait for autocomplete suggestions

                    # Click first address suggestion from dropdown
                    suggestion_selectors = [
                        "button[aria-label*='search result']:first-child",  # Matches "search result: 10221 Vicksburg Drive..."
                        "button[role='button'][aria-label*='search result']",
                        "button:has-text('Deliver to')",  # Common pattern: "Deliver to 123 Main St"
                        "li[role='option']:first-child",
                        "div[role='option']:first-child",
                        "[class*='suggestion']:first-child",
                        "[class*='autocomplete']:first-child button:first-child",
                        "[class*='address-option']:first-child",
                        "ul li:first-child",
                        "[data-testid='address-suggestion']:first-child"
                    ]

                    clicked_suggestion = False
                    for suggestion_selector in suggestion_selectors:
                        try:
                            suggestion = page.locator(suggestion_selector).first
                            if await suggestion.count() > 0 and await suggestion.is_visible():
                                await suggestion.click(timeout=3000)
                                logger.info(f"✓ Clicked first address suggestion: {suggestion_selector}")
                                clicked_suggestion = True
                                break
                        except:
                            continue

                    if not clicked_suggestion:
                        # Fallback: just press Enter
                        await field.press('Enter')
                        logger.info(f"✓ Pressed Enter to submit address (no suggestion found)")

                    # Wait for address to be selected and continue button to appear
                    await page.wait_for_timeout(BRIEF_PAUSE * 4)  # 2 seconds for continue button

                    # Click submit/continue button (appears after selecting address)
                    submit_selectors = [
                        "button:has-text('Continue')",
                        "button:has-text('Submit')",
                        "button:has-text('Deliver here')",
                        "button:has-text('Confirm')",
                        "button:has-text('Next')",
                        "button[type='submit']",
                        "[data-testid='continue-button']",
                        "[data-testid='submit-button']"
                    ]

                    for submit_selector in submit_selectors:
                        try:
                            submit_btn = page.locator(submit_selector).first
                            if await submit_btn.count() > 0 and await submit_btn.is_visible():
                                await submit_btn.click(timeout=3000)
                                logger.info(f"✓ Clicked continue button: {submit_selector}")
                                break
                        except:
                            continue

                    # Wait for any additional options/selections to appear
                    await page.wait_for_timeout(BRIEF_PAUSE * 4)  # 2 seconds

                    # Click through any additional option screens (select first/default option)
                    # Common patterns: delivery instructions, tip options, utensils, etc.
                    additional_continue_selectors = [
                        "button:has-text('Continue')",
                        "button:has-text('Skip')",
                        "button:has-text('No thanks')",
                        "button:has-text('Next')",
                        "button:has-text('Confirm')"
                    ]

                    for continue_selector in additional_continue_selectors:
                        try:
                            continue_btn = page.locator(continue_selector).first
                            if await continue_btn.count() > 0 and await continue_btn.is_visible():
                                await continue_btn.click(timeout=3000)
                                logger.info(f"✓ Clicked additional continue: {continue_selector}")
                                await page.wait_for_timeout(BRIEF_PAUSE * 2)  # Wait after clicking
                                break
                        except:
                            continue

                    return True
            except:
                continue

        # No address popup found
        return False

    except Exception as e:
        logger.warning(f"Error filling initial delivery address: {e}")
        return False


async def fill_user_info(page: Page, user_info: Dict) -> bool:
    """Auto-fill user contact and delivery information on checkout page (best effort)"""
    try:
        # Address field (search for address autocomplete)
        address_selectors = [
            "input[aria-label='Search for address']",
            "input[placeholder*='address' i]",
            "input[name*='address' i]",
            "input[id*='address' i]",
            "#address"
        ]

        full_address = f"{user_info.get('address', '')}, {user_info.get('city', '')}, {user_info.get('state', '')} {user_info.get('zip', '')}"

        for selector in address_selectors:
            try:
                field = page.locator(selector).first
                if await field.count() > 0 and await field.is_visible():
                    await field.fill(full_address, timeout=3000)
                    await field.press('Enter')  # Trigger autocomplete
                    await page.wait_for_timeout(1000)
                    logger.info(f"✓ Filled address: {full_address}")
                    break
            except:
                continue

        # Name fields
        first_name_selectors = [
            "input[name='firstName']",
            "input[name='firstname']",
            "input[placeholder*='first name' i]",
            "#firstName",
            "#firstname"
        ]
        last_name_selectors = [
            "input[name='lastName']",
            "input[name='lastname']",
            "input[placeholder*='last name' i]",
            "#lastName",
            "#lastname"
        ]

        for selector in first_name_selectors:
            try:
                field = page.locator(selector).first
                if await field.count() > 0 and await field.is_visible():
                    await field.fill(user_info.get('first_name', ''), timeout=3000)
                    logger.info(f"✓ Filled first name")
                    break
            except:
                continue

        for selector in last_name_selectors:
            try:
                field = page.locator(selector).first
                if await field.count() > 0 and await field.is_visible():
                    await field.fill(user_info.get('last_name', ''), timeout=3000)
                    logger.info(f"✓ Filled last name")
                    break
            except:
                continue

        # Email
        email_selectors = [
            "input[type='email']",
            "input[name='email']",
            "input[placeholder*='email' i]",
            "#email"
        ]
        for selector in email_selectors:
            try:
                field = page.locator(selector).first
                if await field.count() > 0 and await field.is_visible():
                    await field.fill(user_info.get('email', ''), timeout=3000)
                    logger.info(f"✓ Filled email")
                    break
            except:
                continue

        # Phone
        phone_selectors = [
            "input[type='tel']",
            "input[name='phone']",
            "input[name='phoneNumber']",
            "input[placeholder*='phone' i]",
            "#phone"
        ]
        for selector in phone_selectors:
            try:
                field = page.locator(selector).first
                if await field.count() > 0 and await field.is_visible():
                    await field.fill(user_info.get('phone', ''), timeout=3000)
                    logger.info(f"✓ Filled phone")
                    break
            except:
                continue

        return True

    except Exception as e:
        logger.error(f"Error filling user info: {e}")
        return False
