"""
Analyze AI conversation logs to improve system

Usage:
  python manage.py analyze_ai_conversations [--days 7] [--restaurant ChIJ...]

This command analyzes user questions and AI responses to help you:
1. Identify common question patterns
2. Find questions that cost money but could be cached
3. Spot response quality issues
4. Generate suggestions for system prompt improvements
"""

from django.core.management.base import BaseCommand
import pandas as pd
from pathlib import Path
from collections import Counter
from django.conf import settings
import re


class Command(BaseCommand):
    help = 'Analyze AI conversation logs for insights and improvements'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=30,
            help='Analyze conversations from last N days (default: 30)'
        )
        parser.add_argument(
            '--restaurant',
            type=str,
            help='Filter by specific restaurant place_id'
        )
        parser.add_argument(
            '--type',
            type=str,
            choices=['home', 'restaurant', 'all'],
            default='all',
            help='Filter by conversation type (default: all)'
        )
        parser.add_argument(
            '--export',
            type=str,
            help='Export analysis to file (e.g., analysis.txt)'
        )

    def handle(self, *args, **options):
        log_file = Path(settings.BASE_DIR) / "var" / "logs" / "ai_conversations.csv"

        if not log_file.exists():
            self.stdout.write(self.style.ERROR("❌ No conversation logs found yet"))
            return

        # Load logs
        df = pd.read_csv(log_file)
        df['timestamp'] = pd.to_datetime(df['timestamp'])

        # Filter by date
        days_ago = pd.Timestamp.now() - pd.Timedelta(days=options['days'])
        df = df[df['timestamp'] >= days_ago]

        # Filter by restaurant if specified
        if options['restaurant']:
            df = df[df['restaurant_id'] == options['restaurant']]

        # Filter by conversation type if specified
        if options['type'] != 'all':
            df = df[df['conversation_type'] == options['type']]

        if df.empty:
            self.stdout.write(self.style.WARNING("⚠️ No conversations found for the specified filters"))
            return

        self.stdout.write(self.style.SUCCESS(f"\n📊 Analyzing {len(df)} conversations from last {options['days']} days\n"))

        # Build analysis report
        report = []
        report.append("=" * 80)
        report.append("AI CONVERSATION ANALYSIS REPORT")
        report.append("=" * 80)
        report.append("")

        # 1. Overall Stats
        report.extend(self._overall_stats(df))

        # 2. Cost Analysis
        report.extend(self._cost_analysis(df))

        # 3. Common Question Patterns
        report.extend(self._question_patterns(df))

        # 4. Cache Optimization Opportunities
        report.extend(self._cache_opportunities(df))

        # 5. Response Quality Issues
        report.extend(self._quality_issues(df))

        # 6. Recommended System Prompt Updates
        report.extend(self._prompt_recommendations(df))

        # 7. Suggested Pattern Matches (for FREE responses)
        report.extend(self._pattern_match_suggestions(df))

        # Output report
        full_report = "\n".join(report)
        self.stdout.write(full_report)

        # Export if requested
        if options['export']:
            export_path = Path(options['export'])
            export_path.write_text(full_report, encoding='utf-8')
            self.stdout.write(self.style.SUCCESS(f"\n✅ Report exported to: {export_path}"))

    def _overall_stats(self, df):
        lines = []
        lines.append("1️⃣ OVERALL STATISTICS")
        lines.append("-" * 80)
        lines.append(f"Total conversations: {len(df)}")
        lines.append(f"Unique restaurants: {df['restaurant_id'].nunique()}")
        lines.append(f"Cache hit rate: {(df['cache_hit'].sum() / len(df) * 100):.1f}%")
        lines.append(f"Total cost: ${df['cost_usd'].sum():.2f}")
        lines.append(f"Avg response time: {df['response_time_ms'].mean():.0f}ms")
        lines.append("")
        return lines

    def _cost_analysis(self, df):
        lines = []
        lines.append("2️⃣ COST ANALYSIS")
        lines.append("-" * 80)

        # Questions that cost money (not cached)
        paid = df[df['cost_usd'] > 0]
        if len(paid) > 0:
            lines.append(f"Paid API calls: {len(paid)} (${paid['cost_usd'].sum():.4f})")
            lines.append(f"Cache hits (FREE): {len(df[df['cache_hit'] == True])} ($0.00)")
            lines.append("")

            # Most expensive question types
            lines.append("💰 Most expensive question patterns:")
            for i, (question, cost) in enumerate(paid.groupby('user_question')['cost_usd'].sum().nlargest(10).items(), 1):
                count = len(paid[paid['user_question'] == question])
                lines.append(f"  {i}. '{question[:60]}...' - {count}x = ${cost:.4f}")
        else:
            lines.append("✅ All questions served from cache (FREE)!")

        lines.append("")
        return lines

    def _question_patterns(self, df):
        lines = []
        lines.append("3️⃣ COMMON QUESTION PATTERNS")
        lines.append("-" * 80)

        # Normalize questions for pattern detection
        def normalize(q):
            q = q.lower().strip()
            # Remove question marks
            q = q.replace('?', '')
            # Extract key phrases
            if 'recommend' in q or 'suggest' in q:
                return 'recommendation_request'
            if 'popular' in q or 'best' in q or "what's good" in q:
                return 'popular_dishes'
            if 'vegetarian' in q or 'vegan' in q:
                return 'dietary_restriction'
            if 'spicy' in q or 'mild' in q:
                return 'spice_level'
            if 'price' in q or 'cost' in q or 'expensive' in q or 'cheap' in q:
                return 'price_inquiry'
            if 'delivery' in q or 'pickup' in q or 'takeout' in q:
                return 'ordering_method'
            if 'hours' in q or 'open' in q or 'close' in q:
                return 'hours_inquiry'
            return 'other'

        df['pattern'] = df['user_question'].apply(normalize)
        pattern_counts = df['pattern'].value_counts()

        lines.append("📝 Top question types:")
        for i, (pattern, count) in enumerate(pattern_counts.items(), 1):
            pct = count / len(df) * 100
            lines.append(f"  {i}. {pattern.replace('_', ' ').title()}: {count}x ({pct:.1f}%)")

        lines.append("")
        return lines

    def _cache_opportunities(self, df):
        lines = []
        lines.append("4️⃣ CACHE OPTIMIZATION OPPORTUNITIES")
        lines.append("-" * 80)

        # Questions asked multiple times but not cached
        question_counts = df[df['cache_hit'] == False]['user_question'].value_counts()
        repeat_questions = question_counts[question_counts > 1]

        if len(repeat_questions) > 0:
            lines.append("🔄 Questions asked multiple times (should be cached):")
            for i, (question, count) in enumerate(repeat_questions.head(10).items(), 1):
                cost = df[df['user_question'] == question]['cost_usd'].sum()
                lines.append(f"  {i}. '{question[:60]}...' - {count}x = ${cost:.4f}")
                lines.append(f"     💡 Savings if cached: ${(cost - (cost / count)):.4f}")
        else:
            lines.append("✅ No repeat questions - caching is working well!")

        lines.append("")
        return lines

    def _quality_issues(self, df):
        lines = []
        lines.append("5️⃣ RESPONSE QUALITY ISSUES")
        lines.append("-" * 80)

        # Detect potential issues
        issues_found = False

        # Short responses (may be incomplete)
        short_responses = df[df['ai_response_preview'].str.len() < 50]
        if len(short_responses) > 0:
            issues_found = True
            lines.append(f"⚠️ {len(short_responses)} suspiciously short responses (< 50 chars)")
            for i, row in short_responses.head(3).iterrows():
                lines.append(f"  - Q: '{row['user_question'][:50]}...'")
                lines.append(f"    A: '{row['ai_response_preview'][:80]}'")
            lines.append("")

        # Responses with apologies (AI couldn't help)
        apology_keywords = ['sorry', 'apologize', "can't help", "don't know", "not sure"]
        has_apology = df['ai_response_preview'].str.lower().str.contains('|'.join(apology_keywords))
        apology_responses = df[has_apology]

        if len(apology_responses) > 0:
            issues_found = True
            lines.append(f"⚠️ {len(apology_responses)} responses with apologies (AI struggled)")
            for i, row in apology_responses.head(3).iterrows():
                lines.append(f"  - Q: '{row['user_question'][:60]}...'")
                lines.append(f"    A: '{row['ai_response_preview'][:100]}...'")
            lines.append("")

        if not issues_found:
            lines.append("✅ No obvious quality issues detected!")

        lines.append("")
        return lines

    def _prompt_recommendations(self, df):
        lines = []
        lines.append("6️⃣ RECOMMENDED SYSTEM PROMPT UPDATES")
        lines.append("-" * 80)

        recommendations = []

        # Analyze patterns and suggest prompt improvements
        if 'dietary_restriction' in df['pattern'].values:
            count = len(df[df['pattern'] == 'dietary_restriction'])
            if count > len(df) * 0.1:  # More than 10% of questions
                recommendations.append(
                    "🌱 Add dietary section to system prompt:\n"
                    "   'When asked about dietary restrictions (vegetarian, vegan, gluten-free),\n"
                    "    proactively mention dishes that match. Use our cached dish data to\n"
                    "    suggest specific items.'"
                )

        if 'price_inquiry' in df['pattern'].values:
            count = len(df[df['pattern'] == 'price_inquiry'])
            if count > len(df) * 0.05:  # More than 5% of questions
                recommendations.append(
                    "💰 Add pricing guidance to system prompt:\n"
                    "   'When users ask about prices, reference our cached menu prices.\n"
                    "    Provide price ranges for popular dishes to set expectations.'"
                )

        if 'spice_level' in df['pattern'].values:
            recommendations.append(
                "🌶️ Add spice level guidance:\n"
                "   'When discussing spicy dishes, ask user preference (mild/medium/hot)\n"
                "    and suggest dishes accordingly.'"
            )

        if recommendations:
            for rec in recommendations:
                lines.append(rec)
                lines.append("")
        else:
            lines.append("✅ System prompt seems well-optimized for current usage patterns")
            lines.append("")

        return lines

    def _pattern_match_suggestions(self, df):
        lines = []
        lines.append("7️⃣ SUGGESTED PATTERN MATCHES (to make FREE)")
        lines.append("-" * 80)
        lines.append("Add these to _handle_common_questions() for instant FREE responses:")
        lines.append("")

        # Find common questions that could be pattern matched
        paid = df[df['cost_usd'] > 0]
        common = paid['user_question'].value_counts().head(10)

        for i, (question, count) in enumerate(common.items(), 1):
            if count >= 3:  # Asked 3+ times
                # Suggest pattern match code
                lines.append(f"  {i}. Question: '{question}'")
                lines.append(f"     Frequency: {count}x (${count * 0.0002:.4f})")
                lines.append(f"     Pattern: {self._suggest_pattern(question)}")
                lines.append("")

        return lines

    def _suggest_pattern(self, question):
        """Suggest a pattern match for a question"""
        q = question.lower()
        if 'recommend' in q or 'suggest' in q:
            return "if any(keyword in msg_lower for keyword in ['recommend', 'suggest']):"
        if 'popular' in q or 'best' in q:
            return "if any(keyword in msg_lower for keyword in ['popular', 'best', 'top']):"
        if 'spicy' in q:
            return "if 'spicy' in msg_lower or 'mild' in msg_lower:"
        if 'price' in q or 'cost' in q:
            return "if any(keyword in msg_lower for keyword in ['price', 'cost', 'expensive']):"
        return "# Custom pattern needed - analyze context"
