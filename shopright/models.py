from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone


class Family(models.Model):
    """Family group for sharing shopping history"""
    name = models.CharField(max_length=100, blank=True, default="My Family")
    invite_code = models.CharField(max_length=6, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Families"

    def __str__(self):
        return f"{self.name} ({self.invite_code})"


class FamilyMember(models.Model):
    """Link users to families"""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='family_memberships')
    family = models.ForeignKey(Family, on_delete=models.CASCADE, related_name='members')
    role = models.CharField(max_length=20, choices=[
        ('owner', 'Owner'),
        ('member', 'Member')
    ], default='member')
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'family')

    def __str__(self):
        return f"{self.user.username} in {self.family.name}"


class ShoppingTrip(models.Model):
    """A single shopping trip with receipt"""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='shopping_trips')
    family = models.ForeignKey(Family, on_delete=models.CASCADE, related_name='shopping_trips', null=True, blank=True)

    # Store info
    store_name = models.CharField(max_length=200)  # e.g., "Trader Joe's"
    store_location = models.CharField(max_length=200, blank=True)  # e.g., "Cupertino, CA"
    store_lat = models.FloatField(null=True, blank=True)
    store_lng = models.FloatField(null=True, blank=True)

    # Receipt data
    receipt_image = models.ImageField(upload_to='receipts/%Y/%m/', blank=True, null=True)  # Store actual image
    items = models.JSONField(default=list)  # Array of grocery items
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    # Timestamps
    trip_date = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-trip_date']

    def __str__(self):
        return f"{self.store_name} - {self.trip_date.strftime('%Y-%m-%d')} ({len(self.items)} items)"


class GroceryItem(models.Model):
    """Store-specific product database - same product at different stores are separate items"""
    name = models.CharField(max_length=300, db_index=True)  # e.g., "Whole Milk"
    category = models.CharField(max_length=100, blank=True)  # e.g., "Dairy", "Produce"
    brand = models.CharField(max_length=100, blank=True)
    size = models.CharField(max_length=50, blank=True)  # e.g., "64oz", "1lb"
    store_name = models.CharField(max_length=200, db_index=True, default='Unknown Store')  # Which store sells this

    # Product image (from barcode API or user uploaded)
    image_url = models.URLField(blank=True)
    barcode = models.CharField(max_length=50, blank=True, db_index=True)  # UPC code
    image_report_count = models.IntegerField(default=0)  # Number of "wrong image" reports
    image_flagged = models.BooleanField(default=False)  # Auto-flagged after X reports

    # Crowdsourced data
    enriched_from_barcode = models.BooleanField(default=False)  # Was barcode scanned?
    first_enriched_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='first_enrichments'
    )
    first_enriched_at = models.DateTimeField(null=True, blank=True)

    # Metadata
    times_purchased = models.IntegerField(default=0)  # Popularity counter (per store)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-times_purchased', 'name']
        # Each store has its own product catalog
        unique_together = ('name', 'brand', 'size', 'store_name')
        indexes = [
            models.Index(fields=['store_name', 'name']),
            models.Index(fields=['store_name', 'barcode']),
        ]

    def __str__(self):
        return f"{self.name} @ {self.store_name}"


class ShoppingList(models.Model):
    """Shopping list for a user or family (one per store location)"""
    family = models.ForeignKey(Family, on_delete=models.CASCADE, related_name='shopping_lists', null=True, blank=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='personal_shopping_lists', null=True, blank=True)
    store_name = models.CharField(max_length=200)  # Which store chain (e.g., "Trader Joe's")
    store_location = models.CharField(max_length=200, blank=True, default='')  # Specific address (e.g., "123 Main St, SF")

    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='created_lists')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    is_active = models.BooleanField(default=True)  # Can archive completed lists

    class Meta:
        # Ensure one list per store LOCATION per family OR per user
        constraints = [
            models.UniqueConstraint(
                fields=['family', 'store_name', 'store_location'],
                condition=models.Q(family__isnull=False),
                name='unique_family_store_location'
            ),
            models.UniqueConstraint(
                fields=['user', 'store_name', 'store_location'],
                condition=models.Q(user__isnull=False, family__isnull=True),
                name='unique_user_store_location'
            ),
        ]

    def __str__(self):
        store_display = f"{self.store_name} - {self.store_location}" if self.store_location else self.store_name
        if self.family:
            return f"{self.family.name} - {store_display} ({self.list_items.count()} items)"
        else:
            return f"{self.user.username} - {store_display} ({self.list_items.count()} items)"

    @property
    def checked_count(self):
        """Count of checked items"""
        return self.list_items.filter(is_checked=True).count()

    @property
    def total_count(self):
        """Total items in list"""
        return self.list_items.count()


class ShoppingListItem(models.Model):
    """Individual items in a shopping list"""
    shopping_list = models.ForeignKey(ShoppingList, on_delete=models.CASCADE, related_name='list_items')

    # Item details (stored as JSON to match receipt format)
    name = models.CharField(max_length=300)
    brand = models.CharField(max_length=100, blank=True)
    size = models.CharField(max_length=50, blank=True)
    price = models.CharField(max_length=20, blank=True)  # Last known price
    category = models.CharField(max_length=100, blank=True)

    quantity = models.IntegerField(default=1)
    is_checked = models.BooleanField(default=True)  # Default checked = want to buy

    # Link to GLOBAL grocery item (for photos, barcodes, aisle locations)
    grocery_item = models.ForeignKey(
        GroceryItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='list_items'
    )

    # Purchase tracking
    last_purchased_date = models.DateTimeField(null=True, blank=True)  # Last time this item was bought
    purchase_count = models.IntegerField(default=0)  # How many times bought

    added_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    added_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_checked', '-last_purchased_date', 'name']  # Checked items first, then by recency
        unique_together = ('shopping_list', 'name', 'brand', 'size')  # Prevent exact duplicates

    def __str__(self):
        return f"{self.name} (x{self.quantity}) - {'✓' if self.is_checked else '○'}"


class AisleLocation(models.Model):
    """Crowdsourced aisle locations (like Waze for groceries)"""
    LOCATION_TYPE_CHOICES = [
        ('aisle', 'Aisle Number'),       # "Aisle 10", "Aisle 10 Bay 3"
        ('relative', 'Relative'),        # "Behind fruit section", "Next to aisle 10 on right"
        ('category', 'Category-Based'),  # "Dairy Section", "Produce", "Back Wall"
    ]

    store_name = models.CharField(max_length=200, db_index=True)
    store_location = models.CharField(max_length=200)  # "Cupertino, CA"

    grocery_item = models.ForeignKey(GroceryItem, on_delete=models.CASCADE, related_name='aisle_locations')

    # Flexible location system - supports multiple input types
    location_type = models.CharField(max_length=20, choices=LOCATION_TYPE_CHOICES, default='aisle')

    # Structured location (for aisle-based)
    aisle_number = models.CharField(max_length=20, blank=True)
    bay_number = models.CharField(max_length=20, blank=True)

    # Flexible text description (for relative/category-based)
    location_description = models.TextField(blank=True, help_text="e.g., 'Behind fruit section', 'Next to aisle 10 on right'")

    # Crowdsourcing metrics
    upvotes = models.IntegerField(default=0)
    downvotes = models.IntegerField(default=0)

    # Reporting system
    is_flagged = models.BooleanField(default=False)
    flag_count = models.IntegerField(default=0)

    # Tracking
    added_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='locations_added')
    last_verified = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-upvotes', '-last_verified']
        indexes = [
            models.Index(fields=['store_name', 'grocery_item']),
            models.Index(fields=['location_type']),
        ]

    def __str__(self):
        location = self.get_display_location()
        return f"{self.grocery_item.name} → {location} at {self.store_name}"

    def get_display_location(self):
        """Get human-readable location string"""
        if self.location_type == 'aisle':
            if self.bay_number:
                return f"Aisle {self.aisle_number} Bay {self.bay_number}"
            return f"Aisle {self.aisle_number}"
        return self.location_description or "Location not specified"

    @property
    def confidence_score(self):
        """Calculate trust score (upvotes - downvotes)"""
        total_votes = self.upvotes + self.downvotes
        if total_votes == 0:
            return 0
        # Return percentage-based confidence (0-100)
        return int((self.upvotes / total_votes) * 100)

    @property
    def net_score(self):
        """Net voting score (upvotes - downvotes)"""
        return self.upvotes - self.downvotes

    def get_user_vote(self, user):
        """
        Get user's current vote on this location
        Returns: 'up', 'down', or None
        """
        try:
            vote = self.votes.get(user=user)
            return vote.vote_type
        except LocationVote.DoesNotExist:
            return None

    def change_vote(self, user, new_vote_type):
        """
        Change or add user's vote
        new_vote_type: 'up', 'down', or None (to remove vote)
        """
        current_vote = self.get_user_vote(user)

        # Remove old vote counts
        if current_vote == 'up':
            self.upvotes = max(0, self.upvotes - 1)
        elif current_vote == 'down':
            self.downvotes = max(0, self.downvotes - 1)

        # Add new vote counts
        if new_vote_type == 'up':
            self.upvotes += 1
        elif new_vote_type == 'down':
            self.downvotes += 1

        # Update or delete vote record
        if new_vote_type is None:
            # Remove vote
            LocationVote.objects.filter(location=self, user=user).delete()
        else:
            # Update or create vote
            LocationVote.objects.update_or_create(
                location=self,
                user=user,
                defaults={'vote_type': new_vote_type}
            )

        self.save()


class LocationVote(models.Model):
    """Individual vote on a location (allows vote changes)"""
    location = models.ForeignKey(AisleLocation, on_delete=models.CASCADE, related_name='votes')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='location_votes')
    vote_type = models.CharField(max_length=4, choices=[
        ('up', 'Upvote'),
        ('down', 'Downvote')
    ])
    voted_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('location', 'user')  # One vote per user per location
        indexes = [
            models.Index(fields=['location', 'user']),
        ]

    def __str__(self):
        return f"{self.user.username} {self.vote_type}voted {self.location.grocery_item.name}"


class ProductRecall(models.Model):
    """Food and product recalls from FDA, FSIS, and CPSC"""
    SOURCE_CHOICES = [
        ('FDA', 'FDA (Food & Drug Administration)'),
        ('FSIS', 'FSIS (USDA Meat/Poultry)'),
        ('CPSC', 'CPSC (Consumer Product Safety)'),
    ]

    CLASSIFICATION_CHOICES = [
        ('Class I', 'Class I - Serious health hazard'),
        ('Class II', 'Class II - Moderate health risk'),
        ('Class III', 'Class III - Minor violation'),
    ]

    STATUS_CHOICES = [
        ('Active', 'Active'),
        ('Closed', 'Closed'),
        ('Ongoing', 'Ongoing'),
    ]

    # Source information
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, db_index=True)
    recall_number = models.CharField(max_length=50, unique=True, db_index=True)  # "036-2025", "F-0234-2025"

    # Recall details
    classification = models.CharField(max_length=20, choices=CLASSIFICATION_CHOICES, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Active', db_index=True)
    recall_initiation_date = models.DateField()
    recall_posted_date = models.DateField(db_index=True)

    # Product information
    product_name = models.CharField(max_length=500, db_index=True)  # "Saint Coxinha Chicken Croquettes"
    product_description = models.TextField()  # Full detailed description
    recalling_firm = models.CharField(max_length=200, db_index=True)  # Brand/company name

    # Product identifiers (for matching)
    upc_codes = models.JSONField(default=list, blank=True)  # Array of UPC/barcode numbers
    lot_numbers = models.JSONField(default=list, blank=True)  # Lot codes, best by dates, etc.

    # Distribution
    distribution_pattern = models.CharField(max_length=200, blank=True)  # "Nationwide", "CA, NY, TX"
    stores = models.JSONField(default=list, blank=True)  # ["Walmart", "Target"]

    # Hazard information
    reason_for_recall = models.TextField()  # "Undeclared allergen: sesame"
    health_hazard_evaluation = models.TextField(blank=True)  # Detailed health risk assessment

    # Remedy
    remedy = models.TextField(blank=True)  # "Return to store for refund"
    contact_info = models.TextField(blank=True)  # Company contact for questions

    # Raw API data (for debugging)
    raw_data = models.JSONField(default=dict)  # Original API response

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-recall_posted_date', '-classification']
        indexes = [
            models.Index(fields=['source', 'status']),
            models.Index(fields=['recall_posted_date', 'classification']),
            models.Index(fields=['product_name', 'recalling_firm']),
        ]

    def __str__(self):
        return f"{self.recall_number}: {self.product_name} ({self.classification})"

    @property
    def is_critical(self):
        """Is this a Class I recall requiring immediate attention?"""
        return self.classification == 'Class I'

    @property
    def severity_level(self):
        """Return severity as integer for sorting (1=most severe, 3=least)"""
        severity_map = {'Class I': 1, 'Class II': 2, 'Class III': 3}
        return severity_map.get(self.classification, 999)


class RecallMatch(models.Model):
    """Links recalls to user purchases"""
    recall = models.ForeignKey(ProductRecall, on_delete=models.CASCADE, related_name='matches')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='recall_matches')

    # What did we match to?
    shopping_trip = models.ForeignKey(ShoppingTrip, on_delete=models.CASCADE, null=True, blank=True)
    grocery_item = models.ForeignKey(GroceryItem, on_delete=models.SET_NULL, null=True, blank=True)

    # Purchase details for display
    purchased_product_name = models.CharField(max_length=300)
    purchased_at_store = models.CharField(max_length=200)
    purchased_date = models.DateField()

    # Matching metadata
    confidence_score = models.IntegerField()  # 0-100
    match_reason = models.TextField()  # "Exact product name + brand match"
    matched_at = models.DateTimeField(auto_now_add=True)

    # User feedback
    USER_RESPONSE_CHOICES = [
        ('unverified', 'Not verified yet'),
        ('confirmed', 'User confirmed - it is their product'),
        ('dismissed', 'User dismissed - not their product'),
        ('unsure', 'User not sure'),
    ]
    user_response = models.CharField(max_length=20, choices=USER_RESPONSE_CHOICES, default='unverified')
    user_response_at = models.DateTimeField(null=True, blank=True)
    user_feedback = models.TextField(blank=True)  # Why they dismissed it, etc.

    # Notification tracking
    notified_at = models.DateTimeField(null=True, blank=True)
    notification_sent = models.BooleanField(default=False)

    # Resolution tracking
    resolved = models.BooleanField(default=False)  # User handled the recall (returned product, etc.)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-recall__recall_posted_date', '-confidence_score']
        unique_together = ('recall', 'user', 'shopping_trip')  # Prevent duplicate matches
        indexes = [
            models.Index(fields=['user', 'user_response']),
            models.Index(fields=['notification_sent', 'notified_at']),
        ]

    def __str__(self):
        return f"{self.user.username}: {self.recall.product_name} ({self.confidence_score}% confidence)"

    def mark_confirmed(self, feedback=''):
        """User confirmed this is their product"""
        self.user_response = 'confirmed'
        self.user_response_at = timezone.now()
        self.user_feedback = feedback
        self.save()

    def mark_dismissed(self, reason=''):
        """User dismissed this as not their product"""
        self.user_response = 'dismissed'
        self.user_response_at = timezone.now()
        self.user_feedback = reason
        self.resolved = True
        self.resolved_at = timezone.now()
        self.save()

    def mark_resolved(self):
        """User handled the recall (returned product, etc.)"""
        self.resolved = True
        self.resolved_at = timezone.now()
        self.save()

