from django.contrib import admin
from .models import ZoomMeeting, Review, UserProfile, DeliveryAddress, PaymentMethod, AIOrder

# Existing models
@admin.register(ZoomMeeting)
class ZoomMeetingAdmin(admin.ModelAdmin):
    list_display = ['topic', 'start_time', 'host_name', 'created_at']
    list_filter = ['start_time', 'created_at']
    search_fields = ['topic', 'host_name', 'host_email']

@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ['author_name', 'rating', 'place_id', 'time_text', 'created_at']
    list_filter = ['rating', 'created_at']
    search_fields = ['author_name', 'place_id', 'text']

# AI Ordering System Admin

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ['user', 'phone', 'has_complete_profile', 'preference_count', 'created_at']
    list_filter = ['created_at']
    search_fields = ['user__username', 'user__email', 'phone', 'first_name', 'last_name']
    readonly_fields = ['has_complete_profile', 'created_at', 'updated_at', 'formatted_preferences']

    fieldsets = (
        ('Basic Info', {
            'fields': ('user', 'first_name', 'last_name', 'email', 'phone')
        }),
        ('AI Learned Preferences (from conversations)', {
            'fields': ('formatted_preferences',),
            'description': 'Preferences automatically extracted from AI chat conversations'
        }),
        ('System Info', {
            'fields': ('has_complete_profile', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        })
    )

    def has_complete_profile(self, obj):
        return obj.has_complete_profile
    has_complete_profile.boolean = True
    has_complete_profile.short_description = 'Complete Profile'

    def preference_count(self, obj):
        """Show how many preferences were learned"""
        return len(obj.preferences) if obj.preferences else 0
    preference_count.short_description = '# Preferences'

    def formatted_preferences(self, obj):
        """Display preferences in readable format"""
        if not obj.preferences:
            return "No preferences learned yet"

        from django.utils.html import format_html
        items = []

        # Group preferences by type for better readability
        dietary = {k: v for k, v in obj.preferences.items() if k.startswith('dietary_')}
        cuisines = {k: v for k, v in obj.preferences.items() if k.startswith('cuisine_')}
        other = {k: v for k, v in obj.preferences.items() if not k.startswith('dietary_') and not k.startswith('cuisine_')}

        if dietary:
            items.append("<strong>🌱 Dietary Restrictions:</strong>")
            for key, value in dietary.items():
                name = key.replace('dietary_', '').replace('_', ' ').title()
                items.append(f"  • {name}: {'Yes' if value else 'No'}")

        if cuisines:
            items.append("<br><strong>🍽️ Cuisine Preferences (mention count):</strong>")
            # Sort by count descending
            sorted_cuisines = sorted(cuisines.items(), key=lambda x: x[1], reverse=True)
            for key, count in sorted_cuisines:
                name = key.replace('cuisine_', '').replace('_count', '').title()
                items.append(f"  • {name}: {count}x")

        if other:
            items.append("<br><strong>⚙️ Other Preferences:</strong>")
            for key, value in other.items():
                name = key.replace('_', ' ').title()
                items.append(f"  • {name}: {value}")

        return format_html("<br>".join(items))
    formatted_preferences.short_description = 'Learned Preferences'

@admin.register(DeliveryAddress)
class DeliveryAddressAdmin(admin.ModelAdmin):
    list_display = ['user', 'name', 'street_address', 'city', 'is_default', 'created_at']
    list_filter = ['is_default', 'city', 'state', 'created_at']
    search_fields = ['user__username', 'name', 'street_address', 'city']
    list_editable = ['is_default']

@admin.register(PaymentMethod)
class PaymentMethodAdmin(admin.ModelAdmin):
    list_display = ['user', 'type', 'last_four', 'is_default', 'created_at']
    list_filter = ['type', 'is_default', 'created_at']
    search_fields = ['user__username', 'last_four']
    list_editable = ['is_default']

@admin.register(AIOrder)
class AIOrderAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'restaurant_name', 'status', 'chosen_platform', 'total_amount', 'created_at']
    list_filter = ['status', 'chosen_platform', 'created_at']
    search_fields = ['user__username', 'restaurant_name', 'platform_order_id']
    readonly_fields = ['created_at', 'updated_at', 'thread_id']

    fieldsets = (
        ('Order Info', {
            'fields': ('user', 'restaurant_name', 'restaurant_place_id', 'dishes')
        }),
        ('AI Processing', {
            'fields': ('status', 'chosen_platform', 'platform_order_id', 'ai_reasoning', 'thread_id')
        }),
        ('User Profile Snapshot', {
            'fields': ('delivery_address', 'payment_method'),
            'classes': ('collapse',)
        }),
        ('Order Details', {
            'fields': ('total_amount', 'estimated_delivery_time')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        })
    )
