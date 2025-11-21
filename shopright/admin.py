from django.contrib import admin
from .models import (
    Family, FamilyMember, ShoppingTrip, GroceryItem,
    ShoppingList, ShoppingListItem, AisleLocation,
    ProductRecall, RecallMatch, UserSubscription
)


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
    list_display = ('user', 'is_premium', 'subscription_type', 'daily_nutrition_scan_count', 'daily_nutrition_scan_quota', 'subscription_end_date')
    list_filter = ('is_premium', 'subscription_type')
    search_fields = ('user__username', 'user__first_name', 'user__last_name')
    readonly_fields = ('created_at', 'updated_at', 'last_quota_reset_date')

    fieldsets = (
        ('User', {
            'fields': ('user',)
        }),
        ('Subscription Status', {
            'fields': ('is_premium', 'subscription_type', 'subscription_start_date', 'subscription_end_date')
        }),
        ('Daily Quota', {
            'fields': ('daily_nutrition_scan_count', 'daily_nutrition_scan_quota', 'last_scan_date', 'last_quota_reset_date')
        }),
        ('Receipt Data', {
            'fields': ('receipt_data', 'transaction_id')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at')
        }),
    )

