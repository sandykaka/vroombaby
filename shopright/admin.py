from django.contrib import admin
from .models import (
    Family, FamilyMember, ShoppingTrip, GroceryItem,
    ShoppingList, ShoppingListItem, AisleLocation
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
