"""
StripeService - Manages Stripe subscription billing for GroceryGuard delivery

This service:
1. Creates/retrieves Stripe customers for users
2. Creates recurring weekly subscriptions ($15 Basic / $30 Premium)
3. Cancels subscriptions when users unsubscribe
4. Handles Stripe webhook events for subscription lifecycle
"""

import logging
import stripe
from django.conf import settings
from django.contrib.auth.models import User
from typing import Tuple, Optional

logger = logging.getLogger(__name__)

# Initialize Stripe with secret key
stripe.api_key = settings.STRIPE_SECRET_KEY


class StripeService:
    """Service for managing Stripe subscriptions for GroceryGuard delivery"""

    @staticmethod
    def get_or_create_stripe_customer(user: User) -> Tuple[bool, str, Optional[str]]:
        """
        Get existing Stripe customer or create a new one

        Args:
            user: Django User instance

        Returns:
            Tuple of (success: bool, customer_id: str, error_message: str or None)
            Examples:
                (True, "cus_ABC123", None)
                (False, "", "Stripe API error: ...")
        """
        try:
            # Check UserProfile first for stripe_customer_id
            from shopright.models import UserProfile
            profile, created = UserProfile.objects.get_or_create(
                user=user,
                defaults={'account_type': 'customer'}
            )

            if profile.stripe_customer_id:
                logger.info(f"Found existing Stripe customer for {user.username}: {profile.stripe_customer_id}")
                return (True, profile.stripe_customer_id, None)

            # Fallback: Check if user has a Stripe customer ID in any subscription (legacy)
            from shopright.models import DeliverySubscription
            existing_sub = DeliverySubscription.objects.filter(
                customer=user,
                stripe_customer_id__isnull=False
            ).exclude(stripe_customer_id='').first()

            if existing_sub and existing_sub.stripe_customer_id:
                # Migrate customer ID to UserProfile
                profile.stripe_customer_id = existing_sub.stripe_customer_id
                profile.save()
                logger.info(f"Migrated Stripe customer ID to UserProfile for {user.username}: {existing_sub.stripe_customer_id}")
                return (True, existing_sub.stripe_customer_id, None)

            # Create new Stripe customer
            customer = stripe.Customer.create(
                email=user.email or f"{user.username}@placeholder.com",
                metadata={
                    'user_id': user.id,
                    'username': user.username
                },
                description=f"GroceryGuard user {user.username}"
            )

            # Store customer ID on UserProfile
            profile.stripe_customer_id = customer.id
            profile.save()

            logger.info(f"✅ Created new Stripe customer for {user.username}: {customer.id}")
            return (True, customer.id, None)

        except stripe.error.StripeError as e:
            logger.error(f"Stripe API error creating customer: {e}")
            return (False, "", str(e))
        except Exception as e:
            logger.error(f"Unexpected error creating Stripe customer: {e}")
            return (False, "", str(e))

    @staticmethod
    def create_subscription(
        customer_id: str,
        price_id: str,
        metadata: dict
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Create a Stripe subscription

        Args:
            customer_id: Stripe customer ID
            price_id: Stripe price ID (STRIPE_PRICE_BASIC or STRIPE_PRICE_PREMIUM)
            metadata: Dict with subscription metadata (delivery_subscription_id, store_name, etc.)

        Returns:
            Tuple of (success: bool, subscription_id: str or None, error_message: str or None)
            Examples:
                (True, "sub_ABC123", None)
                (False, None, "Stripe API error: ...")
        """
        try:
            subscription = stripe.Subscription.create(
                customer=customer_id,
                items=[{'price': price_id}],
                metadata=metadata,
                proration_behavior='none'  # No prorating for weekly subscriptions
            )

            logger.info(f"✅ Created Stripe subscription {subscription.id} for customer {customer_id}")
            return (True, subscription.id, None)

        except stripe.error.StripeError as e:
            logger.error(f"Stripe API error creating subscription: {e}")
            return (False, None, str(e))
        except Exception as e:
            logger.error(f"Unexpected error creating Stripe subscription: {e}")
            return (False, None, str(e))

    @staticmethod
    def cancel_subscription(subscription_id: str) -> Tuple[bool, Optional[str]]:
        """
        Cancel a Stripe subscription (immediately)

        Args:
            subscription_id: Stripe subscription ID

        Returns:
            Tuple of (success: bool, error_message: str or None)
            Examples:
                (True, None)
                (False, "Subscription not found")
        """
        try:
            stripe.Subscription.cancel(subscription_id)
            logger.info(f"✅ Cancelled Stripe subscription {subscription_id}")
            return (True, None)

        except stripe.error.StripeError as e:
            logger.error(f"Stripe API error cancelling subscription: {e}")
            return (False, str(e))
        except Exception as e:
            logger.error(f"Unexpected error cancelling Stripe subscription: {e}")
            return (False, str(e))

    @staticmethod
    def upgrade_subscription(
        subscription_id: str,
        new_price_id: str,
        metadata: dict
    ) -> Tuple[bool, Optional[str]]:
        """
        Upgrade existing Stripe subscription to new tier (Apple-way: prorated billing)

        This method:
        1. Modifies existing subscription to new price tier
        2. Enables prorated billing (only charge difference)
        3. Keeps same billing cycle anchor
        4. Updates metadata

        Args:
            subscription_id: Existing Stripe subscription ID
            new_price_id: New Stripe price ID (e.g., premium tier)
            metadata: Updated metadata for subscription

        Returns:
            Tuple of (success: bool, error_message: str or None)
            Examples:
                (True, None)  # Upgrade successful, user charged prorated amount
                (False, "Subscription not found")
        """
        try:
            # First, get the existing subscription to find the current item
            existing_subscription = stripe.Subscription.retrieve(subscription_id)

            # Access subscription items correctly - items is a property, not method
            subscription_items = existing_subscription['items']['data']
            if not subscription_items:
                logger.error(f"No subscription items found for {subscription_id}")
                return (False, "Invalid subscription: no items found")

            # Get the first (and should be only) subscription item
            current_item = subscription_items[0]

            # Modify the subscription to upgrade the price
            updated_subscription = stripe.Subscription.modify(
                subscription_id,
                items=[{
                    'id': current_item['id'],  # Existing item ID
                    'price': new_price_id,  # New price (e.g., premium)
                }],
                proration_behavior='create_prorations',  # Enable prorated billing (Apple way!)
                billing_cycle_anchor='unchanged',  # Keep same billing day
                metadata=metadata  # Update metadata with new tier info
            )

            logger.info(f"✅ Upgraded Stripe subscription {subscription_id} to new price {new_price_id} (prorated)")
            # Log billing cycle info if available
            if 'current_period_end' in updated_subscription:
                logger.info(f"   Billing cycle anchor maintained: {updated_subscription['current_period_end']}")
            else:
                logger.info(f"   Subscription successfully upgraded with prorated billing")
            return (True, None)

        except stripe.error.StripeError as e:
            logger.error(f"Stripe API error upgrading subscription: {e}")
            return (False, str(e))
        except Exception as e:
            logger.error(f"Unexpected error upgrading Stripe subscription: {e}")
            return (False, str(e))

    @staticmethod
    def pause_subscription(subscription_id: str) -> Tuple[bool, Optional[str]]:
        """
        Pause a Stripe subscription (customer can resume later)

        Args:
            subscription_id: Stripe subscription ID

        Returns:
            Tuple of (success: bool, error_message: str or None)
        """
        try:
            stripe.Subscription.modify(
                subscription_id,
                pause_collection={'behavior': 'void'}  # Pause without canceling
            )
            logger.info(f"✅ Paused Stripe subscription {subscription_id}")
            return (True, None)

        except stripe.error.StripeError as e:
            logger.error(f"Stripe API error pausing subscription: {e}")
            return (False, str(e))
        except Exception as e:
            logger.error(f"Unexpected error pausing Stripe subscription: {e}")
            return (False, str(e))

    @staticmethod
    def resume_subscription(subscription_id: str) -> Tuple[bool, Optional[str]]:
        """
        Resume a paused Stripe subscription

        Args:
            subscription_id: Stripe subscription ID

        Returns:
            Tuple of (success: bool, error_message: str or None)
        """
        try:
            stripe.Subscription.modify(
                subscription_id,
                pause_collection=''  # Remove pause
            )
            logger.info(f"✅ Resumed Stripe subscription {subscription_id}")
            return (True, None)

        except stripe.error.StripeError as e:
            logger.error(f"Stripe API error resuming subscription: {e}")
            return (False, str(e))
        except Exception as e:
            logger.error(f"Unexpected error resuming Stripe subscription: {e}")
            return (False, str(e))

    @staticmethod
    def create_and_attach_payment_method(
        customer_id: str,
        card_number: str,
        exp_month: int,
        exp_year: int,
        cvc: str
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Create a payment method from card details and attach to customer

        Args:
            customer_id: Stripe customer ID
            card_number: Card number (e.g., "4242424242424242")
            exp_month: Expiration month (1-12)
            exp_year: Expiration year (e.g., 2034)
            cvc: Card verification code

        Returns:
            Tuple of (success: bool, payment_method_id: str or None, error_message: str or None)
        """
        try:
            # Create payment method
            payment_method = stripe.PaymentMethod.create(
                type='card',
                card={
                    'number': card_number,
                    'exp_month': exp_month,
                    'exp_year': exp_year,
                    'cvc': cvc,
                }
            )

            # Attach to customer
            stripe.PaymentMethod.attach(
                payment_method.id,
                customer=customer_id,
            )

            # Set as default
            stripe.Customer.modify(
                customer_id,
                invoice_settings={'default_payment_method': payment_method.id}
            )

            logger.info(f"✅ Created and attached payment method {payment_method.id} to customer {customer_id}")
            return (True, payment_method.id, None)

        except stripe.error.StripeError as e:
            logger.error(f"Stripe API error creating payment method: {e}")
            return (False, None, str(e))
        except Exception as e:
            logger.error(f"Unexpected error creating payment method: {e}")
            return (False, None, str(e))

    @staticmethod
    def create_setup_intent(customer_id: str) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Create a SetupIntent for collecting payment method

        Args:
            customer_id: Stripe customer ID

        Returns:
            Tuple of (success: bool, client_secret: str or None, error_message: str or None)
        """
        try:
            setup_intent = stripe.SetupIntent.create(
                customer=customer_id,
                payment_method_types=['card'],
            )

            logger.info(f"✅ Created SetupIntent for customer {customer_id}")
            return (True, setup_intent.client_secret, None)

        except stripe.error.StripeError as e:
            logger.error(f"Stripe API error creating SetupIntent: {e}")
            return (False, None, str(e))
        except Exception as e:
            logger.error(f"Unexpected error creating SetupIntent: {e}")
            return (False, None, str(e))

    @staticmethod
    def attach_payment_method(
        customer_id: str,
        payment_method_id: str
    ) -> Tuple[bool, Optional[str]]:
        """
        Attach a payment method to a customer and set it as default

        Args:
            customer_id: Stripe customer ID
            payment_method_id: Stripe payment method ID (from client-side)

        Returns:
            Tuple of (success: bool, error_message: str or None)
        """
        try:
            # Try to attach payment method to customer
            try:
                stripe.PaymentMethod.attach(
                    payment_method_id,
                    customer=customer_id,
                )
                logger.info(f"✅ Attached payment method {payment_method_id} to customer {customer_id}")
            except stripe.error.InvalidRequestError as e:
                # If already attached (e.g., from SetupIntent), that's fine - just continue to set as default
                if "already been attached" in str(e):
                    logger.info(f"Payment method {payment_method_id} already attached to customer {customer_id} (from SetupIntent)")
                else:
                    raise  # Re-raise if it's a different error

            # Set as default payment method
            stripe.Customer.modify(
                customer_id,
                invoice_settings={'default_payment_method': payment_method_id}
            )

            logger.info(f"✅ Set payment method {payment_method_id} as default for customer {customer_id}")
            return (True, None)

        except stripe.error.StripeError as e:
            logger.error(f"Stripe API error attaching payment method: {e}")
            return (False, str(e))
        except Exception as e:
            logger.error(f"Unexpected error attaching payment method: {e}")
            return (False, str(e))

    @staticmethod
    def charge_subscription_fee(
        customer_id: str,
        payment_method_id: str,
        amount: float,
        description: str,
        metadata: dict = None
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Charge a one-time subscription fee (NOT recurring)

        Used when shopper accepts delivery to charge the weekly subscription fee upfront.

        Args:
            customer_id: Stripe customer ID
            payment_method_id: Stripe payment method ID
            amount: Amount in dollars (e.g., 30.00 for $30)
            description: Charge description (e.g., "ShopRight Weekly Delivery - Dec 14")
            metadata: Optional metadata dict

        Returns:
            Tuple of (success: bool, charge_id: str or None, error_message: str or None)
            Examples:
                (True, "ch_ABC123", None)
                (False, None, "Card declined")
        """
        try:
            # Create charge using PaymentIntent (recommended over legacy Charges API)
            payment_intent = stripe.PaymentIntent.create(
                amount=int(amount * 100),  # Convert dollars to cents
                currency='usd',
                customer=customer_id,
                payment_method=payment_method_id,
                off_session=True,  # Customer is not present for this charge
                confirm=True,  # Auto-confirm the payment
                description=description,
                statement_descriptor='GroceryGuard',  # Appears on credit card statement (max 22 chars)
                metadata=metadata or {}
            )

            logger.info(f"✅ Charged ${amount} to customer {customer_id}: {payment_intent.id}")
            return (True, payment_intent.id, None)

        except stripe.error.CardError as e:
            # Card was declined
            logger.warning(f"Card declined for customer {customer_id}: {e.user_message}")
            return (False, None, e.user_message)
        except stripe.error.StripeError as e:
            logger.error(f"Stripe API error charging customer: {e}")
            return (False, None, str(e))
        except Exception as e:
            logger.error(f"Unexpected error charging customer: {e}")
            return (False, None, str(e))

    @staticmethod
    def pre_authorize_payment(customer, amount: int, delivery_id: int) -> dict:
        """
        Pre-authorize a payment by creating a PaymentIntent in requires_capture mode
        This places a hold on the customer's payment method without actually charging them

        Args:
            customer: User object or UserProfile with Stripe customer info
            amount: Amount in cents to pre-authorize
            delivery_id: Delivery ID for metadata

        Returns:
            dict: {'success': bool, 'authorization_id': str, 'error': str}
        """
        try:
            # Handle both User and UserProfile objects
            if hasattr(customer, 'stripe_customer_id'):
                # It's a UserProfile
                customer_profile = customer
            else:
                # It's a User object - get or create the UserProfile
                from shopright.models import UserProfile
                try:
                    customer_profile = customer.profile
                except UserProfile.DoesNotExist:
                    # Auto-create UserProfile if it doesn't exist (for existing users)
                    logger.warning(f"Creating missing UserProfile for customer {customer.username}")
                    customer_profile = UserProfile.objects.create(
                        user=customer,
                        account_type='customer'
                    )

            # Get customer's Stripe customer ID
            if not customer_profile.stripe_customer_id:
                logger.error(f"Customer {customer.username if hasattr(customer, 'username') else 'Unknown'} has no Stripe customer ID")
                return {
                    'success': False,
                    'error': 'No payment method configured'
                }

            # Get customer from Stripe to find default payment method
            stripe_customer = stripe.Customer.retrieve(customer_profile.stripe_customer_id)

            if not stripe_customer.invoice_settings.default_payment_method:
                logger.error(f"Customer {customer_profile.stripe_customer_id} has no default payment method")
                return {
                    'success': False,
                    'error': 'No default payment method found'
                }

            # Create PaymentIntent with capture_method='manual' for pre-authorization
            payment_intent = stripe.PaymentIntent.create(
                amount=amount,
                currency='usd',
                customer=customer_profile.stripe_customer_id,
                payment_method=stripe_customer.invoice_settings.default_payment_method,
                capture_method='manual',  # This creates a hold instead of charging immediately
                confirm=True,
                off_session=True,  # Indicates customer is not present (server-side payment)
                metadata={
                    'delivery_id': str(delivery_id),
                    'type': 'grocery_delivery_hold',
                    'user_id': str(customer_profile.user.id)
                },
                description=f"Grocery delivery authorization for delivery #{delivery_id}"
            )

            logger.info(f"✅ Pre-authorized ${amount/100:.2f} for delivery {delivery_id}: {payment_intent.id}")

            return {
                'success': True,
                'authorization_id': payment_intent.id,
                'amount': amount
            }

        except stripe.error.CardError as e:
            # Card was declined
            logger.error(f"Card declined for pre-authorization: {e.user_message}")
            return {
                'success': False,
                'error': f"Payment method declined: {e.user_message}"
            }
        except stripe.error.StripeError as e:
            logger.error(f"Stripe error during pre-authorization: {e}")
            return {
                'success': False,
                'error': 'Payment system error. Please try again.'
            }
        except Exception as e:
            logger.error(f"Unexpected error during pre-authorization: {e}")
            return {
                'success': False,
                'error': 'Payment authorization failed. Please try again.'
            }

    @staticmethod
    def capture_authorized_payment(authorization_id: str, final_amount: int, delivery_id: int) -> dict:
        """
        Capture a previously authorized payment with the final amount from receipt
        This actually charges the customer's payment method

        Args:
            authorization_id: PaymentIntent ID from pre_authorize_payment
            final_amount: Final amount in cents from scanned receipt
            delivery_id: Delivery ID for logging/metadata

        Returns:
            dict: {'success': bool, 'charge_id': str, 'error': str}
        """
        try:
            # Retrieve the PaymentIntent
            payment_intent = stripe.PaymentIntent.retrieve(authorization_id)

            if payment_intent.status == 'succeeded':
                # Payment was already captured automatically (some payment methods auto-capture)
                logger.warning(f"PaymentIntent {authorization_id} already succeeded, processing adjustment...")

                authorized_amount = payment_intent.amount
                amount_difference = final_amount - authorized_amount

                if amount_difference == 0:
                    # No adjustment needed - amounts match perfectly
                    logger.info(f"✅ Payment already captured for exact amount: ${final_amount/100:.2f}")
                    return {
                        'success': True,
                        'charge_id': payment_intent.latest_charge,
                        'amount_captured': final_amount
                    }
                elif amount_difference > 0:
                    # Customer owes more - create additional charge
                    logger.info(f"📈 Customer owes additional ${amount_difference/100:.2f}")

                    additional_payment = stripe.PaymentIntent.create(
                        amount=amount_difference,
                        currency='usd',
                        customer=payment_intent.customer,
                        payment_method=payment_intent.payment_method,
                        confirm=True,
                        off_session=True,
                        metadata={
                            'delivery_id': str(delivery_id),
                            'type': 'grocery_delivery_adjustment',
                            'original_payment': authorization_id
                        },
                        description=f"Additional charge for delivery #{delivery_id} (${amount_difference/100:.2f})"
                    )

                    logger.info(f"✅ Additional charge successful: {additional_payment.id}")
                    return {
                        'success': True,
                        'charge_id': payment_intent.latest_charge,
                        'amount_captured': final_amount
                    }
                else:
                    # Customer paid too much - process refund
                    refund_amount = abs(amount_difference)
                    logger.info(f"📉 Refunding ${refund_amount/100:.2f} to customer")

                    refund = stripe.Refund.create(
                        charge=payment_intent.latest_charge,
                        amount=refund_amount,
                        metadata={
                            'delivery_id': str(delivery_id),
                            'type': 'grocery_delivery_adjustment'
                        },
                        reason='requested_by_customer'
                    )

                    logger.info(f"✅ Refund successful: {refund.id}")
                    return {
                        'success': True,
                        'charge_id': payment_intent.latest_charge,
                        'amount_captured': final_amount
                    }

            elif payment_intent.status != 'requires_capture':
                logger.error(f"PaymentIntent {authorization_id} is in unexpected status: {payment_intent.status}")
                return {
                    'success': False,
                    'error': f'Payment authorization is in {payment_intent.status} status and cannot be processed'
                }

            # Check if final amount exceeds authorized amount
            authorized_amount = payment_intent.amount

            if final_amount > authorized_amount:
                # Cannot capture more than authorized - need to create additional charge
                # This happens when receipt total exceeds our pre-authorization estimate
                logger.info(f"📈 Final amount ${final_amount/100:.2f} exceeds authorized ${authorized_amount/100:.2f}")

                # First capture the full authorized amount
                captured_intent = stripe.PaymentIntent.capture(authorization_id)
                logger.info(f"✅ Captured authorized amount: ${authorized_amount/100:.2f}")

                # Then create additional charge for the difference
                additional_amount = final_amount - authorized_amount
                logger.info(f"💳 Creating additional charge for ${additional_amount/100:.2f}")

                additional_payment = stripe.PaymentIntent.create(
                    amount=additional_amount,
                    currency='usd',
                    customer=payment_intent.customer,
                    payment_method=payment_intent.payment_method,
                    confirm=True,
                    off_session=True,
                    metadata={
                        'delivery_id': str(delivery_id),
                        'type': 'grocery_delivery_additional_charge',
                        'original_payment': authorization_id
                    },
                    description=f"Additional charge for delivery #{delivery_id} - Receipt total exceeded estimate"
                )

                logger.info(f"✅ Additional charge successful: {additional_payment.id}")
                return {
                    'success': True,
                    'charge_id': captured_intent.latest_charge,
                    'amount_captured': final_amount,
                    'additional_charge_id': additional_payment.id
                }

            # Capture with the final amount (less than or equal to authorized amount)
            captured_intent = stripe.PaymentIntent.capture(
                authorization_id,
                amount_to_capture=final_amount
            )

            logger.info(f"✅ Captured ${final_amount/100:.2f} for delivery {delivery_id}: {captured_intent.id}")

            return {
                'success': True,
                'charge_id': captured_intent.latest_charge,
                'amount_captured': final_amount
            }

        except stripe.error.InvalidRequestError as e:
            logger.error(f"Invalid capture request for {authorization_id}: {e}")
            return {
                'success': False,
                'error': 'Payment capture failed. Authorization may be expired.'
            }
        except stripe.error.StripeError as e:
            logger.error(f"Stripe error during capture: {e}")
            return {
                'success': False,
                'error': 'Payment system error during final charge.'
            }
        except Exception as e:
            logger.error(f"Unexpected error during payment capture: {e}")
            return {
                'success': False,
                'error': 'Payment capture failed. Please contact support.'
            }

    @staticmethod
    def get_price_id_for_tier(tier: str) -> Optional[str]:
        """
        Get Stripe price ID for subscription tier

        Args:
            tier: 'basic' or 'premium'

        Returns:
            Stripe price ID string, or None if not configured
        """
        if tier == 'basic':
            return settings.STRIPE_PRICE_BASIC
        elif tier == 'premium':
            return settings.STRIPE_PRICE_PREMIUM
        else:
            logger.error(f"Unknown subscription tier: {tier}")
            return None
