from django.contrib import admin
from django.utils import timezone
from .models import (
    Family, FamilyMember, ShoppingTrip, GroceryItem,
    ShoppingList, ShoppingListItem, AisleLocation,
    ProductRecall, RecallMatch, UserSubscription, UserProfile,
    # Delivery Service Models
    DeliveryZone, Store, Shopper, DeliverySubscription, WeeklyDelivery
)


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'account_type', 'is_approved_shopper', 'shopper_approved_at', 'fcm_token_status')
    list_filter = ('is_approved_shopper', 'account_type')
    search_fields = ('user__username', 'user__first_name', 'user__last_name')
    readonly_fields = ('created_at', 'updated_at', 'shopper_approved_by', 'shopper_approved_at')

    actions = ['approve_as_shopper', 'revoke_shopper_access']

    fieldsets = (
        ('User', {
            'fields': ('user', 'account_type')
        }),
        ('Shopper Approval', {
            'fields': ('is_approved_shopper', 'shopper_approved_by', 'shopper_approved_at')
        }),
        ('Payment & Notifications', {
            'fields': ('stripe_customer_id', 'default_payment_method', 'fcm_token')
        }),
        ('Store Association', {
            'fields': ('store',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at')
        }),
    )

    def fcm_token_status(self, obj):
        return '✅ Set' if obj.fcm_token else '❌ Not set'
    fcm_token_status.short_description = 'FCM Token'

    def approve_as_shopper(self, request, queryset):
        """Approve selected users as shoppers"""
        for profile in queryset:
            profile.is_approved_shopper = True
            profile.shopper_approved_by = request.user
            profile.shopper_approved_at = timezone.now()
            profile.save()

        self.message_user(request, f"{queryset.count()} shopper(s) approved")
    approve_as_shopper.short_description = "Approve as shopper"

    def revoke_shopper_access(self, request, queryset):
        """Revoke shopper access"""
        queryset.update(
            is_approved_shopper=False,
            shopper_approved_by=None,
            shopper_approved_at=None
        )
        self.message_user(request, f"{queryset.count()} shopper(s) revoked")
    revoke_shopper_access.short_description = "Revoke shopper access"


@admin.register(Family)
class FamilyAdmin(admin.ModelAdmin):
    list_display = ('name', 'invite_code', 'member_count', 'created_at')
    search_fields = ('name', 'invite_code')
    readonly_fields = ('created_at', 'updated_at')

    def member_count(self, obj):
        return obj.members.count()
    member_count.short_description = 'Members'


@admin.register(FamilyMember)
class FamilyMemberAdmin(admin.ModelAdmin):
    list_display = ('user', 'family', 'role', 'joined_at')
    list_filter = ('role', 'joined_at')
    search_fields = ('user__username', 'family__name')
    readonly_fields = ('joined_at',)


@admin.register(ShoppingTrip)
class ShoppingTripAdmin(admin.ModelAdmin):
    list_display = ('store_name', 'user', 'trip_date', 'item_count', 'total_amount')
    list_filter = ('store_name', 'trip_date')
    search_fields = ('store_name', 'store_location', 'user__username')
    readonly_fields = ('created_at', 'updated_at')
    date_hierarchy = 'trip_date'

    def item_count(self, obj):
        return len(obj.items)
    item_count.short_description = 'Items'


@admin.register(GroceryItem)
class GroceryItemAdmin(admin.ModelAdmin):
    list_display = ('name', 'brand', 'size', 'category', 'times_purchased', 'has_image', 'image_status', 'first_enriched_by')
    list_filter = ('category', 'image_flagged', 'enriched_from_barcode')
    search_fields = ('name', 'brand', 'barcode')
    readonly_fields = ('created_at', 'updated_at', 'times_purchased', 'first_enriched_at')
    ordering = ('-image_report_count', '-times_purchased')

    # Custom actions for bulk operations
    actions = ['clear_reports', 'flag_images', 'unflag_images']

    def has_image(self, obj):
        return bool(obj.image_url)
    has_image.boolean = True
    has_image.short_description = 'Image'

    def image_status(self, obj):
        if obj.image_flagged:
            return f'🚩 FLAGGED ({obj.image_report_count} reports)'
        elif obj.image_report_count > 0:
            return f'⚠️ {obj.image_report_count} report(s)'
        elif obj.image_url:
            return '✅ OK'
        else:
            return '❌ No image'
    image_status.short_description = 'Image Status'

    def clear_reports(self, request, queryset):
        updated = queryset.update(image_report_count=0, image_flagged=False)
        self.message_user(request, f'Cleared reports for {updated} items')
    clear_reports.short_description = 'Clear reports and unflag'

    def flag_images(self, request, queryset):
        updated = queryset.update(image_flagged=True, image_url='')
        self.message_user(request, f'Flagged and hid images for {updated} items')
    flag_images.short_description = 'Flag and hide images'

    def unflag_images(self, request, queryset):
        updated = queryset.update(image_flagged=False)
        self.message_user(request, f'Unflagged {updated} items')
    unflag_images.short_description = 'Unflag images'

    # Custom list filter for reported items
    class ReportedFilter(admin.SimpleListFilter):
        title = 'Report Status'
        parameter_name = 'report_status'

        def lookups(self, request, model_admin):
            return (
                ('flagged', 'Flagged (3+ reports)'),
                ('reported', 'Has Reports (1-2)'),
                ('clean', 'No Reports'),
            )

        def queryset(self, request, queryset):
            if self.value() == 'flagged':
                return queryset.filter(image_flagged=True)
            if self.value() == 'reported':
                return queryset.filter(image_report_count__gte=1, image_flagged=False)
            if self.value() == 'clean':
                return queryset.filter(image_report_count=0)

    list_filter = ('category', 'image_flagged', 'enriched_from_barcode', ReportedFilter)


@admin.register(ShoppingList)
class ShoppingListAdmin(admin.ModelAdmin):
    list_display = ('family', 'store_name', 'created_by', 'item_count', 'is_active', 'created_at')
    list_filter = ('is_active', 'store_name', 'created_at')
    search_fields = ('family__name', 'store_name', 'created_by__username')
    readonly_fields = ('created_at', 'updated_at')

    def item_count(self, obj):
        return obj.list_items.count()
    item_count.short_description = 'Items'


@admin.register(ShoppingListItem)
class ShoppingListItemAdmin(admin.ModelAdmin):
    list_display = ('name', 'shopping_list', 'brand', 'quantity', 'is_checked', 'last_purchased_date', 'added_by')
    list_filter = ('is_checked', 'added_at', 'last_purchased_date')
    search_fields = ('name', 'brand', 'shopping_list__family__name')
    readonly_fields = ('added_at', 'updated_at', 'purchase_count')


@admin.register(AisleLocation)
class AisleLocationAdmin(admin.ModelAdmin):
    list_display = ('grocery_item', 'store_name', 'aisle_number', 'bay_number', 'confidence_score', 'last_verified')
    list_filter = ('store_name', 'last_verified')
    search_fields = ('grocery_item__name', 'store_name', 'store_location')
    readonly_fields = ('last_verified', 'created_at', 'confidence_score')
    ordering = ('-upvotes', '-last_verified')

    def confidence_score(self, obj):
        return obj.confidence_score
    confidence_score.short_description = 'Confidence'


@admin.register(ProductRecall)
class ProductRecallAdmin(admin.ModelAdmin):
    list_display = ('recall_number', 'source', 'classification', 'product_name_short', 'recalling_firm', 'recall_posted_date', 'status', 'severity_indicator')
    list_filter = ('source', 'classification', 'status', 'recall_posted_date')
    search_fields = ('recall_number', 'product_name', 'recalling_firm', 'reason_for_recall')
    readonly_fields = ('created_at', 'updated_at', 'is_critical', 'severity_level')
    date_hierarchy = 'recall_posted_date'
    ordering = ('-recall_posted_date', 'classification')

    fieldsets = (
        ('Recall Information', {
            'fields': ('source', 'recall_number', 'classification', 'status', 'is_critical', 'severity_level')
        }),
        ('Product Details', {
            'fields': ('product_name', 'product_description', 'recalling_firm', 'upc_codes', 'lot_numbers')
        }),
        ('Hazard & Distribution', {
            'fields': ('reason_for_recall', 'health_hazard_evaluation', 'distribution_pattern', 'stores')
        }),
        ('Remedy & Contact', {
            'fields': ('remedy', 'contact_info')
        }),
        ('Dates', {
            'fields': ('recall_initiation_date', 'recall_posted_date', 'created_at', 'updated_at')
        }),
        ('Raw Data', {
            'fields': ('raw_data',),
            'classes': ('collapse',)
        }),
    )

    def product_name_short(self, obj):
        return obj.product_name[:80] + '...' if len(obj.product_name) > 80 else obj.product_name
    product_name_short.short_description = 'Product Name'

    def severity_indicator(self, obj):
        if obj.classification == 'Class I':
            return '🚨 Critical'
        elif obj.classification == 'Class II':
            return '⚠️ Moderate'
        else:
            return 'ℹ️ Minor'
    severity_indicator.short_description = 'Severity'


@admin.register(RecallMatch)
class RecallMatchAdmin(admin.ModelAdmin):
    list_display = ('user', 'purchased_product_name', 'recall_number', 'classification_indicator', 'confidence_score', 'user_response_display', 'notified', 'matched_at')
    list_filter = ('user_response', 'notification_sent', 'resolved', 'recall__classification', 'recall__source', 'matched_at')
    search_fields = ('user__username', 'purchased_product_name', 'recall__recall_number', 'recall__product_name')
    readonly_fields = ('matched_at', 'notified_at', 'user_response_at', 'resolved_at', 'confidence_score', 'match_reason')
    date_hierarchy = 'matched_at'
    ordering = ('-matched_at', '-recall__classification')

    actions = ['mark_as_notified', 'mark_as_resolved']

    fieldsets = (
        ('Match Information', {
            'fields': ('recall', 'user', 'confidence_score', 'match_reason')
        }),
        ('Purchase Details', {
            'fields': ('purchased_product_name', 'purchased_at_store', 'purchased_date', 'shopping_trip', 'grocery_item')
        }),
        ('User Response', {
            'fields': ('user_response', 'user_response_at', 'user_feedback')
        }),
        ('Notification Status', {
            'fields': ('notification_sent', 'notified_at')
        }),
        ('Resolution', {
            'fields': ('resolved', 'resolved_at')
        }),
        ('Timestamps', {
            'fields': ('matched_at',)
        }),
    )

    def recall_number(self, obj):
        return obj.recall.recall_number
    recall_number.short_description = 'Recall #'

    def classification_indicator(self, obj):
        classification = obj.recall.classification
        if classification == 'Class I':
            return '🚨 Class I'
        elif classification == 'Class II':
            return '⚠️ Class II'
        else:
            return 'ℹ️ Class III'
    classification_indicator.short_description = 'Classification'

    def user_response_display(self, obj):
        if obj.user_response == 'unverified':
            return '⏳ Unverified'
        elif obj.user_response == 'confirmed':
            return '✅ Confirmed'
        elif obj.user_response == 'dismissed':
            return '❌ Dismissed'
        else:
            return '❓ Unsure'
    user_response_display.short_description = 'User Response'

    def notified(self, obj):
        return obj.notification_sent
    notified.boolean = True
    notified.short_description = 'Notified?'

    # Custom actions
    def mark_as_notified(self, request, queryset):
        from django.utils import timezone
        updated = queryset.update(notification_sent=True, notified_at=timezone.now())
        self.message_user(request, f'Marked {updated} matches as notified')
    mark_as_notified.short_description = 'Mark as notified'

    def mark_as_resolved(self, request, queryset):
        from django.utils import timezone
        updated = queryset.update(resolved=True, resolved_at=timezone.now())
        self.message_user(request, f'Marked {updated} matches as resolved')
    mark_as_resolved.short_description = 'Mark as resolved'


@admin.register(UserSubscription)
class UserSubscriptionAdmin(admin.ModelAdmin):
    list_display = ('user', 'is_premium', 'subscription_type', 'daily_nutrition_scans_used', 'nutrition_scans_remaining', 'premium_expires_at')
    list_filter = ('is_premium', 'subscription_type')
    search_fields = ('user__username', 'user__first_name', 'user__last_name')
    readonly_fields = ('created_at', 'updated_at', 'last_nutrition_scan_reset', 'nutrition_scans_remaining', 'is_premium_active')

    fieldsets = (
        ('User', {
            'fields': ('user',)
        }),
        ('Subscription Status', {
            'fields': ('is_premium', 'subscription_type', 'premium_expires_at', 'is_premium_active')
        }),
        ('Daily Quota', {
            'fields': ('daily_nutrition_scans_used', 'last_nutrition_scan_reset', 'nutrition_scans_remaining')
        }),
        ('Apple Receipt Data', {
            'fields': ('apple_receipt_data', 'apple_transaction_id')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at')
        }),
    )

    def nutrition_scans_remaining(self, obj):
        remaining = obj.nutrition_scans_remaining
        if remaining == 999:
            return '∞ (Unlimited)'
        return remaining
    nutrition_scans_remaining.short_description = 'Scans Remaining Today'

    def is_premium_active(self, obj):
        return obj.is_premium_active
    is_premium_active.boolean = True
    is_premium_active.short_description = 'Premium Active'


# ========================================
# DELIVERY SERVICE ADMIN
# ========================================

@admin.register(WeeklyDelivery)
class WeeklyDeliveryAdmin(admin.ModelAdmin):
    list_display = ('id', 'delivery_date', 'customer_name', 'shopper_name', 'status', 'store_name', 'rating_display', 'has_feedback', 'created_at')
    search_fields = ('subscription__customer__username', 'shopper__username')
    readonly_fields = ('created_at', 'updated_at', 'packing_started_at', 'packing_completed_at', 'picked_up_at', 'delivered_at', 'shopper_avg_rating')

    actions = ['reset_to_scheduled', 'mark_as_delivered', 'show_shopper_stats']

    fieldsets = (
        ('Delivery Info', {
            'fields': ('delivery_date', 'subscription', 'shopping_list', 'status')
        }),
        ('Assignment', {
            'fields': ('shopper',)
        }),
        ('Timing', {
            'fields': ('packing_started_at', 'packing_completed_at', 'picked_up_at', 'delivered_at')
        }),
        ('Financial', {
            'fields': ('actual_cost', 'commission_amount')
        }),
        ('Receipt & Feedback', {
            'fields': ('receipt_image', 'shopping_trip', 'customer_rating', 'customer_feedback')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at')
        }),
    )

    def customer_name(self, obj):
        return obj.subscription.customer.username if obj.subscription else 'No customer'
    customer_name.short_description = 'Customer'

    def shopper_name(self, obj):
        return obj.shopper.username if obj.shopper else 'Unassigned'
    shopper_name.short_description = 'Shopper'

    def store_name(self, obj):
        return obj.subscription.store.name if obj.subscription and obj.subscription.store else 'No store'
    store_name.short_description = 'Store'

    def rating_display(self, obj):
        """Show rating as stars or 'Not rated'"""
        if obj.customer_rating:
            stars = '⭐' * obj.customer_rating
            return f'{stars} ({obj.customer_rating}/5)'
        return '—'
    rating_display.short_description = 'Rating'

    def has_feedback(self, obj):
        """Show if customer left text feedback"""
        return bool(obj.customer_feedback and obj.customer_feedback.strip())
    has_feedback.boolean = True
    has_feedback.short_description = 'Feedback?'

    def shopper_avg_rating(self, obj):
        """Show average rating for this delivery's shopper (read-only field in detail view)"""
        if not obj.shopper:
            return 'No shopper assigned'

        from django.db.models import Avg
        avg_rating = WeeklyDelivery.objects.filter(
            shopper=obj.shopper,
            customer_rating__isnull=False
        ).aggregate(Avg('customer_rating'))['customer_rating__avg']

        if avg_rating:
            return f'{avg_rating:.2f} stars ({WeeklyDelivery.objects.filter(shopper=obj.shopper, customer_rating__isnull=False).count()} ratings)'
        return 'No ratings yet'
    shopper_avg_rating.short_description = 'Shopper Avg Rating'

    # Custom list filter for ratings
    class RatingFilter(admin.SimpleListFilter):
        title = 'Rating Status'
        parameter_name = 'rating_status'

        def lookups(self, request, model_admin):
            return (
                ('rated', 'Has Rating'),
                ('unrated', 'No Rating'),
                ('5_star', '5 Stars'),
                ('4_star', '4 Stars'),
                ('3_star', '3 Stars or Below'),
            )

        def queryset(self, request, queryset):
            if self.value() == 'rated':
                return queryset.filter(customer_rating__isnull=False)
            if self.value() == 'unrated':
                return queryset.filter(customer_rating__isnull=True)
            if self.value() == '5_star':
                return queryset.filter(customer_rating=5)
            if self.value() == '4_star':
                return queryset.filter(customer_rating=4)
            if self.value() == '3_star':
                return queryset.filter(customer_rating__lte=3)

    list_filter = ('status', 'delivery_date', 'created_at', RatingFilter)

    def reset_to_scheduled(self, request, queryset):
        """Reset deliveries to scheduled status (unassign from shopper) and notify other shoppers"""
        from .services.notification_service import NotificationService
        from django.contrib.auth.models import User

        reset_count = 0
        for delivery in queryset:
            # Skip already scheduled/cancelled/delivered
            if delivery.status in ['cancelled', 'delivered']:
                continue

            delivery.status = 'scheduled'
            delivery.shopper = None
            delivery.packing_started_at = None
            delivery.packing_completed_at = None
            delivery.packed_by = None
            delivery.picked_up_at = None
            delivery.delivered_at = None
            delivery.save()
            reset_count += 1

            # Notify all approved shoppers about available delivery
            try:
                approved_shoppers = User.objects.filter(
                    profile__account_type='shopper',
                    profile__is_approved_shopper=True
                )

                for shopper in approved_shoppers:
                    NotificationService.send_new_delivery_available(
                        shopper=shopper,
                        delivery=delivery
                    )
            except Exception as e:
                self.message_user(request, f"Warning: Failed to notify shoppers for delivery {delivery.id}: {e}", level='warning')

        self.message_user(request, f"Reset {reset_count} deliveries to 'scheduled' status. Shoppers notified.")
    reset_to_scheduled.short_description = "🔄 Reset to Scheduled (unassign & notify shoppers)"

    def mark_as_delivered(self, request, queryset):
        """Mark deliveries as delivered"""
        queryset.update(status='delivered', delivered_at=timezone.now())
        self.message_user(request, f"Marked {queryset.count()} deliveries as delivered")
    mark_as_delivered.short_description = "✅ Mark as Delivered"


@admin.register(DeliverySubscription)
class DeliverySubscriptionAdmin(admin.ModelAdmin):
    list_display = ('customer', 'store', 'delivery_day', 'delivery_window', 'subscription_tier', 'status', 'created_at')
    list_filter = ('status', 'subscription_tier', 'delivery_day')
    search_fields = ('customer__username', 'store__name')
    readonly_fields = ('created_at', 'updated_at', 'stripe_subscription_id')


@admin.register(Shopper)
class ShopperAdmin(admin.ModelAdmin):
    list_display = ('user', 'full_name', 'phone', 'background_check_status', 'is_active', 'rating', 'total_deliveries')
    list_filter = ('background_check_status', 'is_active')
    search_fields = ('user__username', 'full_name', 'phone')
    readonly_fields = ('created_at', 'total_deliveries')


@admin.register(Store)
class StoreAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'commission_rate', 'is_active', 'created_at')
    list_filter = ('is_active', 'commission_rate')
    search_fields = ('name', 'address', 'owner__username')
    readonly_fields = ('created_at',)


@admin.register(DeliveryZone)
class DeliveryZoneAdmin(admin.ModelAdmin):
    list_display = ('name', 'zip_count', 'is_active', 'created_at')
    list_filter = ('is_active', 'created_at')
    search_fields = ('name', 'zip_codes')
    readonly_fields = ('created_at',)

    fieldsets = (
        ('Zone Information', {
            'fields': ('name', 'is_active')
        }),
        ('ZIP Codes', {
            'fields': ('zip_codes',),
            'description': 'Enter ZIP codes as a JSON array, e.g. ["94102", "94103", "94104"]'
        }),
        ('Metadata', {
            'fields': ('created_at',)
        }),
    )

    def zip_count(self, obj):
        """Show number of ZIP codes in this zone"""
        return len(obj.zip_codes) if obj.zip_codes else 0
    zip_count.short_description = 'ZIP Count'

    def get_form(self, request, obj=None, **kwargs):
        """Customize form to show helpful ZIP code format"""
        form = super().get_form(request, obj, **kwargs)
        if 'zip_codes' in form.base_fields:
            form.base_fields['zip_codes'].help_text = '''
            Enter ZIP codes as JSON array format. Examples:
            • Bay Area: ["94102", "94103", "94104", "94301", "95014"]
            • Single ZIP: ["94102"]
            • Large area: ["94102", "94103", "94104", "94105", "94107", "94108"]
            '''
        return form
