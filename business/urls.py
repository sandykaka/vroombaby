from django.http import HttpResponse
from django.urls import path
from . import views

app_name = 'business'
urlpatterns = [
    path('', views.index, name='index'),
    path('index.html', views.index, name='index'),
    path('products.html',views.products, name='products'),
    path('support.html', views.support_view, name='support'),
    path('privacy.html', views.privacy_view, name='privacy'),
    path('googleeb914ff572b518f7', views.googleeb914ff572b518f7),
    path('create-meeting/', views.create_zoom_meeting, name='create_zoom_meeting'),
    path("get-meetings/", views.get_meetings, name="get_meetings"),
    path('delete-meeting/<int:meeting_id>/', views.delete_meeting, name='delete_meeting'),
    path('update-meeting/<int:meeting_id>/', views.update_meeting, name='update_meeting'),
    path('linkedin-login/', views.linkedin_login, name='linkedin_login'),
    path('linkedin-callback', views.linkedin_callback, name='linkedin_callback'),
    path('get-user-linkedin-details/', views.get_user_linkedin_details, name='get_user_linkedin_details'),
    path('apple-app-site-association', views.apple_app_site_association, name='apple_app_site_association'),
    path('.well-known/apple-app-site-association', views.apple_app_site_association, name='apple_app_site_association'),

    # ===== All API Endpoints (consistent /api/ prefix) =====

    # Restaurant APIs
    path("api/restaurant-recommendations/", views.restaurant_recommendations, name="restaurant_recommendations"),
    path("api/restaurant-contact-info/", views.restaurant_contact_info, name="restaurant_contact_info"),
    path("api/restaurant-menu-structure/", views.restaurant_menu_structure_api, name="restaurant_menu_structure_api"),
    path("api/restaurant-ordering-capability/", views.restaurant_ordering_capability_api, name="restaurant_ordering_capability_api"),

    # AI Ordering APIs
    path("api/ai/order/", views.ai_order_api, name="ai_order_api"),
    path("api/ai/order/<int:order_id>/status/", views.ai_order_status_api, name="ai_order_status_api"),
    path("api/ai/conversation/", views.ai_conversation_api, name="ai_conversation_api"),
    path("api/ai/home-chat/", views.ai_home_chat_api, name="ai_home_chat_api"),

    # User Profile APIs
    path("api/user-profile/", views.user_profile_api, name="user_profile_api"),
    path("api/user-profile/addresses/", views.delivery_addresses_api, name="delivery_addresses_api"),
    path("api/user-profile/addresses/<int:address_id>/", views.delivery_address_detail_api, name="delivery_address_detail_api"),
    path("api/user-profile/payment-methods/", views.payment_methods_api, name="payment_methods_api"),
    path("api/user-profile/payment-methods/<int:payment_id>/", views.payment_method_detail_api, name="payment_method_detail_api"),
    path("api/validate-user-profile/", views.validate_user_profile_api, name="validate_user_profile_api"),

    # Address Validation APIs
    path("api/address-autocomplete/", views.address_autocomplete_api, name="address_autocomplete_api"),
    path("api/address-details/", views.address_details_api, name="address_details_api"),

    # Dish Customization APIs
    path("api/discover-dish-customizations/", views.discover_dish_customizations_api, name="discover_dish_customizations_api"),

    # Order Automation API
    path("api/automate-order/", views.automate_order_api, name="automate_order_api"),

]
