"""
Dish Customization Discovery System

Discovers and caches customization options for restaurant dishes
using Playwright + OpenAI vision analysis.

Flow:
1. User selects dishes → API call triggers discovery
2. Backend clicks each dish → screenshots customization screen
3. AI extracts options (size, add-ons, special instructions)
4. Cache results for future orders
5. Return structured data to frontend
"""

import json
import logging
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict
from urllib.parse import urlparse

from django.conf import settings
from playwright.async_api import async_playwright, Page
from openai import OpenAI

logger = logging.getLogger(__name__)

# Cache directory (same structure as menu/reviews)
CACHE_DIR = Path(getattr(settings, "REVIEWS_CACHE_DIR", Path(settings.BASE_DIR) / "var" / "reviews"))

# Cache TTL - customizations don't change often
CUSTOMIZATION_CACHE_DAYS = 90

# Playwright timeouts
PAGE_LOAD_TIMEOUT = 15_000
ELEMENT_WAIT_TIMEOUT = 10_000
BRIEF_PAUSE = 500


@dataclass
class CustomizationGroup:
    """A group of related customization options (e.g., 'How would you like it cooked?')"""
    label: str  # Question/section text from website
    type: str  # 'single_choice' (radio) or 'multi_choice' (checkbox)
    required: bool  # Whether user must select something
    options: List[Dict[str, Optional[str]]]  # [{"name": "Rare", "price": None}, {"name": "Cajun Fries", "price": "+$2.50"}]


@dataclass
class DishCustomization:
    """Customization options for a single dish"""
    dish_name: str
    restaurant_id: str

    # Customization groups (preserves website structure)
    groups: List[CustomizationGroup]
    special_instructions_allowed: bool

    # Metadata
    cached_at: datetime
    selector_cache: Optional[Dict[str, str]] = None  # Cached selectors for this dish

    def is_stale(self) -> bool:
        """Check if cache is stale"""
        return datetime.now() - self.cached_at > timedelta(days=CUSTOMIZATION_CACHE_DAYS)

    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization"""
        data = {
            'dish_name': self.dish_name,
            'restaurant_id': self.restaurant_id,
            'groups': [
                {
                    'label': g.label,
                    'type': g.type,
                    'required': g.required,
                    'options': g.options
                }
                for g in self.groups
            ],
            'special_instructions_allowed': self.special_instructions_allowed,
            'cached_at': self.cached_at.isoformat(),
            'selector_cache': self.selector_cache
        }
        return data

    @classmethod
    def from_dict(cls, data: dict) -> 'DishCustomization':
        """Create from dict (deserialization)"""
        groups = [
            CustomizationGroup(
                label=g['label'],
                type=g['type'],
                required=g['required'],
                options=g['options']
            )
            for g in data.get('groups', [])
        ]
        return cls(
            dish_name=data['dish_name'],
            restaurant_id=data['restaurant_id'],
            groups=groups,
            special_instructions_allowed=data['special_instructions_allowed'],
            cached_at=datetime.fromisoformat(data['cached_at']),
            selector_cache=data.get('selector_cache')
        )


class DishCustomizationDiscovery:
    """Handles dish customization discovery and caching"""

    def __init__(self):
        self.openai_client = OpenAI(api_key=settings.OPENAI_API_KEY)

    def _get_cache_path(self, restaurant_id: str, dish_name: str) -> Path:
        """Get cache file path for a specific dish"""
        # Sanitize dish name for filename
        safe_name = "".join(c if c.isalnum() or c in ('-', '_') else '_' for c in dish_name.lower())
        restaurant_dir = CACHE_DIR / restaurant_id
        restaurant_dir.mkdir(parents=True, exist_ok=True)
        return restaurant_dir / f"customization_{safe_name}.json"

    def get_cached_customization(self, restaurant_id: str, dish_name: str) -> Optional[DishCustomization]:
        """Get cached customization if available and fresh"""
        cache_path = self._get_cache_path(restaurant_id, dish_name)

        if not cache_path.exists():
            logger.info(f"No cache found for {dish_name} at {restaurant_id}")
            return None

        try:
            with open(cache_path, 'r') as f:
                data = json.load(f)

            customization = DishCustomization.from_dict(data)

            if customization.is_stale():
                logger.info(f"Cache stale for {dish_name} at {restaurant_id}")
                return None

            logger.info(f"Cache hit for {dish_name} at {restaurant_id}")
            return customization

        except Exception as e:
            logger.error(f"Error loading cache for {dish_name}: {e}")
            return None

    def save_customization(self, customization: DishCustomization):
        """Save customization to cache"""
        cache_path = self._get_cache_path(customization.restaurant_id, customization.dish_name)

        try:
            with open(cache_path, 'w') as f:
                json.dump(customization.to_dict(), f, indent=2)
            logger.info(f"Cached customization for {customization.dish_name}")
        except Exception as e:
            logger.error(f"Error saving cache for {customization.dish_name}: {e}")

    async def discover_customizations(
        self,
        restaurant_id: str,
        ordering_url: str,
        dishes: List[str],
        delivery_address: Optional[str] = None,
        restaurant_location: Optional[str] = None  # Fallback for address prompt
    ) -> Dict[str, DishCustomization]:
        """
        Discover customizations for multiple dishes

        Args:
            restaurant_id: Place ID or unique restaurant identifier
            ordering_url: Restaurant's online ordering URL
            dishes: List of dish names to discover customizations for

        Returns:
            Dict mapping dish_name -> DishCustomization
        """
        results = {}

        # Check cache first for each dish
        uncached_dishes = []
        for dish_name in dishes:
            cached = self.get_cached_customization(restaurant_id, dish_name)
            if cached:
                results[dish_name] = cached
            else:
                uncached_dishes.append(dish_name)

        if not uncached_dishes:
            logger.info(f"All dishes cached for {restaurant_id}")
            return results

        # Discover uncached dishes using Playwright
        logger.info(f"Discovering customizations for {len(uncached_dishes)} dishes: {uncached_dishes}")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)  # Set to False to see browser
            context = await browser.new_context(
                viewport={'width': 1280, 'height': 800},
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
            )
            page = await context.new_page()

            try:
                # DoorDash: Return empty (will handle in WebView due to automation hurdles)
                if 'doordash.com' in ordering_url.lower():
                    logger.info(f"🔍 DoorDash detected - skipping automation, will use WebView")
                    logger.info(f"ℹ️  DoorDash requires WebView - no customizations cached")
                    # Return empty results - iOS will show WebView for ordering
                    return results

                # Load ordering page for non-DoorDash restaurants
                logger.info(f"Loading ordering page: {ordering_url}")
                await page.goto(ordering_url, timeout=PAGE_LOAD_TIMEOUT, wait_until='domcontentloaded')
                await page.wait_for_timeout(BRIEF_PAUSE * 6)  # 3 seconds

                # Handle Uber Eats address prompt (must do this first!)
                if 'ubereats.com' in ordering_url.lower() or 'uber.com' in ordering_url.lower():
                    logger.info(f"🏠 Handling Uber Eats address prompt...")
                    await self._handle_uber_eats_address_prompt(page, delivery_address, restaurant_location)
                    # IMPORTANT: Extra wait after address selection for page to fully settle
                    logger.info(f"⏳ Waiting 3s for menu to fully load after address selection...")
                    await page.wait_for_timeout(3000)
                    logger.info(f"✅ Ready to search for dishes")

                # Process each dish
                for dish_name in uncached_dishes:
                    try:
                        customization = await self._discover_single_dish(
                            page, restaurant_id, dish_name
                        )

                        # Always cache results, even if no customizations found
                        # This prevents expensive AI re-discovery for dishes with no options
                        if customization:
                            results[dish_name] = customization
                            self.save_customization(customization)

                            if customization.groups:
                                logger.info(f"✅ Successfully discovered customizations for {dish_name} ({len(customization.groups)} groups)")
                            else:
                                logger.info(f"✅ Cached {dish_name} with no customizations (avoids future AI calls)")

                    except Exception as e:
                        logger.error(f"Error discovering {dish_name}: {e}")
                        # Don't save on exception - let it retry next time

            finally:
                await browser.close()

        return results

    async def _discover_single_dish(
        self,
        page: Page,
        restaurant_id: str,
        dish_name: str
    ) -> DishCustomization:
        """
        Discover customizations for a single dish

        Steps (matching menu extraction pattern):
        1. Find and click the dish
        2. Wait for customization screen
        3. Scroll to load all content (like menu extraction scrolling)
        4. Get HTML content
        5. Clean and send to AI (same as menu extraction)
        6. Return structured data
        """
        logger.info(f"Discovering customizations for: {dish_name}")

        # Step 1: Try to find and click the dish
        clicked = await self._find_and_click_dish(page, dish_name)

        if not clicked:
            logger.warning(f"Could not find/click {dish_name}, returning empty customizations")
            return DishCustomization(
                dish_name=dish_name,
                restaurant_id=restaurant_id,
                groups=[],
                special_instructions_allowed=True,
                cached_at=datetime.now()
            )

        # Step 2: Wait for customization screen to load
        await page.wait_for_timeout(BRIEF_PAUSE * 4)  # Increased wait time

        # Step 2b: Verify the modal is for the correct dish
        modal_verified = await self._verify_customization_modal(page, dish_name)
        if not modal_verified:
            logger.warning(f"Modal opened but doesn't match {dish_name}, skipping")
            await self._close_customization_screen(page)
            return DishCustomization(
                dish_name=dish_name,
                restaurant_id=restaurant_id,
                groups=[],
                special_instructions_allowed=True,
                cached_at=datetime.now()
            )

        # Step 3: Scroll to load all options (matching menu extraction pattern)
        modal_element = None
        try:
            # Try scrolling within modal first
            modal_selectors = [
                "[role='dialog']",
                ".modal",
                "[class*='Modal']",
                "[class*='customization']",
                "div[class*='drawer']"
            ]

            scrolled = False
            for selector in modal_selectors:
                try:
                    element = page.locator(selector).first
                    if await element.count() > 0:
                        # Scroll modal to bottom multiple times to ensure all content loads
                        for _ in range(3):  # Scroll 3 times
                            await element.evaluate("el => el.scrollTo(0, el.scrollHeight)")
                            await page.wait_for_timeout(BRIEF_PAUSE)
                        logger.info(f"Scrolled modal with selector: {selector}")
                        modal_element = element  # Save reference to modal
                        scrolled = True
                        break
                except:
                    continue

            if not scrolled:
                # Fallback: scroll entire page multiple times
                for _ in range(3):
                    await page.mouse.wheel(0, 1200)
                    await page.wait_for_timeout(BRIEF_PAUSE)
                logger.info("Scrolled entire page")

        except Exception as e:
            logger.warning(f"Could not scroll: {e}")

        # Step 4: Get HTML content ONLY from modal (not entire page!)
        # This prevents contamination from hidden/previous modals
        if modal_element:
            html_content = await modal_element.inner_html()
            logger.info(f"✅ Extracted HTML from modal element only")
        else:
            # Fallback: get entire page (less reliable)
            html_content = await page.content()
            logger.warning(f"⚠️  Using entire page HTML (modal not found)")

        # Step 5: Clean HTML and send to AI (matching menu extraction pattern)
        customization_data = await self._extract_customizations_with_ai(
            dish_name, html_content
        )

        # Step 6: Find selectors for automation
        selector_cache = await self._find_selectors_with_ai(
            dish_name, html_content, customization_data
        )

        # Step 7: Close modal/go back
        await self._close_customization_screen(page)

        # Convert groups to CustomizationGroup objects
        groups = [
            CustomizationGroup(
                label=g['label'],
                type=g['type'],
                required=g['required'],
                options=g['options']
            )
            for g in customization_data.get('groups', [])
        ]

        return DishCustomization(
            dish_name=dish_name,
            restaurant_id=restaurant_id,
            groups=groups,
            special_instructions_allowed=customization_data.get('special_instructions_allowed', True),
            cached_at=datetime.now(),
            selector_cache=selector_cache
        )

    async def _find_and_click_dish(self, page: Page, dish_name: str) -> bool:
        """
        Find and click a dish on the ordering page

        Strategy:
        1. Try direct text match (fast, works if dish is visible)
        2. If not found, use search bar (fallback)

        Returns True if successful, False otherwise
        """
        try:
            # Strategy 1: Direct text match for visible dishes
            logger.info(f"Trying direct text match for: {dish_name}")

            selectors = [
                f"text=/{dish_name}/i",  # Playwright's text selector
                f"button:has-text('{dish_name}')",
                f"div:has-text('{dish_name}')",
                f"a:has-text('{dish_name}')",
                "[data-testid*='menu-item']",
                ".menu-item",
                ".dish",
                "[class*='MenuItem']"
            ]

            for selector in selectors:
                try:
                    element = page.locator(selector).first
                    if await element.count() > 0:
                        logger.info(f"Found {dish_name} with selector: {selector}")
                        await element.click(timeout=ELEMENT_WAIT_TIMEOUT)
                        return True
                except Exception:
                    continue

            # Strategy 2: Use search bar (fallback)
            logger.info(f"Direct match failed, trying search bar for: {dish_name}")
            return await self._search_and_click_dish(page, dish_name)

        except Exception as e:
            logger.error(f"Error clicking dish {dish_name}: {e}")
            return False

    async def _search_and_click_dish(self, page: Page, dish_name: str) -> bool:
        """
        Use search bar to find and click a dish

        For Uber Eats: Search → Click Quick Add (+) button (more accurate)
        For others: Search → Click dish result (generic)

        Returns True if successful, False otherwise
        """
        try:
            # Detect platform from URL
            current_url = page.url.lower()
            is_uber_eats = 'ubereats.com' in current_url or 'uber.com' in current_url

            # Try to find search input
            search_input = None

            # For Uber Eats, prioritize restaurant-specific search (NOT global "Search Uber Eats")
            if is_uber_eats:
                logger.info(f"Uber Eats detected - looking for restaurant-specific search box")
                uber_eats_selectors = [
                    "input[inputmode='search']",  # Restaurant-specific search has inputmode="search"
                    "input[placeholder*='Search in' i]",  # "Search in [Restaurant Name]"
                ]

                for selector in uber_eats_selectors:
                    try:
                        element = page.locator(selector).first
                        if await element.count() > 0 and await element.is_visible():
                            # Verify it's NOT the global search (which has name="searchTerm")
                            name_attr = await element.get_attribute('name')
                            if name_attr != 'searchTerm':  # Exclude global "Search Uber Eats" box
                                search_input = element
                                logger.info(f"✅ Found restaurant-specific search (not global): {selector}")
                                break
                    except:
                        continue

            # Generic search selectors (for non-Uber Eats or if above failed)
            if not search_input:
                generic_selectors = [
                    "input[placeholder*='Search' i]",
                    "input[type='search']",
                    "input[placeholder*='Find' i]",
                    "input[name*='search' i]",
                    "input[aria-label*='search' i]",
                    ".search-input",
                    "#search",
                    "input[type='text']"  # Generic fallback
                ]

                for selector in generic_selectors:
                    try:
                        element = page.locator(selector).first
                        if await element.count() > 0 and await element.is_visible():
                            search_input = element
                            logger.info(f"Found search input with selector: {selector}")
                            break
                    except:
                        continue

            if not search_input:
                logger.warning(f"No search bar found for {dish_name}")
                return False

            # Clear and type dish name
            await search_input.click()
            await search_input.fill("")  # Clear first
            await search_input.type(dish_name, delay=50)  # Type with small delay
            logger.info(f"Typed '{dish_name}' into search bar")

            # Wait for search results to appear
            await page.wait_for_timeout(BRIEF_PAUSE * 4)  # 2 seconds for results

            # UBER EATS: Click Quick Add (+) button
            if is_uber_eats:
                logger.info(f"Uber Eats detected - trying Quick Add (+) button")
                quick_add_selectors = [
                    "button[data-testid='quick-add-button']",
                    "button[aria-label='Quick Add']",
                    "button[aria-label*='Add' i]:has(svg)",
                ]

                for selector in quick_add_selectors:
                    try:
                        button = page.locator(selector).first
                        if await button.count() > 0 and await button.is_visible():
                            logger.info(f"✅ Found Quick Add button: {selector}")
                            await button.click(timeout=ELEMENT_WAIT_TIMEOUT)
                            logger.info(f"✅ Clicked Quick Add (+) for {dish_name}")
                            return True
                    except:
                        continue

                logger.warning(f"Quick Add button not found, falling back to dish click")

            # GENERIC: Click dish result (works for all platforms)
            result_selectors = [
                f"text=/{dish_name}/i",
                "[class*='search-result']",
                "[class*='SearchResult']",
                "[role='option']",
                ".result-item"
            ]

            for selector in result_selectors:
                try:
                    element = page.locator(selector).first
                    if await element.count() > 0:
                        logger.info(f"Found search result with selector: {selector}")
                        await element.click(timeout=ELEMENT_WAIT_TIMEOUT)
                        return True
                except:
                    continue

            logger.warning(f"Could not click dish result for {dish_name}")
            return False

        except Exception as e:
            logger.error(f"Error in search fallback for {dish_name}: {e}")
            return False

    async def _verify_customization_modal(self, page: Page, dish_name: str) -> bool:
        """
        Verify that the opened modal is for the correct dish

        This prevents extracting customizations from the wrong dish modal.
        Returns True if modal matches dish_name, False otherwise.
        """
        try:
            # Get modal content
            modal_selectors = [
                "[role='dialog']",
                ".modal",
                "[class*='Modal']",
                "[class*='customization']"
            ]

            modal_text = ""
            for selector in modal_selectors:
                try:
                    element = page.locator(selector).first
                    if await element.count() > 0:
                        modal_text = await element.inner_text()
                        break
                except:
                    continue

            if not modal_text:
                # Fallback: get entire page text
                modal_text = await page.inner_text('body')

            # Check if dish name appears in modal (case-insensitive, fuzzy)
            modal_lower = modal_text.lower()
            dish_lower = dish_name.lower()

            # Exact match
            if dish_lower in modal_lower:
                logger.info(f"✅ Modal verified for {dish_name} (exact match)")
                return True

            # Fuzzy match: Check if most words from dish name appear
            dish_words = set(dish_lower.split())
            matching_words = sum(1 for word in dish_words if word in modal_lower and len(word) > 2)

            if matching_words >= len(dish_words) * 0.7:  # 70% of words match
                logger.info(f"✅ Modal verified for {dish_name} (fuzzy match: {matching_words}/{len(dish_words)} words)")
                return True

            logger.warning(f"❌ Modal does NOT match {dish_name} (only {matching_words}/{len(dish_words)} words match)")
            logger.warning(f"   Modal text preview: {modal_text[:200]}")
            return False

        except Exception as e:
            logger.error(f"Error verifying modal: {e}")
            return False  # Better to skip than extract wrong data

    async def _handle_uber_eats_address_prompt(
        self,
        page: Page,
        delivery_address: Optional[str] = None,
        restaurant_location: Optional[str] = None
    ):
        """
        Handle Uber Eats address prompt that appears before ordering

        Uber Eats shows an address entry modal when you first visit. This must be
        handled before we can click dishes.

        Args:
            page: Playwright page
            delivery_address: Optional user's delivery address (e.g., "123 Main St, San Jose, CA 95128")
            restaurant_location: Optional restaurant location as fallback (e.g., "San Jose, CA")
        """
        try:
            # Wait 10-15 seconds for address prompt to appear
            logger.info(f"⏳ Waiting 1 for Uber Eats address prompt...")
            await page.wait_for_timeout(1000)

            # Look for address input field
            address_input_selectors = [
                "input[placeholder*='address' i]",
                "input[placeholder*='location' i]",
                "input[placeholder*='deliver' i]",
                "input[id*='location-typeahead-home-input']",
                "input[data-testid*='address']"
            ]

            address_input = None
            for selector in address_input_selectors:
                try:
                    element = page.locator(selector).first
                    if await element.count() > 0 and await element.is_visible():
                        address_input = element
                        logger.info(f"✅ Found address input: {selector}")
                        break
                except:
                    continue

            if not address_input:
                logger.info(f"ℹ️  No address prompt detected (may already be set)")
                return

            # Determine which address to use (priority: user's delivery address > restaurant location > generic city)
            address_to_use = delivery_address or restaurant_location or "San Jose, CA"

            logger.info(f"📍 Entering address: {address_to_use}")
            if not delivery_address and restaurant_location:
                logger.info(f"   ℹ️  Using restaurant location as fallback")
            elif not delivery_address:
                logger.info(f"   ℹ️  Using generic city as fallback")

            await address_input.click()
            await address_input.fill(address_to_use)
            await address_input.type(" ")  # Trigger autocomplete
            logger.info(f"⏳ Waiting for autocomplete suggestions...")
            await page.wait_for_timeout(2000)  # Wait for dropdown to appear

            # Click autocomplete suggestion that matches our typed address
            # Uber Eats uses <ul><button aria-label="address">
            clicked_suggestion = False

            # Extract key parts from typed address for matching (street number + street name)
            # E.g., "10221 Vicksburg Drive" → match "10221 Vicksburg"
            import re
            address_parts = re.split(r'[,\s]+', address_to_use.strip())
            # Get first 2 parts (street number + street name, e.g., "10221", "Vicksburg")
            match_pattern = ' '.join(address_parts[:2]) if len(address_parts) >= 2 else address_parts[0]

            logger.info(f"🔍 Looking for suggestion containing: '{match_pattern}'")

            try:
                # Get all autocomplete buttons
                all_buttons = await page.locator("ul button[aria-label]").all()

                for button in all_buttons:
                    aria_label = await button.get_attribute('aria-label')
                    if aria_label and match_pattern.lower() in aria_label.lower():
                        logger.info(f"🎯 Found matching suggestion: '{aria_label}'")
                        await button.click(timeout=3000)
                        clicked_suggestion = True
                        logger.info(f"✅ Clicked matching address suggestion, waiting for page to update...")

                        # CRITICAL: Wait for page to fully update after address selection
                        await page.wait_for_timeout(5000)  # 5 seconds for menu to reload

                        # Verify menu loaded
                        try:
                            await page.wait_for_selector("[data-testid*='menu'], [class*='menu-item'], [class*='store-item']", timeout=5000)
                            logger.info(f"✅ Menu loaded successfully after address selection")
                        except:
                            logger.warning(f"⚠️  Menu items not detected, but continuing...")

                        return

            except Exception as e:
                logger.warning(f"Error finding matching address button: {e}")

            # Fallback: Try generic selectors
            if not clicked_suggestion:
                autocomplete_selectors = [
                    "[role='option']",
                    "[data-testid*='autocomplete']",
                    "li[role='option']"
                ]

                for selector in autocomplete_selectors:
                    try:
                        suggestion = page.locator(selector).first
                        if await suggestion.count() > 0 and await suggestion.is_visible():
                            suggestion_text = await suggestion.inner_text()
                            logger.info(f"🎯 Clicking fallback suggestion: '{suggestion_text}'")
                            await suggestion.click(timeout=3000)
                            clicked_suggestion = True
                            await page.wait_for_timeout(5000)
                            return
                    except:
                        continue

            # Last resort: Press Enter if no dropdown clicked
            if not clicked_suggestion:
                logger.info(f"⚠️  No autocomplete dropdown found, trying Enter key...")
                await address_input.press('Enter')
                await page.wait_for_timeout(5000)  # Wait for page update
                logger.info(f"✅ Submitted address via Enter key")

        except Exception as e:
            logger.warning(f"Error handling address prompt: {e}")
            # Continue anyway - might not be needed

    async def _close_customization_screen(self, page: Page):
        """Close customization modal/screen to return to menu"""
        try:
            # Try common close button selectors
            close_selectors = [
                "button[aria-label='Close']",
                "button:has-text('Close')",
                ".close",
                ".modal-close",
                "[data-testid='close-button']",
                "button.close"
            ]

            for selector in close_selectors:
                try:
                    element = page.locator(selector).first
                    if await element.count() > 0:
                        await element.click(timeout=3000)
                        await page.wait_for_timeout(BRIEF_PAUSE)
                        return
                except Exception:
                    continue

            # If no close button, try ESC key
            await page.keyboard.press('Escape')
            await page.wait_for_timeout(BRIEF_PAUSE)

        except Exception as e:
            logger.warning(f"Could not close customization screen: {e}")

    async def _extract_customizations_with_ai(
        self,
        dish_name: str,
        html_content: str
    ) -> dict:
        """
        Use OpenAI to extract customization options from HTML (matching menu extraction pattern)

        Returns dict with: sizes, add_ons, modifications, special_instructions_allowed
        """
        import re

        # Clean HTML (same pattern as menu extraction)
        # Remove scripts, styles, comments
        html_content = re.sub(r'<script[^>]*>.*?</script>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
        html_content = re.sub(r'<style[^>]*>.*?</style>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
        html_content = re.sub(r'<!--.*?-->', '', html_content, flags=re.DOTALL)
        html_content = re.sub(r'\s+style="[^"]*"', '', html_content, flags=re.IGNORECASE)

        # Truncate to avoid token limits (like menu extraction does 90k)
        max_html_length = 50000  # Smaller since modal content is less than full menu
        if len(html_content) > max_html_length:
            html_content = html_content[:max_html_length]
            logger.info(f"Truncated HTML to {max_html_length} chars")

        # Build prompt - generic structure discovery (NO SPECIFIC EXAMPLES TO AVOID BIAS!)
        prompt = f'''You are analyzing a restaurant ordering customization screen for the dish: "{dish_name}"

Your task: Extract ALL customization sections EXACTLY as they appear on the website, preserving the original question text and structure.

Return ONLY a valid JSON object with this structure:

{{
  "groups": [
    {{
      "label": "<exact label text from HTML>",
      "type": "single_choice or multi_choice",
      "required": true or false,
      "options": [
        {{"name": "<option name>", "price": "<price or null>"}},
        ...more options...
      ]
    }},
    ...more groups...
  ],
  "special_instructions_allowed": true or false
}}

CRITICAL RULES:
1. **Extract exact label text** from the HTML - look for headings, legends, or labels that appear before groups of options
   These are usually in heading tags, div elements with prominent text, or label elements

2. **Detect selection type** from HTML input elements:
   - type="single_choice" if options use radio buttons (only one can be selected)
   - type="multi_choice" if options use checkboxes (multiple can be selected)

3. **Detect if required**:
   - Set required: true if you see "Required", "choose one", "select one", asterisk (*), or required attribute
   - Set required: false for optional sections (usually checkboxes or "choose up to X" language)

4. **Extract ALL options** in each group:
   - Include every option you find in the HTML
   - Don't skip any options
   - Read option names from labels, button text, or nearby text elements

5. **Extract prices**:
   - Use "+$2.50" format for paid add-ons (preserve the + sign and format)
   - Use "+$0" or "included" for free upgrades
   - Use null if no price is shown

6. **Preserve order** - options should appear in the same order as in the HTML

7. Set special_instructions_allowed to true ONLY if there's a visible text field, textarea, or input for special requests/notes

IMPORTANT: Extract ONLY from the provided HTML content. Do not use examples or assume any structure.

HTML content:
{html_content}
'''

        try:
            logger.info(f"Calling OpenAI API (gpt-4o-mini) for customization extraction...")
            logger.info(f"Input: ~{len(prompt):,} chars")

            start_time = time.time()

            response = self.openai_client.chat.completions.create(
                model="gpt-4o-mini",  # Same model as menu extraction
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1500,
                temperature=0.1
            )

            elapsed = time.time() - start_time
            logger.info(f"OpenAI response received in {elapsed:.2f}s")

            ai_response = response.choices[0].message.content.strip()

            # Parse JSON response (same cleanup as menu extraction)
            if ai_response.startswith("```json"):
                ai_response = ai_response[7:]
            if ai_response.startswith("```"):
                ai_response = ai_response[3:]
            if ai_response.endswith("```"):
                ai_response = ai_response[:-3]

            customization_data = json.loads(ai_response.strip())

            logger.info(f"AI extracted customizations for {dish_name}: {customization_data}")
            return customization_data

        except Exception as e:
            logger.error(f"Error extracting customizations with AI: {e}")
            # Return empty customizations as fallback
            return {
                "groups": [],
                "special_instructions_allowed": True
            }


    async def _find_selectors_with_ai(
        self,
        dish_name: str,
        html_content: str,
        customization_data: dict
    ) -> dict:
        """
        Use AI to find CSS selectors for automating customization selection

        Returns dict with selectors for:
        - Each option button/checkbox
        - Add to cart button
        - Special instructions field
        """
        import re

        # Clean HTML (same as customization extraction)
        html_content = re.sub(r'<script[^>]*>.*?</script>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
        html_content = re.sub(r'<style[^>]*>.*?</style>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
        html_content = re.sub(r'<!--.*?-->', '', html_content, flags=re.DOTALL)
        html_content = re.sub(r'\s+style="[^"]*"', '', html_content, flags=re.IGNORECASE)

        # Truncate
        max_html_length = 50000
        if len(html_content) > max_html_length:
            html_content = html_content[:max_html_length]

        # Build list of ALL options to find selectors for
        all_options = []
        for group in customization_data.get('groups', []):
            for option in group.get('options', []):
                all_options.append(option['name'])

        if not all_options:
            logger.info(f"No options to find selectors for")
            return {}

        # Don't limit options - we need selectors for ALL of them
        logger.info(f"Finding selectors for {len(all_options)} options")

        prompt = f'''You are analyzing HTML for a restaurant ordering customization screen for "{dish_name}".

Your task: Find CSS selectors for ALL interactive elements on this customization screen.

Categorize selectors into 3 groups:
1. option_selectors: For selecting dish customizations (cooking temp, sides, modifications)
2. action_buttons: For actions like "Add to Cart", "Cancel", "Close"
3. form_fields: For input fields like quantity, special instructions, recipient name, etc.

Return ONLY a valid JSON object:

{{
  "option_selectors": {{
    "Medium Rare": "input[type='radio'][value='medium-rare']",
    "Cajun Fries": "input[type='checkbox'][value='cajun-fries']",
    "No Onions": "input[type='checkbox'][value='no-onions']"
  }},
  "action_buttons": {{
    "add_to_cart": "button.add-to-cart",
    "cancel": "button.cancel",
    "close": "button[aria-label='Close']"
  }},
  "form_fields": {{
    "quantity": "input[type='number']",
    "special_instructions": "textarea[name='instructions']",
    "made_for": "input[name='recipient']"
  }}
}}

IMPORTANT RULES:
- Include selectors for ALL these options: {json.dumps(all_options)}
- Find ALL buttons (Add to Cart is critical - look for "Add", "Continue", "Done", "Submit")
- Find ALL form fields (quantity, special instructions, recipient/made-for name, etc.)
- Use SIMPLEST working CSS selector (prefer [data-*], [name], [value], [aria-label])
- For input elements, use input[type='radio'][value='...'] or input[type='checkbox'][value='...']
- For text buttons: button:has-text("Add to Cart")
- If element doesn't exist, omit it from JSON (don't use "UNKNOWN")
- Scroll through the ENTIRE HTML - options and buttons are often below the fold

HTML content:
{html_content}
'''

        try:
            logger.info(f"Calling OpenAI API to find selectors for {dish_name}...")

            response = self.openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=3000,  # Increased from 1500 to accommodate all option selectors
                temperature=0.1
            )

            ai_response = response.choices[0].message.content.strip()

            # Parse JSON
            if ai_response.startswith("```json"):
                ai_response = ai_response[7:]
            if ai_response.startswith("```"):
                ai_response = ai_response[3:]
            if ai_response.endswith("```"):
                ai_response = ai_response[:-3]

            selector_data = json.loads(ai_response.strip())

            logger.info(f"AI found selectors for {dish_name}: {selector_data}")
            return selector_data

        except Exception as e:
            logger.error(f"Error finding selectors with AI: {e}")
            return {}


# Convenience function for API views
async def discover_dish_customizations(
    restaurant_id: str,
    ordering_url: str,
    dishes: List[str],
    delivery_address: Optional[str] = None,
    restaurant_location: Optional[str] = None
) -> Dict[str, dict]:
    """
    Convenience function to discover customizations

    Args:
        restaurant_id: Place ID
        ordering_url: Restaurant's online ordering URL
        dishes: List of dish names
        delivery_address: Optional user's delivery address for Uber Eats
        restaurant_location: Optional restaurant location as fallback (e.g., "San Jose, CA")

    Returns dict mapping dish_name -> customization dict (JSON-serializable)
    """
    discovery = DishCustomizationDiscovery()
    results = await discovery.discover_customizations(
        restaurant_id, ordering_url, dishes, delivery_address, restaurant_location
    )

    # Convert to JSON-serializable format
    return {
        dish_name: customization.to_dict()
        for dish_name, customization in results.items()
    }


# ================================================================================
# BLOCKER DIAGNOSIS SYSTEM - AI-Powered Adaptive Automation
# ================================================================================

@dataclass
class BlockerDiagnosis:
    """AI diagnosis of what's blocking automation"""
    blocker_type: str  # "address_modal", "login_required", "item_unavailable", "none"
    explanation: str  # Human-readable explanation
    selectors: Dict[str, str]  # CSS selectors to handle the blocker
    next_action: str  # What to do next: "fill_form", "fallback_manual", "retry"
    discovered_at: datetime
    success_count: int = 0  # Track how many times this solution worked

    def to_dict(self):
        return {
            "blocker_type": self.blocker_type,
            "explanation": self.explanation,
            "selectors": self.selectors,
            "next_action": self.next_action,
            "discovered_at": self.discovered_at.isoformat(),
            "success_count": self.success_count
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            blocker_type=data["blocker_type"],
            explanation=data["explanation"],
            selectors=data["selectors"],
            next_action=data["next_action"],
            discovered_at=datetime.fromisoformat(data["discovered_at"]),
            success_count=data.get("success_count", 0)
        )


def detect_platform_from_url(url: str) -> str:
    """Detect ordering platform from URL - extracted to avoid duplication"""
    url_lower = url.lower()
    if 'doordash.com' in url_lower or 'order.online' in url_lower:
        return 'doordash_order_online'
    elif 'ubereats.com' in url_lower or 'uber.com' in url_lower:
        return 'ubereats'
    elif 'grubhub.com' in url_lower:
        return 'grubhub'
    else:
        return 'custom'


def get_cache_key(restaurant_id: str, current_url: str) -> str:
    """
    Generate cache key: platform-shared or restaurant-unique

    Known platforms → shared cache (e.g., 'ubereats')
    Custom sites → unique cache (e.g., 'custom_ChIJ123...')
    """
    platform = detect_platform_from_url(current_url)
    return platform if platform != 'custom' else f"custom_{restaurant_id}"


def get_blocker_cache_path(cache_key: str) -> Path:
    """Get path to blocker cache file using cache key"""
    cache_dir = CACHE_DIR.parent / "automation_caches" / cache_key
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / "blockers.json"


def load_blocker_cache(restaurant_id: str, current_url: str) -> List[BlockerDiagnosis]:
    """Load cached blocker solutions using platform-aware cache key"""
    cache_key = get_cache_key(restaurant_id, current_url)
    cache_path = get_blocker_cache_path(cache_key)

    if not cache_path.exists():
        return []

    try:
        with open(cache_path, 'r') as f:
            data = json.load(f)
            return [BlockerDiagnosis.from_dict(b) for b in data.get("blockers", [])]
    except Exception as e:
        logger.error(f"Failed to load blocker cache: {e}")
        return []


def save_blocker_diagnosis(restaurant_id: str, diagnosis: BlockerDiagnosis, current_url: str):
    """Save blocker diagnosis to cache using platform-aware cache key"""
    cache_key = get_cache_key(restaurant_id, current_url)
    cache_path = get_blocker_cache_path(cache_key)

    # Load existing cache
    existing_blockers = load_blocker_cache(restaurant_id, current_url)

    # Check if similar blocker already exists (avoid duplicates)
    for existing in existing_blockers:
        if existing.blocker_type == diagnosis.blocker_type:
            # Update existing with newer selectors
            existing.selectors = diagnosis.selectors
            existing.explanation = diagnosis.explanation
            existing.discovered_at = diagnosis.discovered_at
            break
    else:
        # Add new blocker
        existing_blockers.append(diagnosis)

    # Save to file
    try:
        platform = detect_platform_from_url(current_url)  # Use extracted function (DRY!)

        data = {
            "platform": platform,
            "cache_key": cache_key,
            "blockers": [b.to_dict() for b in existing_blockers],
            "last_updated": datetime.now().isoformat()
        }

        with open(cache_path, 'w') as f:
            json.dump(data, f, indent=2)

        logger.info(f"✅ Cached blocker diagnosis to {cache_key}: {diagnosis.blocker_type}")

    except Exception as e:
        logger.error(f"Failed to save blocker diagnosis: {e}")


def increment_blocker_success(restaurant_id: str, blocker_type: str, current_url: str):
    """Increment success count for a blocker solution"""
    blockers = load_blocker_cache(restaurant_id, current_url)

    for blocker in blockers:
        if blocker.blocker_type == blocker_type:
            blocker.success_count += 1
            break

    # Save updated cache
    cache_key = get_cache_key(restaurant_id, current_url)
    cache_path = get_blocker_cache_path(cache_key)
    platform = detect_platform_from_url(current_url)

    try:
        data = {
            "platform": platform,
            "cache_key": cache_key,
            "blockers": [b.to_dict() for b in blockers],
            "last_updated": datetime.now().isoformat()
        }

        with open(cache_path, 'w') as f:
            json.dump(data, f, indent=2)

    except Exception as e:
        logger.error(f"Failed to increment blocker success: {e}")


async def ai_diagnose_blocker(
    html: str,
    intended_action: str,
    error_message: str,
    restaurant_id: str,
    current_url: str
) -> Optional[BlockerDiagnosis]:
    """
    Use AI to diagnose what's blocking automation

    Args:
        html: Current page HTML
        intended_action: What we were trying to do (e.g., "add New York Style Cheese Pizza to cart")
        error_message: Error message from automation
        restaurant_id: Place ID
        current_url: Current page URL

    Returns:
        BlockerDiagnosis object with solution, or None if AI fails
    """
    import re

    # Check cache first (maybe we've seen this blocker before)
    cached_blockers = load_blocker_cache(restaurant_id, current_url)

    # Generate HTML signature (first 500 chars of visible text, normalized)
    html_text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html_text = re.sub(r'<style[^>]*>.*?</style>', '', html_text, flags=re.DOTALL | re.IGNORECASE)
    html_text = re.sub(r'<[^>]+>', ' ', html_text)
    html_text = ' '.join(html_text.split())[:500].lower()

    # Check if we've seen similar blocker before
    for cached in cached_blockers:
        # If blocker type matches and success_count > 0, trust the cache
        if cached.success_count > 0:
            # Quick check: does HTML contain key blocker indicators?
            if cached.blocker_type == "address_modal" and "address" in html_text:
                logger.info(f"📦 Using cached blocker solution: {cached.blocker_type}")
                return cached
            elif cached.blocker_type == "login_required" and ("login" in html_text or "sign in" in html_text):
                logger.info(f"📦 Using cached blocker solution: {cached.blocker_type}")
                return cached

    # No cache - ask AI
    logger.info(f"🤖 No cached solution, asking AI to diagnose blocker...")

    # Clean HTML (same as customization extraction)
    cleaned_html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    cleaned_html = re.sub(r'<style[^>]*>.*?</style>', '', cleaned_html, flags=re.DOTALL | re.IGNORECASE)
    cleaned_html = re.sub(r'<!--.*?-->', '', cleaned_html, flags=re.DOTALL)
    cleaned_html = re.sub(r'\s+style="[^"]*"', '', cleaned_html, flags=re.IGNORECASE)

    # Truncate
    max_html_length = 15000
    if len(cleaned_html) > max_html_length:
        cleaned_html = cleaned_html[:max_html_length]

    prompt = f"""I'm automating a restaurant order and got blocked.

**What I was trying to do:**
{intended_action}

**Error:**
{error_message}

**Current URL:**
{current_url}

**Page HTML (truncated):**
{cleaned_html}

**Your task:**
Analyze what's blocking the automation and provide a solution.

**Common blocker types:**
1. address_modal - Modal/form asking for delivery address before showing menu
2. login_required - Login/sign-in page blocking access
3. item_unavailable - Restaurant closed or item out of stock
4. none - False alarm, no blocker detected

**Return ONLY valid JSON:**
{{
  "blocker_type": "address_modal|login_required|item_unavailable|none",
  "explanation": "There's a delivery address modal that must be filled before browsing menu",
  "selectors": {{
    "street_address": "input#address-line-1",
    "city": "input#locality",
    "state": "select#administrative_area",
    "zip": "input#postal-code",
    "submit_button": "button[type='submit']"
  }},
  "next_action": "fill_form|fallback_manual|retry"
}}

**IMPORTANT:**
- Only include selectors that actually exist in the HTML
- Use simplest CSS selectors (prefer id, name, type attributes)
- If no blocker detected, return blocker_type: "none" with empty selectors
- For login_required, return next_action: "fallback_manual"
"""

    try:
        openai_client = OpenAI(api_key=settings.OPENAI_API_KEY)

        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1000,
            temperature=0.1
        )

        ai_response = response.choices[0].message.content.strip()

        # Parse JSON
        if ai_response.startswith("```json"):
            ai_response = ai_response[7:]
        if ai_response.startswith("```"):
            ai_response = ai_response[3:]
        if ai_response.endswith("```"):
            ai_response = ai_response[:-3]

        diagnosis_data = json.loads(ai_response.strip())

        # Create BlockerDiagnosis object
        diagnosis = BlockerDiagnosis(
            blocker_type=diagnosis_data["blocker_type"],
            explanation=diagnosis_data["explanation"],
            selectors=diagnosis_data.get("selectors", {}),
            next_action=diagnosis_data["next_action"],
            discovered_at=datetime.now()
        )

        # Store URL for platform detection
        diagnosis._url = current_url

        logger.info(f"✅ AI diagnosed blocker: {diagnosis.blocker_type}")
        logger.info(f"   Explanation: {diagnosis.explanation}")
        logger.info(f"   Next action: {diagnosis.next_action}")

        # Cache if it's a useful diagnosis
        if diagnosis.blocker_type != "none":
            save_blocker_diagnosis(restaurant_id, diagnosis, current_url)

        return diagnosis

    except Exception as e:
        logger.error(f"❌ AI blocker diagnosis failed: {e}", exc_info=True)
        return None
