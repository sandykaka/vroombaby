"""
Product name/size cleanup utilities for receipt parsing.

This module provides functions to fix data quality issues where AI receipt
parsing includes size information in product names instead of extracting it
to the separate size field.

Example Issues:
    - "Raspberries 12 oz" should be name="Raspberries", size="12 oz"
    - "Strawberries Org 1 lb" should be name="Strawberries Org", size="1 lb"

Author: Claude Code
Date: 2025-01-18
"""

import re
from typing import Tuple


def clean_product_name_and_size(name: str, size: str = "") -> Tuple[str, str]:
    """
    Extract size information from product name if not already in size field.

    This function implements a two-layer approach:
    1. If size field is already populated and non-trivial, leave as-is
    2. Otherwise, extract trailing size patterns from name using regex

    Args:
        name: Product name (may contain size info)
        size: Size field (may be empty or minimal)

    Returns:
        Tuple of (cleaned_name, extracted_size)

    Examples:
        >>> clean_product_name_and_size("Raspberries 12 oz", "")
        ("Raspberries", "12 oz")

        >>> clean_product_name_and_size("Strawberries Org 1 lb", "")
        ("Strawberries Org", "1 lb")

        >>> clean_product_name_and_size("Blackberries", "6 oz")
        ("Blackberries", "6 oz")  # Already has size, no change

        >>> clean_product_name_and_size("Milk Gallon", "")
        ("Milk", "1 gallon")

        >>> clean_product_name_and_size("Eggs 12 ct", "")
        ("Eggs", "12 ct")
    """
    if not name:
        return name, size

    # Normalize inputs
    name = name.strip()
    size = (size or "").strip()

    # First, clean up trailing orphaned numbers (OCR artifacts like "Tomatoes 0", "Chicken 9")
    # Only remove if it's JUST a number without units (not "9 oz" or "12 ct")
    orphaned_number_pattern = re.compile(r'\s+\d+\s*$')
    match = orphaned_number_pattern.search(name)
    if match:
        # Check if this looks like a size with units coming up
        # If the number is alone at the end, it's likely noise
        name = name[:match.start()].strip()

    # Size field handling:
    # Even if size field has a value, we should still clean the NAME if it contains size info
    # Example: name="Raspberries 12 oz", size="12 oz" → should clean to name="Raspberries"
    #
    # We'll always attempt to extract/clean, but if size field has a meaningful value AND
    # we don't find size in the name, we'll keep the size field value

    # Regex pattern for common size formats at END of name
    # Matches: 12 oz, 1 lb, 16ct, 1.5 l, 64 fl oz, 12 pack, etc.
    # Pattern explanation:
    # - \d+\.?\d* : Number (with optional decimal)
    # - \s* : Optional whitespace
    # - (oz|lb|...) : Unit abbreviations
    # - s? : Optional plural 's'
    # - \b : Word boundary
    size_pattern = re.compile(
        r'\s+(\d+\.?\d*\s*(?:oz|lb|ct|count|ml|l|kg|g|gal|gallon|qt|quart|pt|pint|fl\s*oz|pack|pk|ea|each)s?)\s*$',
        re.IGNORECASE
    )

    # Also match standalone size words like "gallon", "dozen", "ea", "each"
    size_word_pattern = re.compile(
        r'\s+(gallon|half\s*gallon|quart|pint|dozen|bundle|bunch|bag|box|container|ea|each)\s*$',
        re.IGNORECASE
    )

    # Match pricing/weighted unit patterns like "per lb", "per oz", "per kg"
    per_unit_pattern = re.compile(
        r'\s+per\s+(lb|oz|kg|g|gram|pound|ounce)\s*$',
        re.IGNORECASE
    )

    # Match shrimp/seafood count sizes like "21-30", "31-40", "16-20"
    count_range_pattern = re.compile(
        r'\s+(\d{1,3}-\d{1,3})\s*$',
        re.IGNORECASE
    )

    # Try to extract size from end of name
    match = size_pattern.search(name)
    if match:
        extracted_size = match.group(1).strip()
        cleaned_name = name[:match.start()].strip()
        return cleaned_name, extracted_size

    # Try word-based size patterns
    match = size_word_pattern.search(name)
    if match:
        size_word = match.group(1).strip()
        cleaned_name = name[:match.start()].strip()
        # Normalize some common patterns
        if size_word.lower() == "gallon":
            extracted_size = "1 gallon"
        elif size_word.lower() == "half gallon":
            extracted_size = "0.5 gallon"
        elif size_word.lower() == "dozen":
            extracted_size = "12 ct"
        elif size_word.lower() in ("ea", "each"):
            extracted_size = "1 ea"
        else:
            extracted_size = size_word
        return cleaned_name, extracted_size

    # Try "per unit" patterns (pricing indicators for weighted items)
    match = per_unit_pattern.search(name)
    if match:
        unit = match.group(1).strip()
        cleaned_name = name[:match.start()].strip()
        # Normalize unit abbreviations
        unit_map = {
            'pound': 'lb',
            'ounce': 'oz',
            'gram': 'g'
        }
        normalized_unit = unit_map.get(unit.lower(), unit.lower())
        extracted_size = f"per {normalized_unit}"
        return cleaned_name, extracted_size

    # Try count range patterns (shrimp/seafood sizes)
    match = count_range_pattern.search(name)
    if match:
        count_range = match.group(1).strip()
        cleaned_name = name[:match.start()].strip()
        extracted_size = f"{count_range} ct"  # "21-30" → "21-30 ct"
        return cleaned_name, extracted_size

    # No size found in name - return as-is
    return name, size


def normalize_size_format(size: str) -> str:
    """
    Normalize size strings to consistent format.

    Examples:
        - "12oz" → "12 oz"
        - "1LB" → "1 lb"
        - "16  ct" → "16 ct"

    Args:
        size: Raw size string

    Returns:
        Normalized size string
    """
    if not size:
        return size

    size = size.strip()

    # Add space between number and unit if missing
    size = re.sub(r'(\d)([a-zA-Z])', r'\1 \2', size)

    # Normalize multiple spaces
    size = re.sub(r'\s+', ' ', size)

    # Lowercase units (but keep numbers as-is)
    parts = size.split()
    if len(parts) >= 2:
        # Number + unit(s)
        number = parts[0]
        units = ' '.join(parts[1:]).lower()
        size = f"{number} {units}"

    return size


def should_extract_size(name: str, size: str) -> bool:
    """
    Determine if size extraction should be attempted.

    Used by cleanup scripts to identify problematic records.

    Args:
        name: Product name
        size: Size field

    Returns:
        True if name likely contains size info that should be extracted OR has trailing noise
    """
    if not name:
        return False

    # Check for orphaned trailing numbers (OCR noise like "Tomatoes 0")
    orphaned_number_pattern = re.compile(r'\s+\d+\s*$')
    if orphaned_number_pattern.search(name):
        return True  # Need cleanup to remove noise

    # Check if name contains size patterns
    size_pattern = re.compile(
        r'\d+\.?\d*\s*(?:oz|lb|ct|count|ml|l|kg|g|gal|gallon|qt|quart|pt|pint|fl\s*oz|pack|pk)\s*$',
        re.IGNORECASE
    )

    size_word_pattern = re.compile(
        r'\s+(gallon|half\s*gallon|quart|pint|dozen|ea|each)\s*$',
        re.IGNORECASE
    )

    per_unit_pattern = re.compile(
        r'\s+per\s+(lb|oz|kg|g|gram|pound|ounce)\s*$',
        re.IGNORECASE
    )

    count_range_pattern = re.compile(
        r'\s+(\d{1,3}-\d{1,3})\s*$',
        re.IGNORECASE
    )

    # Check if name contains ANY size patterns
    name_has_size = bool(
        size_pattern.search(name) or
        size_word_pattern.search(name) or
        per_unit_pattern.search(name) or
        count_range_pattern.search(name)
    )

    if not name_has_size:
        return False  # Name doesn't have size info, nothing to extract

    # Name has size pattern - we should ALWAYS extract it to clean the name
    # Examples:
    # - "Raspberries 12 oz" + size="12 oz" → Clean name to "Raspberries"
    # - "Karela per lb" + size="" → Extract "per lb" to size field
    # - "Blueberries 18 oz" + size="per oz" → Replace size with extracted "18 oz"
    return True
