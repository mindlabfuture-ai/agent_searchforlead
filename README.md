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

Python 3.10+ (the Docker image uses 3.12). Standard library plus Pillow (`pip install -r requirements.txt`) for the store-preview images. Data lives in `data/leads.db` (SQLite, git-ignored).

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
- **At most two emails per business, ever:** the first email, and one store-preview email 7+ days later that you approve by hand (see below). Nothing else is sent to a lead. A repeat to the same lead or address is blocked, and Resend idempotency keys stop double-sends on retries.
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

## The assistant (your right hand)

Open **Assistant** in the dashboard (`/assistant`). It watches the whole system and tells you what needs you:
- **Health**: missing or weak settings, bounce and spam-complaint rates against Resend's limits, failed sends, a silent Resend webhook, a stalled daily search, demo sites that cannot be taken down. Worst first; the lead queue shows a red count when something is critical.
- **Needs you**: first emails and prospects waiting for approval, showcase previews to review, and a reminder to check your inbox for replies *before* a prospect's next email goes out (replies are not detected automatically; mark them).
- **Daily brief**: saved every morning (08:00 PHT, `BRIEF_HOUR_PHT`) and emailed to `OWNER_EMAIL` if you set it, plus an email when a new critical problem appears (at most once a day each). It cannot report a total outage: use Railway's healthcheck plus an uptime monitor on `/health` for that.
- **Suggestions**: things it thinks you should stop or tidy (reject a prospect, mark a reply, pause a client, take a demo down) wait for your **Confirm** click. Nothing runs before that.
- **Chat** (needs `ANTHROPIC_API_KEY`; `ASSISTANT_MODEL`, default `claude-opus-5-5`; `ASSISTANT_DAILY_LIMIT` questions a day, default 60): ask "what should I do today?", "which prospects look strongest?", "why did sending stop?". It reads live data with tools, can suggest actions, and can run internal tidy-up tasks (verify prospects, adopt Shopify leads, build previews, health check).

What it can never do, enforced in code and tests: approve a first email, a showcase email, a POPLoad sequence or a demo site, send anything to a lead, or publish. It can stop things; starting outreach stays yours, one by one. Text from leads and websites reaches the model only as tool data, and the worst it can lead to is a suggestion you can dismiss.

## The support inbox (moved here from sms-compliance)

The agent that watches `support@mindlabfuture-ai.com` now runs inside this service, under the assistant. It reads new mail and the website contact form, moves spam to an `Agent-Spam` folder, triages with Claude (category, priority, lead score, a one-line summary), tells you on Telegram and on the **Inbox** page, and drafts a reply that waits for your **Send** (you can edit it first). Quiet sales enquiries get gentle follow-up drafts after 2, 5 and 10 days, in business hours. What it may say is in `leadagent/inbox_knowledge.md`.

What changed from the standalone agent:
- **Replies to our own outreach are recognised** (by address, or by the business's own domain) and stop that lead's sequence on their own, so nobody is emailed again after answering. "Stop", "unsubscribe" and similar suppress the address for good, with no model involved.
- **A reply to someone we cold-emailed is always a draft**, even in `REPLY_MODE=auto`. Auto-replies stay limited to people who wrote in on their own, and never for urgent messages.
- **Nothing gets lost**: a message the model fails on is retried, then reported to you and left unread in the mailbox.
- Subjects and summaries are stored, never message bodies. The assistant sees counts, drafts and problems, and warns if the mailbox has not been read for 15 minutes.

**Switching over** (do it in this order, or two agents will answer the same mail): 1) stop the old `agent` service in the Railway project `mindlab-inbound-agent`; 2) copy its variables into this service (`ANTHROPIC_API_KEY`, `AGENT_MODEL`, `IMAP_HOST`, `SMTP_HOST`, `SMTP_PORT`, `MAIL_USER`, `MAIL_PASSWORD`, `MAIL_FROM_NAME`, `POLL_SECONDS`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `WEBHOOK_TOKEN`, `REPLY_MODE`, `AUTO_CONFIDENCE`, `NURTURE_DAYS`; same names, so a copy-paste works); 3) set `INBOX_ENABLED=true`; 4) in Netlify, point the contact form's outgoing webhook to `https://leads.mindlabfuture-ai.com/webhook/form?token=<WEBHOOK_TOKEN>`. Start with `REPLY_MODE=draft`.

## POPLoad prospects (existing Shopify stores)

A separate track for stores that already run on Shopify and take GCash or bank transfer, to offer POPLoad (customers upload the receipt, you approve it in one click). Open **POPLoad prospects** in the dashboard, or use `python -m leadagent prospects import FILE | verify | list | run`.

1. **Import** a CSV (`Merchant Brand, Niche / Products, Platform / Domain, Manual Payment Instructions`, or name, niche, website, notes). Stores already in the store-build list are skipped.
2. **Verification** reads each site's own pages (home, contact, payment, FAQ, refund policy): the domain must load, run on Shopify, show GCash/Maya/bank-transfer wording (ranked higher when it asks for proof of payment), and publish a business email. Emails come only from the site itself, never guessed. Lists written by an AI assistant often contain domains that do not exist; those are rejected here (the first list: 119 of 156 did not load).
   More prospects arrive on their own, no import needed: (a) any store-build lead whose website turns out to run Shopify is copied over (`prospects adopt`), and (b) once a day, if a search key is set, `PROSPECT_SEARCH_QUERIES` (default 4) searches use phrases manual-payment stores write on their own pages ("proof of payment" + GCash + order number, and similar), rotating through niches (`prospects discover`). Results are only candidates; the same verification decides.
3. **Approve** each verified prospect yourself. Approval starts a 3-email sequence: day 0 intro, day 4 "how it works", day 11 last note. Same brand template, unsubscribe link, suppression, business hours and one-click `List-Unsubscribe`. A reply, unsubscribe or bounce stops the rest; mark replies with *They replied*. Cap: `PROSPECT_DAILY_CAP` (default 10). Dry run until `EMAIL_SENDING_ENABLED=true`.

## Store previews (the day-7 email)

Seven days after the first email, a lead that has not replied can get one more email showing a mock-up of the store you would build for them. Nothing is sent until you approve it.

1. From day 5 the agent builds a draft preview for each contacted lead: it reads the lead's own website (theme colour, logo, products), picks a palette, and falls back to a generated logo and clearly labelled "(sample)" products.
2. Open **Previews** in the dashboard. For each lead you can upload page screenshots (used for the palette only, never shown or published), the logo, up to three products (name, price, photo), and set the store name, tagline, style, niche or exact brand colours. Uploaded product photos and the logo drive the palette; platform blue from screenshots is ignored.
3. The page shows the exact email. **Approve** it and it sends once the lead is 7+ days past the first email, inside business hours and the shared daily cap. Skip, mark replied or do-not-contact from the same page.
4. `theme.json` and a generated `logo.svg` can be downloaded per lead. CLI: `preview <lead_id>` rebuilds a preview, `showcase [--force]` sends approved ones that are due (dry run unless `EMAIL_SENDING_ENABLED=true`).

Uploads are decoded and re-encoded (metadata stripped, non-images rejected). Only the logo and product images used in the email are served, through signed unlisted links; screenshots are visible only to you after login. The agent never scrapes Facebook or Instagram, so those images come from you.

### Shopify theme ZIP

Also on a lead's preview page: **theme.zip**, **products.csv** and a **setup guide**. The theme is Shopify's Dawn 16.0.0 (vendored in `leadagent/theme_base/`, pinned to a commit) with their palette (Dawn's five colour schemes, checked for readable contrast), button and image shape by style, the logo in the header (their image, or the generated placeholder), and the home page heading and tagline. Upload it under Online Store > Themes > Add theme. `products.csv` imports the products as drafts (sample products are tagged `sample`; images are fetched from your signed image links, so `BASE_URL` must be public). The guide lists the steps through handover.

Dawn's licence only allows themes that work with Shopify and requires its notice to stay with the code; the zip includes `LICENSE.md`. The patches to Dawn fail loudly if its markup ever changes. The theme passes Shopify's `theme-check` with the same 9 findings as unmodified Dawn, but it has **not been uploaded to a live store**: open it in the theme editor before handing over.

### Demo site (a live preview at the business-name address)

On a lead's preview page, **Publish demo site** builds a small storefront from the same preview (their palette, logo, up to three products, asymmetric layout, mobile-first) and publishes it to Netlify at `<business-name>.mindlabfuture-ai.com` (`glow-ph-skin-co`, with `-2` if the name is taken). Set `NETLIFY_AUTH_TOKEN` in Railway (a personal access token; `NETLIFY_ACCOUNT_SLUG` if your sites live in a team, `DEMO_DOMAIN` if not `mindlabfuture-ai.com`). The domain's DNS must be on Netlify, and the same token needs access to that DNS zone. **Download demo site (zip)** gives the same files without publishing.

Guardrails, because it shows a real business's name on your domain: a banner on every page says it is a design preview and not a live store; nothing can be bought (Add to cart only shows a notice); sample products are labelled; `noindex`, `robots.txt` and an `X-Robots-Tag` header keep search engines out; no scripts or styles from other sites except Google Fonts; it is deleted after 30 days (extendable), and at once when the lead unsubscribes, bounces, is marked do-not-contact or lost, or the showcase is skipped. Only you publish; the day-7 email links to it only while it is live.

## Free local discovery (OpenStreetMap)

On by default and free: no key, no account, no prepayment. Each daily run asks the public Overpass servers (`OSM_DAILY_QUERIES`
searches a day, default 3) for shops in one of your cities that mappers tagged with a website, a Facebook or Instagram
page, an email or a phone, rotating through six shop groups (fashion, beauty, home, food, kids/pets/gifts, gadgets).
Turn it off with `OSM_DISCOVERY=false`; point it at your own server with `OVERPASS_URL` (comma-separated list).
Try it by hand: `python -m leadagent osm --group fashion --location "Cebu City"`.

A listing becomes a lead by what it carries: a Facebook / Instagram / Shopee page is an ordinary lead of that platform
(so Facebook pages turn up without touching Facebook), an own domain is a `web` lead checked for Shopify, and a listed
`email` tag is kept as the lead's public email. A listing with only a phone number is a `maps` lead (phone or in person).
Coverage of small Philippine online sellers is thinner than Google's: expect a steady trickle, not a flood. The public
servers are shared and sometimes busy, so each query tries several in turn and a failure is logged, never fatal.
Data (c) OpenStreetMap contributors, ODbL.

## Google Places discovery (optional, paid)

Set `GOOGLE_PLACES_API_KEY` and the daily run also asks Google's official Places API for local shops (`PLACES_DAILY_QUERIES`
searches a day, default 3, each following up to `PLACES_MAX_PAGES` pages of 20, default 2). Nothing is scraped. A listing
becomes a lead by what its website field holds:

- a Facebook / Instagram / Shopee / Lazada page: an ordinary lead of that platform, so Facebook pages are found without touching Facebook;
- the business's own domain: a `web` lead, checked for Shopify and for a public email like any other site;
- no website: a `maps` lead with its phone number in the notes. Google supplies no email, so these are phone or in-person leads until you add one (`set-email`).

Listings score +25 as real, operating businesses; ones already on Shopify are dropped. Try it by hand with
`python -m leadagent places --niche candles --location Cebu`. Places bills per request and more for website and phone
fields, so check the current price on Google's pricing page and set a budget alert in Google Cloud before raising the caps.

## Rules the agent follows (on purpose)

- **No scraping or logged-in bots on any platform** (Facebook, Instagram, TikTok, Shopee and Lazada all forbid it). Meta's terms forbid it and it gets your Page and personal account banned. Discovery goes through search APIs or your own manual finds.
- **No auto-sending of DMs.** Drafts are reviewed and sent by a person from your Page inbox, a few a day. Bulk unsolicited DMs trigger Facebook spam blocks. Email is the only automated channel, with the guardrails above. Messenger can't be used for cold outreach either: Meta's API only lets a Page message people who messaged it first.
- **Do-not-contact list** is checked on every insert. Honor STOP replies immediately with `mark ... do_not_contact`.
- **Philippine Data Privacy Act (RA 10173):** store only public business info (page name, public snippet, linked site), no personal profiles, and delete on request.
- Always verify a lead by eye before messaging (the `no_store` result for pages with no linked site only means "none found").

## Next steps

- Push `won` leads into your CRM (GoHighLevel) and track the POPLoad follow-up (`outreach.popload_followup`).
- Inbound Messenger/Instagram auto-replies for people who message your Page (see the Messenger note above).
