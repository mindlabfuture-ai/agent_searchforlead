# agent_searchforlead

Lead agent for MindLab Future AI (Shopify Partner). It finds Filipino sellers on Facebook (and fallback platforms) who have **no Shopify store**, scores them, and drafts a first message offering a basic store setup. POPLoad (bank transfer / GCash / Maya receipt uploads) comes in a follow-up once their store exists.

## How it works

```
search / import  ->  check  ->  score  ->  draft  ->  export CSV  ->  you review & send
```

1. **Discover**: Facebook first. If Facebook is blocked, returns nothing, or yields fewer than `--min-new` (default 5) new leads, `--platform auto` (the default) falls back through **Instagram -> TikTok -> Shopee -> Lazada -> Carousell**. Pick one with `--platform instagram` etc. Details: Google-style search (`site:facebook.com` + niche + PH location, via Serper or Brave) or `import` a CSV of pages you found by hand. Optional Meta Graph Pages Search stub in `search.py`.
2. **Check**: opens the seller's *own* linked website and looks for Shopify markers (`cdn.shopify.com`, `myshopify.com`, headers). No link = Facebook-only. Shopee/Lazada-only is flagged separately.
3. **Score** (0-100): PH signals (₱, GCash, COD, city names), Taglish, selling language, plus no store. Existing Shopify stores score 0. 50+ becomes `qualified`.
4. **Draft**: English or Taglish message, with a STOP opt-out line.
### Duplicates across platforms
The same business often shows up on Facebook, Instagram and Shopee. `dedupe` merges them into one lead, so you only get one draft and never message a business twice. It runs automatically after `search` and `import`.

| Evidence | Action |
|---|---|
| Same website host, or the same handle on different platforms (`glowph` / `glow.ph` / `glow_ph`) | **Merged automatically** |
| Same business name only (e.g. two "Kapeng Bukid" pages) | **Listed for review**; merge with `python -m leadagent merge ID ID` |

What a merge does:
- Keeps the most advanced profile as the main lead (contacted beats new) and marks the others `merged`. They stay in the database, so re-discovering them won't create a duplicate.
- Combines snippets and websites, lists the other profiles in `also_on`, and adds a small score bonus for being on several platforms.
- If any profile is on Shopify, the whole business is skipped. If any profile opted out, they all are.
- When a merge brings in a new website, the store status resets to `unchecked`. Run `check` and `score` again afterwards.

`python -m leadagent dedupe --dry-run` previews without changing anything. Name-only matches are never merged automatically, because generic names ("Online Shop") would join unrelated sellers.

### Qualifying alternatives
| Source | Why it qualifies | Store check |
|---|---|---|
| Instagram / TikTok | Sells via DM or live, link in bio | Opens bio website if present; none = social-only |
| Shopee / Lazada / Carousell | Pays marketplace fees, owns no brand store | No site = `marketplace_only` (+20 score); draft adds a "no fees, own your customers" line |

Every platform gets the same PH/Taglish/selling-signal scoring. Pages already on Shopify score 0. Profile URLs are normalized per platform (product, post and reel URLs are rejected) and deduplicated.

5. **Export**: `data/leads.csv`, ranked.

## Usage

```bash
cp .env.example .env            # add SERPER_API_KEY (or BRAVE_API_KEY)
python -m leadagent search --niche skincare --platform auto --location Cebu --max-queries 10
python -m leadagent import my_pages.csv     # columns: url,name,snippet,website; any supported platform
python -m leadagent check && python -m leadagent score
python -m leadagent draft --limit 25
python -m leadagent export
python -m leadagent mark https://facebook.com/somepage contacted
python -m leadagent mark https://facebook.com/somepage do_not_contact --reason "asked to stop"
python -m unittest discover -s tests
```

Stdlib only, Python 3.10+. Data lives in `data/leads.db` (SQLite, git-ignored).

## Rules the agent follows (on purpose)

- **No scraping or logged-in bots on any platform** (Facebook, Instagram, TikTok, Shopee and Lazada all forbid it). Meta's terms forbid it and it gets your Page and personal account banned. Discovery goes through search APIs or your own manual finds.
- **No auto-sending.** Drafts are reviewed and sent by a person from your Page inbox, a few a day. Bulk unsolicited DMs trigger Facebook spam blocks.
- **Do-not-contact list** is checked on every insert. Honor STOP replies immediately with `mark ... do_not_contact`.
- **Philippine Data Privacy Act (RA 10173):** store only public business info (page name, public snippet, linked site), no personal profiles, and delete on request.
- Always verify a lead by eye before messaging (the `no_store` result for pages with no linked site only means "none found").

## Next steps

- Push `won` leads into your CRM (GoHighLevel) and track the POPLoad follow-up (`outreach.popload_followup`).
- Daily scheduled run that emails you the top 10 new drafts.
