"""Shared utilities for Shillak services."""


def format_plaid_category(pfc):
    """Convert a Plaid personal_finance_category to a user-friendly display name.
    Handles any category Plaid sends — known ones get nice names, unknown ones
    get auto-formatted from the raw string."""
    if not pfc:
        return 'Other'
    # Just format the raw string nicely: RENT_AND_UTILITIES → Rent & Utilities
    return pfc.replace('_AND_', ' & ').replace('_', ' ').title()
