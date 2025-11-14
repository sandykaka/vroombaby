from django.urls import path
from . import views

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

    # Shopping Lists
    path('api/shopping-lists/', views.shopping_lists_api, name='shopping_lists'),
    path('api/shopping-list/<int:list_id>/', views.shopping_list_detail_api, name='shopping_list_detail'),
    path('api/shopping-list-item/<int:item_id>/', views.delete_list_item_api, name='delete_list_item'),
    path('api/search-items/', views.search_grocery_items_api, name='search_items'),  # Autocomplete for adding items

    # Barcode Scanning
    path('api/scan-barcode/', views.scan_barcode_api, name='scan_barcode'),
    path('api/lookup-barcode/', views.lookup_barcode_api, name='lookup_barcode'),  # Standalone nutrition lookup
    path('api/upload-product-photo/', views.upload_product_photo_api, name='upload_product_photo'),
    path('api/report-wrong-image/', views.report_wrong_image_api, name='report_wrong_image'),
    path('api/flagged-images/', views.flagged_images_api, name='flagged_images'),

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
]

