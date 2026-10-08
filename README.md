# agent_searchforlead

Lead agent for MindLab Future AI (Shopify Partner). It finds Filipino sellers on Facebook (and fallback platforms) who have **no Shopify store**, scores them, and drafts a first message offering a basic store setup. POPLoad (bank transfer / GCash / Maya receipt uploads) comes in a follow-up once their store exists.

## How it works

```
search / import  ->  check  ->  score  ->  draft  ->  export CSV  ->  you review & send
```

1. **Discover**: Facebook first. If Facebook is blocked, returns nothing, or yields fewer than `--min-new` (default 5) new leads, `--platform auto` (the default) falls back through **Instagram -> TikTok -> Shopee -> Lazada -> Carousell**. Pick one with `--platform instagram` etc. Details: Google-style search (`site:facebook.com` + niche + PH location, via Serper or Brave) or `import` a CSV of pages you found by hand. Optional Meta Graph Pages Search stub in `search.py`.
2. **Check**: opens the seller's *own* linked website and looks for Shopify markers (`cdn.shopify.com`, `myshopify.com`, headers). No link = Facebook-only. Shopee/Lazada-only is flagged separately.
3. **Score** (0-100): PH signals (₱, GCash, COD, city names), Taglish, selling language, plus no store. Existing Shopify stores score 0. 50+ becomes `qualified`.
4. **Draft**: a message to send by hand (English, or Taglish if the page is written that way), with a STOP opt-out line. The *emails* are English only, see below.
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
python -m leadagent set-email 12 hello@theirshop.ph   # add a publicly listed email by hand
python -m leadagent send          # approved emails; dry run unless EMAIL_SENDING_ENABLED=true
python -m leadagent serve         # dashboard + scheduler (what Railway runs)
python -m unittest discover -s tests
```

Stdlib only, Python 3.10+ (the Docker image uses 3.12). Data lives in `data/leads.db` (SQLite, git-ignored).

## Adding leads by hand

Open **Import leads** on the dashboard (`/import`) to add one lead (URL, name, notes, website, email) or paste a CSV of up to 200 lines: `url,name,notes,website,email`. Only the URL is required, and a header row is optional. The CLI `import` command takes the same format.

- Duplicates, businesses that opted out, and addresses that bounced are skipped, and the page says why.
- New leads are checked, scored and drafted in the background, then show up in the queue. Leads you add yourself stay in the queue even with a low score, unless the business is already on Shopify.
- Only add an email you found publicly listed for the business.
- Lead websites are fetched to look for Shopify, so the fetcher only talks to public web addresses (http/https on ports 80/443). Internal, loopback, link-local and cloud-metadata addresses are refused, including via redirects.

## Follow-ups after you hand over a store

When a merchant accepts their transferred store, add them under **Clients** in the dashboard (`/clients`), or with `python -m leadagent client add "Glow PH" owner@glow.ph --store-url https://glowph.myshopify.com --popload installed`. That schedules four emails in the same brand, written in English:

| Day | Email | What it does |
|---|---|---|
| 0 | Welcome | Three things to do first. If POPLoad is not installed yet, the first item offers to set it up. |
| 7 | POPLoad check | A 3-step test of order, receipt upload, approve. Skipped if you marked POPLoad "in use". |
| 30 | Growth tips | Four ways to get first orders, plus a review request. |
| 60 | Next step | Custom touches, automations, VIPriority (early access) or a 15-minute call. |

These go to merchants who asked for the work, so they send on a schedule without per-email approval, but the same safeguards apply: unsubscribe link and `List-Unsubscribe` headers, bounces and unsubscribes stop the whole sequence, business hours only (Mon-Fri 9-17 PHT), and `FOLLOWUP_DAILY_CAP` (default 20). A step more than 7 days late is skipped rather than sent late, and no two follow-ups go to one client within 3 days. Sending uses the same `EMAIL_SENDING_ENABLED` switch as outreach, so it is a dry run until you turn it on. From the Clients page you can pause or resume a client, skip the next email, mark POPLoad installed or in use, or mark the client done. `python -m leadagent followups` runs whatever is due now.

## Email outreach with Resend

Every email (outreach and follow-ups) is **one English version**: native Tagalog speakers find Taglish email awkward, and a single version is one thing to review, test and keep honest. The copy lives in `OFFER`, `TEMPLATE` and `EXTRA` in `leadagent/emailing.py` and in `COPY` in `leadagent/followups.py`.

Qualified leads that have a **publicly listed business email** can get one personalized email. The agent finds addresses on the seller's own website or search snippet (never a personal profile), or you add one with `set-email ID address`.

```
daily job -> drafts -> you click "Approve email" in the dashboard -> sender emails it (Mon-Fri 9-17 PHT, capped)
```

The email is branded to match mindlabfuture-ai.com (dark navy card, brass accents, Space Grotesk headings, logo from `mindlabfuture-ai.com/img/logo-ml.png`; override with `LOGO_URL`). It is built from tables with inline styles so it holds up in Gmail, Outlook and Apple Mail, stacks cleanly on phones, and always ships with a plain-text version. It has no tracking pixels or tracked links: the only image is the logo and the button is a plain `mailto:`. The design lives in `leadagent/emailtemplate.py`.

Guardrails, all enforced in code:
- **Human approval per lead.** Nothing sends unless you approved that lead.
- **Dry run by default.** Set `EMAIL_SENDING_ENABLED=true` only after domain setup. `python -m leadagent send` previews until then.
- **One email per business, ever.** No automatic follow-ups. A repeat to the same lead or address is blocked, and Resend idempotency keys stop double-sends on retries.
- **One-click unsubscribe** (`List-Unsubscribe` + `List-Unsubscribe-Post`, RFC 8058) and a visible link. The page needs a button press, so mail scanners can't unsubscribe people by prefetching.
- **Auto-suppression.** Bounces and spam complaints (Resend webhook, signature-verified) and unsubscribes block the address for good and opt the business out on every channel. `mark ... do_not_contact` does the same.
- **Daily cap** (`EMAIL_DAILY_CAP`, default 20), spaced sends, no sending on weekends or at night.
- **Identity in every email:** your company, physical address, why they were contacted, and how to stop.

**Resend's terms prohibit unsolicited bulk and cold email**, and accounts that do it get suspended. This is built as low-volume, personalized, one-to-one B2B outreach, which is the defensible end of that line, but the risk is yours to judge. Keep volume low, use a dedicated sending subdomain (e.g. `mail.mindlabfuture-ai.com`) so a problem never touches your main domain's reputation, watch bounce and complaint rates in Resend, and stop if either climbs.

## Deploy on Railway

One service runs the approval dashboard, the unsubscribe page, the Resend webhook and a scheduler (daily search, check, score and draft, then business-hours sending). Data is SQLite on a Railway volume, so **keep one replica**.

1. New Railway project, deploy from this GitHub repo (`Dockerfile` and `railway.json` are included).
2. Add a **volume** mounted at `/data`.
3. Generate a public domain for the service.
4. Set variables from `.env.example`. At minimum: `ADMIN_PASSWORD`, plus `SERPER_API_KEY` for discovery. Enter secrets in Railway's dashboard, not in chat or git.
5. Open the domain, log in as `admin`, review the queue.

To turn email on:
1. In Resend, add and verify your sending domain (SPF, DKIM, and DMARC at least `p=none`). Use a subdomain.
2. Create a Resend webhook to `https://<your-domain>/webhooks/resend` for `email.delivered`, `email.bounced` and `email.complained`. Copy its signing secret to `RESEND_WEBHOOK_SECRET`.
3. Set `RESEND_API_KEY`, `SENDER_FROM_EMAIL`, `UNSUB_SECRET`, then `EMAIL_SENDING_ENABLED=true`. The service refuses to start live without them.
4. Ramp slowly: start at `EMAIL_DAILY_CAP=5` for a week or two and check bounces and complaints before raising it.

## Rules the agent follows (on purpose)

- **No scraping or logged-in bots on any platform** (Facebook, Instagram, TikTok, Shopee and Lazada all forbid it). Meta's terms forbid it and it gets your Page and personal account banned. Discovery goes through search APIs or your own manual finds.
- **No auto-sending of DMs.** Drafts are reviewed and sent by a person from your Page inbox, a few a day. Bulk unsolicited DMs trigger Facebook spam blocks. Email is the only automated channel, with the guardrails above. Messenger can't be used for cold outreach either: Meta's API only lets a Page message people who messaged it first.
- **Do-not-contact list** is checked on every insert. Honor STOP replies immediately with `mark ... do_not_contact`.
- **Philippine Data Privacy Act (RA 10173):** store only public business info (page name, public snippet, linked site), no personal profiles, and delete on request.
- Always verify a lead by eye before messaging (the `no_store` result for pages with no linked site only means "none found").

## Next steps

- Push `won` leads into your CRM (GoHighLevel) and track the POPLoad follow-up (`outreach.popload_followup`).
- Inbound Messenger/Instagram auto-replies for people who message your Page (see the Messenger note above).
