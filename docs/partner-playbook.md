# Shopify Partner playbook: build, transfer, grow

How MindLab turns an outreach reply into a live store, partner commission and POPLoad revenue. Facts below come from Shopify's own help pages as read on 2026-10-08. Shopify can change them, so re-check anything you are about to promise.

## Why the offer is a "build and hand over" offer
- **Commission needs a transfer.** Partners earn on stores they build as a **client transfer store** (Dev Dashboard, Stores, menu, *Transfer store*) and hand to the merchant. Trial stores and plain collaborator access do not earn referral commission. [Transferring store ownership](https://help.shopify.com/en/partners/building-stores-for-merchants/transferring-store-ownership), [Partner earnings](https://help.shopify.com/en/partners/partner-program/how-to-earn).
- **No $1/month promo after a transfer.** Shopify's transfer page says: "After transfer to the client, the store isn't eligible for promotions or free trials." So the email makes no promo claim. The merchant picks and pays for a plan themselves when they take the store over.
- **Dev stores cannot be transferred.** Use a *client transfer store*.

## Steps
1. **Agree** with the merchant (reply to the email). Get their business name, products, prices, GCash/Maya/bank details, shipping method, and the address for the store settings (a wrong address can cause extra tax on their invoice).
2. **Create** a client transfer store in the Dev Dashboard. Build theme, products, pages, shipping, and a policy page.
3. **Install POPLoad** on it and set the payment method(s) the widget should appear for. See "POPLoad on a transfer store" below: it should work, but there are catches to check first.
4. **Before transferring**, make sure Shopify Payments (even test mode), Shopify Balance, Credit and Capital are not active. The transfer fails if they are.
5. **Send the transfer** (*Transfer store*, opens Settings, General). The merchant accepts and chooses their plan. Starter and Lite plans do not earn commission.
6. **Ask for collaborator access** right after. Once the store is transferred it leaves your organization, so you need a collaborator request for ongoing work and for POPLoad support.
7. **Record it** in the lead list (status `won`) and note the date, plan and whether POPLoad is installed.

## POPLoad on a transfer store
Yes, you can build the store with POPLoad installed and then transfer it. Three things to know:
1. **Installable?** While a client transfer store is in your organization, Shopify says "you can only install free apps and partner-friendly apps. Custom and draft apps can't be installed." It does not define "partner-friendly". POPLoad is still in Shopify's review (the email calls it "early access"), and community reports say apps awaiting review can be blocked from installing. **Test the install on your first transfer store before promising it to a merchant.** If it is blocked, the fallback is to hand the store over first, then install POPLoad from the merchant's side with a collaborator request, and say so in the email.
2. **Billing before and after.** POPLoad's own code (`web/lib/billing.js`) creates *test* subscriptions on development stores and real ones only when `BILLING_TEST=false` on a live shop. Test charges are cancelled when a store goes live, so the merchant must approve a real plan after the handover. Keep the store on POPLoad's free tier (10 uploads) while you build, then tell the merchant about the monthly charge before they accept the transfer. Paid plans can't be approved on client transfer stores anyway.
3. **After transfer.** The store leaves your organization. Ask for collaborator access immediately. Consider handling Shopify's `shop/update` webhook in POPLoad to notice the switch from development to live.

Before transferring: no real transactions can run on a transfer store, the online store stays in private mode, and Shopify Payments, Balance, Credit and Capital must be off.

## Economics (check against your Partner Dashboard)
| Item | Amount | Notes |
|---|---|---|
| Referral commission | 20% of the monthly base platform fee, for 4 years | Calculated on fees actually paid, net of discounts. Excludes Starter and Lite. Payouts monthly with a 30-day hold. Shopify decides payouts at its discretion. |
| Example: Basic plan | about $5/month at $25/month, about $3.80 at the yearly rate | Shopify's Philippines pricing page lists Basic at $25/month monthly, $19/month yearly. |
| POPLoad subscription | about $9.70/month at $9.99 | App revenue share is 100% up to $1M, minus a 2.9% processing fee. This is the bigger earner per store. |

**Confirm in the Partner Dashboard before counting on it:** search results disagreed on whether standard plans still earn the 20% after July 2025. The Help Center page says they do (excluding Starter and Lite). Check the "Partner earning model" FAQ it links to.

## No guarantee
The offer has no money-back guarantee. The merchant gets a free build and early access to POPLoad, and pays only Shopify (their plan) and any domain. Your cost per store is build time, so time-box it: one cloned template store, a fixed product limit, about 3 hours.

## Growth follow-up (planned)
Per store after handover: day 7 check POPLoad is working and the first payment-receipt flow has been tested; day 30 growth tips (product photos, Facebook/Instagram catalog links, GCash QR on the thank-you page) and a short review ask; day 80 a heads-up on the plan price and an upgrade or VIPriority suggestion where it fits (luxury items).
