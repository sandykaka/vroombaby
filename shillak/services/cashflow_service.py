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

    # Extract payee/merchant names for new transactions using AI
    _extract_expense_groups(plaid_item.home)

    return len(transactions)


def _extract_expense_groups(home):
    """Use AI to extract payee/merchant from transaction names.
    Only processes transactions that don't have expense_group set yet."""
    ungrouped = Transaction.objects.filter(
        home=home, expense_group__isnull=True
    ).exclude(
        expense_group__gt=''
    ).values_list('id', 'name', 'merchant_name')

    if not ungrouped:
        return

    # If Plaid already gave us merchant_name, use it directly — no AI needed
    to_update_direct = []
    needs_ai = []
    for txn_id, name, merchant in ungrouped:
        if merchant and merchant.strip():
            to_update_direct.append((txn_id, merchant.strip()[:35]))
        else:
            needs_ai.append((txn_id, name))

    # Batch update ones with merchant_name
    for txn_id, group in to_update_direct:
        Transaction.objects.filter(id=txn_id).update(expense_group=group)

    if not needs_ai:
        logger.info(f"All {len(to_update_direct)} transactions grouped via merchant_name")
        return

    # Send remaining to AI for entity extraction
    from openai import OpenAI
    from django.conf import settings

    txn_list = [{'id': tid, 'name': name} for tid, name in needs_ai]

    prompt = f"""Extract the payee, merchant, or recipient from each transaction name.
Return a short, clean name (max 25 chars) that identifies WHO is being paid.

Examples:
- "Zelle Recurring payment to SENTHILKUMAR PALANISAMY Conf# abc" → "SENTHILKUMAR PALANISAMY"
- "Zelle payment to SENTHILKUMAR PALAN for Mar rent" → "SENTHILKUMAR PALANISAMY"
- "PNC MORTGAGE DES:PNC PYMT ID:XXXXX09385" → "PNC Mortgage"
- "CHECKCARD 0405 TMOBILE AUTO P BELLEVUE WA" → "T-Mobile"
- "AMERICAN EXPRESS DES:ACH PMT ID:A7314" → "American Express"
- "CITI AUTOPAY DES:PAYMENT ID:XXXXX" → "Citi"
- "DEPT EDUCATION DES:STUDENT LN ID:0000" → "Dept Education"
- "APPLE INC. DES:PAYROLL ID:537093" → "Apple Inc"
- "Venmo" → "Venmo"

Return ONLY valid JSON: {{"results": [{{"id": 123, "payee": "Clean Name"}}]}}

Transactions:
{json.dumps(txn_list, indent=None)}"""

    try:
        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        response = client.chat.completions.create(
            model='gpt-4o-mini',
            messages=[
                {'role': 'system', 'content': 'Extract payee names. Return only JSON.'},
                {'role': 'user', 'content': prompt},
            ],
            temperature=0,
            response_format={'type': 'json_object'},
        )

        result = json.loads(response.choices[0].message.content)
        for item in result.get('results', []):
            Transaction.objects.filter(id=item['id']).update(
                expense_group=item['payee'][:35]
            )

        logger.info(
            f"AI extracted {len(result.get('results', []))} payee names for {home.name}"
        )

    except Exception as e:
        logger.error(f"AI entity extraction failed: {e}")
        # Fallback: use the normalize function
        for txn_id, name in needs_ai:
            cleaned = re.split(r'\bDES:|\bID:|\bConf#|\bfor "', name)[0]
            cleaned = re.sub(r'^CHECKCARD\s*|^ACH HOLD\s*', '', cleaned)
            cleaned = re.sub(r'\b\d{2}/?\d{2}\b', '', cleaned)
            cleaned = re.sub(r'X{3,}\d*', '', cleaned)
            cleaned = re.sub(r'\s+', ' ', cleaned).strip()
            Transaction.objects.filter(id=txn_id).update(
                expense_group=cleaned[:35] if cleaned else name[:35]
            )

    # Deduplicate similar group names (e.g. "JONG HO YOU" and "JONG YOU")
    _dedup_expense_groups(home)


def _dedup_expense_groups(home):
    """Merge similar expense_group names that likely refer to the same payee.
    Uses name similarity + transaction pattern matching (timing, amounts, direction)."""
    from difflib import SequenceMatcher

    groups = list(
        Transaction.objects.filter(home=home, expense_group__isnull=False)
        .exclude(expense_group='')
        .values_list('expense_group', flat=True)
        .distinct()
    )

    if len(groups) < 2:
        return

    # Build profile for each group
    group_profiles = {}
    for g in groups:
        txns = Transaction.objects.filter(home=home, expense_group=g)
        amounts = [float(t.amount) for t in txns]
        days = [t.date.day for t in txns]
        # Direction: mostly positive (expense) or mostly negative (income)
        positive_count = sum(1 for a in amounts if a > 0)
        direction = 'expense' if positive_count > len(amounts) / 2 else 'income'
        group_profiles[g] = {
            'count': len(amounts),
            'median_amount': sorted([abs(a) for a in amounts])[len(amounts) // 2] if amounts else 0,
            'typical_day': round(sum(days) / len(days)) if days else 0,
            'direction': direction,
        }

    # Find pairs to merge
    merges = {}
    processed = set()

    for i, g1 in enumerate(groups):
        if g1 in processed:
            continue
        for g2 in groups[i + 1:]:
            if g2 in processed:
                continue

            p1 = group_profiles[g1]
            p2 = group_profiles[g2]

            # Must have same direction (both expenses or both income)
            if p1['direction'] != p2['direction']:
                continue

            # Name similarity
            name_sim = SequenceMatcher(None, g1.upper(), g2.upper()).ratio()
            if name_sim < 0.5:
                continue

            containment = g1.upper() in g2.upper() or g2.upper() in g1.upper()

            # Amount similarity (within 50%)
            if p1['median_amount'] > 0 and p2['median_amount'] > 0:
                ratio = min(p1['median_amount'], p2['median_amount']) / max(p1['median_amount'], p2['median_amount'])
                amount_similar = ratio > 0.5
            else:
                amount_similar = True

            # Day similarity (within 5 days)
            day_similar = abs(p1['typical_day'] - p2['typical_day']) <= 5

            should_merge = (
                name_sim >= 0.8 or
                (containment and (amount_similar or day_similar)) or
                (name_sim >= 0.6 and amount_similar and day_similar)
            )

            if should_merge:
                canonical = g1 if len(g1) >= len(g2) else g2
                shorter = g2 if canonical == g1 else g1
                merges[shorter] = canonical
                processed.add(shorter)
                logger.info(f"Dedup: '{shorter}' → '{canonical}' (sim={name_sim:.2f})")

    for old_name, new_name in merges.items():
        count = Transaction.objects.filter(
            home=home, expense_group=old_name
        ).update(expense_group=new_name)
        if count:
            logger.info(f"Merged {count} transactions: '{old_name}' → '{new_name}'")


def analyze_cashflow(home, dry_run=False):
    """Analyze transaction history with OpenAI and generate predictions."""
    # Gather all transactions for the home
    transactions = Transaction.objects.filter(
        home=home, pending=False
    ).order_by('date').values(
        'id', 'plaid_transaction_id', 'date', 'amount', 'name', 'merchant_name',
        'category', 'personal_finance_category', 'expense_group',
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
            'plaid_id': t['plaid_transaction_id'],
            'db_id': t['id'],
            'expense_group': t.get('expense_group') or '',
        })

    # Pre-analyze: group expenses and income by merchant
    import re
    from collections import defaultdict

    def normalize_name(name, merchant=None):
        """Use Plaid's merchant_name if available, otherwise clean raw name."""
        if merchant and merchant.strip():
            return merchant.strip()[:35]
        # Fall back to cleaning the raw transaction name
        cleaned = re.split(r'\bDES:', name)[0]
        cleaned = re.split(r'\bID:', cleaned)[0]
        cleaned = re.split(r'\bConf#', cleaned)[0]
        cleaned = re.split(r'\bfor "', cleaned)[0]
        cleaned = re.sub(r'^CHECKCARD\s*', '', cleaned)
        cleaned = re.sub(r'^ACH HOLD\s*', '', cleaned)
        cleaned = re.sub(r'\b\d{2}/?\d{2}\b', '', cleaned)
        cleaned = re.sub(r'X{3,}\d*', '', cleaned)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        return cleaned[:35] if cleaned else name[:35]

    # Detect internal transfers: transactions that appear as both positive AND negative
    # with similar amounts (e.g. $3,000 out of BofA = $3,000 into Capital One)
    # Also filter out bank fees and interest
    EXCLUDE_PATTERNS = {'Monthly Interest Paid', 'OVERDRAFT', 'INTEREST', 'FEE FOR ACTIVITY'}

    def is_excludable(name):
        normalized = normalize_name(name).upper()
        return any(p.upper() in normalized for p in EXCLUDE_PATTERNS)

    # Find institution names from linked accounts to detect internal transfers
    institution_names = set()
    for a in accounts:
        inst = a['institution_name'].upper().replace(',', '').replace('.', '')
        institution_names.add(inst)
        for word in inst.split():
            if len(word) > 3:
                institution_names.add(word)

    def is_internal_transfer(name):
        """Detect transfers between user's own accounts."""
        normalized = normalize_name(name).upper().replace(',', '').replace('.', '')
        return any(inst in normalized for inst in institution_names)

    expense_groups = defaultdict(list)
    income_groups = defaultdict(list)
    txn_to_group = {}

    for t in txn_data:
        # Use expense_group from DB (set by AI entity extraction)
        # Fall back to normalize_name if not set yet
        key = t.get('expense_group') or normalize_name(t['name'], t.get('merchant'))
        if is_internal_transfer(t['name']) or is_excludable(t['name']):
            continue
        txn_to_group[t.get('plaid_id', t['name'])] = key
        if t['amount'] > 0:
            expense_groups[key].append({'date': t['date'], 'amount': t['amount']})
        elif t['amount'] < 0:
            income_groups[key].append({'date': t['date'], 'amount': abs(t['amount'])})

    # Merge consecutive same-person payments within 2 days
    # (e.g. Zelle rent split: $3,500 + $1,800 on same/next day = $5,300 single payment)
    def merge_consecutive_payments(occurrences):
        """Merge payments to the same person within 1 business day into single entries.
        Friday→Monday (3 calendar days) counts as consecutive since banks skip weekends."""
        if len(occurrences) < 2:
            return occurrences

        from datetime import datetime as dt
        sorted_occ = sorted(occurrences, key=lambda x: x['date'])
        merged = []
        i = 0
        while i < len(sorted_occ):
            current = sorted_occ[i]
            combined_amount = current['amount']
            while i + 1 < len(sorted_occ):
                next_occ = sorted_occ[i + 1]
                curr_date = dt.strptime(current['date'], '%Y-%m-%d').date()
                next_date = dt.strptime(next_occ['date'], '%Y-%m-%d').date()
                gap = (next_date - curr_date).days
                # 0-1 = same/next day, 2 = skip one day, 3 = Fri→Mon
                if gap <= 3:
                    combined_amount += next_occ['amount']
                    i += 1
                else:
                    break
            merged.append({'date': current['date'], 'amount': combined_amount})
            i += 1
        return merged

    # Apply merging to expense groups
    for key in expense_groups:
        expense_groups[key] = merge_consecutive_payments(expense_groups[key])

    def detect_frequency(dates_list):
        """Detect payment frequency from actual date intervals."""
        if len(dates_list) < 2:
            return 'monthly'
        from datetime import datetime as dt
        parsed = sorted([dt.strptime(d, '%Y-%m-%d').date() for d in dates_list])
        intervals = [(parsed[i+1] - parsed[i]).days for i in range(len(parsed)-1)]
        avg_interval = sum(intervals) / len(intervals)
        if avg_interval <= 10:
            return 'weekly'
        elif avg_interval <= 20:
            return 'biweekly'
        elif avg_interval <= 45:
            return 'monthly'
        elif avg_interval <= 100:
            return 'quarterly'
        else:
            return 'annual'

    # Build expense summary — only include recurring (2+ occurrences)
    import statistics

    def calc_typical_day(days, frequency):
        """Calculate typical day based on frequency and actual payment dates."""
        if not days:
            return 1
        if frequency in ('weekly', 'biweekly'):
            # For weekly/biweekly, typical_day isn't meaningful
            # Return the most recent day as reference
            return days[-1]

        # Monthly/quarterly: find the typical day of month
        # Handle month-boundary bills (e.g. [30, 1, 31, 2, 30])
        if max(days) - min(days) > 20:
            # Spans month boundary — shift low days up by 31
            adjusted = [d + 31 if d <= 5 else d for d in days]
            result = round(statistics.median(adjusted))
            return result if result <= 31 else result - 31
        else:
            return round(statistics.median(days))

    today = date.today()

    # Load hidden bills
    from shillak.models import BillAlias
    hidden_groups = set(
        BillAlias.objects.filter(home=home, hidden=True)
        .values_list('normalized_name', flat=True)
    )

    # Auto-reset hidden bills older than 90 days
    ninety_days_ago = today - timedelta(days=90)
    BillAlias.objects.filter(
        home=home, hidden=True, hidden_at__lt=ninety_days_ago
    ).update(hidden=False, hidden_at=None)

    # Auto-unhide bills that have new transactions after hidden_at
    for alias in BillAlias.objects.filter(home=home, hidden=True, hidden_at__isnull=False):
        latest_txn = Transaction.objects.filter(
            home=home, expense_group=alias.normalized_name
        ).order_by('-date').first()
        if latest_txn and latest_txn.date > alias.hidden_at.date():
            alias.hidden = False
            alias.hidden_at = None
            alias.save(update_fields=['hidden', 'hidden_at'])
            hidden_groups.discard(alias.normalized_name)
            logger.info(f"Auto-unhidden bill: {alias.normalized_name}")

    INTERVAL_DAYS = {'weekly': 7, 'biweekly': 14, 'monthly': 30, 'quarterly': 90, 'annual': 365}

    expense_summary = []
    total_monthly_expenses = 0
    for name, occurrences in sorted(expense_groups.items(), key=lambda x: -sum(o['amount'] for o in x[1])):
        if len(occurrences) < 2:
            continue
        if name in hidden_groups:
            continue

        amounts = [o['amount'] for o in occurrences]
        median_amt = statistics.median(amounts)
        stdev = statistics.stdev(amounts) if len(amounts) > 1 else 0
        consistency = 'high' if stdev < median_amt * 0.1 else ('medium' if stdev < median_amt * 0.5 else 'low')
        dates_list = [o['date'] for o in occurrences]
        days = [int(d.split('-')[2]) for d in dates_list]
        frequency = detect_frequency(dates_list)
        typical_day = calc_typical_day(days, frequency)

        # Calculate bill status
        last_date = max(dates_list)
        days_since = (today - date.fromisoformat(last_date)).days
        expected_interval = INTERVAL_DAYS.get(frequency, 30)
        if days_since <= expected_interval * 1.5:
            status = 'active'
        else:
            status = 'inactive'

        # Only include active bills in spending total and predictions
        if status == 'active':
            total_monthly_expenses += median_amt

        expense_summary.append({
            'name': name,
            'occurrences': len(occurrences),
            'avg_amount': round(median_amt, 2),
            'typical_day_of_month': typical_day,
            'frequency': frequency,
            'confidence': consistency,
            'status': status,
            'last_paid': last_date,
            'dates': dates_list,
            'amounts': [o['amount'] for o in occurrences],
        })

    # Build income summary — split groups with very different amounts
    # (e.g. APPLE INC. has payroll $3,765 AND subscription $250)
    split_income_groups = defaultdict(list)
    for name, occurrences in income_groups.items():
        if len(occurrences) >= 2:
            amounts = [o['amount'] for o in occurrences]
            avg = sum(amounts) / len(amounts)
            # If max is >3x min, split into high/low groups
            if max(amounts) > 3 * min(amounts):
                for o in occurrences:
                    if o['amount'] > avg:
                        split_income_groups[name + ' (payroll)'].append(o)
                    else:
                        split_income_groups[name + ' (other)'].append(o)
            else:
                split_income_groups[name] = occurrences
        else:
            split_income_groups[name] = occurrences

    income_summary = []
    total_monthly_income = 0
    for name, occurrences in sorted(split_income_groups.items(), key=lambda x: -sum(o['amount'] for o in x[1])):
        if len(occurrences) < 2:
            continue
        amounts = [o['amount'] for o in occurrences]
        median_amt = statistics.median(amounts)
        stdev = statistics.stdev(amounts) if len(amounts) > 1 else 0
        consistency = 'high' if stdev < median_amt * 0.1 else ('medium' if stdev < median_amt * 0.5 else 'low')
        days = [int(d.split('-')[2]) for d in [o['date'] for o in occurrences]]
        frequency = detect_frequency([o['date'] for o in occurrences])
        typical_day = calc_typical_day(days, frequency)
        total_monthly_income += median_amt
        income_summary.append({
            'name': name,
            'occurrences': len(occurrences),
            'avg_amount': round(median_amt, 2),
            'typical_day_of_month': typical_day,
            'frequency': frequency,
            'confidence': consistency,
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
    import calendar
    import holidays

    us_holidays = holidays.US(years=[today.year, today.year + 1])

    def next_business_day(d):
        """Shift weekends and federal holidays to next business day."""
        while d.weekday() >= 5 or d in us_holidays:
            d += timedelta(days=1)
        return d

    # Find Monday of current week
    monday = today - timedelta(days=today.weekday())

    code_predictions = []
    running_balance = float(current_total_balance)

    for week_num in range(4):
        week_start = monday + timedelta(weeks=week_num)
        week_end = week_start + timedelta(days=6)

        # Find bills due this week (only active bills)
        week_bills = []
        week_spend = 0
        for exp in expense_summary:
            if exp.get('status') != 'active':
                continue
            freq = exp.get('frequency', 'monthly')

            if freq in ('weekly', 'biweekly'):
                # Use interval projection from last known date
                dates = sorted(exp['dates'])
                if len(dates) >= 2:
                    from datetime import datetime as dt
                    parsed = [dt.strptime(d, '%Y-%m-%d').date() for d in dates]
                    intervals = [(parsed[i+1] - parsed[i]).days for i in range(len(parsed)-1)]
                    avg_interval = max(1, round(sum(intervals) / len(intervals)))
                    next_date = parsed[-1] + timedelta(days=avg_interval)
                    while next_date < week_start:
                        next_date += timedelta(days=avg_interval)
                    next_date = next_business_day(next_date)
                    if week_start <= next_date <= week_end:
                        week_bills.append({'name': exp['name'][:40], 'amount': exp['avg_amount']})
                        week_spend += exp['avg_amount']
            else:
                # Monthly/quarterly: use typical_day_of_month
                typical_day = exp['typical_day_of_month']
                try:
                    bill_date = week_start.replace(day=typical_day)
                except ValueError:
                    last_day = calendar.monthrange(week_start.year, week_start.month)[1]
                    bill_date = week_start.replace(day=min(typical_day, last_day))

                bill_date = next_business_day(bill_date)

                if week_start <= bill_date <= week_end:
                    week_bills.append({'name': exp['name'][:40], 'amount': exp['avg_amount']})
                    week_spend += exp['avg_amount']

        # Find income this week
        # Detect frequency from actual date intervals, then project forward
        week_income = 0
        for inc in income_summary:
            dates = sorted(inc['dates'])
            if len(dates) >= 2:
                from datetime import datetime as dt
                parsed = [dt.strptime(d, '%Y-%m-%d').date() for d in dates]
                # Calculate average interval between consecutive payments
                intervals = [(parsed[i+1] - parsed[i]).days for i in range(len(parsed)-1)]
                avg_interval = sum(intervals) / len(intervals)

                # Project forward from last known date using actual interval
                last_date = parsed[-1]
                next_date = last_date + timedelta(days=round(avg_interval))
                while next_date < week_start:
                    next_date += timedelta(days=round(avg_interval))
                if week_start <= next_date <= week_end:
                    week_income += inc['avg_amount']
            else:
                # Single occurrence — use typical day
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

    # Plaid category display name mapping
    PLAID_CATEGORY_MAP = {
        'RENT_AND_UTILITIES': 'Rent & Utilities',
        'LOAN_PAYMENTS': 'Loan Payments',
        'GENERAL_MERCHANDISE': 'Shopping',
        'GENERAL_SERVICES': 'Services',
        'HOME_IMPROVEMENT': 'Home Improvement',
        'TRANSFER_OUT': 'Transfers',
        'BANK_FEES': 'Bank Fees',
        'FOOD_AND_DRINK': 'Dining',
        'TRANSPORTATION': 'Transport',
        'ENTERTAINMENT': 'Entertainment',
        'PERSONAL_CARE': 'Personal Care',
        'MEDICAL': 'Healthcare',
        'GOVERNMENT_AND_NON_PROFIT': 'Government',
        'OTHER': 'Other',
    }
    INCOME_CATEGORIES = {'INCOME', 'TRANSFER_IN'}

    # Build name → Plaid category lookup from transactions
    name_to_plaid_cat = {}
    for t in transactions:
        key = normalize_name(t['name'], t['merchant_name'])
        pfc = t.get('personal_finance_category')
        if pfc and key not in name_to_plaid_cat:
            display = PLAID_CATEGORY_MAP.get(pfc, pfc.replace('_', ' ').title())
            name_to_plaid_cat[key] = display

    # Load user alias categories
    from shillak.models import BillAlias
    alias_cat_map = {
        a.normalized_name: a.category
        for a in BillAlias.objects.filter(home=home)
        if a.category
    }

    # Override recurring_bills with code-calculated data
    analysis['recurring_bills'] = [
        {
            'name': exp['name'],
            'amount': exp['avg_amount'],
            'typical_day': exp['typical_day_of_month'],
            'frequency': exp.get('frequency', 'monthly'),
            'merchant': exp['name'],
            'category': alias_cat_map.get(exp['name'], name_to_plaid_cat.get(exp['name'], '')),
            'status': exp.get('status', 'active'),
            'last_paid': exp.get('last_paid', ''),
        }
        for exp in expense_summary
    ]

    # Override income_patterns too
    analysis['income_patterns'] = [
        {
            'source': inc['name'],
            'amount': inc['avg_amount'],
            'frequency': inc.get('frequency', 'monthly'),
            'typical_days': [inc['typical_day_of_month']],
        }
        for inc in income_summary
    ]

    # Build spending categories from Plaid's transaction categories (not AI)
    # Query transactions for past ~30 days and build categories
    # Filter out internal transfers and excludable transactions
    from django.db.models import Sum
    thirty_days_ago = today - timedelta(days=30)
    recent_txns = Transaction.objects.filter(
        home=home,
        amount__gt=0,
        date__gte=thirty_days_ago,
    ).exclude(
        personal_finance_category__in=INCOME_CATEGORIES
    ).exclude(
        personal_finance_category__isnull=True
    ).values('name', 'merchant_name', 'amount', 'personal_finance_category')

    # Group by Plaid category, excluding internal transfers
    from collections import defaultdict as dd
    cat_totals = dd(float)
    for txn in recent_txns:
        if is_internal_transfer(txn['name']) or is_excludable(txn['name']):
            continue
        plaid_cat = txn['personal_finance_category']
        display = PLAID_CATEGORY_MAP.get(plaid_cat, plaid_cat.replace('_', ' ').title())
        cat_totals[display] += float(txn['amount'])

    code_categories = [
        {'category': cat, 'amount': round(amt, 2)}
        for cat, amt in sorted(cat_totals.items(), key=lambda x: -x[1])
        if amt > 0
    ]

    # Apply user alias category overrides
    # If user assigned a category to a bill, move that bill's amount
    # from Plaid's category to the user's chosen category
    from shillak.models import BillAlias
    alias_categories = {
        a.normalized_name: a.category
        for a in BillAlias.objects.filter(home=home)
        if a.category
    }

    if alias_categories:
        adjusted_totals = dd(float)

        for cat in code_categories:
            adjusted_totals[cat['category']] = cat['amount']

        # Reuse recent_txns to apply alias category overrides
        for txn in recent_txns:
            if is_internal_transfer(txn['name']) or is_excludable(txn['name']):
                continue
            txn_key = normalize_name(txn['name'], txn.get('merchant_name'))
            if txn_key in alias_categories:
                user_cat = alias_categories[txn_key]
                plaid_cat = txn.get('personal_finance_category', '')
                plaid_display = PLAID_CATEGORY_MAP.get(
                    plaid_cat, (plaid_cat or 'Other').replace('_', ' ').title()
                )
                amt = float(txn['amount'])
                # Move from Plaid category to user category
                if plaid_display in adjusted_totals:
                    adjusted_totals[plaid_display] = max(0, adjusted_totals[plaid_display] - amt)
                adjusted_totals[user_cat] = adjusted_totals.get(user_cat, 0) + amt

        # Remove zero categories and rebuild
        code_categories = [
            {'category': cat, 'amount': round(amt, 2)}
            for cat, amt in sorted(adjusted_totals.items(), key=lambda x: -x[1])
            if amt > 0
        ]

    if code_categories:
        analysis['monthly_summary']['top_categories'] = code_categories
        analysis['monthly_summary']['avg_monthly_spend'] = round(
            sum(c['amount'] for c in code_categories), 2
        )

    # Persist expense_group on each transaction so the spending chart
    # can use the same grouping without re-normalizing
    for t in txn_data:
        group = txn_to_group.get(t.get('plaid_id', t['name']))
        if group and t.get('db_id'):
            Transaction.objects.filter(id=t['db_id']).update(expense_group=group)

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
