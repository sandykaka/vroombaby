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
    path('api/account/delete/', views.delete_account_api, name='delete_account'),
    path('api/account/premium/', views.update_premium_api, name='update_premium'),
    path('api/account/check-premium/', views.check_premium_api, name='check_premium'),

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

    # Cash flow
    path('api/cashflow/predictions/', views.cashflow_predictions_api, name='cashflow_predictions'),
    path('api/cashflow/analyze/', views.analyze_cashflow_api, name='analyze_cashflow'),
    path('api/cashflow/rename-bill/', views.rename_bill_api, name='rename_bill'),
    path('api/cashflow/categories/', views.available_categories_api, name='available_categories'),
    path('api/cashflow/spending/', views.monthly_spending_api, name='monthly_spending'),
    path('api/cashflow/category-detail/', views.category_detail_api, name='category_detail'),
    path('api/transactions/', views.transactions_api, name='transactions'),

    # Static pages
    path('privacy/', views.privacy_policy, name='privacy_policy'),
    path('support/', views.support, name='support'),
]
