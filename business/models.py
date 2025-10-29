from django.db import models
from django.contrib.auth.models import User

class ZoomMeeting(models.Model):
    zoom_id = models.BigIntegerField()  # Meeting ID returned by Zoom
    topic = models.CharField(max_length=255)
    join_url = models.URLField()
    start_time = models.DateTimeField()
    duration = models.IntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    host_name = models.CharField(max_length=255, null=True, blank=True)
    host_email = models.EmailField(null=True, blank=True)
    linkedin_profile_url = models.URLField(null=True, blank=True)
    linkedin_profile_picture = models.URLField(null=True, blank=True)

    def __str__(self):
        return f"{self.topic} at {self.start_time}"


class Review(models.Model):
    place_id = models.CharField(max_length=80, db_index=True)
    review_id = models.CharField(max_length=80, unique=True)
    author_name = models.CharField(max_length=200)
    rating = models.FloatField()
    time_text = models.CharField(max_length=100)  # e.g. “2 weeks ago”
    text = models.TextField()

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.author_name} ({self.rating})"


# AI Ordering System Models

class UserProfile(models.Model):
    """User profile for AI ordering system"""
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    phone = models.CharField(max_length=20, blank=True)
    first_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True)
    email = models.EmailField(blank=True)
    preferences = models.JSONField(default=dict, blank=True)  # Store AI preferences like favorite cuisines, dietary restrictions
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Profile for {self.user.username}"

    @property
    def has_complete_profile(self):
        """Check if user has minimum required info for AI ordering"""
        has_address = self.user.addresses.filter(is_default=True).exists()
        has_payment = self.user.payment_methods.filter(is_default=True).exists()
        return has_address and has_payment


class DeliveryAddress(models.Model):
    """User delivery addresses for AI ordering"""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='addresses')
    name = models.CharField(max_length=100, help_text="Name like 'Home', 'Work', 'Mom's House'")
    street_address = models.CharField(max_length=255)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=50)
    zip_code = models.CharField(max_length=10)

    # Coordinates for delivery platform APIs
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)

    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-is_default', '-created_at']

    def save(self, *args, **kwargs):
        # Ensure only one default address per user
        if self.is_default:
            DeliveryAddress.objects.filter(user=self.user, is_default=True).update(is_default=False)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name}: {self.street_address}, {self.city}"


class PaymentMethod(models.Model):
    """User payment methods for AI ordering"""
    PAYMENT_TYPES = [
        ('apple_pay', 'Apple Pay'),
        ('google_pay', 'Google Pay'),
        ('stripe_card', 'Credit Card'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='payment_methods')
    type = models.CharField(max_length=20, choices=PAYMENT_TYPES)
    is_default = models.BooleanField(default=False)

    # For Stripe integration (never store actual card numbers!)
    stripe_payment_method_id = models.CharField(max_length=255, blank=True,
                                               help_text="Stripe payment method ID for secure payments")
    last_four = models.CharField(max_length=4, blank=True, help_text="Last 4 digits for display")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-is_default', '-created_at']

    def save(self, *args, **kwargs):
        # Ensure only one default payment method per user
        if self.is_default:
            PaymentMethod.objects.filter(user=self.user, is_default=True).update(is_default=False)
        super().save(*args, **kwargs)

    def __str__(self):
        display_name = self.get_type_display()
        if self.last_four:
            return f"{display_name} (*{self.last_four})"
        return display_name


class AIOrder(models.Model):
    """Track AI-placed orders"""
    STATUS_CHOICES = [
        ('validating', 'Validating User Profile'),
        ('processing', 'Processing Order'),
        ('calling', 'Calling Restaurant'),
        ('confirmed', 'Order Confirmed'),
        ('failed', 'Order Failed'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ai_orders')
    restaurant_name = models.CharField(max_length=255)
    restaurant_place_id = models.CharField(max_length=100)
    dishes = models.JSONField(help_text="List of dish names/IDs")

    # AI decision tracking
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='validating')
    chosen_platform = models.CharField(max_length=50, blank=True)  # 'doordash', 'ubereats', etc.
    platform_order_id = models.CharField(max_length=255, blank=True)

    # User profile snapshot at time of order
    delivery_address = models.JSONField()
    payment_method = models.JSONField()

    # OpenAI Assistants API integration
    assistant_id = models.CharField(max_length=255, blank=True, help_text="OpenAI Assistant ID")
    thread_id = models.CharField(max_length=255, blank=True, help_text="OpenAI Thread ID")
    ai_reasoning = models.TextField(blank=True, help_text="AI's decision-making process")

    total_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    estimated_delivery_time = models.IntegerField(null=True, blank=True, help_text="Minutes")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"AI Order #{self.id} - {self.restaurant_name} ({self.status})"
