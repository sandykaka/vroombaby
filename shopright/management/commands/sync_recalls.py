"""
Management command to sync product recalls from FDA, FSIS, and CPSC APIs

This command:
1. Fetches recalls from 3 government APIs (all FREE)
2. Matches recalls against user purchase history
3. Creates RecallMatch records with confidence scores
4. Notifies users for high-confidence matches (80%+ confidence)

Note:
    FDA API updates weekly (not real-time), so default lookback is 7 days
    to ensure we catch all recalls despite API lag.

Usage:
    # Daily sync (run at 2 AM via cron) - queries last 7 days
    python manage.py sync_recalls

    # Urgent check for Class I recalls (run at 5 PM via cron)
    python manage.py sync_recalls --urgent

    # Custom date range
    python manage.py sync_recalls --days 14

    # Test mode (fetch but don't match)
    python manage.py sync_recalls --fetch-only
"""

from django.core.management.base import BaseCommand
from shopright.services.recall_service import RecallService
from shopright.models import ProductRecall, RecallMatch
from django.utils import timezone


class Command(BaseCommand):
    help = 'Sync product recalls from FDA, FSIS, and CPSC APIs'

    def add_arguments(self, parser):
        parser.add_argument(
            '--urgent',
            action='store_true',
            help='Only fetch Class I (critical) recalls for urgent check',
        )
        parser.add_argument(
            '--days',
            type=int,
            default=7,
            help='Number of days to look back (default: 7 to account for FDA API lag)',
        )
        parser.add_argument(
            '--fetch-only',
            action='store_true',
            help='Fetch recalls but skip matching (for testing)',
        )

    def handle(self, *args, **options):
        urgent = options['urgent']
        days_back = options['days']
        fetch_only = options['fetch_only']

        # Header
        if urgent:
            self.stdout.write(self.style.ERROR('🚨 URGENT RECALL CHECK (Class I only)'))
        else:
            self.stdout.write(self.style.SUCCESS(f'🔄 Syncing recalls (last {days_back} day(s))...'))

        self.stdout.write(self.style.SUCCESS('=' * 80))
        self.stdout.write('')

        # Initialize service
        service = RecallService()

        try:
            # Run sync
            if urgent:
                counts = service.sync_urgent_recalls()
            else:
                counts = service.sync_all_recalls(days_back=days_back)

            # Display results
            self.stdout.write(self.style.SUCCESS('📊 RECALL SYNC RESULTS:'))
            self.stdout.write(self.style.SUCCESS('-' * 80))

            # Fetch counts
            if not urgent:
                self.stdout.write(f'  FDA (Food & Drug):      {counts["fda"]:>3} new recalls')
                self.stdout.write(f'  FSIS (Meat/Poultry):    {counts["fsis"]:>3} new recalls')
                self.stdout.write(f'  CPSC (Consumer Safety): {counts["cpsc"]:>3} new recalls')
                self.stdout.write(f'  {"-" * 30}')
                total_recalls = counts['fda'] + counts['fsis'] + counts['cpsc']
                self.stdout.write(self.style.SUCCESS(f'  Total new recalls:      {total_recalls:>3}'))
            else:
                self.stdout.write(self.style.ERROR(f'  Class I (critical):     {counts["critical_recalls"]:>3} recalls'))

            self.stdout.write('')

            # Matching counts (if not fetch-only)
            if not fetch_only:
                self.stdout.write(self.style.WARNING(f'  High-confidence matches: {counts["matches"]:>3} users notified'))
            else:
                self.stdout.write(self.style.WARNING('  Matching skipped (--fetch-only mode)'))

            self.stdout.write('')

            # Database stats
            self._display_database_stats(urgent)

            # Display critical matches (if any)
            self._display_critical_matches()

            # Next steps
            self.stdout.write('')
            self.stdout.write(self.style.SUCCESS('💡 NEXT STEPS:'))
            self.stdout.write('   • Users will receive push notifications for matches')
            self.stdout.write('   • View matches in Django admin:')
            self.stdout.write('     http://localhost:8000/admin/shopright/recallmatch/')
            self.stdout.write('')

            # Success
            if urgent and counts.get('critical_recalls', 0) > 0:
                self.stdout.write(self.style.ERROR('⚠️  URGENT: Critical Class I recalls found!'))
            else:
                self.stdout.write(self.style.SUCCESS('✅ Recall sync completed successfully!'))

        except Exception as e:
            self.stdout.write(self.style.ERROR(f'❌ Recall sync failed: {e}'))
            import traceback
            self.stdout.write(traceback.format_exc())

    def _display_database_stats(self, urgent_only=False):
        """Display database statistics"""
        self.stdout.write(self.style.SUCCESS('📈 DATABASE STATS:'))
        self.stdout.write(self.style.SUCCESS('-' * 80))

        # Total recalls by source
        fda_total = ProductRecall.objects.filter(source='FDA').count()
        fsis_total = ProductRecall.objects.filter(source='FSIS').count()
        cpsc_total = ProductRecall.objects.filter(source='CPSC').count()

        self.stdout.write(f'  Total recalls in DB:')
        self.stdout.write(f'    FDA:  {fda_total:>4}')
        self.stdout.write(f'    FSIS: {fsis_total:>4}')
        self.stdout.write(f'    CPSC: {cpsc_total:>4}')

        # Active recalls by classification
        class_i = ProductRecall.objects.filter(classification='Class I', status='Active').count()
        class_ii = ProductRecall.objects.filter(classification='Class II', status='Active').count()
        class_iii = ProductRecall.objects.filter(classification='Class III', status='Active').count()

        self.stdout.write('')
        self.stdout.write(f'  Active recalls by severity:')
        self.stdout.write(self.style.ERROR(f'    Class I (critical):  {class_i:>4}'))
        self.stdout.write(self.style.WARNING(f'    Class II (moderate): {class_ii:>4}'))
        self.stdout.write(f'    Class III (minor):   {class_iii:>4}')

        # Total matches
        total_matches = RecallMatch.objects.count()
        unverified = RecallMatch.objects.filter(user_response='unverified').count()

        self.stdout.write('')
        self.stdout.write(f'  Total user matches: {total_matches:>4}')
        self.stdout.write(f'  Pending verification: {unverified:>4}')

    def _display_critical_matches(self):
        """Display critical Class I matches requiring immediate attention"""
        critical_matches = RecallMatch.objects.filter(
            recall__classification='Class I',
            user_response='unverified',
            notified_at__isnull=True
        ).select_related('recall', 'user')[:5]  # Top 5

        if critical_matches.exists():
            self.stdout.write('')
            self.stdout.write(self.style.ERROR('🚨 CRITICAL MATCHES (Class I):'))
            self.stdout.write(self.style.ERROR('=' * 80))

            for match in critical_matches:
                self.stdout.write(
                    self.style.ERROR(
                        f'  • {match.user.username} bought {match.purchased_product_name}\n'
                        f'    Recall: {match.recall.recall_number} - {match.recall.product_name[:50]}\n'
                        f'    Reason: {match.recall.reason_for_recall[:60]}\n'
                        f'    Confidence: {match.confidence_score}% ({match.match_reason})\n'
                    )
                )
