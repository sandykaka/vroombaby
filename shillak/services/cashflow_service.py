import json
import logging
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone
from openai import OpenAI

from shillak.models import (
    BankAccount, CashFlowPrediction, Home, HomeMember, PlaidItem, Transaction,
)
from shillak.services import plaid_service
from shillak.services.notification_service import NotificationService

logger = logging.getLogger(__name__)


def sync_transactions(plaid_item):
    """Fetch 6 months of transactions from Plaid and cache in DB."""
    transactions = plaid_service.get_transactions(plaid_item.access_token, months=6)

    created = 0
    for txn in transactions:
        # Find the matching BankAccount
        bank_account = BankAccount.objects.filter(
            plaid_account_id=txn['account_id']
        ).first()

        _, was_created = Transaction.objects.update_or_create(
            plaid_transaction_id=txn['transaction_id'],
            defaults={
                'user': plaid_item.user,
                'home': plaid_item.home,
                'bank_account': bank_account,
                'date': txn['date'],
                'amount': txn['amount'],
                'name': txn['name'],
                'merchant_name': txn.get('merchant_name'),
                'category': txn.get('category'),
                'personal_finance_category': txn.get('personal_finance_category'),
                'pending': txn.get('pending', False),
            },
        )
        if was_created:
            created += 1

    logger.info(
        f"Synced {len(transactions)} transactions for {plaid_item.institution_name} "
        f"({created} new)"
    )
    return len(transactions)


def analyze_cashflow(home, dry_run=False):
    """Analyze transaction history with OpenAI and generate predictions."""
    # Gather all transactions for the home
    transactions = Transaction.objects.filter(
        home=home, pending=False
    ).order_by('date').values(
        'date', 'amount', 'name', 'merchant_name',
        'category', 'personal_finance_category',
    )

    if not transactions:
        logger.warning(f"No transactions found for {home.name}")
        return None

    # Get current balances
    accounts = BankAccount.objects.filter(home=home).values(
        'account_name', 'institution_name', 'account_type',
        'balance', 'balance_available',
    )

    # Prepare data for AI
    txn_data = []
    for t in transactions:
        txn_data.append({
            'date': str(t['date']),
            'amount': float(t['amount']),
            'name': t['name'],
            'merchant': t['merchant_name'] or '',
            'category': t['category'] or [],
            'pfc': t['personal_finance_category'] or '',
        })

    balance_data = []
    for a in accounts:
        balance_data.append({
            'account': a['account_name'],
            'institution': a['institution_name'],
            'type': a['account_type'],
            'balance': float(a['balance']),
            'available': float(a['balance_available']) if a['balance_available'] else None,
        })

    today = date.today()

    prompt = f"""You are a personal finance analyst. Analyze 6 months of bank transaction
history and predict the user's cash flow for the next 4 weeks.

Note: In Plaid data, NEGATIVE amounts are income (money coming in) and
POSITIVE amounts are expenses (money going out).

Today's date: {today}

Transaction history ({len(txn_data)} transactions):
{json.dumps(txn_data, indent=None)}

Current account balances:
{json.dumps(balance_data, indent=None)}

Analyze the data and return ONLY valid JSON (no markdown, no explanation):
{{
  "recurring_bills": [
    {{"name": "Rent", "amount": 2000, "typical_day": 1, "frequency": "monthly", "merchant": "LANDLORD LLC"}}
  ],
  "income_patterns": [
    {{"source": "Employer Direct Deposit", "amount": 3500, "frequency": "biweekly", "typical_days": [15, 30]}}
  ],
  "weekly_predictions": [
    {{
      "week_start": "YYYY-MM-DD",
      "week_end": "YYYY-MM-DD",
      "predicted_spend": 800,
      "predicted_income": 0,
      "bills_due": [{{"name": "Netflix", "amount": 15}}],
      "estimated_end_balance": 2700,
      "risk_level": "low"
    }}
  ],
  "alerts": [
    "Description of any weeks where balance may be insufficient"
  ],
  "monthly_summary": {{
    "avg_monthly_income": 7000,
    "avg_monthly_spend": 5500,
    "top_categories": [{{"category": "Rent", "amount": 2000}}]
  }}
}}

Provide exactly 4 weekly predictions starting from the Monday of the current week.
risk_level should be: "low" (comfortable), "medium" (tight), "high" (likely insufficient).
"""

    if dry_run:
        logger.info(f"[DRY RUN] Would analyze {len(txn_data)} transactions for {home.name}")
        return None

    # Call OpenAI
    client = OpenAI(api_key=settings.OPENAI_API_KEY)

    try:
        response = client.chat.completions.create(
            model='gpt-4o-mini',
            messages=[
                {'role': 'system', 'content': 'You are a financial analyst. Return only valid JSON.'},
                {'role': 'user', 'content': prompt},
            ],
            temperature=0.2,
            response_format={'type': 'json_object'},
        )

        raw = response.choices[0].message.content
        analysis = json.loads(raw)

        logger.info(f"AI analysis complete for {home.name}")

    except Exception as e:
        logger.error(f"OpenAI analysis failed for {home.name}: {e}")
        return None

    # Save predictions
    # Remember old alert state so we don't re-alert if balance unchanged
    old_predictions = {
        (str(p.week_start), str(p.week_end)): p
        for p in CashFlowPrediction.objects.filter(home=home)
    }

    # Get current total balance for comparison
    total_balance = sum(
        float(a['balance']) for a in accounts
    )

    CashFlowPrediction.objects.filter(home=home).delete()

    predictions = []
    for week in analysis.get('weekly_predictions', []):
        week_key = (week['week_start'], week['week_end'])
        old = old_predictions.get(week_key)

        # Carry over alert state if balance hasn't changed significantly (>10%)
        already_alerted = False
        old_balance = None
        if old and old.alerted and old.alerted_at_balance is not None:
            balance_change = abs(total_balance - float(old.alerted_at_balance))
            if balance_change < float(old.alerted_at_balance) * 0.1:
                already_alerted = True
                old_balance = old.alerted_at_balance

        prediction = CashFlowPrediction.objects.create(
            home=home,
            week_start=week['week_start'],
            week_end=week['week_end'],
            predicted_spend=Decimal(str(week.get('predicted_spend', 0))),
            predicted_income=Decimal(str(week.get('predicted_income', 0))),
            estimated_end_balance=Decimal(str(week.get('estimated_end_balance', 0))),
            risk_level=week.get('risk_level', 'low'),
            bills_due=week.get('bills_due', []),
            recurring_bills=analysis.get('recurring_bills', []),
            income_patterns=analysis.get('income_patterns', []),
            alerts=analysis.get('alerts', []),
            monthly_summary=analysis.get('monthly_summary', {}),
            ai_analysis=raw,
            alerted=already_alerted,
            alerted_at_balance=old_balance,
        )
        predictions.append(prediction)

    # Send alerts for high-risk weeks
    high_risk = [p for p in predictions if p.risk_level in ('high', 'medium')]
    if high_risk:
        members = [
            m.user for m in
            HomeMember.objects.filter(home=home).select_related('user')
        ]

        for prediction in high_risk:
            if prediction.risk_level == 'high':
                bills_str = ', '.join(
                    f"{b['name']} ${b['amount']}" for b in prediction.bills_due
                )
                for user in members:
                    NotificationService.send_notification(
                        user=user,
                        title='Cash Flow Alert',
                        body=f"Upcoming bills ({bills_str}) may exceed your balance. "
                             f"Estimated end balance: ${prediction.estimated_end_balance:,.2f}",
                        data={
                            'week_start': str(prediction.week_start),
                            'risk_level': prediction.risk_level,
                            'action': 'open_dashboard',
                        },
                        notification_type='cashflow_alert',
                    )

    logger.info(
        f"Saved {len(predictions)} predictions for {home.name}, "
        f"{len(high_risk)} high/medium risk"
    )

    return analysis
