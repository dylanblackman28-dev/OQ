# Old Quarter Coffee — Product Recommendation Quiz

A self-hosted replacement for the "Product Recommendation Quiz" Shopify app —
no usage limits, no per-response fees, fully on-brand.

The quiz asks the same three questions as the original:

1. **Do you prefer milk, black or coffee both ways?** → filters by
   `Best Enjoyed_*` tags (filter roasts are excluded for Milk / Both Ways —
   filter coffee is always served black). Choosing **Milk** also hides the
   Pour Over and Batch Brew options in question 2, since those are
   filter-roast methods.
2. **How do you brew coffee at home?** → **multi-select**: customers pick
   every method they use. A coffee matches if it suits any of them, coffees
   suiting more of their methods rank first, and each result card shows
   "✓ AeroPress"-style chips for the methods it matches (when more than one
   was selected). "Other" matches everything.
3. **How many coffees do you brew at home per day?** → recommends a bag size
   using the freshness formula (see below)

## Why it's always up to date

Product data is fetched **live** from the store's own
`/collections/coffees/products.json` endpoint every time the quiz runs. That
endpoint only returns **active products published to the Online Store**, so:

- deactivate a product → it disappears from recommendations immediately
- publish a new roast with the right tags → it appears immediately
- a coffee sells out completely → it's skipped automatically

No API keys, no tokens, no syncing, nothing to maintain.

## Where it's installed

**Live page:** https://oldquartercoffee.com.au/pages/find-your-coffee
("Discover The Best Coffee For You", created via the Admin API.)

The page body is a small stub that loads `quiz.css`, `quiz-config.js` and
`quiz.js` from this repo via jsDelivr, **pinned to a specific commit SHA**
(immutable, permanently cached). To ship a quiz change:

1. Edit `quiz/quiz-config.js` (or the other files), commit and push.
2. Update the three `@<sha>` references in the page body (Shopify admin →
   Online Store → Pages → "Discover The Best Coffee For You" → edit HTML) to
   the new commit SHA — or ask Claude to do it via the API.

### Alternative: fully self-contained embed (no CDN)

`quiz/dist/oq-quiz-embed.html` bundles everything into one block. Paste its
entire contents into a Custom Liquid section (or the page body) instead of
the script stub — no external hosting involved. Rebuild it after config
edits with `python3 scripts/build_quiz_embed.py`.

## Editing the quiz

Everything editable lives in **`quiz/quiz-config.js`** — questions, choices,
tag rules, sizing formula, all customer-facing copy, and brand colours/fonts.
`quiz/quiz.js` (engine) and `quiz/quiz.css` (styles) shouldn't need touching
for content changes.

After editing, rebuild the single-file embed and re-paste it into the theme:

```bash
python3 scripts/build_quiz_embed.py
# then copy quiz/dist/oq-quiz-embed.html into the Custom Liquid section again
```

Common edits:

- **New brew method / choice**: add a `{ id, label, match: { anyTags: [...] } }`
  entry to the `brew` question. Any product carrying one of those tags matches.
- **Exclude decaf from a choice**: add `'Processing_Decaf'` to that choice's
  `excludeTags`.
- **Change how many/which products rank first**: edit `boostTags`.
- **Change the freshness maths**: edit `sizing` (grams per serve, window days,
  or the size list).

## Bag size recommendation (the freshness formula)

Coffee is fresh food — rested ~1 week, best finished within ~3 weeks. The quiz
recommends the **largest bag the customer will finish inside the window**,
using the low end of their cups-per-day range so nobody gets pushed into a bag
that goes stale:

```
serves = bag grams / 17.5g per serve
days   = serves / cups per day
recommend the biggest size where days <= 21
```

Which reproduces: 1–2 cups → 250g · 2–3 → 500g · 3–4 → 1kg · 4+ → 1kg.
The results page states the recommendation simply ("Based on 2-3 coffees a
day, we recommend the 500g bag…") without surfacing the maths.

## How matching works (mirrors the old app's Link Collections)

| Question | Choice | Matches products tagged | Excludes |
|---|---|---|---|
| Style | Milk | `Best Enjoyed_Milk` | `Roast Type_Filter`, `Filter` |
| Style | Black | `Best Enjoyed_Black` | — |
| Style | Both Ways | `Best Enjoyed_Both Ways` | `Roast Type_Filter`, `Filter` |
| Brew | Espresso | `Roast Type_Espresso` **or** `Brew Method_Espresso` | — |
| Brew | AeroPress…Cold Brew | matching `Brew Method_*` tag | — |
| Brew | Pour Over / Batch Brew | matching `Brew Method_*` tag | hidden when Q1 = Milk |
| Brew | Other | any coffee | — |

Results are the intersection of the style filter with the union of the
selected brew methods. If a combination matches nothing in the current
lineup, the brew filter is relaxed and a friendly note is shown instead of
an empty page — the style filter (and its filter-roast exclusion) is never
relaxed, so a Milk/Both Ways customer can never be shown a filter roast.

Hiding a choice based on an earlier answer is config-driven:
`hideWhen: { style: ['milk'] }` on any choice.

Products must carry a `Best Enjoyed_*` tag to be quiz-eligible at all — this
is what keeps bundles, gift cards, gear and subscriptions out. **When adding a
new coffee, give it its `Best Enjoyed_*` and `Brew Method_*` tags and it will
slot straight into the quiz.**

Note: the original app matched Espresso via `Roast Type_Espresso` + the Blends
collection. Some current espresso coffees (e.g. "Laos - La's Java Honey
(Espresso)") only carry `Brew Method_Espresso`, so this quiz accepts either
tag. Change it in `quiz-config.js` if you'd rather match strictly.

## Files

| File | Purpose |
|---|---|
| `quiz-config.js` | The entire quiz definition — the only file you edit |
| `quiz.js` | Widget engine (fetch, match, rank, render, add-to-cart) |
| `quiz.css` | Styles (scoped under `.oqq`, brand tokens as CSS variables) |
| `index.html` | Standalone dev preview (`?data=snapshot.json` for off-store testing) |
| `dist/oq-quiz-embed.html` | Built single-file embed — what you paste into Shopify |
| `../scripts/build_quiz_embed.py` | Rebuilds the embed after config edits |

## Extras built in

- **Add to cart** uses Shopify's AJAX cart (`/cart/add.js`) with the customer's
  chosen size variant preselected; off-store it degrades to a product-page link.
- **Analytics**: pushes `oq_quiz_start`, `oq_quiz_answer`, `oq_quiz_results`
  and `oq_quiz_add_to_cart` events to `window.dataLayer` — ready for GTM /
  Microsoft Clarity funnels whenever you want them.
- **Sold-out handling**: fully sold-out products are hidden; sold-out sizes are
  shown struck-through and disabled.
- **Ranking**: `__label:Best Seller` then `Limited Release` products first.
