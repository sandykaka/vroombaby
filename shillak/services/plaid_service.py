import json
import logging
from datetime import date, timedelta

import plaid
from plaid.api import plaid_api
from plaid.model.country_code import CountryCode
from plaid.model.products import Products
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest
from plaid.model.institutions_get_by_id_request import InstitutionsGetByIdRequest
from plaid.model.item_remove_request import ItemRemoveRequest
from plaid.model.transactions_get_request import TransactionsGetRequest
from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions

from django.conf import settings

from shillak.services.notification_service import NotificationService

logger = logging.getLogger(__name__)

_client = None


def get_plaid_client():
    """Get or create the Plaid API client singleton."""
    global _client
    if _client is not None:
        return _client

    env_map = {
        'sandbox': plaid.Environment.Sandbox,
        'development': plaid.Environment.Sandbox,
        'production': plaid.Environment.Production,
    }

    configuration = plaid.Configuration(
        host=env_map.get(settings.PLAID_ENV, plaid.Environment.Sandbox),
        api_key={
            'clientId': settings.PLAID_CLIENT_ID,
            'secret': settings.PLAID_SECRET,
        },
    )

    api_client = plaid.ApiClient(configuration)
    _client = plaid_api.PlaidApi(api_client)
    return _client


def create_link_token(user_id, access_token=None):
    """Create a Plaid Link token for the iOS app.

    Pass access_token to open Link in update mode, which re-authenticates an
    existing item instead of linking a new one.
    """
    client = get_plaid_client()

    params = dict(
        user=LinkTokenCreateRequestUser(client_user_id=str(user_id)),
        client_name='Shillak',
        language='en',
        country_codes=[CountryCode('US')],
    )
    if access_token:
        # Update mode: products must be omitted
        params['access_token'] = access_token
    else:
        params['products'] = [Products('auth'), Products('transactions')]

    response = client.link_token_create(LinkTokenCreateRequest(**params))
    mode = 'update' if access_token else 'new'
    logger.info(f"Link token created for user {user_id} ({mode})")
    return response.link_token


def is_login_required(error):
    """True if a Plaid error means the user must re-authenticate via Link."""
    if not isinstance(error, plaid.ApiException):
        return False
    try:
        body = json.loads(error.body)
    except (TypeError, ValueError):
        return False
    return body.get('error_code') == 'ITEM_LOGIN_REQUIRED'


def flag_reauth_if_needed(plaid_item, error):
    """Mark an item as needing re-auth and notify its owner once.

    Returns True if the error was a login-required error.
    """
    if not is_login_required(error):
        return False

    if not plaid_item.needs_reauth:
        plaid_item.needs_reauth = True
        plaid_item.save(update_fields=['needs_reauth'])
        logger.warning(f"Item {plaid_item.item_id} ({plaid_item.institution_name}) needs re-auth")
        NotificationService.send_reauth_alert(plaid_item)

    return True


def clear_reauth(plaid_item):
    """Clear the re-auth flag after a successful Plaid call."""
    if plaid_item.needs_reauth:
        plaid_item.needs_reauth = False
        plaid_item.save(update_fields=['needs_reauth'])
        logger.info(f"Item {plaid_item.item_id} ({plaid_item.institution_name}) reconnected")


def exchange_public_token(public_token):
    """Exchange a public token for an access token and item ID."""
    client = get_plaid_client()

    request = ItemPublicTokenExchangeRequest(public_token=public_token)
    response = client.item_public_token_exchange(request)

    logger.info(f"Public token exchanged, item_id={response.item_id}")
    return {
        'access_token': response.access_token,
        'item_id': response.item_id,
    }


def get_account_balances(access_token):
    """Fetch account balances for a given access token."""
    client = get_plaid_client()

    request = AccountsBalanceGetRequest(access_token=access_token)
    response = client.accounts_balance_get(request)

    accounts = []
    for acct in response.accounts:
        accounts.append({
            'plaid_account_id': acct.account_id,
            'name': acct.name,
            'official_name': acct.official_name,
            'type': acct.type.value if acct.type else 'depository',
            'subtype': acct.subtype.value if acct.subtype else 'checking',
            'balance_current': float(acct.balances.current) if acct.balances.current is not None else 0,
            'balance_available': float(acct.balances.available) if acct.balances.available is not None else None,
            'currency': acct.balances.iso_currency_code or 'USD',
        })

    return accounts


def get_institution_name(institution_id):
    """Look up institution name by ID."""
    client = get_plaid_client()

    try:
        request = InstitutionsGetByIdRequest(
            institution_id=institution_id,
            country_codes=[CountryCode('US')],
        )
        response = client.institutions_get_by_id(request)
        return response.institution.name
    except Exception as e:
        logger.warning(f"Could not fetch institution name for {institution_id}: {e}")
        return institution_id


def remove_item(access_token):
    """Revoke a Plaid access token (unlink an institution)."""
    client = get_plaid_client()

    try:
        request = ItemRemoveRequest(access_token=access_token)
        client.item_remove(request)
        logger.info("Plaid item removed")
    except Exception as e:
        logger.warning(f"Could not remove Plaid item: {e}")


def get_transactions(access_token, months=6):
    """Fetch transaction history from Plaid.

    Args:
        access_token: Plaid access token for the linked institution.
        months: Number of months of history to fetch (default 6).

    Returns:
        List of transaction dicts with raw + categorized data.
    """
    client = get_plaid_client()

    end_date = date.today()
    start_date = end_date - timedelta(days=months * 30)

    all_transactions = []
    offset = 0
    page_size = 100

    while True:
        request = TransactionsGetRequest(
            access_token=access_token,
            start_date=start_date,
            end_date=end_date,
            options=TransactionsGetRequestOptions(
                count=page_size,
                offset=offset,
            ),
        )

        response = client.transactions_get(request)

        for txn in response.transactions:
            # Get personal finance category if available
            pfc = None
            if hasattr(txn, 'personal_finance_category') and txn.personal_finance_category:
                pfc = txn.personal_finance_category.primary

            all_transactions.append({
                'transaction_id': txn.transaction_id,
                'account_id': txn.account_id,
                'date': str(txn.date),
                'amount': float(txn.amount),
                'name': txn.name,
                'merchant_name': txn.merchant_name,
                'category': txn.category,
                'personal_finance_category': pfc,
                'pending': txn.pending,
                'payment_channel': txn.payment_channel,
                'iso_currency_code': txn.iso_currency_code,
            })

        total = response.total_transactions
        offset += page_size

        if offset >= total:
            break

    logger.info(f"Fetched {len(all_transactions)} transactions ({months} months)")
    return all_transactions
