import random
import string

from django.contrib.auth.models import User
from django.db import models


class UserProfile(models.Model):
    """Extended user profile for Shillak"""
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='shillak_profile'
    )
    display_name = models.CharField(max_length=100, blank=True, default='')
    fcm_token = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.display_name or self.user.username}"


class Home(models.Model):
    """Shared home unit between partners"""
    name = models.CharField(max_length=100, default="My Home")
    invite_code = models.CharField(max_length=6, unique=True, db_index=True)
    low_balance_threshold = models.DecimalField(
        max_digits=10, decimal_places=2, default=100
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def member_count(self):
        return self.members.count()

    @staticmethod
    def generate_invite_code():
        """Generate a unique 6-character alphanumeric invite code."""
        while True:
            code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
            if not Home.objects.filter(invite_code=code).exists():
                return code

    def __str__(self):
        return f"{self.name} ({self.invite_code})"


class HomeMember(models.Model):
    """Links a user to a home with a role."""
    ROLE_CHOICES = [
        ('owner', 'Owner'),
        ('partner', 'Partner'),
    ]

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='shillak_memberships'
    )
    home = models.ForeignKey(
        Home, on_delete=models.CASCADE, related_name='members'
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='partner')
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'home')

    def __str__(self):
        return f"{self.user.username} — {self.home.name} ({self.role})"


class PlaidItem(models.Model):
    """A linked Plaid item (one per institution per user)."""
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='shillak_plaid_items'
    )
    home = models.ForeignKey(
        Home, on_delete=models.CASCADE, related_name='plaid_items'
    )
    item_id = models.CharField(max_length=255, unique=True, db_index=True)
    access_token = models.CharField(max_length=255)
    institution_name = models.CharField(max_length=100, blank=True, default='')
    institution_id = models.CharField(max_length=100, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.institution_name} ({self.user.username})"


class BankAccount(models.Model):
    """Linked bank account populated via Plaid."""
    ACCOUNT_TYPE_CHOICES = [
        ('checking', 'Checking'),
        ('savings', 'Savings'),
        ('credit', 'Credit Card'),
    ]

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='shillak_bank_accounts'
    )
    home = models.ForeignKey(
        Home, on_delete=models.CASCADE, related_name='bank_accounts'
    )
    plaid_item = models.ForeignKey(
        PlaidItem, on_delete=models.CASCADE, related_name='accounts',
        null=True, blank=True,
    )
    plaid_account_id = models.CharField(
        max_length=255, blank=True, null=True, db_index=True
    )
    institution_name = models.CharField(max_length=100)
    account_name = models.CharField(max_length=100, blank=True, default='')
    account_type = models.CharField(
        max_length=20, choices=ACCOUNT_TYPE_CHOICES, default='checking'
    )
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    balance_available = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    currency = models.CharField(max_length=3, default='USD')
    low_balance_threshold = models.DecimalField(
        max_digits=10, decimal_places=2, default=100
    )
    last_synced_at = models.DateTimeField(blank=True, null=True)
    last_alert_balance = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.institution_name} — {self.account_name} ({self.account_type})"


class TransferRequest(models.Model):
    """A request to transfer money between partners."""
    METHOD_CHOICES = [
        ('zelle', 'Zelle'),
        ('venmo', 'Venmo'),
    ]
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]

    from_user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='shillak_transfer_requests_sent'
    )
    to_user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='shillak_transfer_requests_received'
    )
    home = models.ForeignKey(
        Home, on_delete=models.CASCADE, related_name='transfer_requests'
    )
    account = models.ForeignKey(
        BankAccount, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='transfer_requests'
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    method = models.CharField(max_length=20, choices=METHOD_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.from_user.username} → {self.to_user.username} ({self.method}, {self.status})"
