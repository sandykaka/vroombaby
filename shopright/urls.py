from django.urls import path
from . import views
from . import views_delivery

app_name = 'shopright'

urlpatterns = [
    # Receipt Scanning (2-step flow)
    path('api/preview-receipt/', views.preview_receipt_api, name='preview_receipt'),  # Step 1: Parse only
    path('api/save-receipt/', views.save_receipt_api, name='save_receipt'),  # Step 2: Save parsed data

    # Shopping History
    path('api/shopping-history/', views.shopping_history_api, name='shopping_history'),
    path('api/trip/<int:trip_id>/', views.trip_detail_api, name='trip_detail'),

    # Family Management
    path('api/family/create/', views.create_family_api, name='create_family'),
    path('api/family/join/', views.join_family_api, name='join_family'),
    path('api/family/info/', views.family_info_api, name='family_info'),
    path('api/family/leave/', views.leave_family_api, name='leave_family'),
    path('api/family/regenerate-code/', views.regenerate_invite_code_api, name='regenerate_invite_code'),
    path('api/family/remove-member/', views.remove_family_member_api, name='remove_family_member'),
    path('api/family/transfer-ownership/', views.transfer_ownership_api, name='transfer_ownership'),

    # Shopping Lists
    path('api/shopping-lists/', views.shopping_lists_api, name='shopping_lists'),
    path('api/shopping-list/<int:list_id>/', views.shopping_list_detail_api, name='shopping_list_detail'),
    path('api/shopping-list-item/<int:item_id>/', views.delete_list_item_api, name='delete_list_item'),
    path('api/search-items/', views.search_grocery_items_api, name='search_items'),  # Autocomplete for adding items

    # Barcode Scanning
    path('api/scan-barcode/', views.scan_barcode_api, name='scan_barcode'),
    path('api/confirm-barcode/', views.confirm_barcode_api, name='confirm_barcode'),  # User confirms mismatch
    path('api/lookup-barcode/', views.lookup_barcode_api, name='lookup_barcode'),  # Standalone nutrition lookup
    path('api/upload-product-photo/', views.upload_product_photo_api, name='upload_product_photo'),
    path('api/report-wrong-image/', views.report_wrong_image_api, name='report_wrong_image'),
    path('api/flagged-images/', views.flagged_images_api, name='flagged_images'),

    # Subscription & Payments
    path('api/verify-subscription/', views.verify_subscription_api, name='verify_subscription'),
    path('api/subscription-status/', views.get_subscription_status_api, name='subscription_status'),

    # Aisle Location Tracking
    path('api/location/add/', views.add_location_api, name='add_location'),
    path('api/location/update/<int:location_id>/', views.update_location_api, name='update_location'),
    path('api/location/vote/', views.vote_location_api, name='vote_location'),
    path('api/location/<int:grocery_item_id>/', views.get_location_api, name='get_location'),
    path('api/location/<int:grocery_item_id>/all/', views.get_all_locations_api, name='get_all_locations'),
    path('api/location/report/', views.report_wrong_location_api, name='report_wrong_location'),

    # Recall Alert System
    path('api/recalls/matches/', views.recall_matches_api, name='recall_matches'),
    path('api/recalls/<int:recall_id>/detail/', views.recall_detail_api, name='recall_detail'),
    path('api/recalls/match/<int:match_id>/confirm/', views.confirm_recall_match_api, name='confirm_recall_match'),
    path('api/recalls/match/<int:match_id>/dismiss/', views.dismiss_recall_match_api, name='dismiss_recall_match'),
    path('api/recalls/match/<int:match_id>/mark-notified/', views.mark_recall_notified_api, name='mark_recall_notified'),

    # Budget/Spending Analytics
    path('api/spending/monthly/', views.monthly_spending_api, name='monthly_spending'),
    path('api/spending/trend/', views.spending_trend_api, name='spending_trend'),

    # Price Comparison (Barcode-First Matching)
    path('api/price-comparison/batch/', views.batch_price_comparison_api, name='batch_price_comparison'),

    # Account Management
    path('api/user-profile/', views.user_profile_api, name='user_profile'),
    path('api/account/set-type/', views_delivery.set_account_type, name='set_account_type'),
    path('api/account/delete/', views.delete_account_api, name='delete_account'),

    # Legal Pages (required for App Store)
    path('terms/', views.terms_of_service, name='terms_of_service'),
    path('privacy/', views.privacy_policy, name='privacy_policy'),
    path('support/', views.support, name='support'),

    # ========================================
    # DELIVERY SERVICE APIs (Phase 1 MVP)
    # ========================================

    # Stripe Configuration (public endpoint)
    path('api/delivery/stripe-config/', views_delivery.stripe_config, name='stripe_config'),
    path('api/delivery/service-areas/', views_delivery.get_service_areas, name='get_service_areas'),
    path('api/delivery/validate-address/', views_delivery.validate_address_endpoint, name='validate_address_endpoint'),

    # Customer APIs
    path('api/delivery/create-setup-intent/', views_delivery.create_setup_intent, name='create_setup_intent'),
    path('api/delivery/attach-payment-method/', views_delivery.attach_payment_method, name='attach_payment_method'),
    path('api/delivery/setup-subscription/', views_delivery.setup_subscription, name='setup_subscription'),
    path('api/delivery/subscribe/', views_delivery.subscribe_delivery, name='subscribe_delivery'),
    path('api/delivery/my-subscriptions/', views_delivery.my_subscriptions, name='my_subscriptions'),
    path('api/delivery/billing-history/', views_delivery.billing_history, name='billing_history'),
    path('api/delivery/delivery-history/', views_delivery.delivery_history, name='delivery_history'),
    path('api/deliveries/rate/', views.submit_delivery_rating_api, name='submit_delivery_rating'),
    path('api/delivery/cancel/', views_delivery.cancel_subscription, name='cancel_subscription'),
    path('api/delivery/remove-delivery/', views_delivery.remove_delivery, name='remove_delivery'),
    path('api/delivery/cancel-subscription-completely/', views_delivery.cancel_subscription_completely, name='cancel_subscription_completely'),
    path('api/delivery/modify/', views_delivery.modify_subscription, name='modify_subscription'),
    path('api/delivery/upgrade-tier/', views_delivery.upgrade_subscription_tier, name='upgrade_subscription_tier'),
    path('api/delivery/add-delivery/', views_delivery.add_delivery, name='add_delivery'),

    # Push Notifications
    path('api/update-fcm-token/', views_delivery.update_fcm_token, name='update_fcm_token'),

    # Store APIs
    path('api/store/deliveries/', views_delivery.store_deliveries, name='store_deliveries'),

    # Shopper APIs
    path('api/shopper/available-deliveries/', views.get_available_deliveries_api, name='get_available_deliveries'),
    path('api/shopper/assign-delivery/', views.assign_delivery_api, name='assign_delivery'),
    path('api/shopper/deny-delivery/', views.deny_delivery_api, name='deny_delivery'),
    path('api/shopper/respond-to-delivery/', views_delivery.shopper_respond_to_delivery, name='shopper_respond_to_delivery'),
    path('api/shopper/release-delivery/', views_delivery.shopper_release_delivery, name='shopper_release_delivery'),
    path('api/shopper/my-deliveries/', views.my_deliveries_api, name='my_deliveries'),
    path('api/shopper/my-past-deliveries/', views.my_past_deliveries_api, name='my_past_deliveries'),
    path('api/shopper/start-packing/', views.start_packing_api, name='start_packing'),
    path('api/shopper/mark-ready/', views.mark_ready_api, name='mark_ready'),
    path('api/shopper/start-delivery/', views.start_delivery_api, name='start_delivery'),
    path('api/shopper/mark-delivered/', views.mark_delivered_api, name='mark_delivered'),
    path('api/shopper/route/', views_delivery.shopper_route, name='shopper_route'),

    # Stripe webhook (no auth required - verified by signature)
    path('api/delivery/stripe-webhook/', views_delivery.stripe_webhook, name='stripe_webhook'),
]

