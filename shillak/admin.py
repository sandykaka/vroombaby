from django.contrib import admin

from .models import BankAccount, CashFlowPrediction, Home, HomeMember, PlaidItem, Transaction, TransferRequest, UserProfile


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'display_name', 'created_at')
    search_fields = ('user__username', 'display_name')


@admin.register(Home)
class HomeAdmin(admin.ModelAdmin):
    list_display = ('name', 'invite_code', 'member_count', 'created_at')
    search_fields = ('name', 'invite_code')
    readonly_fields = ('invite_code',)


@admin.register(HomeMember)
class HomeMemberAdmin(admin.ModelAdmin):
    list_display = ('user', 'home', 'role', 'joined_at')
    list_filter = ('role',)


@admin.register(PlaidItem)
class PlaidItemAdmin(admin.ModelAdmin):
    list_display = ('user', 'institution_name', 'item_id', 'created_at')
    search_fields = ('user__username', 'institution_name')


@admin.register(BankAccount)
class BankAccountAdmin(admin.ModelAdmin):
    list_display = (
        'user', 'institution_name', 'account_name',
        'account_type', 'balance', 'last_synced_at',
    )
    list_filter = ('account_type', 'institution_name')
    search_fields = ('user__username', 'institution_name', 'account_name')


@admin.register(TransferRequest)
class TransferRequestAdmin(admin.ModelAdmin):
    list_display = ('from_user', 'to_user', 'method', 'amount', 'status', 'created_at')
    list_filter = ('status', 'method')
    search_fields = ('from_user__username', 'to_user__username')


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ('date', 'name', 'amount', 'merchant_name', 'user')
    list_filter = ('date', 'personal_finance_category')
    search_fields = ('name', 'merchant_name')
    ordering = ('-date',)


@admin.register(CashFlowPrediction)
class CashFlowPredictionAdmin(admin.ModelAdmin):
    list_display = ('home', 'week_start', 'week_end', 'predicted_spend', 'risk_level', 'created_at')
    list_filter = ('risk_level',)
    ordering = ('-created_at',)
