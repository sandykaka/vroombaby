import json
import logging

from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .decorators import require_firebase_auth
from .models import BankAccount, BillAlias, CashFlowPrediction, Home, HomeMember, PlaidItem, Transaction, TransferRequest, UserProfile
from .services import plaid_service
from .services import format_plaid_category
from .services.notification_service import NotificationService
from .services import cashflow_service

logger = logging.getLogger(__name__)


# ========================================
# USER PROFILE
# ========================================

@csrf_exempt
@require_firebase_auth
def user_profile_api(request):
    """GET: return profile. POST: update profile."""
    profile, created = UserProfile.objects.get_or_create(user=request.user)

    if request.method == 'GET':
        membership = HomeMember.objects.filter(user=request.user).first()
        return JsonResponse({
            'user_id': request.user.id,
            'username': request.user.username,
            'display_name': profile.display_name,
            'home_id': membership.home_id if membership else None,
            'created_at': profile.created_at.isoformat(),
        })

    if request.method == 'POST':
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)

        if 'display_name' in data:
            profile.display_name = data['display_name']
        profile.save()

        return JsonResponse({'status': 'updated', 'display_name': profile.display_name})

    return JsonResponse({'error': 'Method not allowed'}, status=405)


@csrf_exempt
@require_firebase_auth
def update_fcm_token_api(request):
    """Store or update the user's FCM push-notification token."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    token = data.get('fcm_token', '').strip()
    if not token:
        return JsonResponse({'error': 'Missing fcm_token'}, status=400)

    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    profile.fcm_token = token
    profile.save()

    return JsonResponse({'status': 'updated'})


# ========================================
# HOME MANAGEMENT
# ========================================

@csrf_exempt
@require_firebase_auth
def create_home_api(request):
    """Create a new home and add the caller as owner."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    existing = HomeMember.objects.filter(user=request.user).first()
    if existing:
        return JsonResponse(
            {'error': f'You are already in a home: {existing.home.name}'},
            status=400,
        )

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        data = {}

    home_name = data.get('name', "My Home")

    home = Home.objects.create(
        name=home_name,
        invite_code=Home.generate_invite_code(),
    )

    HomeMember.objects.create(
        user=request.user, home=home, role='owner'
    )

    logger.info(f"Home created: {home.name} by {request.user.username}")

    return JsonResponse({
        'home_id': home.id,
        'name': home.name,
        'invite_code': home.invite_code,
    })


@csrf_exempt
@require_firebase_auth
def join_home_api(request):
    """Join an existing home using an invite code."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    invite_code = data.get('invite_code', '').strip().upper()
    if not invite_code:
        return JsonResponse({'error': 'Missing invite_code'}, status=400)

    try:
        home = Home.objects.get(invite_code=invite_code)
    except Home.DoesNotExist:
        return JsonResponse({'error': 'Invalid invite code'}, status=404)

    if HomeMember.objects.filter(user=request.user, home=home).exists():
        return JsonResponse({'error': 'Already a member of this home'}, status=400)

    if HomeMember.objects.filter(user=request.user).exists():
        return JsonResponse(
            {'error': 'You are already in another home. Leave it first.'},
            status=400,
        )

    if home.member_count >= 4:
        return JsonResponse(
            {'error': 'This home already has 4 members.'},
            status=400,
        )

    HomeMember.objects.create(
        user=request.user, home=home, role='partner'
    )

    # Auto-regenerate invite code (one-time use)
    home.invite_code = Home.generate_invite_code()
    home.save()

    logger.info(f"{request.user.username} joined home '{home.name}'")

    return JsonResponse({
        'home_id': home.id,
        'name': home.name,
        'member_count': home.member_count,
    })


@require_firebase_auth
def home_info_api(request):
    """Get the current user's home details and member list."""
    membership = HomeMember.objects.filter(user=request.user).first()

    if not membership:
        return JsonResponse({'home': None})

    home = membership.home
    members = HomeMember.objects.filter(home=home).select_related('user')

    accounts = BankAccount.objects.filter(home=home).values(
        'id', 'institution_name', 'account_name', 'account_type',
        'balance', 'currency', 'user__username',
    )

    return JsonResponse({
        'home': {
            'id': home.id,
            'name': home.name,
            'invite_code': home.invite_code,
            'low_balance_threshold': str(home.low_balance_threshold),
            'member_count': members.count(),
            'members': [
                {
                    'user_id': m.user.id,
                    'username': m.user.username,
                    'display_name': getattr(
                        m.user, 'shillak_profile', None
                    ) and m.user.shillak_profile.display_name or '',
                    'role': m.role,
                    'joined_at': m.joined_at.isoformat(),
                }
                for m in members
            ],
            'accounts': list(accounts),
        }
    })


@csrf_exempt
@require_firebase_auth
def leave_home_api(request):
    """Leave the current home. Owners cannot leave (must transfer first)."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    membership = HomeMember.objects.filter(user=request.user).first()
    if not membership:
        return JsonResponse({'error': 'You are not in a home'}, status=400)

    if membership.role == 'owner':
        return JsonResponse(
            {'error': 'Owners cannot leave. Transfer ownership first.'},
            status=400,
        )

    home_name = membership.home.name
    membership.delete()

    logger.info(f"{request.user.username} left home '{home_name}'")

    return JsonResponse({'status': 'left', 'home_name': home_name})


@csrf_exempt
@require_firebase_auth
def rename_home_api(request):
    """Rename the home."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    name = data.get('name', '').strip()
    if not name:
        return JsonResponse({'error': 'Missing name'}, status=400)

    membership = HomeMember.objects.filter(user=request.user).first()
    if not membership:
        return JsonResponse({'error': 'Not in a home'}, status=400)

    home = membership.home
    home.name = name
    home.save(update_fields=['name'])

    logger.info(f"{request.user.username} renamed home to '{name}'")

    return JsonResponse({'status': 'updated', 'name': home.name})


@csrf_exempt
@require_firebase_auth
def remove_member_api(request):
    """Remove a member from the home. Owner only."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    user_id = data.get('user_id')
    if not user_id:
        return JsonResponse({'error': 'Missing user_id'}, status=400)

    membership = HomeMember.objects.filter(user=request.user).first()
    if not membership:
        return JsonResponse({'error': 'Not in a home'}, status=400)

    if membership.role != 'owner':
        return JsonResponse({'error': 'Only the owner can remove members'}, status=403)

    if int(user_id) == request.user.id:
        return JsonResponse({'error': 'Cannot remove yourself'}, status=400)

    target = HomeMember.objects.filter(user_id=user_id, home=membership.home).first()
    if not target:
        return JsonResponse({'error': 'Member not found'}, status=404)

    target_name = target.user.username
    target.delete()

    logger.info(f"{request.user.username} removed {target_name} from {membership.home.name}")

    return JsonResponse({'status': 'removed'})


# ========================================
# PLAID BANK LINKING
# ========================================

@csrf_exempt
@require_firebase_auth
def create_link_token_api(request):
    """Create a Plaid Link token for the iOS client."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    try:
        link_token = plaid_service.create_link_token(request.user.id)
        return JsonResponse({'link_token': link_token})
    except Exception as e:
        logger.error(f"Failed to create link token: {e}")
        return JsonResponse({'error': 'Could not create link token'}, status=500)


@csrf_exempt
@require_firebase_auth
def exchange_token_api(request):
    """Exchange Plaid public token for access token, fetch and save accounts."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    public_token = data.get('public_token', '').strip()
    institution_id = data.get('institution_id', '')
    institution_name = data.get('institution_name', '')

    if not public_token:
        return JsonResponse({'error': 'Missing public_token'}, status=400)

    # Get user's home
    membership = HomeMember.objects.filter(user=request.user).first()
    if not membership:
        return JsonResponse({'error': 'You must be in a home first'}, status=400)

    home = membership.home

    try:
        # Exchange token
        result = plaid_service.exchange_public_token(public_token)
        access_token = result['access_token']
        item_id = result['item_id']

        # Look up institution name if not provided
        if not institution_name and institution_id:
            institution_name = plaid_service.get_institution_name(institution_id)

        # Create PlaidItem
        plaid_item = PlaidItem.objects.create(
            user=request.user,
            home=home,
            item_id=item_id,
            access_token=access_token,
            institution_name=institution_name,
            institution_id=institution_id,
        )

        # Fetch accounts and balances
        plaid_accounts = plaid_service.get_account_balances(access_token)

        created_accounts = []
        for acct in plaid_accounts:
            # Map Plaid subtype to our account type
            subtype = acct.get('subtype', 'checking')
            if subtype in ('credit card',):
                account_type = 'credit'
            elif subtype in ('savings', 'cd', 'money market'):
                account_type = 'savings'
            else:
                account_type = 'checking'

            bank_account = BankAccount.objects.create(
                user=request.user,
                home=home,
                plaid_item=plaid_item,
                plaid_account_id=acct['plaid_account_id'],
                institution_name=institution_name,
                account_name=acct['name'],
                account_type=account_type,
                balance=acct['balance_current'],
                balance_available=acct.get('balance_available'),
                currency=acct['currency'],
                last_synced_at=timezone.now(),
            )
            created_accounts.append({
                'id': bank_account.id,
                'institution_name': bank_account.institution_name,
                'account_name': bank_account.account_name,
                'account_type': bank_account.account_type,
                'balance': str(bank_account.balance),
                'balance_available': str(bank_account.balance_available) if bank_account.balance_available else None,
                'currency': bank_account.currency,
            })

        logger.info(
            f"Linked {len(created_accounts)} accounts from "
            f"{institution_name} for {request.user.username}"
        )

        return JsonResponse({'accounts': created_accounts})

    except Exception as e:
        logger.error(f"Failed to exchange token: {e}")
        return JsonResponse({'error': 'Could not link account'}, status=500)


@require_firebase_auth
def accounts_api(request):
    """Get all bank accounts for the user's home."""
    membership = HomeMember.objects.filter(user=request.user).first()
    if not membership:
        return JsonResponse({'accounts': []})

    accounts = BankAccount.objects.filter(
        home=membership.home
    ).select_related('user').order_by('institution_name', 'account_name')

    return JsonResponse({
        'accounts': [
            {
                'id': a.id,
                'owner': a.user.username,
                'owner_display_name': getattr(
                    a.user, 'shillak_profile', None
                ) and a.user.shillak_profile.display_name or a.user.username,
                'institution_name': a.institution_name,
                'account_name': a.account_name,
                'account_type': a.account_type,
                'balance': str(a.balance),
                'balance_available': str(a.balance_available) if a.balance_available else None,
                'currency': a.currency,
                'last_synced_at': a.last_synced_at.isoformat() if a.last_synced_at else None,
            }
            for a in accounts
        ]
    })


@csrf_exempt
@require_firebase_auth
def refresh_accounts_api(request):
    """Re-fetch balances from Plaid for all linked items in the home."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    membership = HomeMember.objects.filter(user=request.user).first()
    if not membership:
        return JsonResponse({'error': 'Not in a home'}, status=400)

    plaid_items = PlaidItem.objects.filter(home=membership.home)
    updated_count = 0

    for item in plaid_items:
        try:
            plaid_accounts = plaid_service.get_account_balances(item.access_token)

            for acct in plaid_accounts:
                BankAccount.objects.filter(
                    plaid_account_id=acct['plaid_account_id']
                ).update(
                    balance=acct['balance_current'],
                    balance_available=acct.get('balance_available'),
                    last_synced_at=timezone.now(),
                )
                updated_count += 1

        except Exception as e:
            logger.error(f"Failed to refresh item {item.item_id}: {e}")

    logger.info(f"Refreshed {updated_count} accounts for home {membership.home.name}")

    return JsonResponse({'status': 'refreshed', 'accounts_updated': updated_count})


@csrf_exempt
@require_firebase_auth
def unlink_account_api(request, account_id):
    """Remove a bank account. If it's the last account for a PlaidItem, remove the item too."""
    if request.method != 'DELETE':
        return JsonResponse({'error': 'Only DELETE allowed'}, status=405)

    membership = HomeMember.objects.filter(user=request.user).first()
    if not membership:
        return JsonResponse({'error': 'Not in a home'}, status=400)

    try:
        account = BankAccount.objects.get(id=account_id, home=membership.home)
    except BankAccount.DoesNotExist:
        return JsonResponse({'error': 'Account not found'}, status=404)

    # Only the account owner can remove it
    if account.user != request.user:
        return JsonResponse({'error': 'You can only remove your own accounts'}, status=403)

    plaid_item = account.plaid_item
    institution_name = account.institution_name
    account.delete()

    # If no more accounts for this PlaidItem, remove the item too
    if plaid_item and not plaid_item.accounts.exists():
        plaid_service.remove_item(plaid_item.access_token)
        plaid_item.delete()
        logger.info(f"Removed PlaidItem for {institution_name}")

    logger.info(f"{request.user.username} unlinked account {account_id} ({institution_name})")

    return JsonResponse({'status': 'removed'})


@csrf_exempt
@require_firebase_auth
def unlink_institution_api(request):
    """Remove all accounts for an institution (by plaid_item_id)."""
    if request.method != 'DELETE':
        return JsonResponse({'error': 'Only DELETE allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    institution_name = data.get('institution_name', '').strip()
    if not institution_name:
        return JsonResponse({'error': 'Missing institution_name'}, status=400)

    membership = HomeMember.objects.filter(user=request.user).first()
    if not membership:
        return JsonResponse({'error': 'Not in a home'}, status=400)

    # Find all PlaidItems for this user + institution
    items = PlaidItem.objects.filter(
        user=request.user, home=membership.home, institution_name=institution_name
    )

    if not items.exists():
        return JsonResponse({'error': 'Institution not found'}, status=404)

    removed_count = 0
    for item in items:
        removed_count += item.accounts.count()
        item.accounts.all().delete()
        plaid_service.remove_item(item.access_token)
        item.delete()

    logger.info(f"{request.user.username} unlinked {institution_name} ({removed_count} accounts)")

    return JsonResponse({'status': 'removed', 'accounts_removed': removed_count})


# ========================================
# THRESHOLD SETTINGS
# ========================================

@csrf_exempt
@require_firebase_auth
def threshold_api(request):
    """GET: return current threshold. POST: update threshold."""
    membership = HomeMember.objects.filter(user=request.user).first()
    if not membership:
        return JsonResponse({'error': 'Not in a home'}, status=400)

    home = membership.home

    if request.method == 'GET':
        return JsonResponse({'low_balance_threshold': str(home.low_balance_threshold)})

    if request.method == 'POST':
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)

        threshold = data.get('low_balance_threshold')
        if threshold is None:
            return JsonResponse({'error': 'Missing low_balance_threshold'}, status=400)

        try:
            from decimal import Decimal
            threshold = Decimal(str(threshold))
            if threshold < 0:
                return JsonResponse({'error': 'Threshold must be positive'}, status=400)
        except Exception:
            return JsonResponse({'error': 'Invalid threshold value'}, status=400)

        home.low_balance_threshold = threshold
        home.save(update_fields=['low_balance_threshold'])

        logger.info(f"{request.user.username} set threshold to ${threshold} for {home.name}")

        return JsonResponse({
            'status': 'updated',
            'low_balance_threshold': str(home.low_balance_threshold),
        })

    return JsonResponse({'error': 'Method not allowed'}, status=405)


# ========================================
# TRANSFER REQUESTS
# ========================================

def _serialize_transfer(t):
    """Serialize a TransferRequest to dict."""
    return {
        'id': t.id,
        'from_user': t.from_user.username,
        'from_display_name': getattr(
            t.from_user, 'shillak_profile', None
        ) and t.from_user.shillak_profile.display_name or t.from_user.username,
        'to_user': t.to_user.username,
        'to_display_name': getattr(
            t.to_user, 'shillak_profile', None
        ) and t.to_user.shillak_profile.display_name or t.to_user.username,
        'account_name': t.account.account_name if t.account else None,
        'institution_name': t.account.institution_name if t.account else None,
        'amount': str(t.amount) if t.amount else None,
        'method': t.method,
        'status': t.status,
        'created_at': t.created_at.isoformat(),
    }


@csrf_exempt
@require_firebase_auth
def create_transfer_request_api(request):
    """Create a transfer request and notify the partner."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    account_id = data.get('account_id')
    method = data.get('method', '').strip().lower()
    amount = data.get('amount')

    if method not in ('zelle', 'venmo'):
        return JsonResponse({'error': 'Method must be zelle or venmo'}, status=400)

    membership = HomeMember.objects.filter(user=request.user).first()
    if not membership:
        return JsonResponse({'error': 'Not in a home'}, status=400)

    home = membership.home

    partner_membership = HomeMember.objects.filter(
        home=home
    ).exclude(user=request.user).first()

    if not partner_membership:
        return JsonResponse({'error': 'No partner in your home yet'}, status=400)

    partner = partner_membership.user

    account = None
    if account_id:
        account = BankAccount.objects.filter(id=account_id, home=home).first()

    from decimal import Decimal, InvalidOperation
    parsed_amount = None
    if amount:
        try:
            parsed_amount = Decimal(str(amount))
        except (InvalidOperation, ValueError):
            pass

    transfer = TransferRequest.objects.create(
        from_user=request.user,
        to_user=partner,
        home=home,
        account=account,
        amount=parsed_amount,
        method=method,
    )

    amount_str = f" ${parsed_amount:,.2f}" if parsed_amount else ""
    account_str = f" for {account.account_name}" if account else ""
    NotificationService.send_notification(
        user=partner,
        title="Transfer Request",
        body=f"Your partner requested{amount_str}{account_str} via {method.title()}.",
        data={
            'transfer_id': str(transfer.id),
            'method': method,
            'action': 'open_activity',
        },
        notification_type='transfer_request',
    )

    logger.info(f"Transfer request created: {transfer}")

    return JsonResponse({'transfer': _serialize_transfer(transfer)})


@require_firebase_auth
def transfer_history_api(request):
    """Get transfer history for the user's home."""
    membership = HomeMember.objects.filter(user=request.user).first()
    if not membership:
        return JsonResponse({'transfers': []})

    transfers = TransferRequest.objects.filter(
        home=membership.home
    ).select_related(
        'from_user', 'to_user', 'account'
    ).order_by('-created_at')[:50]

    return JsonResponse({
        'transfers': [_serialize_transfer(t) for t in transfers]
    })


@csrf_exempt
@require_firebase_auth
def complete_transfer_api(request, transfer_id):
    """Mark a transfer request as completed."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    try:
        transfer = TransferRequest.objects.get(id=transfer_id)
    except TransferRequest.DoesNotExist:
        return JsonResponse({'error': 'Transfer not found'}, status=404)

    if transfer.from_user != request.user and transfer.to_user != request.user:
        return JsonResponse({'error': 'Not your transfer'}, status=403)

    transfer.status = 'completed'
    transfer.save(update_fields=['status', 'updated_at'])

    return JsonResponse({'transfer': _serialize_transfer(transfer)})


@csrf_exempt
@require_firebase_auth
def cancel_transfer_api(request, transfer_id):
    """Mark a transfer request as cancelled."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    try:
        transfer = TransferRequest.objects.get(id=transfer_id)
    except TransferRequest.DoesNotExist:
        return JsonResponse({'error': 'Transfer not found'}, status=404)

    if transfer.from_user != request.user and transfer.to_user != request.user:
        return JsonResponse({'error': 'Not your transfer'}, status=403)

    transfer.status = 'cancelled'
    transfer.save(update_fields=['status', 'updated_at'])

    return JsonResponse({'transfer': _serialize_transfer(transfer)})


# ========================================
# STATIC PAGES
# ========================================


# ========================================
# CASH FLOW PREDICTIONS
# ========================================

@require_firebase_auth
def cashflow_predictions_api(request):
    """Get current cash flow predictions for the user's home."""
    membership = HomeMember.objects.filter(user=request.user).first()
    if not membership:
        return JsonResponse({'predictions': []})

    predictions = CashFlowPrediction.objects.filter(home=membership.home)

    # Get bill aliases for display name and category mapping
    alias_objs = BillAlias.objects.filter(home=membership.home)
    aliases = {a.normalized_name: a.display_name for a in alias_objs}
    alias_categories = {a.normalized_name: a.category for a in alias_objs if a.category}
    hidden_groups = {a.normalized_name for a in alias_objs if a.hidden}

    def apply_alias(name):
        return aliases.get(name, name)

    # Build spending categories dynamically (so category edits reflect immediately)
    from datetime import timedelta
    from collections import defaultdict
    thirty_days_ago = timezone.now().date() - timedelta(days=30)

    cat_totals = defaultdict(float)
    recent_txns = Transaction.objects.filter(
        home=membership.home, amount__gt=0, date__gte=thirty_days_ago,
        expense_group__isnull=False, pending=False,
    ).exclude(
        personal_finance_category__in=['INCOME', 'TRANSFER_IN']
    ).values('expense_group', 'amount', 'personal_finance_category')

    for txn in recent_txns:
        group = txn['expense_group']
        if group in alias_categories:
            cat = alias_categories[group]
        else:
            pfc = txn.get('personal_finance_category') or 'OTHER'
            cat = format_plaid_category(pfc)
        cat_totals[cat] += float(txn['amount'])

    live_categories = [
        {'category': c, 'amount': round(a, 2)}
        for c, a in sorted(cat_totals.items(), key=lambda x: -x[1])
        if a > 0
    ]
    live_spend = round(sum(c['amount'] for c in live_categories), 2)

    # Build response with live spending data
    result_predictions = []
    for p in predictions:
        summary = dict(p.monthly_summary)
        if live_categories:
            summary['top_categories'] = live_categories
            summary['avg_monthly_spend'] = live_spend

        result_predictions.append({
            'id': p.id,
            'week_start': str(p.week_start),
            'week_end': str(p.week_end),
            'predicted_spend': str(p.predicted_spend),
            'predicted_income': str(p.predicted_income),
            'estimated_end_balance': str(p.estimated_end_balance),
            'risk_level': p.risk_level,
            'bills_due': [
                {**b, 'name': apply_alias(b['name'])}
                for b in p.bills_due
            ],
            'alerts': p.alerts,
            'monthly_summary': summary,
            'recurring_bills': [
                {
                    **b,
                    'normalized_name': b['name'],
                    'name': apply_alias(b['name']),
                    'category': alias_categories.get(b['name'], b.get('category', '')),
                }
                for b in p.recurring_bills
                if b['name'] not in hidden_groups
            ],
            'income_patterns': p.income_patterns,
        })

    return JsonResponse({'predictions': result_predictions})


@csrf_exempt
@require_firebase_auth
def analyze_cashflow_api(request):
    """Trigger on-demand cash flow analysis."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    membership = HomeMember.objects.filter(user=request.user).first()
    if not membership:
        return JsonResponse({'error': 'Not in a home'}, status=400)

    home = membership.home

    # Sync transactions first
    plaid_items = PlaidItem.objects.filter(home=home)
    for item in plaid_items:
        try:
            cashflow_service.sync_transactions(item)
        except Exception as e:
            logger.error(f"Failed to sync transactions for {item.institution_name}: {e}")

    # Run analysis
    try:
        analysis = cashflow_service.analyze_cashflow(home)
        if analysis:
            return JsonResponse({'status': 'analyzed', 'analysis': analysis})
        else:
            return JsonResponse({'error': 'No transactions to analyze'}, status=400)
    except Exception as e:
        logger.error(f"Cash flow analysis failed: {e}")
        return JsonResponse({'error': 'Analysis failed'}, status=500)


@csrf_exempt
@require_firebase_auth
def rename_bill_api(request):
    """Rename a recurring bill for display purposes."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    normalized_name = data.get('normalized_name', '').strip()
    display_name = data.get('display_name', '').strip()
    category = data.get('category', '').strip()
    hidden = data.get('hidden')

    if not normalized_name:
        return JsonResponse({'error': 'Missing normalized_name'}, status=400)

    # Allow hide without requiring display_name
    if not display_name and hidden is None:
        return JsonResponse({'error': 'Missing display_name'}, status=400)

    membership = HomeMember.objects.filter(user=request.user).first()
    if not membership:
        return JsonResponse({'error': 'Not in a home'}, status=400)

    defaults = {}
    if display_name:
        defaults['display_name'] = display_name
    if category:
        defaults['category'] = category
    if hidden is not None:
        defaults['hidden'] = hidden
        defaults['hidden_at'] = timezone.now() if hidden else None

    alias, created = BillAlias.objects.update_or_create(
        home=membership.home,
        normalized_name=normalized_name,
        defaults=defaults,
    )

    return JsonResponse({
        'status': 'updated',
        'normalized_name': alias.normalized_name,
        'display_name': alias.display_name,
        'category': alias.category,
    })


@require_firebase_auth
def monthly_spending_api(request):
    """Get spending breakdown for a specific month."""
    membership = HomeMember.objects.filter(user=request.user).first()
    if not membership:
        return JsonResponse({'categories': [], 'income': 0, 'spending': 0})

    # Parse month param (default: current month)
    import calendar
    month_str = request.GET.get('month', '')
    if month_str:
        try:
            year, month = int(month_str[:4]), int(month_str[5:7])
        except (ValueError, IndexError):
            year, month = timezone.now().year, timezone.now().month
    else:
        year, month = timezone.now().year, timezone.now().month

    from datetime import date
    first_day = date(year, month, 1)
    last_day = date(year, month, calendar.monthrange(year, month)[1])

    alias_objs = BillAlias.objects.filter(home=membership.home)
    alias_categories = {a.normalized_name: a.category for a in alias_objs if a.category}
    hidden_groups = {a.normalized_name for a in alias_objs if a.hidden}

    from collections import defaultdict
    # Detect internal transfers: opposite transactions across accounts in same Home
    # Same absolute amount, opposite signs, within 3 days, different accounts,
    # at least one side categorized as TRANSFER_IN or TRANSFER_OUT
    from datetime import timedelta as td
    txns_list = list(Transaction.objects.filter(
        home=membership.home,
        date__gte=first_day,
        date__lte=last_day,
        expense_group__isnull=False,
        pending=False,
    ).values('id', 'expense_group', 'amount', 'date', 'bank_account_id', 'personal_finance_category'))

    TRANSFER_CATEGORIES = {'TRANSFER_IN', 'TRANSFER_OUT'}
    internal_transfer_ids = set()

    for i, t1 in enumerate(txns_list):
        if t1['id'] in internal_transfer_ids:
            continue
        for t2 in txns_list[i + 1:]:
            if t2['id'] in internal_transfer_ids:
                continue
            if (t1['bank_account_id'] != t2['bank_account_id']
                    and abs(float(t1['amount']) + float(t2['amount'])) < 0.01
                    and abs((t1['date'] - t2['date']).days) <= 3
                    and (t1.get('personal_finance_category') in TRANSFER_CATEGORIES
                         or t2.get('personal_finance_category') in TRANSFER_CATEGORIES)):
                internal_transfer_ids.add(t1['id'])
                internal_transfer_ids.add(t2['id'])
                break

    cat_totals = defaultdict(float)
    total_income = 0

    for txn in txns_list:
        if txn['id'] in internal_transfer_ids:
            continue
        group = txn['expense_group']
        if group in hidden_groups:
            continue
        pfc = txn.get('personal_finance_category') or 'OTHER'

        if txn['amount'] < 0 or pfc in ('INCOME', 'TRANSFER_IN'):
            total_income += abs(float(txn['amount']))
        elif txn['amount'] > 0:
            if group in alias_categories:
                cat = alias_categories[group]
            else:
                cat = format_plaid_category(pfc)
            cat_totals[cat] += float(txn['amount'])

    categories = [
        {'category': c, 'amount': round(a, 2)}
        for c, a in sorted(cat_totals.items(), key=lambda x: -x[1])
        if a > 0
    ]
    total_spend = round(sum(c['amount'] for c in categories), 2)

    return JsonResponse({
        'month': f"{year}-{month:02d}",
        'income': round(total_income, 2),
        'spending': total_spend,
        'saved': round(total_income - total_spend, 2),
        'categories': categories,
    })


@require_firebase_auth
def available_categories_api(request):
    """Get categories from user's actual Plaid transaction data."""
    membership = HomeMember.objects.filter(user=request.user).first()
    if not membership:
        return JsonResponse({'categories': []})

    raw_cats = Transaction.objects.filter(
        home=membership.home,
        personal_finance_category__isnull=False,
    ).values_list('personal_finance_category', flat=True).distinct()

    categories = []
    for cat in raw_cats:
        if cat and cat not in ('INCOME', 'TRANSFER_IN'):
            display = format_plaid_category(cat)
            categories.append(display)

    return JsonResponse({'categories': sorted(set(categories))})


@require_firebase_auth
def category_detail_api(request):
    """Get individual transactions for a specific category in a month."""
    membership = HomeMember.objects.filter(user=request.user).first()
    if not membership:
        return JsonResponse({'transactions': []})

    import calendar
    month_str = request.GET.get('month', '')
    category_name = request.GET.get('category', '')

    if not category_name:
        return JsonResponse({'transactions': []})

    if month_str:
        try:
            year, month = int(month_str[:4]), int(month_str[5:7])
        except (ValueError, IndexError):
            year, month = timezone.now().year, timezone.now().month
    else:
        year, month = timezone.now().year, timezone.now().month

    from datetime import date
    first_day = date(year, month, 1)
    last_day = date(year, month, calendar.monthrange(year, month)[1])

    alias_objs = BillAlias.objects.filter(home=membership.home)
    alias_categories = {a.normalized_name: a.category for a in alias_objs if a.category}
    aliases = {a.normalized_name: a.display_name for a in alias_objs}

    txns = Transaction.objects.filter(
        home=membership.home,
        date__gte=first_day,
        date__lte=last_day,
        expense_group__isnull=False,
        pending=False,
        amount__gt=0,
    ).order_by('-date')

    result = []
    for t in txns:
        group = t.expense_group
        if group in alias_categories:
            cat = alias_categories[group]
        else:
            pfc = t.personal_finance_category or 'OTHER'
            cat = format_plaid_category(pfc)

        if cat == category_name:
            result.append({
                'date': str(t.date),
                'amount': str(t.amount),
                'name': aliases.get(group, group),
            })

    return JsonResponse({
        'transactions': result,
        'category': category_name,
        'total': str(sum(float(r['amount']) for r in result)),
    })


@require_firebase_auth
def transactions_api(request):
    """Get recent transactions for the user's home."""
    membership = HomeMember.objects.filter(user=request.user).first()
    if not membership:
        return JsonResponse({'transactions': []})

    transactions = Transaction.objects.filter(
        home=membership.home
    ).order_by('-date')[:100]

    return JsonResponse({
        'transactions': [
            {
                'id': t.id,
                'date': str(t.date),
                'amount': str(t.amount),
                'name': t.name,
                'merchant_name': t.merchant_name,
                'category': t.category,
                'personal_finance_category': t.personal_finance_category,
                'pending': t.pending,
            }
            for t in transactions
        ]
    })


# ========================================
# STATIC PAGES (keep at bottom)
# ========================================

def privacy_policy(request):
    return render(request, 'shillak/privacy.html')


def support(request):
    return render(request, 'shillak/support.html')
