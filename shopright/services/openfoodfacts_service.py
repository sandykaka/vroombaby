"""
OpenFoodFacts API Service

Fetches nutrition data and Nutri-Score grades for food products.
API Documentation: https://openfoodfacts.github.io/openfoodfacts-server/api/

Free, unlimited API with no authentication required.
"""

import requests
import logging
from typing import Dict, Optional
from django.utils import timezone

logger = logging.getLogger(__name__)


class OpenFoodFactsService:
    """Service for fetching product nutrition data from OpenFoodFacts API"""

    BASE_URL = "https://world.openfoodfacts.org/api/v2"
    USER_AGENT = "ShopRight/1.0 (https://vroombaby.com/shopright)"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': self.USER_AGENT
        })

    def fetch_product_by_barcode(self, barcode: str) -> Optional[Dict]:
        """
        Fetch product nutrition data by barcode

        Args:
            barcode: UPC/EAN barcode (e.g., "737628064502")

        Returns:
            Dict with nutrition data or None if not found
            {
                'nutriscore_grade': 'a',  # A-E (lowercase)
                'nova_group': 3,          # 1-4
                'nutrients': {
                    'energy_kcal_100g': 350,
                    'sugars_100g': 5.2,
                    'sodium_100g': 0.3,
                    'fat_100g': 12.0,
                    'saturated_fat_100g': 2.5,
                    'carbohydrates_100g': 45.0,
                    'proteins_100g': 8.0,
                    'fiber_100g': 2.5
                },
                'product_name': 'Whole Milk',
                'brands': 'Organic Valley',
                'image_url': 'https://...'
            }
        """
        try:
            # API endpoint: /api/v2/product/{barcode}
            url = f"{self.BASE_URL}/product/{barcode}.json"

            logger.info(f"🔍 Fetching nutrition for barcode: {barcode}")
            response = self.session.get(url, timeout=10)

            if response.status_code != 200:
                logger.warning(f"⚠️  OpenFoodFacts API returned {response.status_code} for barcode {barcode}")
                return None

            data = response.json()

            # Check if product was found
            if data.get('status') != 1 or 'product' not in data:
                logger.info(f"ℹ️  Product not found in OpenFoodFacts: {barcode}")
                return None

            product = data['product']

            # Extract nutrition data
            nutrition_data = self._extract_nutrition_data(product)

            if nutrition_data:
                logger.info(f"✅ Found nutrition for {barcode}: Nutri-Score {nutrition_data.get('nutriscore_grade', 'N/A').upper()}")
            else:
                logger.info(f"⚠️  Partial data for {barcode} (no Nutri-Score)")

            return nutrition_data

        except requests.exceptions.Timeout:
            logger.error(f"❌ OpenFoodFacts API timeout for barcode {barcode}")
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ OpenFoodFacts API error for barcode {barcode}: {e}")
            return None
        except Exception as e:
            logger.error(f"❌ Unexpected error fetching nutrition for {barcode}: {e}")
            return None

    def _extract_nutrition_data(self, product: Dict) -> Optional[Dict]:
        """
        Extract and normalize nutrition data from OpenFoodFacts product response

        Args:
            product: Raw product data from API

        Returns:
            Normalized nutrition dict or None if insufficient data
        """
        nutrition_data = {}

        # Nutri-Score (A-E grade)
        nutriscore_grade = product.get('nutriscore_grade', '').lower()
        if nutriscore_grade and nutriscore_grade in ['a', 'b', 'c', 'd', 'e']:
            nutrition_data['nutriscore_grade'] = nutriscore_grade

        # NOVA group (processing level: 1-4)
        nova_group = product.get('nova_group')
        if nova_group and isinstance(nova_group, int) and 1 <= nova_group <= 4:
            nutrition_data['nova_group'] = nova_group
        elif isinstance(nova_group, str) and nova_group.isdigit():
            nova_int = int(nova_group)
            if 1 <= nova_int <= 4:
                nutrition_data['nova_group'] = nova_int

        # Nutrients (per 100g)
        nutrients = product.get('nutriments', {})
        if nutrients:
            nutrition_data['nutrients'] = {
                'energy_kcal_100g': nutrients.get('energy-kcal_100g') or nutrients.get('energy_100g'),
                'sugars_100g': nutrients.get('sugars_100g'),
                'sodium_100g': nutrients.get('sodium_100g'),  # in grams
                'salt_100g': nutrients.get('salt_100g'),
                'fat_100g': nutrients.get('fat_100g'),
                'saturated_fat_100g': nutrients.get('saturated-fat_100g'),
                'carbohydrates_100g': nutrients.get('carbohydrates_100g'),
                'proteins_100g': nutrients.get('proteins_100g'),
                'fiber_100g': nutrients.get('fiber_100g'),
            }
            # Remove None values
            nutrition_data['nutrients'] = {k: v for k, v in nutrition_data['nutrients'].items() if v is not None}

        # Product metadata (useful for display)
        if product.get('product_name'):
            nutrition_data['product_name'] = product['product_name']
        if product.get('brands'):
            nutrition_data['brands'] = product['brands']
        if product.get('image_url'):
            nutrition_data['image_url'] = product['image_url']

        # Additives (optional - can flag ultra-processed)
        if product.get('additives_tags'):
            nutrition_data['additives'] = product['additives_tags']

        # Only return if we have at least Nutri-Score or nutrients
        if 'nutriscore_grade' in nutrition_data or nutrition_data.get('nutrients'):
            return nutrition_data

        return None

    def enrich_grocery_item(self, grocery_item) -> bool:
        """
        Fetch and save nutrition data to a GroceryItem model instance

        Args:
            grocery_item: GroceryItem model instance with barcode

        Returns:
            True if nutrition data was successfully fetched and saved
        """
        if not grocery_item.barcode:
            logger.warning(f"⚠️  Cannot fetch nutrition: {grocery_item.name} has no barcode")
            return False

        # Check if we recently fetched nutrition (cache for 30 days)
        if grocery_item.last_nutrition_fetch:
            days_since_fetch = (timezone.now() - grocery_item.last_nutrition_fetch).days
            if days_since_fetch < 30:
                logger.info(f"⏭️  Skipping {grocery_item.name}: nutrition fetched {days_since_fetch} days ago")
                return False

        # Fetch nutrition data
        nutrition_data = self.fetch_product_by_barcode(grocery_item.barcode)

        if not nutrition_data:
            # Mark as fetched (but no data) to avoid repeated API calls
            grocery_item.last_nutrition_fetch = timezone.now()
            grocery_item.save(update_fields=['last_nutrition_fetch'])
            return False

        # Save to model
        if 'nutriscore_grade' in nutrition_data:
            grocery_item.nutriscore_grade = nutrition_data['nutriscore_grade']

        if 'nova_group' in nutrition_data:
            grocery_item.nova_group = nutrition_data['nova_group']

        if 'nutrients' in nutrition_data or nutrition_data.get('product_name'):
            grocery_item.nutrition_data = nutrition_data

        grocery_item.last_nutrition_fetch = timezone.now()
        grocery_item.save()

        logger.info(f"✅ Enriched {grocery_item.name} with nutrition data (Nutri-Score: {grocery_item.nutriscore_grade.upper() if grocery_item.nutriscore_grade else 'N/A'})")
        return True


# Singleton instance
_service_instance = None

def get_service() -> OpenFoodFactsService:
    """Get singleton OpenFoodFacts service instance"""
    global _service_instance
    if _service_instance is None:
        _service_instance = OpenFoodFactsService()
    return _service_instance
