import gc
import logging
import os, csv
import traceback
from urllib.parse import urlparse, parse_qs

from django.core.management.base import BaseCommand

from googlemaps import Client as GoogleMapsClient
from openai import OpenAI

import asyncio
from playwright.async_api import async_playwright

import re, hashlib, time
from typing import List, Optional, Dict, Tuple
import pandas as pd
import math
import json
from pathlib import Path
from datetime import timedelta
from django.conf import settings
from difflib import SequenceMatcher
from unidecode import unidecode
import inflect
# Defer heavy ML imports until needed (after menu extraction)
# import spacy - loaded lazily in _get_nlp()
# import ethnicolr - loaded lazily in enrich_groups_with_ethnicolr()
from functools import lru_cache

logger = logging.getLogger(__name__)

CACHE_BASE = Path(settings.REVIEWS_CACHE_DIR)

TTL = timedelta(days=7)   # tune as you like
TABS = {"popular", "indian","american","chinese","mexican","italian"}
_ETH_MAP = {
    "southasian":"Indian","indiansubcontinent":"Indian",
    "eastasian":"Chinese","hanchinese":"Chinese","chinese":"Chinese",
    "mexican":"Mexican","mexicanamerican":"Mexican",
    "italian":"Italian",
    "angloamerican":"American","northamerican":"American","us":"American","european":"American",
}

BAD_KW = re.compile(
    r"\b(parking|wheelchair|kid[-\s]?friendly|kid[-\s]?friendliness|accessibilit|"
    r"dietary\s+restrictions?|vegetarian\s+(menu|offerings)|gluten[-\s]?free\s+labeled|"
    r"paid\s+parking|parking\s+options)\b", re.I
)

FIELD_HEADERS = [
    "Meal type", "Price per person", "Food:", "Service:",
    "Atmosphere:", "Wait time", "Seating type"
]

TAB_LABELS = {"Popular","Indian","American","Chinese","Mexican","Italian"}

class Command(BaseCommand):
    help = "Scrape Google Maps reviews for a place_id, then build dish_mentions for that place."

    def add_arguments(self, parser):
        parser.add_argument("-p", "--place_id", required=True)
        parser.add_argument("--target", type=int, default=40)
        parser.add_argument("--time-budget", type=int, default=12)
        parser.add_argument("--out-dir")
        parser.add_argument("--append", action="store_true")
        parser.add_argument("--fast", action="store_true")
        parser.add_argument("--category", default="restaurant", help="Category: restaurant, coffee, bar, brunch, or dessert")

    def handle(self, *args, **options):
        place_id = options["place_id"]
        category = options.get("category", "restaurant")
        target = int(options.get("target") or 0)
        time_budget = int(options.get("time_budget") or 0)
        if options.get("fast"):
            target, time_budget = max(target, 24), max(time_budget, 10)
        else:
            target, time_budget = max(target, 150), max(time_budget, 90)

        default_base = Path(getattr(settings, "REVIEWS_CACHE_DIR",
                                    Path(settings.BASE_DIR) / "var" / "reviews"))
        out_dir = Path(options["out_dir"]) if options.get("out_dir") else (default_base / place_id)
        out_dir.mkdir(parents=True, exist_ok=True)

        # 1) Get contact info first (with caching)
        contact_file = out_dir / "contact_info.json"
        contact_info = None
        
        # Check if contact info exists and is fresh (< 30 days)
        if contact_file.exists():
            try:
                with open(contact_file, 'r', encoding='utf-8') as f:
                    cached_contact = json.load(f)
                    cached_time = pd.to_datetime(cached_contact.get('cached_at', '1970-01-01'))
                    age_days = (pd.Timestamp.now() - cached_time).days
                    
                    if age_days < 30:  # Contact info is fresh
                        contact_info = cached_contact
                        logger.info(f"Using cached contact info for {place_id} (age: {age_days} days)")
                    else:
                        logger.info(f"Contact info stale for {place_id} (age: {age_days} days), refreshing")
            except Exception as e:
                logger.warning(f"Error reading cached contact info: {e}")
        
        # Fetch contact info if not cached or stale
        if not contact_info:
            gmaps = GoogleMapsClient(key=settings.GOOGLE_API_KEY)
            try:
                resp = gmaps.place(place_id=place_id, fields=[
                    "url", "name", "formatted_phone_number", "website", 
                    "opening_hours", "current_opening_hours", "rating", "user_ratings_total"
                ])
                place_data = resp["result"]
                
                # Extract and structure contact info
                contact_info = {
                    "name": place_data.get("name"),
                    "phone": place_data.get("formatted_phone_number"),
                    "website": place_data.get("website"),
                    "rating": place_data.get("rating"),
                    "user_ratings_total": place_data.get("user_ratings_total"),
                    "current_opening_hours": place_data.get("current_opening_hours"),
                    "opening_hours": place_data.get("opening_hours"),
                    "cached_at": pd.Timestamp.now().isoformat(),
                    "place_url": place_data.get("url")
                }
                
                # Save contact info to file
                with open(contact_file, 'w', encoding='utf-8') as f:
                    json.dump(contact_info, f, indent=2, ensure_ascii=False)
                
                logger.info(f"Cached fresh contact info for {place_id}")
                
            except Exception as e:
                if "NOT_FOUND" in str(e):
                    self.stderr.write("❌ Place ID invalid, aborting.")
                    return
                logger.error(f"Error fetching contact info: {e}")
                # Continue with review scraping even if contact info fails
                contact_info = {"error": str(e), "cached_at": pd.Timestamp.now().isoformat()}
        
        # Get place URL for review scraping
        place_url = contact_info.get("place_url")
        if not place_url:
            # Fallback: make minimal API call just for URL
            gmaps = GoogleMapsClient(key=settings.GOOGLE_API_KEY)
            try:
                resp = gmaps.place(place_id=place_id, fields=["url"])
                place_url = resp["result"]["url"]
            except Exception as e:
                if "NOT_FOUND" in str(e):
                    self.stderr.write("❌ Place ID invalid, aborting.")
                    return
                raise

        # 2) Canonicalize: force English and pin to ?cid=… if present
        p = urlparse(place_url)
        q = parse_qs(p.query)
        if "cid" in q:
            cid = q["cid"][0]
            place_url = f"https://www.google.com/maps/place/?cid={cid}&hl=en"
        else:
            sep = "&" if "?" in place_url else "?"
            place_url = f"{place_url}{sep}hl=en"

        # 3) Scrape (async) - Pass category from frontend
        asyncio.run( scrape_reviews(
            place_url=place_url, place_id=place_id, target_reviews=target, time_budget=time_budget, out_dir=out_dir,
            build_mentions_now=True,
            harvest_images_now=True,
            top_k_images=5,
            category=category,  # Pass category instead of business_types
            is_fast_job=options.get("fast", False),  # Pass fast flag for menu timing
            contact_info=contact_info,  # Pass contact info for menu extraction
        ))

        # Clear lock if our run created it
        lock = out_dir / ".refresh.lock"
        try:
            lock.unlink(missing_ok=True)
        except Exception:
            pass

# --- dish normalization (library-based) ---
# pip install spacy unidecode inflect rapidfuzz
# python -m spacy download en_core_web_sm


# Note: spaCy is lazy-loaded in _get_nlp() to avoid 2-minute startup delay
_inflect = inflect.engine()
_SMALL = {"and","of","with","on","in","to","for","by","at"}

# Words that should stay plural for better readability
_KEEP_PLURAL = {
    "fries", "chips", "wings", "ribs", "tacos", "nachos",
    "beans", "peas", "greens", "tots", "bites", "strips",
    "nuggets", "rings", "noodles", "potatoes", "vegetables",
    "meatballs", "dumplings", "wontons", "enchiladas", "tamales"
}

def _singular(tok: str) -> str:
    """Singularize a word, but keep certain plural food terms"""
    tok_lower = tok.lower()

    # Keep plural for common food items
    if tok_lower in _KEEP_PLURAL:
        return tok

    s = _inflect.singular_noun(tok)
    return s if isinstance(s, str) and s else tok

def smart_normalize_dish(text: str) -> str:
    """
    Smart normalization to handle common variations without hardcoded lexicons
    """
    if not text:
        return ""
    
    t = text.strip()
    
    # Handle common abbreviations and symbols
    t = re.sub(r'\b&\b', ' and ', t)  # & -> and
    t = re.sub(r'\bn\b', ' and ', t)  # n -> and (in context like "mac n cheese")
    t = re.sub(r'\bn\'\b', ' and ', t)  # n' -> and 
    
    # Handle "the" prefix (case insensitive)
    t = re.sub(r'^the\s+', '', t, flags=re.I)
    
    # Handle common misspellings
    misspellings = {
        r'\bcappucino\b': 'cappuccino',
        r'\bexpresso\b': 'espresso', 
        r'\bmocha\b': 'mocha',
    }
    for wrong, right in misspellings.items():
        t = re.sub(wrong, right, t, flags=re.I)
    
    # Clean up extra whitespace
    t = re.sub(r'\s+', ' ', t).strip()
    
    # Title case for display
    return t.title() if t else ""

@lru_cache(maxsize=4096)
def normalize_dish_key_and_label(text: str) -> Tuple[str, str]:
    """
    Returns (key, display_label).
    key: lowercase, no leading DET, unified connectors, punctuation stripped, singularized.
    label: Title Case (keeps small words lowercase) for UI.
    """
    if not text:
        return "", ""
    t = unidecode(str(text)).strip()
    if not t:
        return "", ""

    doc = _get_nlp()(t)

    norm_tokens = []
    for i, tok in enumerate(doc):
        if i == 0 and tok.pos_ == "DET":  # drop leading 'the/a/an' via POS
            continue
        if tok.is_space or tok.is_punct:
            continue

        s = tok.text.lower()
        if s in {"&", "n", "n."}:
            s = "and"

        s = re.sub(r"[''`]", "", s)          # apostrophes
        s = re.sub(r"[-_/]", " ", s)         # separators -> space
        s = re.sub(r"[^a-z0-9\s]", " ", s)   # other punct -> space
        s = re.sub(r"\s+", " ", s).strip()
        if not s:
            continue

        parts = [_singular(p) for p in s.split()]
        # Keep plural form for food items (don't singularize the last word which is typically the main noun)
        # This preserves "Fries", "Chips", "Wings" etc. in their natural plural form
        if parts and len(s.split()) > 0:
            original_parts = s.split()
            parts[-1] = original_parts[-1]  # Keep the last word in its original form
        norm_tokens.extend(parts)

    key = " ".join(norm_tokens).strip()
    if not key:
        return "", ""

    words = key.split()
    label = " ".join(w if (i and w in _SMALL) else w.capitalize()
                     for i, w in enumerate(words))
    return key, label

# Add this helper near the top of scrape_reviews.py
def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

_nlp = None

def _get_nlp():
    """Lazy-load spacy (deferred to avoid 2-minute startup delay for menu extraction)"""
    global _nlp
    if _nlp is None:
        import spacy  # Import only when needed (after menu extraction)
        _nlp = spacy.load("en_core_web_sm", disable=["ner","parser","lemmatizer","textcat"])
    return _nlp

def _read_seed_reviews(out_dir: Path):
    """Return (seed_reviews, seen_ids, seen_text_norm) from reviews.json if present."""
    src = out_dir / "reviews.json"
    if not src.exists():
        return [], set(), set()
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except Exception:
        return [], set(), set()

    seen_ids: set[str] = set()
    seen_text: set[str] = set()
    for r in data:
        rid = (r.get("id") or "").strip()
        txt = _norm_text(r.get("text") or "")
        if rid:
            seen_ids.add(rid)
        elif txt:
            seen_text.add(txt)
    return data, seen_ids, seen_text



def _match_dishes_to_menu(out_dir: Path):
    """
    Match review dishes to menu items and add prices.
    Only keeps dishes that match menu items (orderable).
    Maintains top 5 per ethnicity after filtering.
    """
    try:
        # Load menu structure
        menu_file = out_dir / "menu_structure.json"
        if not menu_file.exists():
            return

        with open(menu_file, 'r', encoding='utf-8') as f:
            menu_data = json.load(f)

        menu_items = menu_data.get('items', [])
        if not menu_items:
            return

        # Load dish mentions
        dish_csv = out_dir / "dish_mentions.csv"
        if not dish_csv.exists():
            return

        # Check if file is empty (no dish mentions found)
        if dish_csv.stat().st_size == 0:
            return

        df = pd.read_csv(dish_csv, dtype=str)
        if df.empty:
            return

        # Match each dish to menu items and build a price/description map
        price_map = {}  # dish_name -> (price, description)

        for _, row in df.iterrows():
            review_dish = row['dish']

            # Find best matching menu item
            best_match = None
            best_score = 0.0

            for menu_item in menu_items:
                menu_name = menu_item.get('name', '')
                menu_category = menu_item.get('category', '')
                if not menu_name:
                    continue

                # Calculate match score with category info
                score = _dish_match_score(review_dish, menu_name, menu_category)

                if score > best_score:
                    best_score = score
                    best_match = menu_item

            # If good match (>= 0.35), add price/description to map (lowered threshold for better fuzzy matching)
            if best_score >= 0.35 and best_match:
                price_map[review_dish] = {
                    'matched_name': best_match.get('name', ''),  # Store matched menu name
                    'price': best_match.get('price', ''),
                    'description': best_match.get('description', ''),
                    'match_score': f"{best_score:.2f}"
                }

        if not price_map:
            return

        # Create reverse map: matched_name -> data (for after we update dish names)
        matched_dish_map = {}
        for review_dish, data in price_map.items():
            matched_name = data.get('matched_name', review_dish)
            matched_dish_map[matched_name] = data

        # Update dish names to match menu (for ordering automation)
        df['dish'] = df['dish'].map(lambda d: price_map.get(d, {}).get('matched_name', d))

        # Add price/description columns using matched names
        df['price'] = df['dish'].map(lambda d: matched_dish_map.get(d, {}).get('price', ''))
        df['description'] = df['dish'].map(lambda d: matched_dish_map.get(d, {}).get('description', ''))
        df['match_score'] = df['dish'].map(lambda d: matched_dish_map.get(d, {}).get('match_score', ''))

        # Save updated CSV with ALL original dishes (but now some have prices)
        df.to_csv(dish_csv, index=False)

        # Also save top5 version
        top5_csv = out_dir / "dish_mentions_top5.csv"
        df.to_csv(top5_csv, index=False)

        matched_count = len(price_map)
        total_count = len(df)

        # Log per-ethnicity stats
        for ethnicity in df['ethnicity_ui'].unique():
            eth_dishes = df[df['ethnicity_ui'] == ethnicity]
            eth_with_price = eth_dishes[eth_dishes['price'] != '']

    except Exception as e:
        traceback.print_exc()


def _dish_match_score(review_dish: str, menu_dish: str, menu_category: str = '') -> float:
    """
    Calculate match score between review dish and menu dish.
    Uses menu category to prioritize entrees over sides when review mentions both.
    Returns 0.0-1.0, higher is better match.
    """
    if not review_dish or not menu_dish:
        return 0.0

    # Normalize both
    r = review_dish.lower().strip()
    m = menu_dish.lower().strip()

    # Remove common noise words and punctuation
    noise_words = {'and', 'with', 'or', 'the', 'a', 'an'}
    r_clean = ' '.join([w for w in r.replace('*', '').replace('+', '').split() if w not in noise_words])
    m_clean = ' '.join([w for w in m.replace('*', '').replace('+', '').split() if w not in noise_words])

    # Exact match
    if r_clean == m_clean:
        return 1.0

    # Substring match (review dish is part of menu dish)
    if r_clean in m_clean:
        return 0.92

    # Reverse substring (menu dish is part of review dish)
    if m_clean in r_clean:
        return 0.85

    # Token overlap (Jaccard similarity) - using cleaned tokens
    r_tokens = set(r_clean.split())
    m_tokens = set(m_clean.split())

    if r_tokens and m_tokens:
        intersection = len(r_tokens & m_tokens)
        union = len(r_tokens | m_tokens)
        jaccard = intersection / union if union > 0 else 0.0

        # Category-based prioritization
        # Normalize category to lower case for matching
        category = menu_category.lower()

        # Main course categories (entrees, mains)
        is_main = any(term in category for term in ['main', 'entree', 'entrée', 'dinner', 'lunch'])

        # Side/appetizer categories
        is_side = any(term in category for term in ['side', 'appetizer', 'starter', 'small plate'])

        # If review has multiple words (combo like "burger and fries")
        # and menu is a side, penalize heavily
        if len(r_tokens) > 1 and is_side:
            # Check if review has words NOT in menu (likely the main dish)
            unmatched_review_words = r_tokens - m_tokens
            if unmatched_review_words:
                # Review mentions other items, menu is just a side -> low score
                return max(0.25, jaccard * 0.5)  # Penalty

        # Boost main courses when matching combos
        category_boost = 0.0
        if len(r_tokens) > 1 and is_main:
            category_boost = 0.15  # Boost entrees when review is a combo

        # Key word matches
        key_matches = len(r_tokens & m_tokens)
        key_boost = 0.0
        if key_matches >= 1:
            key_boost = min(0.3, key_matches * 0.15)

        # Fuzzy string similarity
        fuzzy = SequenceMatcher(None, r_clean, m_clean).ratio()

        # Blend: 50% jaccard + 30% fuzzy + key boost + category boost
        return min(0.95, 0.5 * jaccard + 0.3 * fuzzy + key_boost + category_boost)

    return 0.0


def _aggregate_now(out_dir: Path, label: str = "", category: str = "restaurant"):  # === SIMPLIFIED ===
    """
    Rebuild authors.csv and dish_mentions CSV from current reviews.json.
    Always creates dish_mentions.csv regardless of category - category is just for search filtering.
    """
    try:
        reviews_json = str(out_dir / "reviews.json")
        authors_csv  = str(out_dir / "authors.csv")

        # authors (incremental if your helper supports it)
        authors_csv_path = write_or_update_authors_csv(reviews_json, authors_csv)


        # Always use dish_lexicon.csv - it covers food, drinks, and other items
        lexicon_csv_path = get_lexicon_path_for_category(out_dir, "restaurant")
        
        # Always use same file naming structure
        out_csv = str(out_dir / "dish_mentions.csv")
        save_raw_csv = str(out_dir / "dish_mentions_raw.csv")
        out_csv_topk = str(out_dir / "dish_mentions_top5.csv")

        # aggregate to unified CSV - extract whatever people recommend
        build_dish_mentions(
            reviews_json=reviews_json,
            authors_csv=authors_csv_path,
            lexicon_csv=lexicon_csv_path,
            out_csv=out_csv,
            save_raw_csv=save_raw_csv,
            mode="both",
            limit_per_ethnicity=5,
            out_csv_topk=out_csv_topk,
        )
        
    except Exception as e:


async def extract_menu_from_page(page, page_url: str) -> Optional[Dict]:
    """
    Extract menu structure from an ordering page using OpenAI.
    Assumes page is already loaded and ready.

    Returns menu_data dict or None if extraction fails.
    """
    try:
        # Scroll to load menu content (slower for lazy-loaded items)
        for i in range(15):  # More scrolls to load all lazy content
            await page.mouse.wheel(0, 2500)
            await page.wait_for_timeout(1500)  # Longer wait for lazy loading
            if i % 3 == 0:

        # Final wait for last batch of items to load
        await page.wait_for_timeout(3000)

        html_content = await page.content()

        # Clean HTML to reduce token usage
        html_content = re.sub(r'<script[^>]*>.*?</script>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
        html_content = re.sub(r'<style[^>]*>.*?</style>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
        html_content = re.sub(r'<!--.*?-->', '', html_content, flags=re.DOTALL)
        html_content = re.sub(r'\s+style="[^"]*"', '', html_content, flags=re.IGNORECASE)

        # Debug: Save cleaned HTML for inspection (DoorDash only)
        if 'doordash.com' in page_url.lower() and len(html_content) < 5000:
            debug_file = Path("/tmp/doordash_menu_debug.html")
            debug_file.write_text(html_content, encoding='utf-8')

        # Verify menu content is present
        menu_indicators = ['price', '$', 'menu', 'item', 'add to cart', 'description']
        has_menu = any(indicator in html_content.lower() for indicator in menu_indicators)
        if not has_menu:
            return None

        # Truncate if needed
        max_html_length = 90000
        if len(html_content) > max_html_length:
            html_content = html_content[:max_html_length]

        # Build AI prompt
        prompt = f'''You are analyzing a restaurant's online ordering page.

Extract the COMPLETE menu structure from this HTML and return ONLY valid JSON with this exact structure:

{{
    "categories": ["Appetizers", "Main Courses", "Sides", "Desserts"],
    "items": [
        {{
            "name": "Item Name",
            "description": "Item description",
            "price": "$12.99",
            "category": "Main Courses",
            "dietary_info": ["vegetarian", "gluten-free"],
            "customizations": ["size", "spice level"]
        }}
    ]
}}

Important:
- Extract EVERY FOOD ITEM in the HTML (aim for 50-100+ items if available)
- Include prices whenever visible (very important!)
- Clean category names (use "Main Courses" not "Entrees", "Sides" not "Side Orders")
- Extract dietary info from badges/labels (vegetarian, vegan, gluten-free, etc.)
- Extract customization options (sizes, add-ons, spice levels, etc.)
- **EXCLUDE ALL BEVERAGES/DRINKS** (soda, juice, coffee, tea, beer, wine, cocktails, etc.)
- Focus ONLY on solid food items (appetizers, entrees, sides, desserts)
- Include items from ALL categories in the menu
- Return ONLY the JSON, no other text

HTML content:
{html_content}
'''

        # Call OpenAI API
        client = OpenAI()

        start_time = time.time()
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=12000,
            temperature=0.1
        )
        elapsed = time.time() - start_time

        # Log usage and cost
        input_tokens = response.usage.prompt_tokens
        output_tokens = response.usage.completion_tokens
        total_tokens = response.usage.total_tokens
        input_cost = (input_tokens / 1_000_000) * 0.15
        output_cost = (output_tokens / 1_000_000) * 0.60
        total_cost = input_cost + output_cost


        # Parse response
        response_text = response.choices[0].message.content.strip()
        json_start = response_text.find('{')
        json_end = response_text.rfind('}') + 1

        if json_start >= 0 and json_end > json_start:
            json_text = response_text[json_start:json_end]
            menu_data = json.loads(json_text)
            items_with_prices = sum(1 for item in menu_data.get('items', []) if item.get('price'))
            return menu_data
        else:
            return None

    except Exception as e:
        traceback.print_exc()
        return None


async def scrape_reviews(
        place_url: str,
        place_id: str,
        target_reviews: int,
        time_budget: Optional[float],
        out_dir: Path,
        *,
        build_mentions_now: bool = True,
        harvest_images_now: bool = True,
        top_k_images: int = 5,
        category: str = "restaurant",  # Use category from frontend instead of business_types
        is_fast_job: bool = False,  # Pass fast flag for menu timing
        contact_info: Optional[Dict] = None,  # Contact info for menu extraction
):
    # ---- locks ----
    lock = out_dir / ".refresh.lock"
    try: lock.write_text(str(os.getpid()), encoding="utf-8")
    except Exception: pass

    seed_reviews, seen_ids, seen_text = _read_seed_reviews(out_dir)
    seed_seen_count = len(seed_reviews)
    total_reviews = int(target_reviews or 0)

    # Note: spaCy pre-warming removed to avoid 2-minute startup delay
    # spaCy will load naturally when first needed (during dish normalization after menu extraction)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,  # Set to False for debugging
            chromium_sandbox=False,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-crash-reporter",
                # no "--single-process"
            ],
        )

        context = await browser.new_context(locale="en-US")
        page = await context.new_page()
        page.set_default_timeout(15_000)

        # request blocking (reviews phase only)
        BLOCK_TYPES = {"image", "font", "stylesheet", "media"}
        SNIPPETS = ("/maps/vt", "lh3.googleusercontent.com", "ggpht.com",
                    "fonts.gstatic.com", ".woff", ".woff2", ".ttf",
                    "/gen_204", "/collect")
        async def _route_filter(route):
            req = route.request
            if (req.resource_type in BLOCK_TYPES) or any(s in req.url for s in SNIPPETS):
                await route.abort()
            else:
                await route.continue_()
        await context.route("**/*", _route_filter)

        try:
            # Navigate main page to place_url FIRST (stays here for review scraping later)
            await page.goto(place_url, wait_until='domcontentloaded')
            await page.wait_for_timeout(2000)

            # ---------- EXTRACT MENU (in separate tabs) ----------
            # Only extract menu if not already cached (skip on FULL job if menu exists)
            menu_file = out_dir / "menu_structure.json"
            skip_menu = menu_file.exists()

            if skip_menu:

            if not skip_menu:
                # Create a SEPARATE page for menu extraction so main page stays completely clean
                menu_page = await context.new_page()

                try:
                    # Turn off request blocking for menu extraction (need images/fonts)
                    try:
                        await context.unroute("**/*", _route_filter)
                    except Exception:
                        pass

                    # Navigate to place in the SEPARATE menu_page (not main page)
                    await menu_page.goto(place_url, wait_until="domcontentloaded")
                    await menu_page.wait_for_timeout(2000)  # Simple wait for content to render

                    # Try to extract menu from ordering URL
                    try:

                        # Look for "Order online" button on menu_page
                        order_button = None
                        order_button_selectors = [
                            'div:has-text("Order online")',
                            'button:has-text("Order online")',
                            'a:has-text("Order online")',
                            '[aria-label*="Order online"]'
                        ]

                        for selector in order_button_selectors:
                            try:
                                button = menu_page.locator(selector).first
                                if await button.count():
                                    try:
                                        await button.wait_for(state="visible", timeout=3000)
                                        order_button = button
                                        break
                                    except:
                                        continue
                            except Exception:
                                continue

                        if order_button:
                            # Click to open ordering page in new tab
                            current_pages = len(context.pages)
                            await order_button.click()
                            await menu_page.wait_for_timeout(2000)

                            # Check if new tab opened
                            new_pages = context.pages
                            if len(new_pages) > current_pages:
                                # Get the ordering page (might be Google chooser)
                                ordering_page = new_pages[-1]
                                await ordering_page.wait_for_load_state('networkidle', timeout=10000)
                                ordering_url = ordering_page.url

                                # Initialize URLs
                                ordering_url_pickup = None
                                ordering_url_delivery = None

                                # Track if menu was already extracted (from delivery page)
                                menu_extracted = False
                                menu_data_cached = None

                                # Check if this is Google's provider chooser page
                                if 'google.com/viewer/chooseprovider' in ordering_url:

                                    # Wait for chooser page to be ready
                                    await ordering_page.wait_for_timeout(3000)

                                    # FIRST: Duplicate tab to get delivery URL
                                    try:
                                        delivery_page = await context.new_page()
                                        await delivery_page.goto(ordering_url, timeout=10000)
                                        await delivery_page.wait_for_load_state('networkidle', timeout=10000)
                                        await delivery_page.wait_for_timeout(2000)

                                        # Try to click "Delivery" button with precise selectors
                                        delivery_selectors = [
                                            'text=/^Delivery$/i',  # Exact match only
                                            'div[role="button"]:has-text("Delivery")',
                                            'button:has-text("Delivery")',
                                            '[role="radio"]:has-text("Delivery")',
                                            '[role="tab"]:has-text("Delivery")',
                                            'span:has-text("Delivery")',
                                            'a:has-text("Delivery")',
                                            '[aria-label*="Delivery" i]',
                                        ]

                                        delivery_clicked = False
                                        for selector in delivery_selectors:
                                            try:
                                                elements = delivery_page.locator(selector)
                                                count = await elements.count()

                                                if count > 0:
                                                    # Try each matching element until one works
                                                    for i in range(count):
                                                        try:
                                                            btn = elements.nth(i)
                                                            if await btn.is_visible():
                                                                # Get text to verify it's the right element
                                                                text = await btn.text_content()
                                                                text = text.strip() if text else ""

                                                                # Must be short text (just "Delivery", not a long description)
                                                                if len(text) < 20:
                                                                    await btn.click(timeout=3000)
                                                                    # Wait for page to update after clicking delivery
                                                                    await delivery_page.wait_for_timeout(2000)
                                                                    delivery_clicked = True
                                                                    break
                                                        except:
                                                            continue

                                                    if delivery_clicked:
                                                        break
                                            except:
                                                continue

                                        if not delivery_clicked:
                                        else:
                                            # Now re-query for platform links AFTER clicking delivery
                                            await delivery_page.wait_for_timeout(1000)
                                            all_elements = await delivery_page.locator('a:visible, button:visible').all()

                                            # Smart platform selection: Preferred by business > DoorDash > First option
                                            platform_element = None
                                            platform_text = None
                                            candidates = []

                                            # Collect all platform candidates
                                            for el in all_elements:
                                                try:
                                                    text = await el.inner_text()
                                                    text = text.strip()
                                                    if len(text) > 0:
                                                        candidates.append((el, text))
                                                except:
                                                    continue

                                            # Prioritize: 1) Preferred by business, 2) DoorDash, 3) First option
                                            if candidates:
                                                # Look for "Preferred by business"
                                                for el, text in candidates:
                                                    if "preferred by business" in text.lower():
                                                        platform_element = el
                                                        platform_text = text
                                                        break

                                                # If not found, look for Uber Eats (easier to automate than DoorDash)
                                                if not platform_element:
                                                    for el, text in candidates:
                                                        if "uber" in text.lower():
                                                            platform_element = el
                                                            platform_text = text
                                                            break

                                                # Fallback to first option
                                                if not platform_element:
                                                    platform_element, platform_text = candidates[0]

                                            # Now click the platform element we found
                                            if platform_element:
                                                try:
                                                    current_page_count = len(context.pages)
                                                    await platform_element.click(force=True)
                                                    await delivery_page.wait_for_timeout(3000)

                                                    # Check if new tab opened
                                                    if len(context.pages) > current_page_count:
                                                        await delivery_page.close()
                                                        delivery_page = context.pages[-1]
                                                        await delivery_page.wait_for_timeout(2000)
                                                    else:
                                                        await delivery_page.wait_for_timeout(2000)

                                                    ordering_url_delivery = delivery_page.url

                                                    # If delivery is DoorDash, wait for Cloudflare then extract
                                                    if 'doordash.com' in ordering_url_delivery.lower():

                                                        # Wait for network to settle (Cloudflare check completes)
                                                        try:
                                                            await delivery_page.wait_for_load_state('networkidle', timeout=20000)
                                                        except Exception:

                                                        # Extra buffer for menu items to render
                                                        await delivery_page.wait_for_timeout(3000)

                                                        menu_data_cached = await extract_menu_from_page(delivery_page, ordering_url_delivery)
                                                        if menu_data_cached:
                                                            menu_extracted = True

                                                except Exception as e:
                                            else:

                                        # Close delivery tab
                                        await delivery_page.close()
                                    except Exception as e:

                                    # SECOND: Click platform on main page to get pickup URL
                                    all_elements = await ordering_page.locator('a:visible, button:visible').all()

                                    # Smart platform selection: Preferred by business > DoorDash > First option
                                    platform_element = None
                                    selected_platform = None
                                    candidates = []

                                    # Collect all platform candidates
                                    for el in all_elements:
                                        try:
                                            text = await el.inner_text()
                                            text = text.strip()
                                            if len(text) > 0:
                                                candidates.append((el, text))
                                        except:
                                            continue

                                    # Prioritize: 1) Preferred by business, 2) DoorDash, 3) First option
                                    if candidates:
                                        # Look for "Preferred by business"
                                        for el, text in candidates:
                                            if "preferred by business" in text.lower():
                                                platform_element = el
                                                selected_platform = text
                                                break

                                        # If not found, look for Uber Eats (easier to automate than DoorDash)
                                        if not platform_element:
                                            for el, text in candidates:
                                                if "uber" in text.lower():
                                                    platform_element = el
                                                    selected_platform = text
                                                    break

                                        # Fallback to first option
                                        if not platform_element:
                                            platform_element, selected_platform = candidates[0]

                                    if platform_element:
                                        current_page_count = len(context.pages)

                                        try:
                                            await platform_element.click(force=True)
                                            await ordering_page.wait_for_timeout(3000)

                                            # Check if a NEW tab opened
                                            if len(context.pages) > current_page_count:
                                                await ordering_page.close()
                                                ordering_page = context.pages[-1]
                                                await ordering_page.wait_for_timeout(2000)
                                            else:
                                                await ordering_page.wait_for_timeout(2000)

                                            ordering_url_pickup = ordering_page.url

                                            # IMMEDIATELY save minimal menu structure (URLs only) for fast iOS access
                                            # iOS app polls for this file within 30 seconds, but full scraping takes 2+ minutes
                                            # Save it now so iOS can open WebView quickly for fresh restaurants
                                            if ordering_url_delivery or ordering_url_pickup:
                                                source_url = ordering_url_delivery or ordering_url_pickup
                                                url_lower = source_url.lower()

                                                if 'doordash.com' in url_lower or 'order.online' in url_lower:
                                                    platform = "doordash"
                                                elif 'ubereats.com' in url_lower or 'uber.com' in url_lower:
                                                    platform = "ubereats"
                                                elif 'grubhub.com' in url_lower:
                                                    platform = "grubhub"
                                                elif 'postmates.com' in url_lower:
                                                    platform = "postmates"
                                                elif 'seamless.com' in url_lower:
                                                    platform = "seamless"
                                                elif 'caviar.com' in url_lower:
                                                    platform = "caviar"
                                                else:
                                                    platform = "custom"

                                                # Save minimal menu structure (URLs only) for immediate access
                                                minimal_menu = {
                                                    'restaurant_id': place_id,
                                                    'restaurant_name': contact_info.get('name', 'Unknown') if contact_info else f'Restaurant_{place_id}',
                                                    'categories': [],
                                                    'items': [],
                                                    'supports_online_ordering': True,
                                                    'ordering_url_pickup': ordering_url_pickup,
                                                    'ordering_url_delivery': ordering_url_delivery,
                                                    'ordering_platform': platform,
                                                    'phone_number': contact_info.get('phone') if contact_info else None,
                                                    'cached_at': pd.Timestamp.now().isoformat(),
                                                    'success': False  # Will be updated to True if menu extraction succeeds
                                                }

                                                menu_file = out_dir / "menu_structure.json"
                                                menu_file.write_text(json.dumps(minimal_menu, indent=2, ensure_ascii=False), encoding='utf-8')
                                        except Exception as e:
                                            try:
                                                await ordering_page.close()
                                            except:
                                                pass
                                    else:
                                        await ordering_page.close()
                                else:
                                    # Not a chooser page - direct ordering URL
                                    ordering_url_pickup = ordering_url
    
                            # Only continue if we successfully got past the chooser (or weren't on one)
                            if not ordering_page.is_closed():
                                # Extract menu only if not already extracted from delivery page
                                if not menu_extracted:
                                    menu_data_cached = await extract_menu_from_page(ordering_page, ordering_url_pickup or ordering_url)
                                else:

                                # ALWAYS save menu structure if we have ordering URLs (even if menu extraction failed)
                                # This enables WebView fallback when Cloudflare blocks menu scraping
                                if ordering_url_delivery or ordering_url_pickup:
                                    # Detect platform from whichever URL we have
                                    source_url = ordering_url_delivery or ordering_url_pickup or ordering_url
                                    url_lower = source_url.lower()

                                    if 'doordash.com' in url_lower or 'order.online' in url_lower:
                                        platform = "doordash"
                                    elif 'ubereats.com' in url_lower or 'uber.com' in url_lower:
                                        platform = "ubereats"
                                    elif 'grubhub.com' in url_lower:
                                        platform = "grubhub"
                                    elif 'postmates.com' in url_lower:
                                        platform = "postmates"
                                    elif 'seamless.com' in url_lower:
                                        platform = "seamless"
                                    elif 'caviar.com' in url_lower:
                                        platform = "caviar"
                                    else:
                                        platform = "custom"

                                    # Save menu structure (full if available, minimal if menu extraction failed)
                                    menu_structure = {
                                        'restaurant_id': place_id,
                                        'restaurant_name': contact_info.get('name', 'Unknown') if contact_info else f'Restaurant_{place_id}',
                                        'categories': menu_data_cached.get('categories', []) if menu_data_cached else [],
                                        'items': menu_data_cached.get('items', []) if menu_data_cached else [],
                                        'supports_online_ordering': True,
                                        'ordering_url_pickup': ordering_url_pickup,
                                        'ordering_url_delivery': ordering_url_delivery,
                                        'ordering_platform': platform,
                                        'phone_number': contact_info.get('phone') if contact_info else None,
                                        'cached_at': pd.Timestamp.now().isoformat(),
                                        'success': bool(menu_data_cached and menu_data_cached.get('items'))  # NEW: Track if menu extraction succeeded
                                    }

                                    menu_file = out_dir / "menu_structure.json"
                                    menu_file.write_text(json.dumps(menu_structure, indent=2, ensure_ascii=False), encoding='utf-8')

                                    if menu_data_cached and menu_data_cached.get('items'):
                                    else:
                                elif menu_data_cached:
                                    # Edge case: Have menu data but no ordering URLs (shouldn't happen but handle it)

                                # Close ordering page after menu extraction
                                try:
                                    await ordering_page.close()
                                except:
                                    pass
                        else:

                    except Exception as menu_ex:
                        traceback.print_exc()

                except Exception as e:
                    traceback.print_exc()
                finally:
                    # Always close menu_page to free resources
                    try:
                        await menu_page.close()
                    except Exception:
                        pass

            # Main page already on place_url, just re-enable request blocking for reviews
            await page.wait_for_timeout(500)  # Brief pause after menu tabs close

            # Re-enable request blocking for reviews
            await context.route("**/*", _route_filter)

            # Set deadline AFTER menu extraction (so menu time doesn't count against review scraping budget)
            deadline = (time.perf_counter() + time_budget) if time_budget else None

            # ---------- scrape reviews ----------
            await page.add_style_tag(content="""
              *,*::before,*::after{animation:none!important;transition:none!important}
              html{scroll-behavior:auto!important}
            """)

            handle = await page.evaluate_handle(
                """() => {
                    const card = document.querySelector("div[data-review-id]");
                    if (!card) return document.scrollingElement;
                    let c = card.closest('[role=region]');
                    if (!c) c = document.querySelector('div.section-scrollbox');
                    return c || document.scrollingElement;
                }"""
            )
            scroll_el = handle.as_element()
            locator = page.locator('div[data-review-id]')

            # --- Hardened wait: treat "no reviews" as OK and keep going ---
            have_reviews = False
            try:
                await locator.first.wait_for(state="visible", timeout=6_000)
                have_reviews = True
            except Exception:
                # Some places simply don't render review cards (or they’re slow/behind a gate).
                logger.warning("[reviews] no visible review cards; continuing without review scroll/sort")

            # Only do the expensive review scrolling/sorting if we actually saw reviews
            if have_reviews:
                # Nudge a little to trigger lazy loading
                for _ in range(3):
                    await scroll_el.evaluate("el => el.scrollBy(0, el.clientHeight * 0.25)")
                    await page.wait_for_timeout(300)

                # "More reviews" (older UI) — optional
                try:
                    more_reviews = page.locator('text=/More reviews/').first
                    if await more_reviews.count():
                        await more_reviews.scroll_into_view_if_needed()
                        await more_reviews.click()
                        await page.wait_for_timeout(600)
                except Exception as e:
                    logger.debug("[reviews] 'More reviews' not clickable: %s", e)

                # Sort → Highest rating (optional)
                try:
                    btn = page.get_by_role("button", name="Sort reviews")
                    if await btn.count():
                        await btn.click()
                        menu = page.get_by_role("menu")
                        await menu.wait_for(state="visible", timeout=5_000)
                        await menu.get_by_role("menuitemradio", name="Highest rating").click()
                        await page.wait_for_timeout(250)
                except Exception as e:
                    logger.debug("[reviews] sort menu not available: %s", e)


            if seed_seen_count:
                curr = await locator.count()
                ff_target = min(total_reviews, max(curr, seed_seen_count + 50))
                stagnant = 0; max_steps = 60
                while curr < ff_target and max_steps > 0:
                    await scroll_el.evaluate("el => el.scrollBy(0, el.clientHeight)")
                    await page.wait_for_timeout(180)
                    nxt = await locator.count()
                    if nxt <= curr:
                        stagnant += 1
                        if stagnant >= 3:
                            break
                    else:
                        stagnant = 0; curr = nxt
                    max_steps -= 1

            reviews = list(seed_reviews)
            batch_size = 20
            prev_count = 0
            while True:
                curr_count = await locator.count()
                if curr_count <= prev_count:
                    await scroll_el.evaluate("el => el.scrollBy(0, el.clientHeight * 0.6)")
                    await page.wait_for_timeout(300)
                    curr_count = await locator.count()
                    if curr_count <= prev_count:
                        break

                end = min(prev_count + batch_size, curr_count)

                await page.evaluate(
                    """([start,end]) => {
                        const cards = Array.from(document.querySelectorAll('div[data-review-id]')).slice(start,end);
                        for (const el of cards) {
                            const btn = el.querySelector('button[aria-label="See more"]');
                            if (btn) btn.click();
                        }
                    }""",
                    [prev_count, end]
                )

                batch = await page.evaluate(
                    """([start,end]) => {
                        const out = [];
                        const cards = Array.from(document.querySelectorAll('div[data-review-id]')).slice(start,end);
                        for (const el of cards) {
                            const id = el.getAttribute('data-review-id') || "";
                            let author = "";
                            const avatar = el.querySelector('button[aria-label^="Photo of "]');
                            if (avatar) author = (avatar.getAttribute('aria-label') || "").replace(/^Photo of\\s+/i, "").trim();
                            if (!author) author = (el.getAttribute('aria-label') || "").trim();
                            if (!author) {
                                const prof = el.querySelector('button[jsaction*="reviewerLink"] div');
                                if (prof) author = (prof.textContent || "").split("\\n")[0].trim();
                            }
                            const txtEl = el.querySelector('[lang]');
                            const text = txtEl ? txtEl.innerText.trim() : "";
                            out.push({ id, author, text });
                        }
                        return out;
                    }""",
                    [prev_count, end]
                )

                for entry in batch:
                    rid  = (entry.get("id") or "").strip()
                    text = entry.get("text") or ""
                    key_text = _norm_text(text)
                    if rid:
                        if rid in seen_ids: continue
                        seen_ids.add(rid)
                    else:
                        if key_text in seen_text: continue
                        if key_text: seen_text.add(key_text)
                    entry["author"] = entry.get("author") or ""
                    reviews.append(entry)

                (out_dir / "reviews.json").write_text(json.dumps(reviews, indent=2), encoding="utf-8")

                prev_count = end
                if len(reviews) >= total_reviews: break
                if deadline and time.perf_counter() >= deadline: break

        finally:
            # turn OFF request blocking; we'll reuse context for images
            try:
                await context.unroute("**/*", _route_filter)
            except Exception:
                try: await context.unroute("**/*")
                except Exception: pass
            try:
                await page.close()
            except Exception:
                pass

        # ---------- FAST aggregate (optional, while browser still open) ----------
        if build_mentions_now:
            try:
                _aggregate_now(out_dir, label="fast", category=category)
            except Exception as e:

        # ---------- harvest images using SAME context ----------
        if harvest_images_now:
            try:
                top_dishes = _top_dishes_for_images(out_dir, top_k=2, dedupe=True)
            except Exception:
                top_dishes = []

            if top_dishes:
                # skip already-downloaded filenames
                have = _existing_image_stems(out_dir)
                top_dishes = [d for d in top_dishes if d.lower() not in have]

            if top_dishes:
                page2 = await context.new_page()
                try:
                    await page2.goto(place_url, wait_until="domcontentloaded")
                    await _harvest_menu_images_on_page(page2, top_dishes, out_dir, max_scrolls=60)
                except Exception as e:
                finally:
                    try: await page2.close()
                    except Exception: pass

        # ---------- close once ----------
        try: await context.close()
        except Exception: pass
        try: await browser.close()
        except Exception: pass

    # Aggregate outside the browser for a final, consistent output
    if not build_mentions_now:
        _aggregate_now(out_dir, label="post-scrape", category=category)

    # Match review dishes to menu items and add prices
    _match_dishes_to_menu(out_dir)

    # No longer need to enqueue menu job - menu is extracted in same browser session above

    for name in (".refresh.lock", ".enqueue.lock"):
        try: (out_dir / name).unlink(missing_ok=True)
        except Exception: pass
    gc.collect()


# ---------- keys & mapping ----------
def author_key_from_name(name: str) -> str:
    norm = (name or "").strip().lower()
    norm = re.sub(r"\s+", " ", norm)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()

_ARTICLES = {"the", "a", "an"}

def normalize_dish(s: str) -> str:
    """
    Normalizes a dish string for stable matching:
    - trim, lowercase
    - unify & -> and
    - strip apostrophes/punctuation noise
    - collapse whitespace
    - (display form) Title-Case except small words like 'and'
    """
    if not s:
        return ""
    t = s.strip().lower()

    # unify connectors and punctuation
    t = t.replace("&", " and ")
    t = re.sub(r"[’'`]", "", t)           # drop apostrophes
    t = re.sub(r"[-_/]", " ", t)          # normalize separators to space
    t = re.sub(r"[^a-z0-9\s]", " ", t)    # strip other punctuation
    t = re.sub(r"\s+", " ", t).strip()

    # we keep leading articles here so the raw text is preserved;
    # de-duplication happens on dish_key() during aggregation.
    if not t:
        return ""

    # Pretty label (Title Case, keep 'and' lowercase)
    words = t.split()
    out = []
    for w in words:
        if w in {"and", "of", "with"}:
            out.append(w)
        else:
            out.append(w.capitalize())
    return " ".join(out)


def map_group_to_ui(group_chain: Optional[str]) -> str:
    if not group_chain: return "Unknown"
    toks = [t.strip().lower() for t in group_chain.split(",") if t.strip()]
    for t in toks:
        if t in _ETH_MAP: return _ETH_MAP[t]
    return toks[0].capitalize() if toks else "Unknown"

# ---------- "Recommended dishes …" extractor ----------
_RE_RECOMMENDED = re.compile(r"(?:^|\n)\s*recommended\s+(?:dish(?:es)?|drink(?:s)?|item(?:s)?|beverage(?:s)?)\s*[:\-]?\s*", re.I)
_RE_SECTION_STOP = re.compile(r"(?:\n{2,}|^|\n)(?:Food:|Service:|Atmosphere:|Price per person|Wait time|Seating type|Photos|Like|Share|Parking|Dietary|Restriction|Vegetarian|Vegan|Gluten|Alternative|Milk|Option)\b", re.I)
_SPLIT_DISHES = re.compile(r"\s*(?:,|·|•|/|\&)\s*", re.I)

# Noise filtering patterns
_NOISE_PATTERNS = re.compile(r"\b(?:parking|wheelchair|kid[-\s]?friendly|dietary\s+restriction|vegetarian\s+(?:menu|offering|option)|gluten[-\s]?free|vegan\s+milk|alternative\s+milk|plenty\s+of\s+parking|outdoor\s+seating|dog\s+friendly|free\s+parking)\b", re.I)

def extract_recommended_dishes(text: str) -> List[str]:
    if not text: return []
    m = _RE_RECOMMENDED.search(text)
    if not m: return []
    chunk = text[m.end():]
    stop = _RE_SECTION_STOP.search(chunk)
    if stop: chunk = chunk[:stop.start()]
    
    # Limit chunk length more aggressively for cleaner extraction
    chunk = chunk.strip()[:150]  # Reduced from 300 to 150
    
    parts = [p.strip() for p in _SPLIT_DISHES.split(chunk) if p.strip()]
    out, seen = [], set()
    for p in parts:
        p = re.sub(r'^[\-\u2022\u2023\u25E6\u2043\u2219"\']+\s*', "", p).strip()
        if len(p) < 2: continue
        
        # Filter out noise patterns
        if _NOISE_PATTERNS.search(p):
            continue
        
        # Clean up obvious nonsense like repeated letters at the end
        p = re.sub(r'\s+([a-zA-Z])\1{3,}\s*$', '', p).strip()  # Remove "mmmmm", "ahhhhh" etc at end
        if not p or len(p) < 2:
            continue
            
        # Apply smart normalization
        p = smart_normalize_dish(p)
        if not p:
            continue
            
        # Limit individual item length
        if len(p) > 50:  # Skip overly long items
            continue
            
        # Clean up whitespace and format
        p = re.sub(r"\s+", " ", p).strip()
        
        key = p.lower()
        if key not in seen:
            seen.add(key); out.append(p)
    return out

def get_lexicon_path_for_category(out_dir: Path, category: str = "restaurant") -> str:
    """Get the appropriate lexicon file for the given category"""
    category_lexicons = {
        "restaurant": "dish_lexicon.csv",
        "coffee": "drink_lexicon.csv", 
        "bar": "cocktail_lexicon.csv",
        "brunch": "brunch_lexicon.csv",
        "dessert": "dessert_lexicon.csv"
    }
    
    lexicon_name = category_lexicons.get(category, "dish_lexicon.csv")
    
    # Check local (place-specific) lexicon first, then global
    prefer = out_dir / lexicon_name
    fallback = Path(settings.BASE_DIR) / lexicon_name
    
    return str(prefer if prefer.exists() else fallback)

# ---------- LEXICON support ----------
def load_lexicon(lexicon_csv: Optional[str]) -> Dict[str, List[str]]:
    """
    CSV columns required: dish,synonym
    Returns {canonical_dish: [synonyms...]}.
    Falls back to a tiny built-in sample if file missing.
    """
    if lexicon_csv and Path(lexicon_csv).exists():
        df = pd.read_csv(lexicon_csv, dtype=str).fillna("")
        df = df[(df["dish"]!="") & (df["synonym"]!="")]
        lex: Dict[str, List[str]] = {}
        for _, r in df.iterrows():
            lex.setdefault(r["dish"].strip(), []).append(r["synonym"].strip())
        return lex
    # minimal fallback so you're never blocked
    return {
        # Restaurant items
        "Fried Chicken": ["fried chicken", "buttermilk fried chicken"],
        "Tavern Burger": ["tavern burger", "the tavern burger", "burger and fries"],
        "Deviled Eggs": ["deviled eggs", "southern deviled eggs"],
        "Mac And Cheese": ["mac & cheese", "mac n cheese", "mac and cheese"],
        "Dumplings": ["dumpling", "dumplings"],
        "Fried Rice": ["fried rice"],
        "Pasta": ["pasta"],
        
        # Coffee drinks
        "Latte": ["latte", "café latte", "caffe latte"],
        "Cappuccino": ["cappuccino", "cappucino", "cappuchino"],
        "Americano": ["americano", "café americano"],
        "Espresso": ["espresso", "expresso"],
        "Mocha": ["mocha", "café mocha", "chocolate mocha"],
        "Cold Brew": ["cold brew", "cold brew coffee"],
        "Drip Coffee": ["drip coffee", "regular coffee", "house coffee"],
        "Macchiato": ["macchiato", "caramel macchiato"],
        "Flat White": ["flat white"],
        "Cortado": ["cortado"],
        
        # Cafe food items
        "Avocado Toast": ["avocado toast", "avo toast"],
        "Croissant": ["croissant", "butter croissant"],
        "Danish": ["danish", "strawberry danish", "cheese danish"],
        "Muffin": ["muffin", "blueberry muffin", "chocolate muffin"],
        "Bagel": ["bagel", "everything bagel", "sesame bagel"],
        "Scone": ["scone", "blueberry scone"],
        "Breakfast Burrito": ["breakfast burrito", "morning burrito"],
        "Overnight Oats": ["overnight oats", "oatmeal"],
        "Frittata": ["frittata", "egg frittata"],
        "Quiche": ["quiche"],
        "Coffee Cake": ["coffee cake"],
        "Pastry": ["pastry", "sweet pastry"],
    }

def _phrase_to_regex(phrase: str) -> str:
    # allow space-or-hyphen between words, word boundaries around
    words = [re.escape(w) for w in phrase.split()]
    body  = r"[\s\-]+".join(words)
    return rf"(?<!\w){body}(?!\w)"

def build_dish_mentions(
        reviews_json: str,
        authors_csv: Optional[str] = None,
        out_csv: str = "dish_mentions.csv",
        save_raw_csv: Optional[str] = None,
        lexicon_csv: Optional[str] = "dish_lexicon.csv",
        mode: str = "both",   # "recommended" | "lexicon" | "both"
        limit_per_ethnicity: Optional[int] = None,      # <-- NEW
        out_csv_topk: Optional[str] = "dish_mentions_top5.csv",  # <-- NEW
) -> pd.DataFrame:
    t0 = time.time()

    # ------------ load reviews ------------
    data = json.loads(Path(reviews_json).read_text(encoding="utf-8"))
    reviews_df = pd.DataFrame([
        {"author": d.get("author","").strip(), "text": (d.get("text") or "").strip()}
        for d in data if (d.get("text") or "").strip()
    ])
    if reviews_df.empty:
        Path(out_csv).write_text("", encoding="utf-8")
        if save_raw_csv:
            Path(save_raw_csv).write_text("", encoding="utf-8")
        return reviews_df

    reviews_df["author_key"] = reviews_df["author"].apply(author_key_from_name)

    # ------------ join authors.csv (author_key, group) ------------
    # ensure the column exists so merge can suffix if needed
    reviews_df["group"] = pd.NA

    if authors_csv and Path(authors_csv).exists():
        a = pd.read_csv(authors_csv, dtype=str).rename(columns=lambda c: c.strip())
        keep = [c for c in ("author_key","group") if c in a.columns]
        if keep:
            a = a[keep].drop_duplicates()
            reviews_df = reviews_df.merge(a, on="author_key", how="left")

    # unify merge suffixes: group_x/group_y -> group
    if "group_x" in reviews_df.columns or "group_y" in reviews_df.columns:
        gx = reviews_df.get("group_x")
        gy = reviews_df.get("group_y")
        if gx is not None:
            gx = gx.astype("string").replace({"": pd.NA, "None": pd.NA})
        if gy is not None:
            gy = gy.astype("string").replace({"": pd.NA, "None": pd.NA})

        if gx is None:
            reviews_df["group"] = gy
        elif gy is None:
            reviews_df["group"] = gx
        else:
            reviews_df["group"] = gx.combine_first(gy)

        reviews_df = reviews_df.drop(columns=[c for c in ("group_x","group_y") if c in reviews_df.columns])

    if "group" not in reviews_df.columns:
        reviews_df["group"] = pd.Series([pd.NA]*len(reviews_df), dtype="string")

    # ------------ map to UI + tab (once) ------------
    g = reviews_df["group"].astype("string").str.strip()
    g = g.mask(g.str.lower().isin({"", "none", "nan"}))
    reviews_df["ethnicity_ui"] = g.apply(lambda x: map_group_to_ui(x) if isinstance(x, str) else None)
    reviews_df["tab"] = g.apply(lambda x: map_group_to_tab(x) if isinstance(x, str) else None)


    # ------------ lexicon index ------------
    use_lex = mode in ("lexicon","both")
    idx = build_lexicon_index(load_lexicon(lexicon_csv)) if use_lex else []

    # ------------ collect dish mentions ------------
    rows = []
    for _, row in reviews_df.iterrows():
        rtext = row["text"]

        dishes_rec = extract_recommended_dishes(rtext) if mode in ("recommended","both") else []
        dishes_lex = extract_with_lexicon(rtext, idx) if use_lex else []

        # Track keys for this specific review to avoid duplicates
        seen_keys_this_review = set()

        # PRIORITY: Recommended dishes should ALWAYS be included, even with poor normalization
        for d in dishes_rec:
            k, label = normalize_dish_key_and_label(d)
            # For recommended dishes, be more lenient - use original text if normalization fails
            if not k and d.strip():
                k = d.strip().lower()
                label = smart_normalize_dish(d) or d.strip()
            
            if k and label and k not in seen_keys_this_review:
                seen_keys_this_review.add(k)
                rows.append({
                    "author_key": row["author_key"],
                    "author": row["author"],
                    "group": row["group"],
                    "tab": row["tab"],
                    "ethnicity_ui": row["ethnicity_ui"],
                    "dish_key": k,
                    "dish": label,
                    "text": rtext,
                    "source": "recommended",
                })

        # Add lexicon matches only if they don't conflict with recommended dishes from this review
        for d in dishes_lex:
            k, label = normalize_dish_key_and_label(d)
            if k and label and k not in seen_keys_this_review:
                seen_keys_this_review.add(k)
                rows.append({
                    "author_key": row["author_key"],
                    "author": row["author"],
                    "group": row["group"],
                    "tab": row["tab"],
                    "ethnicity_ui": row["ethnicity_ui"],
                    "dish_key": k,
                    "dish": label,
                    "text": rtext,
                    "source": "lexicon",
                })
    raw = pd.DataFrame(rows)

    # optional raw dump
    if save_raw_csv:
        raw.to_csv(save_raw_csv, index=False)

    # if nothing, still produce the (empty) out_csv
    if raw.empty:
        Path(out_csv).write_text("", encoding="utf-8")
        return raw

    # ------------ aggregate for UI ------------
    raw = raw[raw["tab"].isin(TAB_LABELS)]
    if raw.empty:
        Path(out_csv).write_text("", encoding="utf-8")
        return raw

    # pick a representative display per (tab, dish_key)
    rep = (
        raw.groupby(["tab","dish_key"])["dish"]
        .agg(lambda s: s.value_counts().index[0])
        .reset_index()
        .rename(columns={"dish": "dish_display"})
    )

    agg = (
        raw.groupby(["tab","dish_key"], dropna=False)
        .agg(
            mentions=("dish","count"),
            unique_authors=("author_key", pd.Series.nunique),
            from_recommended=("source", lambda s: int((s == "recommended").any())),
        )
        .reset_index()
        .merge(rep, on=["tab","dish_key"], how="left")
        .rename(columns={"tab": "ethnicity_ui"})
    )

    # finalize columns for CSVs: use display label as 'dish'
    agg = (agg
           .rename(columns={"dish_display": "dish"})
           .drop(columns=["dish_key"])
           .sort_values(["ethnicity_ui","mentions","unique_authors","dish"],
                        ascending=[True, False, False, True]))

    agg.to_csv(out_csv, index=False)

    # optional top-k file remains consistent
    if "limit_per_ethnicity" in locals() and limit_per_ethnicity is not None and out_csv_topk:
        tmp = agg.copy()
        tmp["__rank"] = tmp.groupby("ethnicity_ui").cumcount()
        topk_df = tmp[tmp["__rank"] < int(limit_per_ethnicity)].drop(columns="__rank")
        topk_df.to_csv(out_csv_topk, index=False)

    return agg



def extract_with_lexicon(text: str, idx: List[Tuple[str, re.Pattern]]) -> List[str]:
    if not text: return []
    hits = []
    for dish, pat in idx:
        if pat.search(text):
            hits.append(dish)
    return hits


def build_lexicon_index(lex: Dict[str, List[str]]) -> List[Tuple[str, re.Pattern]]:
    """
    Returns list of (canonical_dish, compiled_pattern) covering both
    canonical name and all synonyms. Case-insensitive.
    """
    idx: List[Tuple[str, re.Pattern]] = []
    for dish, syns in lex.items():
        forms = [dish] + syns
        alts  = "|".join(_phrase_to_regex(f) for f in forms if f.strip())
        if not alts: continue
        pat = re.compile(alts, re.I)
        idx.append((dish, pat))
    return idx


def _safe_map_group_to_ui(v):
    # treat empty/None/"None"/NaN/"nan" as missing
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, str):
        s = v.strip()
        if not s or s.lower() in {"none", "nan"}:
            return None
        return map_group_to_ui(s)
    # anything else (e.g., pandas NA)
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass
    return map_group_to_ui(str(v).strip())

def extract_from_recommended(text: str) -> list[str]:
    if not text:
        return []
    hdr = "|".join(map(re.escape, FIELD_HEADERS))
    pat = re.compile(
        rf"(?:Recommended|Popular)\s+dishes\s*[:\n]\s*(.+?)(?=\n(?:{hdr})\b|\Z)",
        re.I | re.S
    )
    m = pat.search(text)
    if not m:
        return []
    return split_candidates(m.group(1))


def split_candidates(block: str) -> list[str]:
    parts = re.split(r"(?:,|\band\b|/|•|·|\u2022|\n)", block, flags=re.I)
    return [p.strip() for p in parts if p and len(p.strip()) >= 3]

def map_group_to_tab(chain):
    if not isinstance(chain, str) or not chain.strip():
        return None
    toks = {t.strip().lower() for t in chain.split(",") if t}

    if "southasian" in toks or "indian" in toks:
        return "Indian"
    if "eastasian" in toks or "chinese" in toks:
        return "Chinese"
    if any(t in toks for t in ("hispanic", "latino", "mexican")):
        return "Mexican"
    if "italian" in toks:
        return "Italian"

    # Map African/European buckets to American
    if any(t in toks for t in ("greatereuropean", "european", "greaterafrican", "african")):
        return "American"

    # Skip unknowns and anything we don't recognize
    if "unknown" in toks:
        return None
    return None

def _title_name(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    parts = re.split(r"(\s+)", s.lower())
    return "".join(p.capitalize() if p.strip() else p for p in parts)

def _split_first_last(author_norm: str):
    parts = [p for p in (author_norm or "").split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]

def write_or_update_authors_csv(reviews_json_path: str, authors_csv_path: str) -> str:
    data = json.loads(Path(reviews_json_path).read_text(encoding="utf-8"))

    # count reviews per author_key
    counts = {}
    for r in data:
        name = (r.get("author") or "").strip()
        if not name:
            continue
        ak = author_key_from_name(name)
        counts[ak] = counts.get(ak, 0) + 1

    # build new rows
    rows = []
    for ak, n in counts.items():
        raw = next(((r.get("author") or "").strip()
                    for r in data
                    if author_key_from_name((r.get("author") or "").strip()) == ak), "")
        author_norm = _title_name(raw)
        first, last = _split_first_last(author_norm)
        rows.append({
            "author_key": ak,
            "author_norm": author_norm,
            "first": first,
            "last": last,
            "group": "",   # blank until we enrich
            "prob": "",
            "lens": "unknown",
            "review_count_by_author": str(n),
            "author_display": author_norm or raw,
        })

    new = pd.DataFrame(rows, dtype=str).fillna("")
    p = Path(authors_csv_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    if p.exists():
        existing = pd.read_csv(p, dtype=str).fillna("")
        combined = pd.concat([existing, new], ignore_index=True, sort=False)
        combined = combined.drop_duplicates(subset=["author_key"], keep="first")

        # only blank rows need enrichment
        mask = combined["group"].astype(str).str.strip().eq("") | combined["prob"].astype(str).str.strip().eq("")

        if mask.any():
            to_enrich = combined.loc[mask].copy()
            to_enrich = enrich_groups_with_ethnicolr(to_enrich, prob_threshold=0.7)
            # write back enriched cols only
            for col in ["group", "prob", "lens"]:
                combined.loc[mask, col] = to_enrich[col].values

        combined.to_csv(p, index=False)
    else:
        # ✅ enrich on first create too
        new = enrich_groups_with_ethnicolr(new, prob_threshold=0.7)
        new.to_csv(p, index=False)

    return str(p)

def enrich_groups_with_ethnicolr(df: pd.DataFrame, prob_threshold: float = 0.7) -> pd.DataFrame:
    """
    Fill df['group'] for rows where it's blank using Ethnicolr.
    Does NOT overwrite existing non-blank groups.
    Robust to Ethnicolr schema/version differences.
    """
    import ethnicolr  # Lazy import (deferred to avoid 2-minute startup delay for menu extraction)

    df = df.copy()
    if "group" not in df.columns:
        df["group"] = ""

    # Only rows that need a label and have at least a first or last name
    need = df["group"].fillna("").eq("")
    if "first" not in df.columns or "last" not in df.columns:
        return df
    sub = df.loc[need, ["first", "last"]].fillna("")
    sub = sub[(sub["first"] != "") | (sub["last"] != "")]
    if sub.empty:
        return df

    # --- Run Ethnicolr (Wiki model) ---
    try:
        pred = ethnicolr.pred_wiki_name(sub.rename(columns={"first": "first", "last": "last"}),
                                        lname_col="last", fname_col="first")
    except TypeError:
        pred = ethnicolr.pred_wiki_name(sub.rename(columns={"first": "first", "last": "last"}),
                                        "last", "first")

    # Build a case-insensitive column lookup
    cols_lc = {c.lower(): c for c in pred.columns}

    # --- Detect label column across versions (case-insensitive) ---
    label_col = None
    for cand in ["race", "ethnicity", "pred", "race_ethnicity"]:
        if cand in cols_lc:
            label_col = cols_lc[cand]
            break
    if label_col is None:
        return df

    # --- Detect probability column (single) or compute from distributed ---
    prob_col = None
    for cand in ["prob", "probability", "race_prob", "ethnicity_prob"]:
        if cand in cols_lc:
            prob_col = cols_lc[cand]
            break

    if prob_col is None:
        # distributed probs: any columns starting with prob_ or p_
        prob_cols = [c for c in pred.columns
                     if c.lower().startswith("prob_") or c.lower().startswith("p_")]
        if prob_cols:
            # coerce to numeric, compute row-wise max
            pred["_prob_max"] = pd.to_numeric(pred[prob_cols], errors="coerce").max(axis=1)
            # pick label with max prob if we didn't get a label_col earlier
            argmax = pd.to_numeric(pred[prob_cols], errors="coerce").idxmax(axis=1)
            pred["_label_from_probs"] = argmax.str.replace(r"^(prob_|p_)", "", regex=True)
            # prefer explicit label if present, otherwise use derived
            if label_col is None:
                label_col = "_label_from_probs"
            prob_col = "_prob_max"

    # Build series; align indices with `sub`
    lab_series = pred[label_col].astype(str)
    if not lab_series.index.equals(sub.index):
        lab_series.index = sub.index

    if prob_col and prob_col in pred.columns:
        p_series = pd.to_numeric(pred[prob_col], errors="coerce")
        if not p_series.index.equals(sub.index):
            p_series.index = sub.index
    else:
        p_series = pd.Series(1.0, index=sub.index, dtype=float)

    mapped = lab_series.str.lower().map(to_chain)

    # --- Apply back to df for rows over threshold and still blank ---
    to_fill_idx = mapped.index[(mapped != "") & (p_series >= prob_threshold)]
    if len(to_fill_idx):
        df.loc[to_fill_idx, "group"] = mapped.loc[to_fill_idx].values
        if "prob" not in df.columns:
            df["prob"] = ""
        df.loc[to_fill_idx, "prob"] = p_series.loc[to_fill_idx].round(3).astype(str).values

    return df


# --- Map Ethnicolr label -> your taxonomy chain ---
def to_chain(label: str) -> str:
        lbl = (label or "").lower()

        # Handle chain-like labels Ethnicolr emits (examples seen in your logs)
        if "indiansubcontinent" in lbl or "indian" in lbl or "southasian" in lbl:
            return "SouthAsian,IndianSubContinent"
        if "eastasian" in lbl or "chinese" in lbl or "japanese" in lbl or "korean" in lbl:
            return "Asian,GreaterEastAsian,EastAsian"
        if "italian" in lbl:
            return "GreaterEuropean,WestEuropean,Italian"
        if "hispanic" in lbl or "latino" in lbl:
            return "Mexican"  # coarse bucket used by your app
        if "greatereuropean" in lbl or "easteuropean" in lbl or "westeuropean" in lbl or lbl == "white":
            return "GreaterEuropean"
        if "greaterafrican" in lbl or "african" in lbl or lbl == "black":
            return "GreaterAfrican"

        # generic fallbacks
        if lbl == "asian":
            return "Asian,GreaterEastAsian,EastAsian"
        return ""  # unknown/low confidence → leave blank

def _norm(s: str) -> str:
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"[^\w\s]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _label_score(dish: str, label: str) -> float:
    """Blend token overlap + fuzzy ratio; no hard-coded synonyms."""
    if not dish or not label:
        return 0.0
    d, l = _norm(dish), _norm(label)

    if d == l:          # exact
        return 1.0
    if d in l or l in d:  # containment
        return 0.92

    dt, lt = set(d.split()), set(l.split())
    jacc = (len(dt & lt) / len(dt | lt)) if dt and lt else 0.0
    fuzz = SequenceMatcher(None, d, l).ratio()
    return min(1.0, 0.65 * jacc + 0.45 * fuzz)

def _top_dishes_for_images(out_dir: Path, top_k: int = 5, dedupe: bool = True) -> list[str]:
    csv_path = out_dir / "dish_mentions.csv"
    if not csv_path.exists():
        return []
    df = pd.read_csv(csv_path, dtype=str).rename(columns=lambda c: c.strip())
    low = {c.lower(): c for c in df.columns}
    req = ["ethnicity_ui","dish","mentions","unique_authors"]
    if not all(k in low for k in req):
        return []
    ecol, dcol = low["ethnicity_ui"], low["dish"]
    mcol, ucol = low["mentions"], low["unique_authors"]
    df[mcol] = pd.to_numeric(df[mcol], errors="coerce").fillna(0).astype(int)
    df[ucol] = pd.to_numeric(df[ucol], errors="coerce").fillna(0).astype(int)
    df = df.sort_values([ecol, mcol, ucol, dcol], ascending=[True, False, False, True])
    df["__rank"] = df.groupby(ecol).cumcount()
    df_top = df[df["__rank"] < int(top_k)]
    dishes = df_top[dcol].astype(str).str.strip().tolist()
    if not dedupe:
        return [d for d in dishes if d]
    seen=set(); out=[]
    for d in dishes:
        k=d.lower()
        if d and k not in seen:
            seen.add(k); out.append(d)
    return out

def _existing_image_stems(out_dir: Path) -> set[str]:
    imgs_dir = out_dir / "menu_images"
    stems = set()
    if imgs_dir.exists():
        for ext in ("*.jpg","*.jpeg","*.png","*.webp"):
            for p in imgs_dir.glob(ext):
                stems.add(p.stem.casefold())
    return stems

async def _harvest_menu_images_on_page(
        page,                     # <-- Playwright Page that is already on place_url
        top_dishes: list[str],
        out_dir: Path,
        max_scrolls: int = 60,
):
    """
    Re-uses an existing Playwright page (images allowed) to:
      - switch to the Menu tab,
      - scroll through the highlights carousel/grid,
      - try to match aria-labels to each dish name,
      - save the first matching image per dish to dish_images.json.
    """
    # 1) Go to "Menu" tab with resilient queries
    # (keep your robust role/text matching logic)
    menu_tab = page.locator('role=tab[name=/menu/i]').first
    if await menu_tab.count():
        await menu_tab.click()
        await page.wait_for_timeout(350)
    else:
        # fallback: text match
        mt = page.locator("text=/^Menu$/i").first
        if await mt.count():
            await mt.click()
            await page.wait_for_timeout(350)

    # 2) Now walk through highlight buttons/images and match aria-labels
    #    Keep your current robust matching (_label_score, SequenceMatcher, etc.)
    found: dict[str, dict] = {}
    seen = set()

    # A resilient query for items that have an image and an aria-label
    items = page.locator('button[aria-label] img, img[alt][crossorigin]').locator("..")  # go back to button if needed

    scrolls = 0
    while scrolls < max_scrolls and len(found) < len(top_dishes):
        count = await items.count()
        for i in range(count):
            btn = items.nth(i)
            # normalize label
            label = (await btn.get_attribute("aria-label")) or ""
            if not label:
                # try the img alt
                img = btn.locator("img").first
                if await img.count():
                    label = (await img.get_attribute("alt")) or ""

            if not label:
                continue

            # best-effort label matching (your existing _label_score)
            for dish in top_dishes:
                if dish in found:  # already found one image for this dish
                    continue
                score = _label_score(label, dish)
                if score >= 0.66:  # tune threshold as you like
                    img = btn.locator("img").first
                    if await img.count():
                        src = await img.get_attribute("src")
                        if src and src not in seen:
                            seen.add(src)
                            found[dish] = {"image_url": src, "caption": label}
        if len(found) >= len(top_dishes):
            break

        # try to reveal more items (scroll the container and page)
        await page.mouse.wheel(0, 1200)
        await page.wait_for_timeout(250)
        scrolls += 1

    # 3) Persist to dish_images.json (merge with any existing)
    path = out_dir / "dish_images.json"
    try:
        existing = json.loads(path.read_text("utf-8"))
    except Exception:
        existing = {}

    changed = False
    for dish, val in found.items():
        if dish not in existing:
            existing[dish] = val
            changed = True

    if changed:
        path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
