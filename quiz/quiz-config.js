/* ============================================================================
   OLD QUARTER COFFEE — "DISCOVER THE BEST COFFEE FOR YOU" QUIZ CONFIG
   ============================================================================
   This file is the entire quiz definition. Edit it to change questions,
   choices, matching rules, sizing logic, or copy — no code changes needed.

   HOW MATCHING WORKS
   ------------------
   Products are fetched LIVE from the store (only active, published,
   in-stock products are ever shown). Each product is matched against the
   customer's answers using its Shopify tags:

     match.anyTags     product must have AT LEAST ONE of these tags
     match.excludeTags product must have NONE of these tags
     (empty anyTags = the choice accepts any product, e.g. "Other")

   Every question with matching rules acts as a filter, and the results are
   the products that pass ALL of them. If the combination filters everything
   out, the questions listed in `fallbackRelaxOrder` are relaxed one at a
   time (a friendly note is shown to the customer when this happens).
   ============================================================================ */

window.OQ_QUIZ_CONFIG = {

  /* ---- Where product data comes from -------------------------------------
     "collections": storefront collection handles fetched via the store's own
     /collections/<handle>/products.json endpoint (no API key needed — works
     because the quiz runs on a page of the store itself). Only ACTIVE
     products published to the Online Store are returned, so the quiz always
     reflects the current lineup automatically. */
  dataSource: {
    collections: ['coffees'],       // "Specialty Coffee" = tag Coffee, minus gifts/subscriptions
    // Optional dev/preview override: URL of a static products.json snapshot.
    // Used automatically when the quiz is NOT running on the store domain.
    staticDataUrl: null,
    storeDomain: 'oldquartercoffee.com.au',
  },

  /* ---- Which products are quiz-eligible at all ----------------------------
     A product must pass this to ever be recommended. Requiring a
     "Best Enjoyed_" tag keeps bundles/gear/gift cards out automatically. */
  eligibility: {
    anyTags: ['Best Enjoyed_Milk', 'Best Enjoyed_Black', 'Best Enjoyed_Both Ways'],
    // NOTE: don't exclude the "Gift" tag here — regular coffees carry it for
    // the gift smart-collections. Gift cards, bundles and subscriptions are
    // already excluded because they have no "Best Enjoyed_" tag and the
    // source collection filters them out.
    excludeTags: ['Gift Card Subscriptions'],
    hideSoldOut: true,              // skip products with no purchasable variants
  },

  /* ---- Ranking ------------------------------------------------------------
     Products with these tags float to the top of the results, in order. */
  boostTags: ['__label:Best Seller', 'Limited Release'],

  /* ---- The questions ------------------------------------------------------ */
  questions: [
    {
      id: 'style',
      title: 'Do you prefer milk, black or coffee both ways?',
      type: 'filter',
      choices: [
        {
          id: 'milk',
          label: 'Milk',
          match: {
            anyTags: ['Best Enjoyed_Milk'],
            // Filter roasts are black-coffee products — never with milk.
            excludeTags: ['Roast Type_Filter', 'Filter'],
          },
        },
        {
          id: 'black',
          label: 'Black',
          match: { anyTags: ['Best Enjoyed_Black'], excludeTags: [] },
        },
        {
          id: 'both',
          label: 'Both Ways',
          match: {
            anyTags: ['Best Enjoyed_Both Ways'],
            excludeTags: ['Roast Type_Filter', 'Filter'],
          },
        },
      ],
    },

    {
      id: 'brew',
      title: 'How do you brew coffee at home?',
      subtitle: 'Select every way you brew — we’ll match coffees to each',
      type: 'filter',
      // Multi-select: a product matches if it suits ANY selected method, and
      // result cards show which of the customer's methods each coffee suits.
      // Coffees that suit more of their methods rank higher.
      multiSelect: true,
      choices: [
        // Espresso accepts either the espresso roast tag or the espresso brew
        // tag — some coffees (e.g. named "(Espresso)") carry only the latter.
        { id: 'espresso',  label: 'Espresso',   match: { anyTags: ['Roast Type_Espresso', 'Brew Method_Espresso'], excludeTags: [] } },
        { id: 'aeropress', label: 'AeroPress',  match: { anyTags: ['Brew Method_Aeropress'],  excludeTags: [] } },
        { id: 'plunger',   label: 'Plunger',    match: { anyTags: ['Brew Method_Plunger'],    excludeTags: [] } },
        // Pour over + batch brew are filter-roast methods — OQ only batch
        // brews filter coffee, and filter is always served black. So these
        // options are hidden entirely for milk drinkers.
        { id: 'pourover',  label: 'Pour Over',  hideWhen: { style: ['milk'] }, match: { anyTags: ['Brew Method_Pour Over'],  excludeTags: [] } },
        { id: 'stovetop',  label: 'Stovetop',   match: { anyTags: ['Brew Method_Stovetop'],   excludeTags: [] } },
        { id: 'batchbrew', label: 'Batch Brew', hideWhen: { style: ['milk'] }, match: { anyTags: ['Brew Method_Batch Brew'], excludeTags: [] } },
        { id: 'coldbrew',  label: 'Cold Brew',  match: { anyTags: ['Brew Method_Cold Brew'],  excludeTags: [] } },
        // "Other" = no brew filter; any coffee that passed Q1 qualifies.
        { id: 'other',     label: 'Other',      match: { anyTags: [], excludeTags: [] } },
      ],
    },

    {
      id: 'cups',
      title: 'How many coffees do you brew at home per day?',
      type: 'size',
      // cupsLow = the conservative (low) end of the range; used in the
      // freshness formula so nobody is pushed into a bag too big to finish
      // inside the freshness window.
      choices: [
        { id: 'c12', label: '1-2', cupsLow: 1 },
        { id: 'c23', label: '2-3', cupsLow: 2 },
        { id: 'c34', label: '3-4', cupsLow: 3 },
        { id: 'c4p', label: '4+',  cupsLow: 4 },
      ],
    },
  ],

  // If a combination matches nothing, relax these question filters in order.
  fallbackRelaxOrder: ['brew'],

  /* ---- Bag size recommendation --------------------------------------------
     Coffee is fresh food: rested ~7 days, best finished within ~3 weeks,
     fading after ~4. The quiz recommends the LARGEST bag the customer will
     finish inside the freshness window:

       serves  = bag grams / gramsPerServe
       days    = serves / cups per day (low end of their range)
       recommend the biggest size where days <= freshnessWindowDays

     With 17.5 g/serve and a 21-day window this reproduces:
       1-2 cups -> 250g, 2-3 -> 500g, 3-4 -> 1kg, 4+ -> 1kg               */
  sizing: {
    gramsPerServe: 17.5,
    freshnessWindowDays: 21,
    optionNames: ['size'],          // product option names (lowercased) that hold the bag size
    sizes: [
      { label: '250g', grams: 250,  matches: ['250'] },
      { label: '500g', grams: 500,  matches: ['500'] },
      { label: '1kg',  grams: 1000, matches: ['1kg', '1 kg', '1000'] },
    ],
  },

  /* ---- Tag-derived badges shown on result cards --------------------------- */
  badges: [
    { prefix: 'Origin_',     format: '{v}' },
    { prefix: 'Processing_', format: '{v} Process' },
    { prefix: 'Roast Type_', format: '{v} Roast' },
  ],
  extraBadgeTags: { 'Limited Release': 'Limited Release', '__label:Best Seller': 'Best Seller' },

  /* ---- Copy ---------------------------------------------------------------- */
  copy: {
    introTitle: 'Discover the best coffee for you',
    introText: 'Three quick questions and we’ll narrow our current roasts down to the ones made for how you actually drink coffee.',
    introButton: 'Find my coffee',
    resultsTitle: 'Your coffee matches',
    resultsIntro: 'Fresh from our current lineup — every one of these suits how you brew and drink.',
    sizeExplainer: 'Based on {cupsLabel} coffees a day, we recommend the {size} bag — you’ll finish it while it’s at its freshest.',
    fallbackNote: 'None of our current roasts are tagged for that exact combination, so here are your closest matches based on how you take your coffee.',
    emptyNote: 'We couldn’t find a match in the current lineup — browse all our coffees instead.',
    addToCart: 'Add to cart',
    added: 'Added ✓',
    viewProduct: 'View product',
    viewCart: 'View cart',
    recommendedSize: 'Recommended',
    soldOut: 'Sold out',
    startOver: 'Start over',
    back: 'Back',
    continueLabel: 'Continue',
    greatFor: 'Great for {method}',
    ofLabel: 'Question {n} of {total}',
  },

  /* ---- Brand (pulled from the live Old Quarter theme) ---------------------- */
  brand: {
    headingFont: "'Graduate', cursive",
    bodyFont: "'Nunito Sans', sans-serif",
    accent: '#2c737f',        // OQ teal (CTA)
    accentHover: '#235c66',
    dark: '#000000',
    brown: '#8a4d36',         // section-title brown
    cream: '#fdfcf7',
    paper: '#ffffff',
    sale: '#ba2323',
    loadFonts: true,          // set false if the host page already loads these fonts
  },
};
