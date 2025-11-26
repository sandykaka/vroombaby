"""
Apple Receipt Verification Service

Verifies In-App Purchase receipts with Apple's servers.
Required for production security - validates receipts haven't been tampered with.

Apple Docs: https://developer.apple.com/documentation/appstorereceipts/verifyreceipt
"""

import logging
import requests
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Apple's verification endpoints
APPLE_SANDBOX_URL = "https://sandbox.itunes.apple.com/verifyReceipt"
APPLE_PRODUCTION_URL = "https://buy.itunes.apple.com/verifyReceipt"

# Status codes from Apple
STATUS_VALID = 0
STATUS_SANDBOX_RECEIPT_SENT_TO_PRODUCTION = 21007
STATUS_PRODUCTION_RECEIPT_SENT_TO_SANDBOX = 21008


class AppleReceiptVerifier:
    """Verifies Apple In-App Purchase receipts"""

    def __init__(self, shared_secret: Optional[str] = None):
        """
        Initialize verifier with optional shared secret.

        Args:
            shared_secret: App-specific shared secret from App Store Connect
                          Required for auto-renewable subscriptions
        """
        self.shared_secret = shared_secret

    def verify_receipt(self, receipt_data: str, bundle_id: str) -> Tuple[bool, Dict]:
        """
        Verify receipt with Apple's servers.

        Args:
            receipt_data: Base64-encoded receipt data
            bundle_id: Expected bundle ID (e.g., "com.shopright.app")

        Returns:
            Tuple of (is_valid: bool, receipt_info: Dict)
        """
        # Try production first
        is_valid, response = self._verify_with_apple(receipt_data, APPLE_PRODUCTION_URL)

        # If it's a sandbox receipt, retry with sandbox
        if response and response.get('status') == STATUS_SANDBOX_RECEIPT_SENT_TO_PRODUCTION:
            logger.info("Receipt is from sandbox environment, retrying with sandbox URL")
            is_valid, response = self._verify_with_apple(receipt_data, APPLE_SANDBOX_URL)

        if not is_valid or not response:
            logger.error(f"Receipt verification failed: {response}")
            return False, {}

        # Extract receipt info
        receipt_info = self._extract_receipt_info(response, bundle_id)

        return receipt_info['is_valid'], receipt_info

    def _verify_with_apple(self, receipt_data: str, url: str) -> Tuple[bool, Optional[Dict]]:
        """
        Call Apple's verifyReceipt API.

        Args:
            receipt_data: Base64-encoded receipt
            url: Apple's verification URL (production or sandbox)

        Returns:
            Tuple of (success: bool, response: Dict)
        """
        payload = {
            "receipt-data": receipt_data,
            "password": self.shared_secret,  # Required for subscriptions
            "exclude-old-transactions": True  # Only return latest renewal info
        }

        try:
            logger.info(f"Verifying receipt with Apple: {url}")
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()

            data = response.json()
            status = data.get('status')

            if status == STATUS_VALID:
                logger.info("✅ Receipt verified successfully by Apple")
                return True, data
            else:
                logger.warning(f"⚠️ Apple returned status {status}: {self._get_status_message(status)}")
                return False, data

        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Network error verifying receipt: {e}")
            return False, None
        except Exception as e:
            logger.error(f"❌ Unexpected error verifying receipt: {e}")
            return False, None

    def _extract_receipt_info(self, apple_response: Dict, expected_bundle_id: str) -> Dict:
        """
        Extract and validate subscription info from Apple's response.

        Args:
            apple_response: Raw response from Apple's API
            expected_bundle_id: Expected bundle ID

        Returns:
            Dict with subscription details
        """
        receipt = apple_response.get('receipt', {})

        # Validate bundle ID
        bundle_id = receipt.get('bundle_id')
        if bundle_id != expected_bundle_id:
            logger.error(f"Bundle ID mismatch: expected {expected_bundle_id}, got {bundle_id}")
            return {'is_valid': False, 'error': 'Bundle ID mismatch'}

        # Extract latest subscription info
        latest_receipt_info = apple_response.get('latest_receipt_info', [])
        if not latest_receipt_info:
            logger.warning("No subscription info found in receipt")
            return {'is_valid': False, 'error': 'No subscription info'}

        # Get most recent transaction (sorted by purchase_date_ms)
        latest_transaction = max(latest_receipt_info, key=lambda x: int(x.get('purchase_date_ms', 0)))

        # Extract fields
        product_id = latest_transaction.get('product_id')
        transaction_id = latest_transaction.get('transaction_id')
        original_transaction_id = latest_transaction.get('original_transaction_id')
        purchase_date_ms = int(latest_transaction.get('purchase_date_ms', 0))
        expires_date_ms = int(latest_transaction.get('expires_date_ms', 0))

        # Convert milliseconds to datetime
        purchase_date = datetime.fromtimestamp(purchase_date_ms / 1000.0) if purchase_date_ms else None
        expires_date = datetime.fromtimestamp(expires_date_ms / 1000.0) if expires_date_ms else None

        # Check if subscription is currently active
        is_active = expires_date and expires_date > datetime.now() if expires_date else False

        # Determine subscription type from product ID
        subscription_type = 'monthly' if 'monthly' in product_id.lower() else 'annual'

        logger.info(f"📦 Subscription: {product_id}")
        logger.info(f"🎫 Transaction ID: {transaction_id}")
        logger.info(f"📅 Expires: {expires_date}")
        logger.info(f"✅ Active: {is_active}")

        return {
            'is_valid': True,
            'bundle_id': bundle_id,
            'product_id': product_id,
            'transaction_id': transaction_id,
            'original_transaction_id': original_transaction_id,
            'purchase_date': purchase_date,
            'expires_date': expires_date,
            'is_active': is_active,
            'subscription_type': subscription_type,
            'environment': apple_response.get('environment', 'Production')
        }

    def _get_status_message(self, status: int) -> str:
        """Get human-readable message for Apple status codes"""
        messages = {
            21000: "App Store cannot read receipt data",
            21002: "Receipt data malformed",
            21003: "Receipt could not be authenticated",
            21004: "Shared secret does not match",
            21005: "Receipt server unavailable",
            21006: "Receipt valid but subscription expired",
            21007: "Sandbox receipt sent to production",
            21008: "Production receipt sent to sandbox",
            21009: "Internal data access error",
            21010: "User account not found or deleted"
        }
        return messages.get(status, f"Unknown status code: {status}")


def verify_subscription_receipt(receipt_data: str, bundle_id: str, shared_secret: Optional[str] = None) -> Tuple[bool, Dict]:
    """
    Convenience function to verify a subscription receipt.

    Args:
        receipt_data: Base64-encoded receipt from iOS app
        bundle_id: Expected bundle ID (e.g., "com.shopright.app")
        shared_secret: App-specific shared secret from App Store Connect

    Returns:
        Tuple of (is_valid: bool, receipt_info: Dict)
    """
    verifier = AppleReceiptVerifier(shared_secret=shared_secret)
    return verifier.verify_receipt(receipt_data, bundle_id)
