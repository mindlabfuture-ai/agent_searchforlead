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
# Priority order matters: `search --platform auto` tries Facebook first, then falls back down the list.
PLATFORM_QUERIES = {
    "facebook": [
        'site:facebook.com "{niche}" "{loc}" "order now" OR "DM to order" OR "COD"',
        'site:facebook.com "{niche}" "{loc}" GCash OR Maya "shop"',
    ],
    "instagram": [
        'site:instagram.com "{niche}" "{loc}" "DM to order" OR "COD" OR "GCash" OR "ship nationwide"',
        'site:instagram.com "{niche}" Philippines "shop" "link in bio"',
    ],
    "tiktok": [
        'site:tiktok.com/@ "{niche}" "{loc}" "shop" OR "order" OR "COD"',
    ],
    "shopee": [
        'site:shopee.ph "{niche}" "{loc}" shop official OR store',
    ],
    "lazada": [
        'site:lazada.com.ph/shop "{niche}" "{loc}"',
    ],
    "carousell": [
        'site:carousell.ph/u "{niche}" "{loc}"',
    ],
}

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
                   "linktr.ee", "lnk.bio", "beacons.ai", "shopee.", "lazada.", "carousell.", "wa.me",
                   "m.me", "messenger.com", "youtube.com", "youtu.be", "twitter.com", "x.com"]

# Shared hosts: the same host does NOT mean the same business, so never match on these.
GENERIC_HOSTS = ["bit.ly", "tinyurl.com", "goo.gl", "forms.gle", "docs.google.com", "sites.google.com",
                 "drive.google.com", "canva.site", "wa.me"]

DB_PATH = os.environ.get("LEADS_DB", "data/leads.db")


def env(name, default=""):
    return os.environ.get(name, default)


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def base_url():
    """Public URL of this service, used in unsubscribe links."""
    if env("BASE_URL"):
        return env("BASE_URL").rstrip("/")
    if env("RAILWAY_PUBLIC_DOMAIN"):
        return "https://" + env("RAILWAY_PUBLIC_DOMAIN")
    return ""
