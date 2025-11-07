"""
Management command to check for reported/flagged product images

Usage:
    python manage.py check_reported_images
    python manage.py check_reported_images --flagged-only
    python manage.py check_reported_images --send-email
"""

from django.core.management.base import BaseCommand
from shopright.models import GroceryItem
from django.utils import timezone


class Command(BaseCommand):
    help = 'Check for reported/flagged product images'

    def add_arguments(self, parser):
        parser.add_argument(
            '--flagged-only',
            action='store_true',
            help='Show only flagged images (3+ reports)',
        )
        parser.add_argument(
            '--send-email',
            action='store_true',
            help='Send email notification (requires email setup)',
        )

    def handle(self, *args, **options):
        flagged_only = options['flagged_only']
        send_email = options['send_email']

        self.stdout.write(self.style.SUCCESS('🔍 Checking for reported images...\n'))

        # Get flagged items
        flagged_items = GroceryItem.objects.filter(image_flagged=True).order_by('-image_report_count')
        flagged_count = flagged_items.count()

        # Get reported items (not yet flagged)
        reported_items = GroceryItem.objects.filter(
            image_report_count__gte=1,
            image_flagged=False
        ).order_by('-image_report_count')
        reported_count = reported_items.count()

        # Display flagged items
        if flagged_count > 0:
            self.stdout.write(self.style.ERROR(f'\n🚩 FLAGGED ITEMS ({flagged_count}):'))
            self.stdout.write(self.style.ERROR('=' * 80))
            for item in flagged_items:
                enriched_by = item.first_enriched_by.username if item.first_enriched_by else 'Unknown'
                self.stdout.write(
                    f'  • {item.name} ({item.brand or "No brand"})\n'
                    f'    Reports: {item.image_report_count} | Uploaded by: {enriched_by}\n'
                    f'    ID: {item.id} | Updated: {item.updated_at.strftime("%Y-%m-%d %H:%M")}\n'
                )
        else:
            self.stdout.write(self.style.SUCCESS('✅ No flagged items'))

        # Display reported items (if not flagged-only mode)
        if not flagged_only and reported_count > 0:
            self.stdout.write(self.style.WARNING(f'\n⚠️  REPORTED ITEMS ({reported_count}):'))
            self.stdout.write(self.style.WARNING('=' * 80))
            for item in reported_items:
                enriched_by = item.first_enriched_by.username if item.first_enriched_by else 'Unknown'
                self.stdout.write(
                    f'  • {item.name} ({item.brand or "No brand"})\n'
                    f'    Reports: {item.image_report_count} | Uploaded by: {enriched_by}\n'
                    f'    ID: {item.id} | Image: {item.image_url[:50]}...\n'
                )
        elif not flagged_only:
            self.stdout.write(self.style.SUCCESS('✅ No reported items'))

        # Summary
        self.stdout.write(self.style.SUCCESS(f'\n📊 SUMMARY:'))
        self.stdout.write(f'  Total flagged (hidden): {flagged_count}')
        self.stdout.write(f'  Total reported (1-2): {reported_count}')
        self.stdout.write(f'  Total needing review: {flagged_count + reported_count}\n')

        # Admin URL hint
        self.stdout.write(self.style.SUCCESS('💡 TIP: View in Django admin at:'))
        self.stdout.write('   http://localhost:8000/admin/shopright/groceryitem/?report_status=reported\n')

        # Send email notification (if requested)
        if send_email and (flagged_count > 0 or reported_count > 0):
            self._send_email_notification(flagged_items, reported_items)

    def _send_email_notification(self, flagged_items, reported_items):
        """Send email notification about reported images"""
        try:
            from django.core.mail import send_mail
            from django.conf import settings

            subject = f'ShopRight: {flagged_items.count()} flagged, {reported_items.count()} reported images'

            message = f"""
ShopRight Image Reports Summary
===============================

FLAGGED ITEMS (3+ reports, hidden):
"""
            for item in flagged_items:
                enriched_by = item.first_enriched_by.username if item.first_enriched_by else 'Unknown'
                message += f"\n  • {item.name} - {item.image_report_count} reports (by {enriched_by})"

            message += f"\n\nREPORTED ITEMS (1-2 reports):"
            for item in reported_items:
                enriched_by = item.first_enriched_by.username if item.first_enriched_by else 'Unknown'
                message += f"\n  • {item.name} - {item.image_report_count} report(s) (by {enriched_by})"

            message += f"\n\nAdmin link: {settings.BASE_URL}/admin/shopright/groceryitem/?report_status=reported"

            send_mail(
                subject,
                message,
                settings.DEFAULT_FROM_EMAIL,
                [settings.ADMIN_EMAIL],  # Add your email to settings
                fail_silently=False,
            )

            self.stdout.write(self.style.SUCCESS('✅ Email notification sent!'))

        except Exception as e:
            self.stdout.write(self.style.ERROR(f'❌ Failed to send email: {e}'))
            self.stdout.write(self.style.WARNING('   Set up Django email settings to enable notifications'))
