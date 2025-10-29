"""
Menu Structure Caching System (READ-ONLY)

This module provides READ-ONLY access to cached restaurant menu structures
and ordering capabilities created by scrape_reviews.py.

Architecture (Simplified):
- Menu extraction happens ONLY in scrape_reviews.py during review scraping
- This module ONLY reads the cached menu_structure.json files
- No duplicate Playwright/OpenAI calls when iOS app requests menu data
- Fast API responses (<10ms) since we just read cached files

Key features:
- Cache menu structure (with prices) for 90 days
- Cache ordering capabilities for 24 hours
- If cache is stale/missing, return None (frontend can handle gracefully)
- Scheduled scraper (scrape_reviews.py) keeps cache fresh
"""

import json
import time
import logging
import asyncio
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
from django.conf import settings
from playwright.sync_api import sync_playwright
from playwright.async_api import async_playwright
from openai import OpenAI

logger = logging.getLogger(__name__)

# Cache settings
MENU_CACHE_DAYS = 90  # Menu structure changes very infrequently - 3 months cache
ORDERING_CACHE_HOURS = 24  # Ordering availability can change daily

# Timeout constants (consistent with review scraping patterns)
PAGE_LOAD_TIMEOUT = 15_000  # 15 seconds for page loads
ELEMENT_WAIT_TIMEOUT = 6_000  # 6 seconds for elements to appear (matches review scraping)
SHORT_WAIT_TIMEOUT = 3_000  # 3 seconds for quick checks
CLICK_TIMEOUT = 5_000  # 5 seconds for clicks to succeed
NETWORK_IDLE_TIMEOUT = 10_000  # 10 seconds for network to settle
BRIEF_PAUSE = 250  # Brief pause between actions (matches review scraping pattern)

# Use same directory structure as reviews cache - everything for a place_id in one folder
REVIEWS_CACHE_DIR = Path(getattr(settings, "REVIEWS_CACHE_DIR", Path(settings.BASE_DIR) / "var" / "reviews"))

@dataclass
class MenuItem:
    """Single menu item structure"""
    name: str
    description: str
    category: str
    dietary_info: List[str]  # ["vegetarian", "gluten-free", etc.]
    customizations: List[str]  # Available modifications
    image_url: Optional[str] = None
    price: Optional[str] = None  # Price as string (e.g., "$19.50")

@dataclass
class MenuStructure:
    """Complete menu structure for a restaurant"""
    restaurant_id: str
    restaurant_name: str
    categories: List[str]
    items: List[MenuItem]
    supports_online_ordering: bool
    ordering_url_pickup: Optional[str]
    ordering_url_delivery: Optional[str]
    ordering_platform: Optional[str]  # NEW: Platform type (doordash, ubereats, custom, etc.)
    phone_number: Optional[str]
    cached_at: datetime
    success: bool = True  # NEW: Whether menu extraction succeeded (False = URLs only, Cloudflare blocked)

    def is_stale(self) -> bool:
        """Check if menu structure cache is stale"""
        return datetime.now() - self.cached_at > timedelta(days=MENU_CACHE_DAYS)

@dataclass
class OrderingCapability:
    """Restaurant's ordering capabilities"""
    restaurant_id: str
    supports_delivery: bool
    supports_pickup: bool
    has_website_ordering: bool
    delivery_platforms: List[str]  # ["doordash", "ubereats", etc.]
    website_url: Optional[str]
    phone_number: Optional[str]
    cached_at: datetime

    def is_stale(self) -> bool:
        """Check if ordering capability cache is stale"""
        return datetime.now() - self.cached_at > timedelta(hours=ORDERING_CACHE_HOURS)


class MenuStructureCache:
    """Handles menu structure caching and extraction"""

    def __init__(self):
        # Use the same directory structure as reviews cache
        self.reviews_cache_dir = REVIEWS_CACHE_DIR
        self.reviews_cache_dir.mkdir(parents=True, exist_ok=True)

    def _get_place_cache_dir(self, restaurant_id: str) -> Path:
        """Get the cache directory for a specific place_id (same as reviews)"""
        place_dir = self.reviews_cache_dir / restaurant_id
        place_dir.mkdir(parents=True, exist_ok=True)
        return place_dir

    def get_menu_structure(self, restaurant_id: str, restaurant_name: str,
                          website_url: Optional[str] = None) -> Optional[MenuStructure]:
        """
        Get menu structure from cache (READ-ONLY)

        This method ONLY reads cached menu data created by scrape_reviews.py.
        It does NOT re-scrape if cache is stale - that's handled by the scheduled scraper.

        Args:
            restaurant_id: Google Places ID
            restaurant_name: Restaurant name for search
            website_url: Restaurant website URL if available (unused, kept for API compatibility)

        Returns:
            MenuStructure object if cached, None if not yet scraped
        """
        # Load from cache (if exists)
        cached_menu = self._load_cached_menu(restaurant_id)

        if cached_menu:
            # Return cached data regardless of staleness
            # The scheduled scraper (scrape_reviews.py) will refresh stale data
            if cached_menu.is_stale():
                logger.info(f"Returning stale cached menu for {restaurant_name} (scheduled scraper will refresh)")
            else:
                logger.info(f"Using cached menu structure for {restaurant_name}")
            return cached_menu

        # No cache found - menu needs to be scraped by scrape_reviews.py first
        logger.warning(f"No cached menu found for {restaurant_name} - run scrape_reviews first")
        return None

    def get_ordering_capability(self, restaurant_id: str, restaurant_name: str,
                              website_url: Optional[str] = None) -> Optional[OrderingCapability]:
        """
        Get ordering capabilities from cache (READ-ONLY)

        This method ONLY reads cached ordering data created by scrape_reviews.py.
        It does NOT re-detect if cache is stale - that's handled by the scheduled scraper.

        Args:
            restaurant_id: Google Places ID
            restaurant_name: Restaurant name
            website_url: Restaurant website URL if available (unused, kept for API compatibility)

        Returns:
            OrderingCapability object if cached, None if not yet scraped
        """
        # Load from cache (if exists)
        cached_capability = self._load_cached_capability(restaurant_id)

        if cached_capability:
            # Return cached data regardless of staleness
            # The scheduled scraper (scrape_reviews.py) will refresh stale data
            if cached_capability.is_stale():
                logger.info(f"Returning stale cached ordering capability for {restaurant_name} (scheduled scraper will refresh)")
            else:
                logger.info(f"Using cached ordering capability for {restaurant_name}")
            return cached_capability

        # No cache found - needs to be scraped by scrape_reviews.py first
        logger.warning(f"No cached ordering capability found for {restaurant_name} - run scrape_reviews first")
        return None

    def _load_cached_menu(self, restaurant_id: str) -> Optional[MenuStructure]:
        """Load menu structure from cache file"""
        place_dir = self._get_place_cache_dir(restaurant_id)
        cache_file = place_dir / "menu_structure.json"

        if not cache_file.exists():
            return None

        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # Convert back to MenuStructure object
            items = [
                MenuItem(
                    name=item['name'],
                    description=item['description'],
                    category=item['category'],
                    dietary_info=item['dietary_info'],
                    customizations=item['customizations'],
                    image_url=item.get('image_url'),
                    price=item.get('price')  # Load price from JSON
                )
                for item in data['items']
            ]

            return MenuStructure(
                restaurant_id=data['restaurant_id'],
                restaurant_name=data['restaurant_name'],
                categories=data['categories'],
                items=items,
                supports_online_ordering=data['supports_online_ordering'],
                ordering_url_pickup=data.get('ordering_url_pickup'),
                ordering_url_delivery=data.get('ordering_url_delivery'),
                ordering_platform=data.get('ordering_platform'),  # NEW: Load platform type
                phone_number=data.get('phone_number'),
                cached_at=datetime.fromisoformat(data['cached_at']),
                success=data.get('success', True)  # NEW: Load success flag (default True for backwards compatibility)
            )

        except Exception as e:
            logger.error(f"Error loading cached menu for {restaurant_id}: {e}")
            return None

    def _load_cached_capability(self, restaurant_id: str) -> Optional[OrderingCapability]:
        """Load ordering capability from cache file"""
        place_dir = self._get_place_cache_dir(restaurant_id)
        cache_file = place_dir / "ordering_capability.json"

        if not cache_file.exists():
            return None

        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            return OrderingCapability(
                restaurant_id=data['restaurant_id'],
                supports_delivery=data['supports_delivery'],
                supports_pickup=data['supports_pickup'],
                has_website_ordering=data['has_website_ordering'],
                delivery_platforms=data['delivery_platforms'],
                website_url=data.get('website_url'),
                phone_number=data.get('phone_number'),
                cached_at=datetime.fromisoformat(data['cached_at'])
            )

        except Exception as e:
            logger.error(f"Error loading cached capability for {restaurant_id}: {e}")
            return None


# Convenience functions for easy access
def get_restaurant_menu(restaurant_id: str, restaurant_name: str,
                       website_url: Optional[str] = None) -> Optional[MenuStructure]:
    """Get cached or fresh menu structure for a restaurant"""
    cache = MenuStructureCache()
    return cache.get_menu_structure(restaurant_id, restaurant_name, website_url)


def get_restaurant_ordering_capability(restaurant_id: str, restaurant_name: str,
                                     website_url: Optional[str] = None) -> Optional[OrderingCapability]:
    """Get cached or fresh ordering capabilities for a restaurant"""
    cache = MenuStructureCache()
    return cache.get_ordering_capability(restaurant_id, restaurant_name, website_url)


def clear_restaurant_cache(restaurant_id: str):
    """Clear all cached data for a restaurant"""
    reviews_cache_dir = REVIEWS_CACHE_DIR
    place_dir = reviews_cache_dir / restaurant_id

    files_to_remove = [
        "menu_structure.json",
        "ordering_capability.json",
        "menu_screenshot.png"
    ]

    for filename in files_to_remove:
        file_path = place_dir / filename
        if file_path.exists():
            file_path.unlink()
            logger.info(f"Removed cached file: {filename}")