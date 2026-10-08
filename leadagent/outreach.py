"""Drafts only. A human reviews and sends every message from the business's own Page/inbox."""
from . import config

EN = """Hi {name}! I came across your page and love what you're selling.

I run {company}, a Shopify Partner based in Taguig. We set up simple online stores for Filipino sellers so customers can order and pay without messaging back and forth: product pages, GCash/Maya/bank transfer checkout, and shipping set up.

Would you like a quick look at what a basic store for {name} could look like? No obligation, and you own the store and the account.

- {sender}, {company}
Reply STOP and I won't message again."""

TL = """Hi po {name}! Napadaan po ako sa page ninyo, ang ganda ng products.

Ako po si {sender} ng {company}, Shopify Partner sa Taguig. Tumutulong po kami mag-set up ng simpleng online store para sa Filipino sellers: product pages, GCash/Maya/bank transfer checkout, at shipping, para hindi na po kailangan ng back-and-forth sa DM.

Gusto po ba ninyong makita kung ano ang itsura ng basic store para sa {name}? Libre po ang preview, at sa inyo po ang store at account.

- {sender}, {company}
Reply STOP po at hindi na ako mag-message ulit."""

FOLLOWUP_POPLOAD = """Hi {name}, quick follow-up. Once your store is live, we also offer POPLoad: customers pay by bank transfer, GCash or Maya, upload their receipt, and you approve it in one click so the order is marked paid. The first 10 uploads are free.

Happy to show it on your store. - {sender}"""


def is_taglish(lead):
    t = f"{lead['name']} {lead['snippet']}".lower() + " "
    return sum(w in t for w in config.TAGALOG_WORDS) >= 2


def draft(lead):
    ctx = dict(name=lead["name"] or "there", company=config.env("SENDER_COMPANY", "MindLab Future AI"),
               sender=config.env("SENDER_NAME", "Mark"))
    return (TL if is_taglish(lead) else EN).format(**ctx)


def popload_followup(lead):
    return FOLLOWUP_POPLOAD.format(name=lead["name"] or "there", sender=config.env("SENDER_NAME", "Mark"))
