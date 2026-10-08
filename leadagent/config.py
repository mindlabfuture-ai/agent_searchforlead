import os

NICHES = [
    "clothing", "apparel", "shoes", "bags", "jewelry", "accessories", "skincare",
    "beauty", "cosmetics", "perfume", "watches", "phone accessories", "gadgets",
    "home decor", "candles", "food", "snacks", "coffee", "pet supplies",
    "baby products", "plants", "handmade crafts", "activewear", "thrift",
]
LOCATIONS = [
    "Philippines", "Metro Manila", "Cebu", "Davao", "Quezon City", "Pampanga",
    "Cavite", "Laguna", "Iloilo", "Baguio", "Batangas",
]
QUERY_TEMPLATES = [
    'site:facebook.com "{niche}" "{loc}" "order now" OR "DM to order" OR "COD"',
    'site:facebook.com "{niche}" "{loc}" GCash OR Maya "shop"',
]

# Signals used for scoring (lowercase).
PH_WORDS = ["philippines", "pilipinas", "manila", "cebu", "davao", "quezon", "makati",
            "taguig", "pasig", "cavite", "laguna", "pampanga", "iloilo", "baguio",
            "batangas", "cagayan", "bacolod", "₱", "php", "gcash", "maya", "cod ",
            "lbc", "j&t", "jnt", "ship nationwide", "po box", "kuya", "ate "]
TAGALOG_WORDS = ["po ", "mga ", "lang", "naman", "paano", "magkano", "meron", "available po",
                 "dm po", "pm po", "pwede", "salamat", "sulit", "murang", "nationwide"]
SELLING_WORDS = ["order", "dm to order", "pm to order", "shop", "for sale", "price", "cod",
                 "gcash", "maya", "free shipping", "ship", "available", "preorder", "pre-order",
                 "reseller", "dropship", "mop"]
# Hosts we never treat as the seller's own store.
NON_STORE_HOSTS = ["facebook.com", "fb.com", "fb.me", "instagram.com", "tiktok.com",
                   "linktr.ee", "lnk.bio", "beacons.ai", "shopee.", "lazada.", "wa.me",
                   "m.me", "messenger.com", "youtube.com", "youtu.be", "twitter.com", "x.com"]

DB_PATH = os.environ.get("LEADS_DB", "data/leads.db")


def env(name, default=""):
    return os.environ.get(name, default)
