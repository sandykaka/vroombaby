"""
RecallService - Fetches and matches product recalls from FDA, FSIS, and CPSC APIs

This service:
1. Fetches recalls from three government APIs (all FREE, no API keys required)
2. Stores recalls in ProductRecall model
3. Matches recalls against user purchase history (ShoppingTrip items)
4. Creates RecallMatch records with confidence scores
5. Notifies users for high-confidence matches (80%+ confidence)
"""

import requests
import logging
from datetime import datetime, timedelta
from django.db import transaction
from django.contrib.auth.models import User
from django.utils import timezone
from rapidfuzz import fuzz
from typing import List, Dict, Tuple, Optional

logger = logging.getLogger(__name__)


class RecallService:
    """Service for fetching and matching product recalls"""

    # API endpoints (all FREE government APIs)
    FDA_API = "https://api.fda.gov/food/enforcement.json"
    FSIS_API = "https://www.fsis.usda.gov/fsis-content-api/v1/recalls"
    CPSC_API = "https://www.saferproducts.gov/RestWebServices/Recall"

    # Matching confidence thresholds
    MIN_CONFIDENCE = 80  # Only notify if confidence >= 80%
    UPC_MATCH_CONFIDENCE = 100
    EXACT_NAME_BRAND_CONFIDENCE = 95
    FUZZY_MATCH_CONFIDENCE = 85

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'ShopRight Grocery App/1.0 (Recall Safety Service)'
        })

    # ==================== API FETCHING ====================

    def fetch_fda_recalls(self, days_back: int = 1) -> List[Dict]:
        """
        Fetch food recalls from FDA openFDA API

        Args:
            days_back: Number of days to look back (default: 1 for daily sync)

        Returns:
            List of recall dictionaries
        """
        try:
            # Calculate date range
            end_date = datetime.now()
            start_date = end_date - timedelta(days=days_back)

            # FDA API uses YYYYMMDD format in brackets
            # Correct format: report_date:[20251104+TO+20251111]
            search_query = f'report_date:[{start_date.strftime("%Y%m%d")}+TO+{end_date.strftime("%Y%m%d")}]'

            # Build query parameters
            params = {
                'search': search_query,
                'limit': 100  # Max results per request
            }

            logger.info(f"📡 Fetching FDA recalls from {start_date.date()} to {end_date.date()}")
            logger.info(f"   Search query: {search_query}")

            response = self.session.get(self.FDA_API, params=params, timeout=30)

            # Check if no results (404 might mean no recalls, not an error)
            if response.status_code == 404:
                logger.info(f"ℹ️  FDA API: No recalls found for date range")
                return []

            response.raise_for_status()

            data = response.json()
            results = data.get('results', [])

            logger.info(f"✅ FDA API: Found {len(results)} recalls")
            return results

        except requests.exceptions.RequestException as e:
            logger.error(f"❌ FDA API error: {e}")
            return []
        except Exception as e:
            logger.error(f"❌ FDA parsing error: {e}")
            return []

    def fetch_fsis_recalls(self, days_back: int = 1) -> List[Dict]:
        """
        Fetch meat/poultry recalls from FSIS USDA API

        Args:
            days_back: Number of days to look back

        Returns:
            List of recall dictionaries
        """
        try:
            # FSIS API might be blocking requests - try with browser-like headers
            headers = {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'application/json',
            }

            logger.info(f"📡 Fetching FSIS recalls (last {days_back} days)")

            response = requests.get(self.FSIS_API, headers=headers, timeout=30)

            # If 403, FSIS API might be down or blocking - skip gracefully
            if response.status_code == 403:
                logger.warning(f"⚠️  FSIS API: Access forbidden (403) - API may require authentication or be temporarily unavailable")
                return []

            response.raise_for_status()

            data = response.json()
            all_recalls = data if isinstance(data, list) else data.get('recalls', [])

            # Filter by date
            cutoff_date = datetime.now() - timedelta(days=days_back)
            recent_recalls = []

            for recall in all_recalls:
                # FSIS uses 'recallDate' field
                recall_date_str = recall.get('recallDate', '')
                if recall_date_str:
                    try:
                        recall_date = datetime.strptime(recall_date_str, '%Y-%m-%d')
                        if recall_date >= cutoff_date:
                            recent_recalls.append(recall)
                    except ValueError:
                        continue

            logger.info(f"✅ FSIS API: Found {len(recent_recalls)} recalls (filtered from {len(all_recalls)} total)")
            return recent_recalls

        except requests.exceptions.RequestException as e:
            logger.warning(f"⚠️  FSIS API error: {e}")
            logger.info(f"   Continuing without FSIS recalls...")
            return []
        except Exception as e:
            logger.error(f"❌ FSIS parsing error: {e}")
            return []

    def fetch_cpsc_recalls(self, days_back: int = 1) -> List[Dict]:
        """
        Fetch consumer product recalls from CPSC API

        Args:
            days_back: Number of days to look back

        Returns:
            List of recall dictionaries
        """
        try:
            # CPSC API endpoint for recent recalls
            # Format: YYYY-MM-DD
            end_date = datetime.now()
            start_date = end_date - timedelta(days=days_back)

            params = {
                'RecallDateStart': start_date.strftime('%Y-%m-%d'),
                'RecallDateEnd': end_date.strftime('%Y-%m-%d'),
                'format': 'json'
            }

            logger.info(f"📡 Fetching CPSC recalls from {start_date.date()} to {end_date.date()}")

            response = self.session.get(self.CPSC_API, params=params, timeout=30)
            response.raise_for_status()

            data = response.json()
            results = data if isinstance(data, list) else []

            logger.info(f"✅ CPSC API: Found {len(results)} recalls")
            return results

        except requests.exceptions.RequestException as e:
            logger.error(f"❌ CPSC API error: {e}")
            return []
        except Exception as e:
            logger.error(f"❌ CPSC parsing error: {e}")
            return []

    # ==================== DATA PROCESSING ====================

    def save_fda_recall(self, recall_data: Dict) -> Optional['ProductRecall']:
        """
        Parse FDA recall JSON and save to ProductRecall model

        FDA recall structure:
        {
            "recall_number": "F-0234-2024",
            "classification": "Class I",
            "status": "Ongoing",
            "product_description": "Whole milk...",
            "reason_for_recall": "Undeclared allergen: milk",
            "recalling_firm": "Company Name",
            "distribution_pattern": "Nationwide",
            "report_date": "20240115",
            ...
        }
        """
        from shopright.models import ProductRecall

        try:
            recall_number = recall_data.get('recall_number', '')

            # Skip if already exists
            if ProductRecall.objects.filter(recall_number=recall_number).exists():
                return None

            # Parse dates
            report_date_str = recall_data.get('report_date', '')
            recall_initiation_str = recall_data.get('recall_initiation_date', report_date_str)

            report_date = datetime.strptime(report_date_str, '%Y%m%d').date() if report_date_str else datetime.now().date()
            recall_initiation_date = datetime.strptime(recall_initiation_str, '%Y%m%d').date() if recall_initiation_str else report_date

            # Extract product info
            product_description = recall_data.get('product_description', '')

            # Try to extract product name from description (usually first 100 chars)
            product_name = product_description[:500] if product_description else 'Unknown Product'

            # Create ProductRecall record
            recall = ProductRecall.objects.create(
                source='FDA',
                recall_number=recall_number,
                classification=recall_data.get('classification', 'Class II'),
                status=recall_data.get('status', 'Active'),
                recall_initiation_date=recall_initiation_date,
                recall_posted_date=report_date,
                product_name=product_name,
                product_description=product_description,
                recalling_firm=recall_data.get('recalling_firm', 'Unknown'),
                distribution_pattern=recall_data.get('distribution_pattern', ''),
                reason_for_recall=recall_data.get('reason_for_recall', ''),
                health_hazard_evaluation=recall_data.get('health_hazard_evaluation', ''),
                raw_data=recall_data
            )

            logger.info(f"✅ Saved FDA recall: {recall_number}")
            return recall

        except Exception as e:
            logger.error(f"❌ Failed to save FDA recall: {e}")
            return None

    def save_fsis_recall(self, recall_data: Dict) -> Optional['ProductRecall']:
        """
        Parse FSIS recall JSON and save to ProductRecall model

        FSIS recall structure:
        {
            "recallNumber": "036-2024",
            "recallClass": "I",
            "productName": "Chicken Croquettes",
            "recallReason": "Undeclared Milk Allergen",
            "companyName": "Saint Coxinha...",
            "recallDate": "2024-01-15",
            ...
        }
        """
        from shopright.models import ProductRecall

        try:
            recall_number = recall_data.get('recallNumber', '')

            # Skip if already exists
            if ProductRecall.objects.filter(recall_number=recall_number).exists():
                return None

            # Parse date
            recall_date_str = recall_data.get('recallDate', '')
            recall_date = datetime.strptime(recall_date_str, '%Y-%m-%d').date() if recall_date_str else datetime.now().date()

            # Map FSIS classification (I, II, III) to our format
            fsis_class = recall_data.get('recallClass', 'II')
            classification = f"Class {fsis_class}"

            # Create ProductRecall record
            recall = ProductRecall.objects.create(
                source='FSIS',
                recall_number=recall_number,
                classification=classification,
                status='Active',  # FSIS doesn't always provide status
                recall_initiation_date=recall_date,
                recall_posted_date=recall_date,
                product_name=recall_data.get('productName', 'Unknown Product'),
                product_description=recall_data.get('productDescription', recall_data.get('productName', '')),
                recalling_firm=recall_data.get('companyName', 'Unknown'),
                distribution_pattern=recall_data.get('distribution', ''),
                reason_for_recall=recall_data.get('recallReason', ''),
                raw_data=recall_data
            )

            logger.info(f"✅ Saved FSIS recall: {recall_number}")
            return recall

        except Exception as e:
            logger.error(f"❌ Failed to save FSIS recall: {e}")
            return None

    def save_cpsc_recall(self, recall_data: Dict) -> Optional['ProductRecall']:
        """
        Parse CPSC recall JSON and save to ProductRecall model

        CPSC recall structure:
        {
            "RecallNumber": "26077",
            "RecallDate": "2025-11-06T00:00:00",
            "Title": "Product Recalled Due to...",
            "Description": "Full description...",
            "Products": [{"Name": "Product Name", "NumberOfUnits": "3,294"}],
            "ConsumerContact": "...",
            ...
        }
        """
        from shopright.models import ProductRecall

        try:
            recall_number = recall_data.get('RecallNumber', '')

            # Skip if already exists
            if ProductRecall.objects.filter(recall_number=recall_number).exists():
                return None

            # Parse date - handle both formats: "2024-01-15" and "2024-01-15T00:00:00"
            recall_date_str = recall_data.get('RecallDate', '')
            if recall_date_str:
                # Remove time component if present
                if 'T' in recall_date_str:
                    recall_date_str = recall_date_str.split('T')[0]
                recall_date = datetime.strptime(recall_date_str, '%Y-%m-%d').date()
            else:
                recall_date = datetime.now().date()

            # CPSC doesn't use Class I/II/III - default to Class II
            classification = 'Class II'

            # Extract product name from Products array (if available)
            products = recall_data.get('Products', [])
            if products and isinstance(products, list) and len(products) > 0:
                product_name = products[0].get('Name', 'Unknown Product')
            else:
                # Fallback to Title if no Products
                title = recall_data.get('Title', 'Unknown Product')
                product_name = title[:500]

            # Get description and title
            description = recall_data.get('Description', '')
            title = recall_data.get('Title', product_name)

            # Extract hazard/reason from Description (CPSC doesn't have separate hazard field)
            # Usually the title contains the hazard reason
            reason = title if title else "See description"

            # Create ProductRecall record
            recall = ProductRecall.objects.create(
                source='CPSC',
                recall_number=recall_number,
                classification=classification,
                status='Active',
                recall_initiation_date=recall_date,
                recall_posted_date=recall_date,
                product_name=product_name[:500],  # Enforce max length
                product_description=description,
                recalling_firm=recall_data.get('Manufacturer', 'Unknown'),
                distribution_pattern='',
                reason_for_recall=reason[:500] if reason else '',
                remedy=recall_data.get('ConsumerContact', ''),
                raw_data=recall_data
            )

            logger.info(f"✅ Saved CPSC recall: {recall_number} - {product_name[:50]}")
            return recall

        except Exception as e:
            logger.error(f"❌ Failed to save CPSC recall: {e}")
            logger.error(f"   Recall data: {recall_data.get('RecallNumber', 'Unknown')} - {recall_data.get('Title', '')[:50]}")
            return None

    # ==================== MATCHING ALGORITHM ====================

    def match_recalls_to_purchases(self, recall_id: Optional[int] = None) -> int:
        """
        Match recalls against user purchase history

        Confidence scoring:
        - 100%: UPC/barcode exact match
        - 95%: Product name + brand exact match
        - 85%: Fuzzy product name + store + purchase date in recall window

        Only creates RecallMatch if confidence >= 80%

        Args:
            recall_id: Specific recall to match (None = match all active recalls)

        Returns:
            Number of new matches created
        """
        from shopright.models import ProductRecall, RecallMatch, ShoppingTrip, GroceryItem

        # Get recalls to match
        recalls = ProductRecall.objects.filter(status='Active')
        if recall_id:
            recalls = recalls.filter(id=recall_id)

        logger.info(f"🔍 Matching {recalls.count()} active recalls against purchase history")

        matches_created = 0

        for recall in recalls:
            logger.info(f"🔍 Processing recall {recall.recall_number}: {recall.product_name[:50]}")

            # Get all shopping trips (limit to last 90 days for performance)
            cutoff_date = timezone.now() - timedelta(days=90)
            trips = ShoppingTrip.objects.filter(trip_date__gte=cutoff_date).select_related('user')

            for trip in trips:
                # Check each item in the trip
                items = trip.items  # JSON array of purchased items

                for item in items:
                    match_result = self._check_item_match(recall, trip, item)

                    if match_result:
                        confidence, reason = match_result

                        # Only create match if confidence >= threshold
                        if confidence >= self.MIN_CONFIDENCE:
                            # Check if match already exists
                            existing = RecallMatch.objects.filter(
                                recall=recall,
                                user=trip.user,
                                shopping_trip=trip
                            ).first()

                            if not existing:
                                # Create new match
                                RecallMatch.objects.create(
                                    recall=recall,
                                    user=trip.user,
                                    shopping_trip=trip,
                                    purchased_product_name=item.get('name', 'Unknown'),
                                    purchased_at_store=trip.store_name,
                                    purchased_date=trip.trip_date.date(),
                                    confidence_score=confidence,
                                    match_reason=reason
                                )

                                matches_created += 1
                                logger.info(f"✅ Match created: {trip.user.username} - {item.get('name', 'Unknown')} ({confidence}% confidence)")

        logger.info(f"🎯 Created {matches_created} new recall matches")
        return matches_created

    def _check_item_match(self, recall: 'ProductRecall', trip: 'ShoppingTrip', item: Dict) -> Optional[Tuple[int, str]]:
        """
        Check if a purchased item matches a recall

        Returns:
            Tuple of (confidence_score, match_reason) or None if no match
        """
        item_name = item.get('name', '').lower()
        item_brand = item.get('brand', '').lower()
        item_barcode = item.get('barcode', '')

        recall_name = recall.product_name.lower()
        recall_firm = recall.recalling_firm.lower()
        recall_upc_codes = recall.upc_codes or []

        # === 100% CONFIDENCE: UPC/Barcode exact match ===
        if item_barcode and recall_upc_codes:
            if item_barcode in recall_upc_codes:
                return (self.UPC_MATCH_CONFIDENCE, f"UPC match: {item_barcode}")

        # === 95% CONFIDENCE: Exact product name + brand match ===
        if item_name and item_brand:
            # Exact match (case-insensitive)
            if item_name in recall_name and item_brand in recall_firm:
                return (self.EXACT_NAME_BRAND_CONFIDENCE, f"Exact product+brand match: {item_name} by {item_brand}")

        # === 85% CONFIDENCE: Fuzzy product name + store match ===
        if item_name and recall_name:
            # Use RapidFuzz for fuzzy string matching
            similarity = fuzz.token_set_ratio(item_name, recall_name)

            # If similarity >= 85% AND purchase date is within recall window
            if similarity >= 85:
                # Check if purchase date is within recall distribution window
                # (purchases should be BEFORE recall date but after typical shelf life window)
                purchase_date = trip.trip_date.date()
                recall_date = recall.recall_posted_date

                # Typical grocery shelf life: 90 days
                min_purchase_date = recall_date - timedelta(days=90)

                if min_purchase_date <= purchase_date <= recall_date:
                    # Extra points if store matches distribution pattern
                    if recall.distribution_pattern:
                        if 'nationwide' in recall.distribution_pattern.lower():
                            return (self.FUZZY_MATCH_CONFIDENCE, f"Fuzzy match ({similarity}% similar): {item_name}")
                        elif trip.store_location:
                            # Check if state/city matches
                            # (This is a simplified check - could be enhanced)
                            if any(loc in trip.store_location for loc in recall.distribution_pattern.split(',')):
                                return (self.FUZZY_MATCH_CONFIDENCE, f"Fuzzy match with location ({similarity}% similar): {item_name}")

        # No match
        return None

    # ==================== MAIN SYNC METHODS ====================

    def sync_all_recalls(self, days_back: int = 7) -> Dict[str, int]:
        """
        Fetch recalls from all sources and match against purchases

        Args:
            days_back: Number of days to look back (default: 7 to account for API lag)

        Note:
            FDA API updates weekly (not real-time), so querying 7 days ensures we catch
            all recalls even with API lag. The code de-dupes by recall_number automatically.

        Returns:
            Dict with counts: {'fda': 5, 'fsis': 3, 'cpsc': 2, 'matches': 8}
        """
        logger.info(f"🚀 Starting recall sync (last {days_back} days)")

        counts = {
            'fda': 0,
            'fsis': 0,
            'cpsc': 0,
            'matches': 0
        }

        # Fetch FDA recalls
        fda_recalls = self.fetch_fda_recalls(days_back)
        for recall_data in fda_recalls:
            if self.save_fda_recall(recall_data):
                counts['fda'] += 1

        # Fetch FSIS recalls
        fsis_recalls = self.fetch_fsis_recalls(days_back)
        for recall_data in fsis_recalls:
            if self.save_fsis_recall(recall_data):
                counts['fsis'] += 1

        # Fetch CPSC recalls
        cpsc_recalls = self.fetch_cpsc_recalls(days_back)
        for recall_data in cpsc_recalls:
            if self.save_cpsc_recall(recall_data):
                counts['cpsc'] += 1

        # Match recalls to purchases
        counts['matches'] = self.match_recalls_to_purchases()

        logger.info(f"✅ Recall sync complete: {counts}")
        return counts

    def sync_urgent_recalls(self) -> Dict[str, int]:
        """
        Fetch only Class I (critical) recalls for urgent 5 PM check

        Note:
            Uses 7-day lookback to account for FDA API lag

        Returns:
            Dict with counts: {'critical_recalls': 3, 'matches': 5}
        """
        from shopright.models import ProductRecall

        logger.info(f"🚨 Urgent Class I recall check")

        # Fetch last 7 days of recalls (to account for API lag)
        counts = self.sync_all_recalls(days_back=7)

        # Count Class I recalls
        critical_count = ProductRecall.objects.filter(
            classification='Class I',
            recall_posted_date__gte=datetime.now().date() - timedelta(days=7)
        ).count()

        return {
            'critical_recalls': critical_count,
            'matches': counts['matches']
        }
