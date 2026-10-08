# agent_searchforlead

Lead agent for MindLab Future AI (Shopify Partner). It finds Filipino sellers on Facebook who have **no Shopify store**, scores them, and drafts a first message offering a basic store setup. POPLoad (bank transfer / GCash / Maya receipt uploads) comes in a follow-up once their store exists.

## How it works

```
search / import  ->  check  ->  score  ->  draft  ->  export CSV  ->  you review & send
```

1. **Discover**: Google-style search (`site:facebook.com` + niche + PH location, via Serper or Brave) or `import` a CSV of pages you found by hand. Optional Meta Graph Pages Search stub in `search.py`.
2. **Check**: opens the seller's *own* linked website and looks for Shopify markers (`cdn.shopify.com`, `myshopify.com`, headers). No link = Facebook-only. Shopee/Lazada-only is flagged separately.
3. **Score** (0-100): PH signals (₱, GCash, COD, city names), Taglish, selling language, plus no store. Existing Shopify stores score 0. 50+ becomes `qualified`.
4. **Draft**: English or Taglish message, with a STOP opt-out line.
5. **Export**: `data/leads.csv`, ranked.

## Usage

```bash
cp .env.example .env            # add SERPER_API_KEY (or BRAVE_API_KEY)
python -m leadagent search --niche skincare --location Cebu --max-queries 10
python -m leadagent import my_pages.csv     # columns: fb_url,name,snippet,website
python -m leadagent check && python -m leadagent score
python -m leadagent draft --limit 25
python -m leadagent export
python -m leadagent mark https://facebook.com/somepage contacted
python -m leadagent mark https://facebook.com/somepage do_not_contact --reason "asked to stop"
python -m unittest discover -s tests
```

Stdlib only, Python 3.10+. Data lives in `data/leads.db` (SQLite, git-ignored).

## Rules the agent follows (on purpose)

- **No Facebook scraping or logged-in bots.** Meta's terms forbid it and it gets your Page and personal account banned. Discovery goes through search APIs or your own manual finds.
- **No auto-sending.** Drafts are reviewed and sent by a person from your Page inbox, a few a day. Bulk unsolicited DMs trigger Facebook spam blocks.
- **Do-not-contact list** is checked on every insert. Honor STOP replies immediately with `mark ... do_not_contact`.
- **Philippine Data Privacy Act (RA 10173):** store only public business info (page name, public snippet, linked site), no personal profiles, and delete on request.
- Always verify a lead by eye before messaging (the `no_store` result for pages with no linked site only means "none found").

## Next steps

- Add Instagram/TikTok Shop discovery.
- Push `won` leads into your CRM (GoHighLevel) and track the POPLoad follow-up (`outreach.popload_followup`).
- Daily scheduled run that emails you the top 10 new drafts.
