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
        })

    # Pre-analyze: group expenses and income by merchant
    from collections import defaultdict
    expense_groups = defaultdict(list)
    income_groups = defaultdict(list)
    for t in txn_data:
        key = t['name'][:40]
        if t['amount'] > 0:
            expense_groups[key].append({'date': t['date'], 'amount': t['amount']})
        elif t['amount'] < 0:
            income_groups[key].append({'date': t['date'], 'amount': abs(t['amount'])})

    # Build expense summary
    expense_summary = []
    total_monthly_expenses = 0
    for name, occurrences in sorted(expense_groups.items(), key=lambda x: -sum(o['amount'] for o in x[1])):
        avg = sum(o['amount'] for o in occurrences) / len(occurrences)
        days = [int(d.split('-')[2]) for d in [o['date'] for o in occurrences]]
        total_monthly_expenses += avg
        expense_summary.append({
            'name': name,
            'occurrences': len(occurrences),
            'avg_amount': round(avg, 2),
            'typical_day_of_month': round(sum(days) / len(days)),
            'dates': [o['date'] for o in occurrences],
            'amounts': [o['amount'] for o in occurrences],
        })

    # Build income summary
    income_summary = []
    total_monthly_income = 0
    for name, occurrences in sorted(income_groups.items(), key=lambda x: -sum(o['amount'] for o in x[1])):
        avg = sum(o['amount'] for o in occurrences) / len(occurrences)
        days = [int(d.split('-')[2]) for d in [o['date'] for o in occurrences]]
        total_monthly_income += avg
        income_summary.append({
            'name': name,
            'occurrences': len(occurrences),
            'avg_amount': round(avg, 2),
            'typical_day_of_month': round(sum(days) / len(days)),
            'dates': [o['date'] for o in occurrences],
            'amounts': [o['amount'] for o in occurrences],
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

    current_total_balance = sum(b['balance'] for b in balance_data)
    today = date.today()

    prompt = f"""You are a personal finance analyst. Based on the pre-analyzed transaction
data below, predict this user's cash flow for the next 4 weeks.

Today's date: {today}
Current total balance across all accounts: ${current_total_balance:,.2f}

=== RECURRING EXPENSES (grouped from 6 months of bank data) ===
Each entry shows: name, how many times it occurred, average amount, typical day of month.
EVERY entry below is a real recurring expense. Include ALL of them.

{json.dumps(expense_summary, indent=2)}

Total estimated monthly expenses: ${total_monthly_expenses:,.2f}

=== INCOME SOURCES (grouped from 6 months of bank data) ===
{json.dumps(income_summary, indent=2)}

Total estimated monthly income: ${total_monthly_income:,.2f}

=== ACCOUNT BALANCES ===
{json.dumps(balance_data, indent=None)}

Return ONLY valid JSON (no markdown):
{{
  "recurring_bills": [
    {{"name": "<from expense data above>", "amount": 0, "typical_day": 0, "frequency": "monthly", "merchant": ""}}
  ],
  "income_patterns": [
    {{"source": "<from income data above>", "amount": 0, "frequency": "biweekly", "typical_days": []}}
  ],
  "weekly_predictions": [
    {{
      "week_start": "YYYY-MM-DD",
      "week_end": "YYYY-MM-DD",
      "predicted_spend": 0,
      "predicted_income": 0,
      "bills_due": [{{"name": "<bill name>", "amount": 0}}],
      "estimated_end_balance": 0,
      "risk_level": "low"
    }}
  ],
  "alerts": [],
  "monthly_summary": {{
    "avg_monthly_income": {total_monthly_income:.2f},
    "avg_monthly_spend": {total_monthly_expenses:.2f},
    "top_categories": [
      {{"category": "<category>", "amount": 0}}
    ]
  }}
}}

RULES:
- recurring_bills: Include EVERY expense group listed above. Each one is a real bill.
- weekly_predictions: For each week, check which bills have their typical_day in that
  week's date range. Include ALL matching bills in bills_due.
- predicted_spend = sum of all bills_due amounts for that week.
- predicted_income: Check which income sources have deposits in that week. Sum them.
- estimated_end_balance: Week 1 starts at ${current_total_balance:,.2f} (actual balance).
  Each next week: previous end_balance + predicted_income - predicted_spend.
- risk_level: "low" (end > 500), "medium" (0-500), "high" (< 0).
- monthly_summary: avg_monthly_income={total_monthly_income:.2f}, avg_monthly_spend={total_monthly_expenses:.2f} (pre-calculated, use these exact values).
- top_categories: Group ALL expenses into categories. Must sum to avg_monthly_spend.

Provide exactly 4 weekly predictions starting from Monday of current week.
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

    # Override weekly predictions with code-calculated values
    # (AI is unreliable at mapping bills to specific weeks)
    from datetime import timedelta
    import calendar

    # Find Monday of current week
    monday = today - timedelta(days=today.weekday())

    code_predictions = []
    running_balance = float(current_total_balance)

    for week_num in range(4):
        week_start = monday + timedelta(weeks=week_num)
        week_end = week_start + timedelta(days=6)

        # Find bills due this week based on typical_day
        week_bills = []
        week_spend = 0
        for exp in expense_summary:
            typical_day = exp['typical_day_of_month']
            # Check if typical_day falls within this week
            for day_offset in range(7):
                check_date = week_start + timedelta(days=day_offset)
                if check_date.day == typical_day:
                    avg_amt = exp['avg_amount']
                    week_bills.append({
                        'name': exp['name'][:40],
                        'amount': avg_amt,
                    })
                    week_spend += avg_amt
                    break

        # Find income this week
        week_income = 0
        for inc in income_summary:
            typical_day = inc['typical_day_of_month']
            for day_offset in range(7):
                check_date = week_start + timedelta(days=day_offset)
                if check_date.day == typical_day:
                    week_income += inc['avg_amount']
                    break

        running_balance = running_balance + week_income - week_spend

        if running_balance > 500:
            risk = 'low'
        elif running_balance >= 0:
            risk = 'medium'
        else:
            risk = 'high'

        code_predictions.append({
            'week_start': str(week_start),
            'week_end': str(week_end),
            'predicted_spend': round(week_spend, 2),
            'predicted_income': round(week_income, 2),
            'bills_due': week_bills,
            'estimated_end_balance': round(running_balance, 2),
            'risk_level': risk,
        })

    # Use code-calculated predictions but keep AI's categorization
    analysis['weekly_predictions'] = code_predictions
    analysis['monthly_summary']['avg_monthly_income'] = round(total_monthly_income, 2)
    analysis['monthly_summary']['avg_monthly_spend'] = round(total_monthly_expenses, 2)

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
