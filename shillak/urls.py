from django.urls import path

from . import views

app_name = 'shillak'

urlpatterns = [
    # User profile
    path('api/user-profile/', views.user_profile_api, name='user_profile'),
    path('api/update-fcm-token/', views.update_fcm_token_api, name='update_fcm_token'),

    # Home management
    path('api/home/create/', views.create_home_api, name='create_home'),
    path('api/home/join/', views.join_home_api, name='join_home'),
    path('api/home/info/', views.home_info_api, name='home_info'),
    path('api/home/leave/', views.leave_home_api, name='leave_home'),
    path('api/home/threshold/', views.threshold_api, name='threshold'),
    path('api/home/rename/', views.rename_home_api, name='rename_home'),
    path('api/home/remove-member/', views.remove_member_api, name='remove_member'),

    # Plaid bank linking
    path('api/plaid/create-link-token/', views.create_link_token_api, name='create_link_token'),
    path('api/plaid/exchange-token/', views.exchange_token_api, name='exchange_token'),

    # Bank accounts
    path('api/accounts/', views.accounts_api, name='accounts'),
    path('api/accounts/refresh/', views.refresh_accounts_api, name='refresh_accounts'),
    path('api/accounts/<int:account_id>/unlink/', views.unlink_account_api, name='unlink_account'),
    path('api/accounts/unlink-institution/', views.unlink_institution_api, name='unlink_institution'),

    # Transfers
    path('api/transfer/request/', views.create_transfer_request_api, name='create_transfer'),
    path('api/transfer/history/', views.transfer_history_api, name='transfer_history'),
    path('api/transfer/<int:transfer_id>/complete/', views.complete_transfer_api, name='complete_transfer'),
    path('api/transfer/<int:transfer_id>/cancel/', views.cancel_transfer_api, name='cancel_transfer'),

    # Static pages
    path('privacy/', views.privacy_policy, name='privacy_policy'),
    path('support/', views.support, name='support'),
]
