"""
Yelp Integration Utilities
Separate module for Google Maps to Yelp business URL conversion and scraping
"""

import re
import requests
import json
import asyncio
import logging
from pathlib import Path
from typing import List, Dict, Optional
from googlemaps import Client as GoogleMapsClient
from django.conf import settings
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)


def construct_yelp_url(restaurant_name, location):
    """Construct Yelp business URL from restaurant name and location"""
    
    # Clean restaurant name for URL
    clean_name = re.sub(r'[^\w\s-]', '', restaurant_name.lower())
    clean_name = re.sub(r'\s+', '-', clean_name.strip())
    
    # Clean location (city, neighborhood)
    clean_location = re.sub(r'[^\w\s-]', '', location.lower()) 
    clean_location = re.sub(r'\s+', '-', clean_location.strip())
    
    # Remove common location words
    clean_location = re.sub(r'\b(city|county|state)\b', '', clean_location).strip('-')
    
    # Construct Yelp URL pattern
    yelp_url = f"https://www.yelp.com/biz/{clean_name}-{clean_location}"
    
    return yelp_url


def get_yelp_url_from_place_id(place_id):
    """
    Convert Google place_id to Yelp business URL
    
    Args:
        place_id (str): Google Maps place ID
        
    Returns:
        dict: {'yelp_url': str, 'valid': bool, 'google_info': dict} or None if error
    """
    try:
        # Get Google Places details
        # gmaps = GoogleMapsClient(key=settings.GOOGLE_API_KEY)
        import os
        from dotenv import load_dotenv
        load_dotenv()
        gmaps = GoogleMapsClient(key=os.getenv('GOOGLE_API_KEY'))
        
        result = gmaps.place(
            place_id=place_id, 
            fields=['name', 'vicinity', 'formatted_address', 'address_component']
        )
        
        place = result['result']
        restaurant_name = place.get('name', '')
        
        # Try to get city from vicinity or address components
        location = ''
        
        # First try address components for city
        if 'address_component' in place:
            for component in place['address_component']:
                if 'locality' in component['types']:
                    location = component['long_name']
                    break
                elif 'sublocality' in component['types']:
                    location = component['long_name']
                    break
        
        # If no city found, try parsing formatted_address
        if not location:
            addr = place.get('formatted_address', '')
            # Extract city (usually second-to-last before state and zip)
            parts = addr.split(',')
            if len(parts) >= 3:
                # Format: "Address, City, State ZIP"
                location = parts[-3].strip()
            elif len(parts) >= 2:
                location = parts[-2].strip()
        
        # Fallback to vicinity but clean it up
        if not location:
            vicinity = place.get('vicinity', '')
            if vicinity:
                # If vicinity contains full address, try to extract city
                parts = vicinity.split(',')
                if len(parts) > 1:
                    location = parts[-1].strip()
                else:
                    location = vicinity
        
        # Construct Yelp URL
        yelp_url = construct_yelp_url(restaurant_name, location)
        
        return {
            'yelp_url': yelp_url,
            'google_info': {
                'name': restaurant_name,
                'location': location,
                'address': place.get('formatted_address', ''),
                'vicinity': place.get('vicinity', '')
            }
        }
        
    except Exception as e:
        print(f"Error getting Yelp URL for place_id {place_id}: {e}")
        return None


def test_yelp_integration(place_id):
    """Test function to verify Yelp URL generation (not validation due to bot blocking)"""
    print(f"Testing Yelp integration for place_id: {place_id}")
    
    result = get_yelp_url_from_place_id(place_id)
    
    if result:
        print(f"Restaurant: {result['google_info']['name']}")
        print(f"Location: {result['google_info']['location']}")
        print(f"Generated Yelp URL: {result['yelp_url']}")
        print(f"✅ URL Generated Successfully")
        print("📝 Note: Use this URL with Playwright for scraping (Yelp blocks direct HTTP requests)")
        return result
    else:
        print("❌ Failed to generate Yelp URL")
        return None



async def scrape_yelp_reviews(
    yelp_url: str,
    place_id: str,
    target_reviews: int,
    out_dir: Path,
    fast: bool = False
):
    """
    Scrape Yelp reviews similar to Google Maps scraping
    
    Args:
        yelp_url: Yelp business URL (e.g. https://www.yelp.com/biz/restaurant-name-city)
        place_id: Google place_id for directory naming
        target_reviews: Number of reviews to scrape
        out_dir: Output directory
        fast: If True, scrape fewer reviews quickly
    """
    
    reviews = []
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    
    # Load existing reviews if any
    reviews_file = out_dir / "reviews.json"
    if reviews_file.exists():
        try:
            existing_data = json.loads(reviews_file.read_text(encoding="utf-8"))
            reviews = existing_data if isinstance(existing_data, list) else []
        except Exception:
            reviews = []
    
    print(f"🎯 Starting Yelp scrape: {yelp_url}")
    print(f"📁 Output: {out_dir}")
    print(f"🎯 Target: {target_reviews} reviews")
    logger.info(f"Starting Yelp scrape: {yelp_url}, target: {target_reviews} reviews")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,  # Show browser window to avoid detection
            chromium_sandbox=False,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage", 
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-crash-reporter",
                "--disable-blink-features=AutomationControlled",  # Hide automation
            ]
        )
        
        try:
            context = await browser.new_context(
                viewport={"width": 1280, "height": 720},
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                # Private browsing mode settings
                locale="en-US",
                timezone_id="America/New_York",
                permissions=[],  # No special permissions
                geolocation=None,  # No location access
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9"
                }
            )
            
            # Add extra stealth measures
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined,
                });
            """)
            
            page = await context.new_page()
            
            # First visit Yelp homepage to establish session
            print("🏠 Visiting Yelp homepage first...")
            try:
                await page.goto("https://www.yelp.com", wait_until="domcontentloaded", timeout=10000)
                await page.wait_for_timeout(2000)
                print("✅ Yelp homepage loaded")
            except Exception as e:
                print(f"⚠️  Homepage load issue: {e}")
            
            # Add human-like delay before navigating to restaurant
            import random
            delay = random.uniform(2, 4)
            print(f"⏱️  Waiting {delay:.1f}s before visiting restaurant...")
            await page.wait_for_timeout(int(delay * 1000))
            
            # Navigate to Yelp business page with more forgiving wait condition
            print("🌐 Navigating to restaurant page...")
            try:
                await page.goto(yelp_url, wait_until="domcontentloaded", timeout=15000)
            except:
                # Fallback: try with even less strict waiting
                print("⚠️  Retrying with basic load...")
                await page.goto(yelp_url, wait_until="load", timeout=10000)
            
            # Wait for page to stabilize
            await page.wait_for_timeout(3000)
            
            # Check if page actually loaded by looking for Yelp content
            yelp_logo = await page.locator('[alt*="Yelp"], .logo, [data-testid*="logo"]').count()
            if yelp_logo == 0:
                print("⚠️  Page may not have loaded properly")
            else:
                print("✅ Yelp page loaded successfully")
            
            # Add human-like delay
            import random
            delay = random.uniform(3, 6)
            print(f"⏱️  Waiting {delay:.1f}s (human-like behavior)")
            await page.wait_for_timeout(int(delay * 1000))
            
            # Navigate to reviews section if not already there
            print("🔍 Looking for reviews section...")
            
            # Try to click "Reviews" tab/link
            reviews_link_selectors = [
                'a:has-text("Reviews")',
                'button:has-text("Reviews")', 
                'a[href*="review"]',
                'a:has-text("All Reviews")',
                '[data-testid*="reviews"]'
            ]
            
            for selector in reviews_link_selectors:
                reviews_link = page.locator(selector)
                if await reviews_link.count() > 0:
                    print(f"✅ Found reviews link: {selector}")
                    await reviews_link.first.click()
                    await page.wait_for_timeout(3000)
                    break
            else:
                # If no reviews link found, try scrolling down to reviews section
                print("🔄 Scrolling to find reviews...")
                for _ in range(3):
                    await page.evaluate("window.scrollBy(0, 800)")
                    await page.wait_for_timeout(1000)
            
            
            seen_review_ids = set(r.get('id', '') for r in reviews)
            scraped_count = 0
            yelp_reviews_added = 0  # Track only new Yelp reviews
            pages_processed = 0  # Track pages to avoid infinite scrolling
            max_pages = 15  # Limit to ~15 pages (150 reviews ÷ 10 per page + buffer)
            
            while yelp_reviews_added < target_reviews and pages_processed < max_pages:
                # Try multiple review selectors (Yelp changes these frequently)
                possible_selectors = [
                    '[data-testid*="review"]:not([data-testid*="rating"])',  # Reviews but not ratings
                    'div[data-testid="serp-ia-card"]',
                    '.review:not(.rating)',
                    '.review-content',
                    '[data-testid="review-card"]',
                    'li[data-testid*="review"]',
                    'div.review-item',
                    # More aggressive selectors for individual reviews
                    'li:has(p)',  # List items with paragraphs (likely reviews)
                    'article:has(p)',  # Articles containing paragraphs
                    'div:has(p):has(a[href*="user"])',  # Divs with paragraphs and user links
                ]
                
                review_locators = None
                review_count = 0
                
                # Try each selector until we find reviews
                for selector in possible_selectors:
                    test_locators = page.locator(selector)
                    count = await test_locators.count()
                    
                    if count > 0:
                        # Validate that these look like actual reviews, not rating summaries
                        first_elem_text = await test_locators.first.inner_text()
                        
                        # Skip if it looks like a rating summary
                        if "overall rating" in first_elem_text.lower() or "stars" in first_elem_text.lower()[:50]:
                            continue
                            
                        review_locators = test_locators
                        review_count = count
                        print(f"✅ Found {count} review elements")
                        logger.info(f"Found {count} review elements using selector")
                        break
                
                if review_count == 0:
                    # Debug: let's see what's actually on the page
                    print("🔍 DEBUG: Looking for any review-like content...")
                    
                    # Save screenshot and HTML for debugging
                    debug_dir = out_dir / "debug"
                    debug_dir.mkdir(exist_ok=True)
                    
                    await page.screenshot(path=str(debug_dir / "page_screenshot.png"))
                    page_html = await page.content()
                    (debug_dir / "page_source.html").write_text(page_html, encoding="utf-8")
                    print(f"📁 Saved debug files to: {debug_dir}")
                    
                    # Check page content
                    if 'review' in page_html.lower():
                        print("✅ Found 'review' text in page")
                        # Count occurrences
                        review_count_in_html = page_html.lower().count('review')
                        print(f"📊 Found {review_count_in_html} mentions of 'review' in HTML")
                    else:
                        print("❌ No 'review' text found in page")
                    
                    # Check for common Yelp content
                    yelp_indicators = ['yelp', 'star', 'rating', 'comment', 'user']
                    for indicator in yelp_indicators:
                        if indicator in page_html.lower():
                            count = page_html.lower().count(indicator)
                            print(f"✅ Found '{indicator}': {count} times")
                    
                    # Look for common patterns
                    debug_selectors = [
                        'div:has-text("star")',
                        'div:has-text("rating")', 
                        'p:has-text("review")',
                        '[class*="review"]',
                        'li', 
                        'article',
                        'div[role="article"]'
                    ]
                    
                    for debug_sel in debug_selectors:
                        debug_count = await page.locator(debug_sel).count()
                        if debug_count > 0:
                            print(f"🔍 Found {debug_count} elements with: {debug_sel}")
                    
                    print("❌ No reviews found - breaking")
                    break
                
                print(f"📄 Found {review_count} review containers on page")
                
                new_reviews_this_batch = 0
                max_elements_per_page = 20  # Limit processing per page
                
                for i in range(min(review_count, max_elements_per_page)):
                    if yelp_reviews_added >= target_reviews:
                        break
                        
                    try:
                        review_elem = review_locators.nth(i)
                        
                        # Extract review data
                        review_data = await extract_yelp_review_data(review_elem)
                        
                        if review_data and review_data['id'] not in seen_review_ids:
                            reviews.append(review_data)
                            seen_review_ids.add(review_data['id'])
                            new_reviews_this_batch += 1
                            scraped_count += 1
                            yelp_reviews_added += 1  # Increment counter for new Yelp reviews
                            
                            print(f"✅ Scraped review #{scraped_count}: {review_data['author']} - {len(review_data['text'])} chars")
                            logger.debug(f"Scraped review #{scraped_count}: {review_data['author']} - {len(review_data['text'])} chars")
                            
                        elif review_data and review_data['id'] in seen_review_ids:
                            pass  # Skip silently - duplicate
                        else:
                            pass  # Skip silently - couldn't extract
                            
                    except Exception as e:
                        pass  # Skip silently - extraction error
                
                # Increment page counter after processing this batch
                pages_processed += 1
                print(f"📄 Processed page {pages_processed}/{max_pages} - Found {new_reviews_this_batch} new reviews")
                logger.info(f"Processed page {pages_processed}/{max_pages} - Found {new_reviews_this_batch} new reviews")
                
                if new_reviews_this_batch == 0:
                    print("🔄 No new reviews found, trying to load more...")
                    
                # Try to load more reviews
                try:
                    # Scroll down more aggressively
                    print("📜 Scrolling to load more reviews...")
                    await page.evaluate("window.scrollBy(0, 1000)")
                    await page.wait_for_timeout(2000)
                    
                    # Scroll to bottom
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(1500)
                    
                    # Look for "Show More" or pagination
                    show_more = page.locator("button:has-text('Show More')")
                    if await show_more.count() > 0:
                        await show_more.click()
                        await page.wait_for_timeout(2000)
                        continue
                    
                    # Try pagination
                    next_button = page.locator('a[aria-label="Next"]')
                    if await next_button.count() > 0:
                        await next_button.click()
                        await page.wait_for_timeout(3000)
                        continue
                    
                    print("🛑 No more reviews to load")
                    break
                    
                except Exception as e:
                    print(f"⚠️  Error loading more reviews: {e}")
                    break
            
            # Save reviews to main reviews.json
            reviews_file.write_text(json.dumps(reviews, indent=2), encoding="utf-8")
            print(f"✅ Saved {len(reviews)} total reviews (including Yelp) to {reviews_file}")
            logger.info(f"Saved {len(reviews)} total reviews to {reviews_file}")
            
        except Exception as e:
            print(f"❌ Scraping error: {e}")
            logger.error(f"Scraping error: {e}")
            
        finally:
            await browser.close()
    
    return reviews


async def extract_author_details(review_elem, author_name):
    """Extract additional author details like full name, location, profile info"""
    details = {}
    
    try:
        # Look for full name in user profile link or hover text
        full_name_selectors = [
            f'a[href*="user"]:has-text("{author_name}")',  # Link containing the author name
            'a[href*="user"]',  # Any user link
            '[title*="' + author_name + '"]',  # Title attribute with name
            f'span:has-text("{author_name}")'  # Span containing name
        ]
        
        for selector in full_name_selectors:
            try:
                elem = review_elem.locator(selector)
                if await elem.count() > 0:
                    # Try to get title attribute (often contains full name)
                    title = await elem.first.get_attribute("title")
                    if title and len(title) > len(author_name):
                        details['full_name'] = title.strip()
                        print(f"  📝 Found full name: '{title}'")
                        break
                        
                    # Try to get href and extract from URL
                    href = await elem.first.get_attribute("href")
                    if href and "/user_details" in href:
                        # Extract user ID or additional info from URL
                        details['profile_url'] = href
                        print(f"  🔗 Found profile URL: '{href}'")
            except Exception:
                continue
        
        # Look for author location/city
        location_selectors = [
            'span:has-text("from")',  # "from San Francisco"
            'div:has-text("Location")',
            '[class*="location"]',
            'span:contains("CA")',  # State abbreviations
            'span:contains("New York")',  # Common cities
        ]
        
        for selector in location_selectors:
            try:
                elem = review_elem.locator(selector)
                if await elem.count() > 0:
                    location_text = await elem.first.inner_text()
                    if location_text and "from" in location_text.lower():
                        location = location_text.replace("from", "").strip()
                        if location:
                            details['location'] = location
                            print(f"  📍 Found location: '{location}'")
                            break
            except Exception:
                continue
        
        # Look for review count or profile stats (indicates ethnicity patterns)
        stats_selectors = [
            'span:has-text("review")',
            'span:has-text("photo")', 
            '[class*="stats"]'
        ]
        
        for selector in stats_selectors:
            try:
                elem = review_elem.locator(selector)
                if await elem.count() > 0:
                    stats_text = await elem.first.inner_text()
                    if "review" in stats_text:
                        details['review_count'] = stats_text.strip()
                        print(f"  📊 Found stats: '{stats_text}'")
                        break
            except Exception:
                continue
                
        # Try to determine if we have a full name vs initials
        if 'full_name' not in details:
            # Check if current author name looks like initials (e.g., "John D.")
            if len(author_name.split()) == 2 and len(author_name.split()[1]) == 2 and author_name.split()[1].endswith('.'):
                details['name_type'] = 'initial'
            else:
                details['name_type'] = 'partial' if len(author_name.split()) == 1 else 'full'
    
    except Exception as e:
        print(f"⚠️  Error extracting author details: {e}")
    
    return details


async def extract_yelp_review_data(review_elem):
    """Extract review data from Yelp review element using generic selectors"""
    try:
        # Try generic selectors for review text (avoid hardcoded class names)
        review_text = ""
        text_selectors = [
            'p:not(:empty)',           # Paragraphs with content
            'div:not(:empty)',         # Divs with content  
            'span:not(:empty)',        # Spans with content
            '*:has-text(".")'          # Any element containing text with periods
        ]
        
        for selector in text_selectors:
            text_elems = review_elem.locator(selector)
            count = await text_elems.count()
            
            if count > 0:
                # Get the longest text (likely the review)
                longest_text = ""
                for i in range(min(count, 3)):  # Check first 3 elements max
                    try:
                        text = await text_elems.nth(i).inner_text()
                        if len(text) > len(longest_text):
                            longest_text = text
                    except:
                        continue
                
                if longest_text.strip() and len(longest_text) > 20:
                    review_text = longest_text
                    break
        
        if not review_text.strip():
            # Ultimate fallback: get all text from the element
            elem_text_preview = await review_elem.inner_text()
            review_text = elem_text_preview
        
        # Basic validation - must have meaningful content
        if not review_text or len(review_text) < 10:
            return None
            
        # Generate ID from text hash
        review_id = f"yelp_{hash(review_text) % 1000000}"
        
        # Try generic selectors for author (look for links and names)
        author = "Anonymous"
        author_selectors = [
            'a[href*="user"]',         # Links containing "user"
            'a[href*="/user_details"]', # Yelp user detail links
            'a:not([href*="photo"]):not([href*="biz"])',  # Links (but not photo/business links)
            'strong:not(:empty)',      # Strong text (often names)
            'b:not(:empty)',           # Bold text
            'span:not(:empty)',        # Spans that might contain names
            'div:not(:empty)'          # Divs that might contain names
        ]
        
        for selector in author_selectors:
            author_elem = review_elem.locator(selector)
            count = await author_elem.count()
            
            if count > 0:
                # Check first few elements for reasonable author names
                for i in range(min(count, 3)):
                    try:
                        author_text = await author_elem.nth(i).inner_text()
                        author_text = author_text.strip()
                        
                        # Validate that this looks like a real Yelp author name
                        # Yelp names typically end with "LastInitial." (e.g. "John D.", "Sarah G.")
                        if (author_text and 
                            len(author_text) > 2 and 
                            len(author_text) < 50 and
                            author_text.endswith('.') and  # Must end with period
                            author_text[-2].isalpha() and  # Second to last char must be letter
                            ' ' in author_text):  # Must have space (first name + last initial)
                            
                            author = author_text
                            break
                    except Exception:
                        continue
                        
                if author != "Anonymous":
                    break
        
        # Try generic selectors for rating (look for aria-labels with stars)
        rating = 0
        rating_selectors = [
            '[aria-label*="star"]',
            '[aria-label*="rating"]',
            '[title*="star"]',
            'div[role="img"]'
        ]
        
        for selector in rating_selectors:
            rating_elem = review_elem.locator(selector)
            if await rating_elem.count() > 0:
                # Try aria-label first
                rating_text = await rating_elem.first.get_attribute("aria-label")
                if not rating_text:
                    rating_text = await rating_elem.first.get_attribute("title")
                
                if rating_text and "star" in rating_text.lower():
                    rating_match = re.search(r'(\d+)', rating_text)
                    if rating_match:
                        rating = int(rating_match.group(1))
                        break
        
        # Clean up review text
        full_text = review_text.strip()
        
        # Basic validation - must have meaningful content
        if not full_text or len(full_text) < 10:
            return None
        
        # Skip if no author found (Anonymous reviews)
        if author == "Anonymous":
            return None
        
        # Skip Q&A sections that aren't real reviews
        if (full_text.endswith('?') or 
            'corkage fee' in full_text.lower() or
            'valet parking' in full_text.lower() or
            'what is' in full_text.lower() or
            'do they' in full_text.lower() or
            len(full_text) < 30):  # Very short texts are likely questions
            return None
            
        return {
            'id': review_id,
            'author': author,
            'text': full_text
        }
        
    except Exception as e:
        print(f"⚠️  Error extracting review data: {e}")
        return None


def scrape_yelp_from_place_id(place_id: str, target_reviews: int = 50, fast: bool = False):
    """
    Complete workflow: Google place_id -> Yelp URL -> Scrape reviews -> Update dish processing
    
    Args:
        place_id: Google Maps place ID
        target_reviews: Number of reviews to scrape
        fast: If True, use faster/fewer reviews mode
    """
    
    # Get Yelp URL from place_id
    result = get_yelp_url_from_place_id(place_id)
    if not result:
        print("❌ Could not generate Yelp URL")
        return None
    
    yelp_url = result['yelp_url']
    restaurant_name = result['google_info']['name']
    
    # Set up output directory - use main reviews directory (not yelp subdirectory)
    reviews_cache_dir = getattr(settings, 'REVIEWS_CACHE_DIR', Path(settings.BASE_DIR) / "var" / "reviews")
    out_dir = Path(reviews_cache_dir) / place_id
    # out_dir = f'/Users/sandeshkakade/gitRepos/vroombaby/var/reviews/{place_id}'
    # Adjust target for fast mode
    if fast:
        target_reviews = min(target_reviews, 25)
    
    print(f"🚀 Starting Yelp scraping for: {restaurant_name}")
    print(f"🔗 Yelp URL: {yelp_url}")
    
    # Run async scraping
    reviews = asyncio.run(scrape_yelp_reviews(yelp_url, place_id, target_reviews, out_dir, fast))
    
    # Import and call _aggregate_now to regenerate dish CSV files
    try:
        from business.management.commands.scrape_reviews import _aggregate_now
        _aggregate_now(out_dir, label="yelp-scrape")
        print(f"✅ Updated dish_mentions.csv and dish_mentions_top5.csv for {place_id}")
        logger.info(f"Updated dish_mentions CSVs for {place_id}")
        
        # Log to scrape.log file
        log_file = out_dir / "scrape.log"
        with open(log_file, "a", encoding="utf-8") as f:
            import time
            timestamp = time.strftime('%F %T')
            review_count = len(reviews) if reviews else 0
            f.write(f"[{timestamp}] YELP SUCCESS: {restaurant_name} -> {review_count} total reviews, target: {target_reviews}\n")
            
    except Exception as e:
        print(f"⚠️ Failed to update dish CSVs: {e}")
        logger.error(f"Failed to update dish CSVs: {e}")
        
        # Log error to scrape.log file  
        log_file = out_dir / "scrape.log"
        with open(log_file, "a", encoding="utf-8") as f:
            import time
            timestamp = time.strftime('%F %T')
            f.write(f"[{timestamp}] YELP ERROR: {restaurant_name} -> Failed to update CSVs: {e}\n")
    
    return {
        'reviews': reviews,
        'yelp_url': yelp_url,
        'restaurant_name': restaurant_name,
        'output_dir': str(out_dir)
    }


if __name__ == "__main__":
    # Test with Sweet Maple
    sweet_maple_place_id = "ChIJTel9dGCAhYARQGwrTfGZ07M"
    
    # Test URL generation
    print("=== Testing URL Generation ===")
    test_yelp_integration(sweet_maple_place_id)
    
    # Test scraping (uncomment to run)
    print("\n=== Testing Scraping ===")
    scrape_yelp_from_place_id(sweet_maple_place_id, target_reviews=50, fast=True)