"""MCC -> spend category mapping.

Issuers in India classify spend by MCC; the category is what reward rules
and exclusions key off. This is a curated subset of ISO 18245 codes that
matter most for Indian card rewards.
"""
from __future__ import annotations

MCC_CATEGORIES: dict[str, str] = {
    # Groceries / supermarkets
    "5411": "grocery", "5422": "grocery", "5499": "grocery",
    # Dining / food delivery
    "5812": "dining", "5813": "dining", "5814": "dining",
    # Fuel
    "5541": "fuel", "5542": "fuel", "5983": "fuel",
    # Travel
    "3000": "airline", "4511": "airline",
    "3501": "hotel", "7011": "hotel",
    "4112": "rail", "4131": "travel", "4722": "travel", "4121": "cab",
    # Utilities & bills
    "4900": "utilities", "4814": "telecom", "4899": "telecom",
    # Insurance
    "6300": "insurance", "5960": "insurance",
    # Education
    "8211": "education", "8220": "education", "8299": "education",
    # Government / tax
    "9311": "government", "9399": "government", "9222": "government",
    # Rent & property
    "6513": "rent",
    # Wallet loads / quasi-cash
    "6540": "wallet", "6051": "quasi_cash", "6012": "quasi_cash",
    # Jewellery
    "5944": "jewellery",
    # Retail / ecommerce
    "5311": "department_store", "5651": "apparel", "5691": "apparel",
    "5732": "electronics", "5045": "electronics",
    "5399": "general_retail", "5999": "general_retail",
    "5912": "pharmacy",
    # Entertainment / OTT
    "7832": "entertainment", "4841": "entertainment", "5815": "digital",
    "5816": "digital", "5817": "digital", "5818": "digital",
}


def category_for(mcc: str) -> str:
    return MCC_CATEGORIES.get(mcc, "other")
