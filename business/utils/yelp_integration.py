"""
Yelp Integration Utilities
Separate module for Google Maps to Yelp business URL conversion and scraping
"""

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from django.conf import settings
from googlemaps import Client as GoogleMapsClient
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
        return None


def test_yelp_integration(place_id):
    """Test function to verify Yelp URL generation (not validation due to bot blocking)"""
    
    result = get_yelp_url_from_place_id(place_id)
    
    if result:
        logger.info("Note: Use this URL with Playwright for scraping (Yelp blocks direct HTTP requests)")
        return result
    else:
        logger.warning("Failed to generate Yelp URL")
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
    
    logger.info(f"Starting Yelp scrape: {yelp_url}, target: {target_reviews} reviews")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,  # Set to False to see browser during debugging
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
            logger.debug("Visiting Yelp homepage first...")
            try:
                await page.goto("https://www.yelp.com", wait_until="domcontentloaded", timeout=10000)
                await page.wait_for_timeout(2000)
                logger.debug("Yelp homepage loaded")
            except Exception as e:
                logger.warning(f"Homepage load issue: {e}")
            
            # Add human-like delay before navigating to restaurant
            import random
            delay = random.uniform(2, 4)
            logger.debug(f"Waiting {delay:.1f}s before visiting restaurant...")
            await page.wait_for_timeout(int(delay * 1000))
            
            # Navigate to Yelp business page with more forgiving wait condition
            logger.debug("Navigating to restaurant page...")
            try:
                await page.goto(yelp_url, wait_until="domcontentloaded", timeout=15000)
            except:
                # Fallback: try with even less strict waiting
                logger.debug("Retrying with basic load...")
                await page.goto(yelp_url, wait_until="load", timeout=10000)
            
            # Wait for page to stabilize
            await page.wait_for_timeout(3000)
            
            # Check if page actually loaded by looking for Yelp content
            yelp_logo = await page.locator('[alt*="Yelp"], .logo, [data-testid*="logo"]').count()
            if yelp_logo == 0:
                logger.warning("Page may not have loaded properly")
            else:
                logger.debug("Yelp page loaded successfully")
            
            # Add human-like delay
            import random
            delay = random.uniform(3, 6)
            logger.debug(f"Waiting {delay:.1f}s (human-like behavior)")
            await page.wait_for_timeout(int(delay * 1000))
            
            # Navigate to reviews section if not already there
            logger.debug("Looking for reviews section...")
            
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
                    logger.debug(f"Found reviews link: {selector}")
                    await reviews_link.first.click()
                    await page.wait_for_timeout(3000)
                    break
            else:
                # If no reviews link found, try scrolling down to reviews section
                logger.debug("Scrolling to find reviews...")
                for _ in range(3):
                    await page.evaluate("window.scrollBy(0, 800)")
                    await page.wait_for_timeout(1000)
            
            
            seen_review_ids = set(r.get('id', '') for r in reviews)
            seen_authors = set(r.get('author', '').strip() for r in reviews if r.get('author', '').strip())
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
                        logger.debug(f"Found {count} review elements")
                        logger.info(f"Found {count} review elements using selector")
                        break
                
                if review_count == 0:
                    # Debug: let's see what's actually on the page
                    logger.debug("Looking for any review-like content...")
                    
                    # Save screenshot and HTML for debugging
                    debug_dir = out_dir / "debug"
                    debug_dir.mkdir(exist_ok=True)
                    
                    await page.screenshot(path=str(debug_dir / "page_screenshot.png"))
                    page_html = await page.content()
                    (debug_dir / "page_source.html").write_text(page_html, encoding="utf-8")
                    logger.debug(f"Saved debug files to: {debug_dir}")
                    
                    # Check page content
                    if 'review' in page_html.lower():
                        logger.debug("Found 'review' text in page")
                        # Count occurrences
                        review_count_in_html = page_html.lower().count('review')
                        logger.debug(f"Found {review_count_in_html} mentions of 'review' in HTML")
                    else:
                        logger.debug("No 'review' text found in page")
                    
                    # Check for common Yelp content
                    yelp_indicators = ['yelp', 'star', 'rating', 'comment', 'user']
                    for indicator in yelp_indicators:
                        if indicator in page_html.lower():
                            count = page_html.lower().count(indicator)
                    
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
                    
                    logger.debug("No reviews found - breaking")
                    break
                
                logger.debug(f"Found {review_count} review containers on page")
                
                new_reviews_this_batch = 0
                max_elements_per_page = 20  # Limit processing per page
                
                for i in range(min(review_count, max_elements_per_page)):
                    if yelp_reviews_added >= target_reviews:
                        break
                        
                    try:
                        review_elem = review_locators.nth(i)
                        
                        # Extract review data
                        review_data = await extract_yelp_review_data(review_elem)
                        
                        if review_data:
                            author = review_data.get('author', '').strip()
                            review_id = review_data.get('id', '')
                            
                            # Check for duplicates by author name first (primary check)
                            if author and author in seen_authors:
                                pass  # Skip silently - author already exists
                            elif review_id in seen_review_ids:
                                pass  # Skip silently - review ID duplicate
                            else:
                                # Add the review
                                reviews.append(review_data)
                                if review_id:
                                    seen_review_ids.add(review_id)
                                if author:
                                    seen_authors.add(author)
                                new_reviews_this_batch += 1
                                scraped_count += 1
                                yelp_reviews_added += 1  # Increment counter for new Yelp reviews
                                
                                logger.info(f"✅ Scraped review #{scraped_count}: {author} - {len(review_data['text'])} chars")
                        else:
                            pass  # Skip silently - couldn't extract
                            
                    except Exception as e:
                        pass  # Skip silently - extraction error
                
                # Increment page counter after processing this batch
                pages_processed += 1
                logger.info(f"Processed page {pages_processed}/{max_pages} - Found {new_reviews_this_batch} new reviews")
                
                if new_reviews_this_batch == 0:
                    logger.debug("No new reviews found, trying to load more...")
                    
                # Try to load more reviews
                try:
                    # Scroll down more aggressively
                    logger.debug("Scrolling to load more reviews...")
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
                    
                    logger.debug("No more reviews to load")
                    break
                    
                except Exception as e:
                    logger.warning(f"Error loading more reviews: {e}")
                    break
            
            # Save reviews to main reviews.json
            reviews_file.write_text(json.dumps(reviews, indent=2), encoding="utf-8")
            logger.info(f"Saved {len(reviews)} total reviews to {reviews_file}")
            
        except Exception as e:
            logger.error(f"Scraping error: {e}")
            logger.error(f"Scraping error: {e}")
            
        finally:
            # After scraping reviews, scroll back to top and harvest missing dish images
            try:
                await page.evaluate("window.scrollTo(0, 0)")
                await page.wait_for_timeout(3000)  # Wait for page to settle
                
                # Check for missing dishes and harvest images
                missing_dishes = find_missing_dish_images(out_dir)
                if missing_dishes:
                    logger.info(f"Found {len(missing_dishes)} dishes without images, harvesting now")
                    
                    harvested_count = await _harvest_images_from_yelp_photos(
                        page, missing_dishes, out_dir
                    )
                    logger.info(f"Successfully harvested {harvested_count} dish images from Yelp")
                    
            except Exception as img_error:
                logger.warning(f"Image harvesting failed: {img_error}")
                
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
                        break
                        
                    # Try to get href and extract from URL
                    href = await elem.first.get_attribute("href")
                    if href and "/user_details" in href:
                        # Extract user ID or additional info from URL
                        details['profile_url'] = href
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
    
    return details


def find_missing_dish_images(out_dir: Path) -> List[str]:
    """
    Compare dish_mentions_top5.csv with dish_images.json to find dishes without images.
    Returns list of dish names that need images.
    """
    # Read top dishes from CSV
    top5_csv = out_dir / "dish_mentions_top5.csv"
    if not top5_csv.exists():
        return []
    
    try:
        df = pd.read_csv(top5_csv)
        top_dishes = df['dish'].unique().tolist()
    except Exception:
        return []
    
    # Read existing images
    images_json = out_dir / "dish_images.json"
    existing_dishes = set()
    if images_json.exists():
        try:
            existing_images = json.loads(images_json.read_text(encoding="utf-8"))
            existing_dishes = set(existing_images.keys())
        except Exception:
            pass
    
    # Find missing dishes (case-insensitive matching)
    missing_dishes = []
    for dish in top_dishes:
        # Check exact match first
        if dish not in existing_dishes:
            # Check case-insensitive match
            dish_lower = dish.lower()
            found = False
            for existing in existing_dishes:
                if existing.lower() == dish_lower:
                    found = True
                    break
            if not found:
                missing_dishes.append(dish)
    
    return missing_dishes


async def _harvest_images_from_yelp_photos(page, missing_dishes: List[str], out_dir: Path) -> int:
    """
    Harvest missing dish images from Yelp photos section using existing page context.
    Returns number of images successfully harvested.
    """
    harvested_count = 0
    
    try:
        # Look for "See all X photos" button
        photos_selectors = [
            'span.y-css-3ptwl3:has-text("See all")',         # Your exact class
            'a[href*="biz_photos"] span:has-text("See all")', # Link to biz_photos with "See all" 
            'div.photo-header-buttons__09f24__UU4lV a',       # Your exact container class
            '[class*="photo-header"] a:has-text("photos")',   # Generic photo header
            'span:has-text("See all") >> xpath=..',           # Parent of "See all" span
            'a:has-text("photos")',                           # Any link with photos
        ]
        
        photos_link = None
        for selector in photos_selectors:
            test_link = page.locator(selector).first
            if await test_link.count() > 0:
                photos_link = test_link
                logger.info(f"Found photos link with selector: {selector}")
                break
        
        if not photos_link:
            logger.warning("No 'See all photos' link found on Yelp page")
            return 0
        
        # Click to open photos section
        await photos_link.click()
        await page.wait_for_timeout(3000)
        
        # Find search box for photos
        search_selectors = [
            'input[placeholder*="Search photos" i]',           # Your exact example
            'input[aria-labelledby*="search" i]',             # Search with aria-labelledby  
            'input.input__09f24__yaqh1',                      # Your specific class
            'input[class*="inline-search"]',                  # Inline search class
            'input[type="text"][placeholder*="search" i]',    # Generic search input
            'input[class*="search"]',                         # Any search-related class
        ]
        
        search_box = None
        for selector in search_selectors:
            test_input = page.locator(selector).first
            if await test_input.count() > 0:
                search_box = test_input
                logger.info(f"Found search box with selector: {selector}")
                break
        
        if not search_box:
            logger.warning("No photo search box found")
            return 0
        
        # Search for each missing dish
        for dish in missing_dishes:  # Process all missing dishes
            try:
                logger.info(f"Searching Yelp photos for: {dish}")
                
                # Try multiple search variations
                search_terms = [dish, dish.lower(), dish.replace(" ", "")]
                
                for search_term in search_terms:
                    # Clear and search for dish
                    await search_box.fill("")
                    await search_box.fill(search_term)
                    await page.keyboard.press("Enter")
                    await page.wait_for_timeout(3000)  # Longer wait for search results
                    
                    # Look for actual dish images (avoid tracking pixels and logos)
                    image_selectors = [
                        'img[src*="bphoto"]',  # Yelp business photos (most likely dish images)
                        'img[alt*="Photo of"][src*="yelp"]',  # Images with photo alt text
                        'img[loading="eager"][src*="yelp"]',  # Eagerly loaded images
                        'img[width="100%"][src*="yelp"]',  # Full-width images
                        'img[src*="yelpcdn.com/bphoto"]',  # Specific yelp photo URLs
                        'img[src*="s3-media"][src*="bphoto"]',  # S3 business photos only
                    ]
                    
                    # Try to find multiple images and pick the best one based on alt text
                    found_image = False
                    for selector in image_selectors:
                        test_images = page.locator(selector)
                        count = await test_images.count()
                        
                        if count > 0:
                            # Try multiple images and look for one with matching alt text
                            for i in range(min(count, 10)):  # Check more images to find dish-specific ones
                                try:
                                    img_elem = test_images.nth(i)
                                    img_src = await img_elem.get_attribute('src')
                                    alt_text = await img_elem.get_attribute('alt') or ""
                                    
                                    # Validate it's not a tracking pixel or business logo
                                    exclude_patterns = ['adroll', 'doubleclick', 'facebook', 'google-analytics', 'businessregularlogo', 'logo', '/ms.jpg']
                                    if img_src and not any(exclude in img_src for exclude in exclude_patterns):
                                        
                                        # Check if alt text contains the dish name (case-insensitive)
                                        dish_lower = dish.lower()
                                        alt_lower = alt_text.lower()
                                        
                                        # Look for exact dish name or key words from dish name in alt text
                                        dish_words = dish_lower.split()
                                        alt_contains_dish = (
                                            dish_lower in alt_lower or
                                            any(word in alt_lower for word in dish_words if len(word) > 3)  # Skip short words like "and"
                                        )
                                        
                                        if alt_contains_dish:
                                            logger.info(f"Found matching image for {dish}: alt='{alt_text}', src={img_src}")
                                            
                                            # Save to dish_images.json
                                            images_json = out_dir / "dish_images.json"
                                            existing = {}
                                            if images_json.exists():
                                                try:
                                                    existing = json.loads(images_json.read_text(encoding="utf-8"))
                                                except Exception:
                                                    pass
                                            
                                            existing[dish] = {
                                                "image_url": img_src,
                                                "caption": dish
                                            }
                                            
                                            images_json.write_text(json.dumps(existing, indent=2), encoding="utf-8")
                                            harvested_count += 1
                                            logger.info(f"✅ Harvested image for {dish}: {img_src}")
                                            found_image = True
                                            break
                                        
                                except Exception:
                                    continue
                            
                            if found_image:
                                break
                        
                        if found_image:
                            break
                    
                    if found_image:
                        break  # Don't try other search terms if we found an image
                
                if not found_image:
                    logger.warning(f"No suitable image found for {dish}")
            
            except Exception as e:
                logger.warning(f"Failed to harvest image for {dish}: {e}")
                continue
    
    except Exception as e:
        logger.error(f"Photo harvesting error: {e}")
    
    return harvested_count

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
    
    logger.info(f"Starting Yelp scraping for: {restaurant_name}")
    logger.info(f"Yelp URL: {yelp_url}")
    
    # Run async scraping
    reviews = asyncio.run(scrape_yelp_reviews(yelp_url, place_id, target_reviews, out_dir, fast))
    
    # Import and call _aggregate_now to regenerate dish CSV files
    try:
        from business.management.commands.scrape_reviews import _aggregate_now
        _aggregate_now(out_dir, label="yelp-scrape")
        logger.info(f"Updated dish_mentions CSVs for {place_id}")
        
        # After updating CSVs, image harvesting is now integrated into review scraping
        logger.info("Image harvesting completed during review scraping")
        
        # Log to scrape.log file
        log_file = out_dir / "scrape.log"
        with open(log_file, "a", encoding="utf-8") as f:
            timestamp = time.strftime('%F %T')
            review_count = len(reviews) if reviews else 0
            f.write(f"[{timestamp}] YELP SUCCESS: {restaurant_name} -> {review_count} total reviews, target: {target_reviews}\n")
            
    except Exception as e:
        logger.error(f"Failed to update dish CSVs: {e}")
        
        # Log error to scrape.log file  
        log_file = out_dir / "scrape.log"
        with open(log_file, "a", encoding="utf-8") as f:
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
    test_yelp_integration(sweet_maple_place_id)
    
    # Test scraping (uncomment to run)
    scrape_yelp_from_place_id(sweet_maple_place_id, target_reviews=50, fast=True)