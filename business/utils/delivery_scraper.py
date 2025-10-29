# delivery_scraper.py
"""
Playwright-based scraper for DoorDash and Uber Eats store IDs
Uses similar patterns to your existing review scraping
"""

import asyncio
import logging
import re
from urllib.parse import urlparse, parse_qs
from playwright.async_api import async_playwright, Page

logger = logging.getLogger(__name__)

async def scrape_doordash_store_id(restaurant_name: str, address: str) -> str | None:
    """
    Search DoorDash for a restaurant and extract its store ID
    Returns the numeric store ID (e.g., "690753") or None if not found
    """
    try:
        async with async_playwright() as p:
            # Launch browser with more human-like settings
            browser = await p.chromium.launch(
                headless=True,  # Set to False to see browser during debugging
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-features=VizDisplayCompositor'
                ]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={'width': 1280, 'height': 720}
            )

            # Remove webdriver property
            await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            page = await context.new_page()

            # Extract city from address for better search
            city = extract_city_from_address(address)
            search_query = f"{restaurant_name} {city}".strip()

            logger.info(f"Searching DoorDash for: {search_query}")

            # Go to DoorDash homepage first to set location
            await page.goto("https://www.doordash.com/", wait_until="networkidle")
            await page.wait_for_timeout(5000)  # Longer wait to appear human

            # Check for verification or captcha
            if "verify" in (await page.content()).lower():
                logger.warning("DoorDash verification detected, waiting longer...")
                await page.wait_for_timeout(10000)  # Wait 10 seconds for verification

            # Handle delivery address input
            try:
                # Look for address input field
                address_input = await page.query_selector('input[placeholder*="delivery address"], input[placeholder*="Enter delivery address"], input[data-anchor-id="AddressInput"], input[data-testid="address-input"]')
                if address_input:
                    await address_input.click()
                    await page.wait_for_timeout(500)
                    await address_input.type(address, delay=100)  # Human-like typing
                    await page.wait_for_timeout(1000)
                    await page.keyboard.press("Enter")
                    await page.wait_for_timeout(4000)
                else:
                    # Try other common selectors
                    address_input = await page.query_selector('input[type="text"]')
                    if address_input:
                        await address_input.click()
                        await page.wait_for_timeout(500)
                        await address_input.type(address, delay=100)
                        await page.wait_for_timeout(1000)
                        await page.keyboard.press("Enter")
                        await page.wait_for_timeout(4000)
            except Exception as e:
                logger.error(f"Could not set delivery address: {e}")

            # Now search for the restaurant
            try:
                # Wait for page to load after address input
                await page.wait_for_timeout(3000)

                # Look for search input
                search_input = await page.query_selector('input[placeholder*="Search"], input[data-anchor-id="SearchInput"], input[data-testid="search-input"]')
                if search_input:
                    await search_input.click()
                    await page.wait_for_timeout(500)
                    await search_input.type(restaurant_name, delay=120)  # Human-like typing
                    await page.wait_for_timeout(1000)
                    await page.keyboard.press("Enter")
                    await page.wait_for_timeout(5000)
                else:
                    # Fallback to URL search
                    from urllib.parse import quote_plus
                    encoded_query = quote_plus(restaurant_name)
                    search_url = f"https://www.doordash.com/search/store/?query={encoded_query}"
                    await page.goto(search_url)
                    await page.wait_for_timeout(4000)
            except Exception as e:
                logger.error(f"Could not perform search: {e}")

            # Look for restaurant links in search results
            store_id = await extract_doordash_store_id_from_page(page, restaurant_name)

            await browser.close()
            return store_id

    except Exception as e:
        logger.error(f"Error scraping DoorDash store ID for {restaurant_name}: {e}")
        return None

async def extract_doordash_store_id_from_page(page: Page, restaurant_name: str) -> str | None:
    """Extract store ID from DoorDash search results page"""
    try:
        # Look for links that contain the restaurant name and have DoorDash store URLs
        # Pattern: /store/restaurant-name-city-STOREID/
        links = await page.query_selector_all('a[href*="/store/"]')

        for link in links:
            href = await link.get_attribute('href')
            if not href:
                continue

            # Extract store ID from URL pattern
            # Example: /store/pacific-catch-cupertino-690753/
            match = re.search(r'/store/[^/]+-(\d+)/?', href)
            if match:
                store_id = match.group(1)

                # Verify this link contains restaurant name (fuzzy match)
                link_text = await link.inner_text() if await link.is_visible() else ""
                if is_restaurant_match(restaurant_name, href, link_text):
                    logger.info(f"Found DoorDash store ID: {store_id} for {restaurant_name}")
                    return store_id

        # Alternative: look for store URLs in page source or API calls
        content = await page.content()
        store_id_from_content = extract_store_id_from_html(content, restaurant_name)
        if store_id_from_content:
            return store_id_from_content

        logger.warning(f"Could not find DoorDash store ID for {restaurant_name}")
        return None

    except Exception as e:
        logger.error(f"Error extracting store ID from page: {e}")
        return None

async def scrape_uber_eats_store_id(restaurant_name: str, address: str) -> str | None:
    """Disabled for testing"""
    return None

def extract_city_from_address(address: str) -> str:
    """Extract city from address for better search targeting"""
    if not address:
        return ""

    # Try to get city from "City, State" or "Street, City, State" pattern
    parts = address.split(", ")
    if len(parts) >= 2:
        # Usually city is second-to-last or last part before state
        city = parts[-2] if len(parts) > 2 else parts[0]
        return city.strip()

    return address.strip()

def is_restaurant_match(restaurant_name: str, url: str, link_text: str) -> bool:
    """Check if a link/text matches the restaurant we're looking for"""
    restaurant_lower = restaurant_name.lower()
    url_lower = url.lower()
    text_lower = link_text.lower()

    # Simple fuzzy matching - check if key words appear
    restaurant_words = restaurant_lower.split()

    # At least half the words should appear in URL or text
    matches = 0
    for word in restaurant_words:
        if len(word) > 2:  # Skip small words like "the", "and"
            if word in url_lower or word in text_lower:
                matches += 1

    return matches >= len(restaurant_words) * 0.5

def extract_store_id_from_html(html_content: str, restaurant_name: str) -> str | None:
    """Extract store ID from HTML content as fallback"""
    try:
        # Look for store URLs in HTML content
        store_matches = re.findall(r'/store/[^/]+-(\d+)/', html_content)
        if store_matches:
            # Return first match (could be improved with better matching)
            return store_matches[0]
    except Exception as e:
        logger.error(f"Error extracting store ID from HTML: {e}")

    return None

# Main function for the background worker
async def lookup_delivery_store_ids(restaurant_name: str, address: str) -> dict:
    """
    Lookup both DoorDash and Uber Eats store IDs
    Returns dict with store IDs and metadata
    """
    result = {
        "doordash_store_id": None,
        "uber_eats_store_id": None,
        "scraped_at": None,
        "restaurant_name": restaurant_name,
        "address": address
    }

    try:
        # Run both scrapers concurrently
        doordash_task = scrape_doordash_store_id(restaurant_name, address)
        # uber_eats_task = scrape_uber_eats_store_id(restaurant_name, address)

        doordash_id = await doordash_task
        uber_eats_id = None  # Disabled for testing

        # Handle results (even if one fails)
        if not isinstance(doordash_id, Exception):
            result["doordash_store_id"] = doordash_id

        if not isinstance(uber_eats_id, Exception):
            result["uber_eats_store_id"] = uber_eats_id

        import time
        result["scraped_at"] = time.time()

        logger.info(f"Lookup complete for {restaurant_name}: DoorDash={doordash_id}, UberEats={uber_eats_id}")

    except Exception as e:
        logger.error(f"Error in lookup_delivery_store_ids: {e}")

    return result