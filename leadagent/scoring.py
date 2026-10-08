from . import config


def score_lead(lead):
    """0-100. Higher = Filipino-run, actively selling on Facebook, no Shopify store."""
    text = f"{lead['name']} {lead['snippet']}".lower() + " "
    score, notes = 0, []

    ph = sum(w in text for w in config.PH_WORDS)
    tl = sum(w in text for w in config.TAGALOG_WORDS)
    sell = sum(w in text for w in config.SELLING_WORDS)

    if ph:
        score += min(30, 10 + 5 * ph); notes.append(f"PH signals x{ph}")
    if tl:
        score += min(15, 5 * tl); notes.append(f"Tagalog/Taglish x{tl}")
    if sell:
        score += min(30, 8 * sell); notes.append(f"selling signals x{sell}")

    if str((lead["source"] if "source" in lead.keys() else "") or "").startswith("places:"):
        score += 25; notes.append("real, operating business on Google Maps")

    also = (lead["also_on"] if "also_on" in lead.keys() else "") or ""
    extra = len([x for x in also.split(";") if x.strip()])
    if extra:
        score += min(10, 5 * extra); notes.append(f"also on {extra} other platform(s)")
    plat = lead["platform"] if "platform" in lead.keys() else "facebook"
    status = lead["shopify_status"]
    if status == "has_shopify":
        return 0, "already on Shopify - skip"
    if status == "no_store":
        score += 25; notes.append("no store found")
    elif status == "marketplace_only":
        score += 20; notes.append(f"{plat} only - pays marketplace fees, owns no store")
    elif status == "unknown":
        score += 5; notes.append("site unreachable - verify")
    return min(score, 100), "; ".join(notes)


QUALIFY_AT = 50
MANUAL_SOURCES = ('dashboard', 'import')  # a person chose these, so they stay in the queue unless on Shopify
