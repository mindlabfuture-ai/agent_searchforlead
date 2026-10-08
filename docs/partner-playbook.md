# Shopify Partner playbook: build, transfer, grow

How MindLab turns an outreach reply into a live store, partner commission and POPLoad revenue. Facts below come from Shopify's own help pages as read on 2026-10-08. Shopify can change them, so re-check anything you are about to promise.

## Why the offer is a "build and hand over" offer
- **Commission needs a transfer.** Partners earn on stores they build as a **client transfer store** (Dev Dashboard, Stores, menu, *Transfer store*) and hand to the merchant. Trial stores and plain collaborator access do not earn referral commission. [Transferring store ownership](https://help.shopify.com/en/partners/building-stores-for-merchants/transferring-store-ownership), [Partner earnings](https://help.shopify.com/en/partners/partner-program/how-to-earn).
- **No $1/month promo after a transfer.** Shopify's transfer page says: "After transfer to the client, the store isn't eligible for promotions or free trials." So the email makes no promo claim. The merchant picks and pays for a plan themselves when they take the store over.
- **Dev stores cannot be transferred.** Use a *client transfer store*.

## Steps
1. **Agree** with the merchant (reply to the email). Get their business name, products, prices, GCash/Maya/bank details, shipping method, and the address for the store settings (a wrong address can cause extra tax on their invoice).
2. **Create** a client transfer store in the Dev Dashboard. Build theme, products, pages, shipping, and a policy page.
3. **Install POPLoad** on it and set the payment method(s) the widget should appear for. Confirm POPLoad can be installed on a client transfer store at this point (it was still in Shopify review on the website). If it cannot yet, do not promise it in the email.
4. **Before transferring**, make sure Shopify Payments (even test mode), Shopify Balance, Credit and Capital are not active. The transfer fails if they are.
5. **Send the transfer** (*Transfer store*, opens Settings, General). The merchant accepts and chooses their plan. Starter and Lite plans do not earn commission.
6. **Ask for collaborator access** right after. Once the store is transferred it leaves your organization, so you need a collaborator request for ongoing work and for POPLoad support.
7. **Record it** in the lead list (status `won`) and note the date, plan and whether POPLoad is installed.

## Economics (check against your Partner Dashboard)
| Item | Amount | Notes |
|---|---|---|
| Referral commission | 20% of the monthly base platform fee, for 4 years | Calculated on fees actually paid, net of discounts. Excludes Starter and Lite. Payouts monthly with a 30-day hold. Shopify decides payouts at its discretion. |
| Example: Basic plan | about $5/month at $25/month, about $3.80 at the yearly rate | Shopify's Philippines pricing page lists Basic at $25/month monthly, $19/month yearly. |
| POPLoad subscription | about $9.70/month at $9.99 | App revenue share is 100% up to $1M, minus a 2.9% processing fee. This is the bigger earner per store. |
| Money-back guarantee exposure | up to 3 months of the plan, about $75 on Basic monthly | Compare with about $15 of commission over the same 3 months. Decide whether to cap it. |

**Confirm in the Partner Dashboard before counting on it:** search results disagreed on whether standard plans still earn the 20% after July 2025. The Help Center page says they do (excluding Starter and Lite). Check the "Partner earning model" FAQ it links to.

## Guarantee: write it down
The email promises: "if your store makes no sales in its first 3 months, I'll refund the Shopify fees you paid." Put the terms in writing before the first handover, for example: counted from the day the store goes live, "no sales" means no paid orders from real customers, refund on proof of the Shopify invoices, within 14 days of asking. Edit `OFFER_EN` and `OFFER_TL` in `leadagent/emailing.py` if the promise changes.

## Growth follow-up (planned)
Per store after handover: day 7 check POPLoad is working and the first payment-receipt flow has been tested; day 30 growth tips (product photos, Facebook/Instagram catalog links, GCash QR on the thank-you page) and a short review ask; day 80 a heads-up on the plan price and an upgrade or VIPriority suggestion where it fits (luxury items).
