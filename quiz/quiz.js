/* ============================================================================
   OLD QUARTER COFFEE QUIZ — ENGINE
   ----------------------------------------------------------------------------
   Self-contained, dependency-free widget. Reads window.OQ_QUIZ_CONFIG
   (quiz-config.js) and renders into <div id="oq-coffee-quiz"></div>.

   Product data is fetched live from the store's own
   /collections/<handle>/products.json endpoint, which only ever returns
   ACTIVE products published to the Online Store — so recommendations always
   reflect the current lineup with no syncing and no API keys.

   You should not need to edit this file to change the quiz — edit
   quiz-config.js instead.
   ============================================================================ */
(function () {
  'use strict';

  var MOUNT_ID = 'oq-coffee-quiz';

  function ready(fn) {
    if (document.readyState !== 'loading') fn();
    else document.addEventListener('DOMContentLoaded', fn);
  }

  /* ---------- tiny DOM helper ---------- */
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        if (k === 'text') node.textContent = attrs[k];
        else if (k === 'html') node.innerHTML = attrs[k];
        else if (k.indexOf('on') === 0 && typeof attrs[k] === 'function') node.addEventListener(k.slice(2), attrs[k]);
        else if (attrs[k] !== null && attrs[k] !== undefined) node.setAttribute(k, attrs[k]);
      });
    }
    (children || []).forEach(function (c) { if (c) node.appendChild(c); });
    return node;
  }

  function fmt(template, vars) {
    return template.replace(/\{(\w+)\}/g, function (_, k) {
      return vars[k] !== undefined ? vars[k] : '{' + k + '}';
    });
  }

  function track(event, data) {
    try {
      window.dataLayer = window.dataLayer || [];
      window.dataLayer.push(Object.assign({ event: 'oq_quiz_' + event }, data || {}));
    } catch (e) { /* analytics must never break the quiz */ }
  }

  /* ==========================================================================
     DATA LAYER
     ========================================================================== */
  function onStoreDomain(cfg) {
    var host = window.location.hostname;
    return host === cfg.dataSource.storeDomain ||
      host === 'www.' + cfg.dataSource.storeDomain ||
      /\.myshopify\.com$/.test(host);
  }

  function fetchJson(url) {
    return fetch(url, { credentials: 'same-origin' }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status + ' for ' + url);
      return r.json();
    });
  }

  function fetchCollection(handle) {
    var all = [];
    function page(n) {
      return fetchJson('/collections/' + handle + '/products.json?limit=250&page=' + n)
        .then(function (data) {
          var items = (data && data.products) || [];
          all = all.concat(items);
          if (items.length === 250 && n < 5) return page(n + 1);
          return all;
        });
    }
    return page(1);
  }

  function loadRawProducts(cfg) {
    if (window.OQ_QUIZ_DATA && window.OQ_QUIZ_DATA.products) {
      return Promise.resolve(window.OQ_QUIZ_DATA.products);
    }
    if (onStoreDomain(cfg)) {
      return Promise.all(cfg.dataSource.collections.map(fetchCollection))
        .then(function (lists) { return [].concat.apply([], lists); });
    }
    if (cfg.dataSource.staticDataUrl) {
      return fetchJson(cfg.dataSource.staticDataUrl).then(function (d) { return d.products || []; });
    }
    return Promise.reject(new Error(
      'Not on ' + cfg.dataSource.storeDomain + ' and no staticDataUrl configured.'));
  }

  /* Normalize a /products.json product into what the quiz needs. */
  function normalize(cfg, raw) {
    var seen = {};
    var out = [];
    raw.forEach(function (p) {
      if (!p || seen[p.id]) return;
      seen[p.id] = true;
      var sizeOptIdx = -1;
      (p.options || []).forEach(function (o, i) {
        var name = ((o && o.name) || '').toLowerCase();
        var pos = (o && o.position) ? o.position - 1 : i;
        if (cfg.sizing.optionNames.indexOf(name) !== -1) sizeOptIdx = pos;
      });
      var variants = (p.variants || []).map(function (v) {
        return {
          id: v.id,
          title: v.title,
          price: parseFloat(v.price),
          available: v.available !== false,
          sizeValue: sizeOptIdx >= 0 ? (v['option' + (sizeOptIdx + 1)] || '') : '',
        };
      });
      out.push({
        id: p.id,
        title: p.title,
        handle: p.handle,
        tags: p.tags || [],
        image: (p.images && p.images[0] && (p.images[0].src || p.images[0].url)) || null,
        variants: variants,
        anyAvailable: variants.some(function (v) { return v.available; }),
      });
    });
    return out;
  }

  /* ==========================================================================
     MATCHING
     ========================================================================== */
  function hasAny(tags, list) {
    if (!list || !list.length) return true;
    return list.some(function (t) { return tags.indexOf(t) !== -1; });
  }
  function hasNone(tags, list) {
    if (!list || !list.length) return true;
    return !list.some(function (t) { return tags.indexOf(t) !== -1; });
  }
  function passes(product, match) {
    return hasAny(product.tags, match.anyTags) && hasNone(product.tags, match.excludeTags);
  }

  function eligible(cfg, products) {
    return products.filter(function (p) {
      if (cfg.eligibility.hideSoldOut && !p.anyAvailable) return false;
      return passes(p, cfg.eligibility);
    });
  }

  /* An answer is a single choice or (for multiSelect questions) an array of
     choices. A product passes a question if it passes ANY selected choice. */
  function asChoices(answer) {
    if (!answer) return [];
    return Array.isArray(answer) ? answer : [answer];
  }
  function passesQuestion(product, answer) {
    var choices = asChoices(answer);
    if (!choices.length) return true;
    return choices.some(function (c) { return passes(product, c.match); });
  }

  /* Which of the customer's selected choices does this product suit?
     (Choices with no anyTags — e.g. "Other" — match everything, so they are
     not interesting to report or count.) */
  function matchedChoices(product, answer) {
    return asChoices(answer).filter(function (c) {
      return c.match && c.match.anyTags && c.match.anyTags.length && passes(product, c.match);
    });
  }

  /* Returns { list, relaxedQuestions } applying all filter questions, then
     relaxing per cfg.fallbackRelaxOrder if everything got filtered out. */
  function computeResults(cfg, pool, answers) {
    var filterQs = cfg.questions.filter(function (q) { return q.type === 'filter'; });
    function run(skipIds) {
      return pool.filter(function (p) {
        return filterQs.every(function (q) {
          if (skipIds.indexOf(q.id) !== -1) return true;
          return passesQuestion(p, answers[q.id]);
        });
      });
    }
    var skipped = [];
    var list = run(skipped);
    var order = cfg.fallbackRelaxOrder || [];
    for (var i = 0; i < order.length && list.length === 0; i++) {
      skipped.push(order[i]);
      list = run(skipped);
    }
    list = rank(cfg, list, answers);
    return { list: list, relaxedQuestions: skipped.filter(function (id) {
      return list.length > 0; // only report relaxation if it produced results
    }) };
  }

  /* Rank: 1) coffees tagged exactly for the customer's answers (a choice's
     preferTags) first, 2) then coffees suiting more of their selected brew
     methods, 3) then boost tags (best sellers etc.), 4) original order. */
  function rank(cfg, list, answers) {
    var boosts = cfg.boostTags || [];
    var filterQs = cfg.questions.filter(function (q) { return q.type === 'filter'; });
    var multiQs = filterQs.filter(function (q) { return q.multiSelect; });
    function preferScore(p) {
      var n = 0;
      filterQs.forEach(function (q) {
        asChoices(answers[q.id]).forEach(function (c) {
          (c.preferTags || []).forEach(function (t) {
            if (p.tags.indexOf(t) !== -1) n++;
          });
        });
      });
      return -n; // more preferred-tag hits = earlier
    }
    function boostScore(p) {
      for (var i = 0; i < boosts.length; i++) {
        if (p.tags.indexOf(boosts[i]) !== -1) return i;
      }
      return boosts.length;
    }
    function methodScore(p) {
      var n = 0;
      multiQs.forEach(function (q) { n += matchedChoices(p, answers[q.id]).length; });
      return -n; // more matches = earlier
    }
    return list.map(function (p, i) { return { p: p, f: preferScore(p), m: methodScore(p), s: boostScore(p), i: i }; })
      .sort(function (a, b) { return a.f - b.f || a.m - b.m || a.s - b.s || a.i - b.i; })
      .map(function (x) { return x.p; });
  }

  /* ==========================================================================
     SIZING
     ========================================================================== */
  function recommendSize(cfg, cupsLow) {
    var s = cfg.sizing;
    var best = null;
    s.sizes.forEach(function (size) {
      var serves = size.grams / s.gramsPerServe;
      var days = serves / cupsLow;
      if (days <= s.freshnessWindowDays && (!best || size.grams > best.size.grams)) {
        best = { size: size, serves: Math.round(serves), days: Math.round(days) };
      }
    });
    if (!best) {
      var smallest = s.sizes[0];
      s.sizes.forEach(function (z) { if (z.grams < smallest.grams) smallest = z; });
      var sv = smallest.grams / s.gramsPerServe;
      best = { size: smallest, serves: Math.round(sv), days: Math.round(sv / cupsLow) };
    }
    return best;
  }

  function sizeOfVariant(cfg, variant) {
    var val = (variant.sizeValue || variant.title || '').toLowerCase().replace(/\s+/g, '');
    var found = null;
    cfg.sizing.sizes.forEach(function (size) {
      size.matches.forEach(function (m) {
        if (val.indexOf(m.toLowerCase().replace(/\s+/g, '')) !== -1) found = found || size;
      });
    });
    return found;
  }

  /* Pick the variant of `product` to preselect for recommended size. */
  function pickVariant(cfg, product, recSize) {
    var bySize = product.variants.filter(function (v) {
      var s = sizeOfVariant(cfg, v);
      return s && recSize && s.label === recSize.label && v.available;
    });
    if (bySize.length) return bySize[0];
    var avail = product.variants.filter(function (v) { return v.available; });
    return avail[0] || product.variants[0] || null;
  }

  /* ==========================================================================
     CART
     ========================================================================== */
  function addToCart(variantId) {
    return fetch('/cart/add.js', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify({ items: [{ id: variantId, quantity: 1 }] }),
    }).then(function (r) {
      if (!r.ok) return r.json().then(function (e) {
        throw new Error((e && (e.description || e.message)) || 'Could not add to cart');
      });
      return r.json();
    });
  }

  /* ==========================================================================
     UI
     ========================================================================== */
  function App(mount, cfg) {
    this.mount = mount;
    this.cfg = cfg;
    this.answers = {};
    this.step = -1; // -1 = intro
    this.products = null;
    this.onStore = onStoreDomain(cfg);
  }

  App.prototype.productUrl = function (product, variant) {
    var base = this.onStore ? '' : 'https://' + this.cfg.dataSource.storeDomain;
    var url = base + '/products/' + product.handle;
    if (variant) url += '?variant=' + variant.id;
    return url;
  };

  App.prototype.start = function () {
    var self = this;
    this.renderLoading();
    loadRawProducts(this.cfg)
      .then(function (raw) {
        self.products = eligible(self.cfg, normalize(self.cfg, raw));
        self.step = -1;
        self.render();
      })
      .catch(function (err) {
        self.renderError(err);
      });
  };

  App.prototype.shell = function (children) {
    var root = el('div', { 'class': 'oqq' }, children);
    this.mount.innerHTML = '';
    this.mount.appendChild(root);
    var b = this.cfg.brand || {};
    var map = {
      '--oqq-indigo': b.indigo, '--oqq-indigo-light': b.indigoLight,
      '--oqq-teal': b.teal, '--oqq-ocean': b.ocean, '--oqq-salmon': b.salmon,
      '--oqq-heading-font': b.headingFont, '--oqq-body-font': b.bodyFont,
    };
    Object.keys(map).forEach(function (k) { if (map[k]) root.style.setProperty(k, map[k]); });
    if (this.mount.getBoundingClientRect().top < 0) this.mount.scrollIntoView({ behavior: 'smooth' });
  };

  App.prototype.renderLoading = function () {
    this.shell([el('div', { 'class': 'oqq-loading' }, [
      el('div', { 'class': 'oqq-spinner' }),
      el('div', { text: 'Brewing up your quiz…' }),
    ])]);
  };

  App.prototype.renderError = function (err) {
    var c = this.cfg.copy;
    this.shell([el('div', { 'class': 'oqq-error' }, [
      el('p', { text: 'Sorry — the quiz couldn’t load our current coffees.' }),
      el('p', {}, [el('a', { href: '/collections/all-coffee', text: 'Browse all coffee instead' })]),
    ])]);
    if (window.console) console.error('[OQ quiz]', err);
  };

  App.prototype.progress = function () {
    var total = this.cfg.questions.length;
    var done = this.step < 0 ? 0 : this.step;
    var pct = this.step === 'results' ? 100 : Math.round((done / total) * 100);
    var label = this.step === 'results' || this.step < 0 ? null :
      el('div', { 'class': 'oqq-step-label', text: fmt(this.cfg.copy.ofLabel, { n: this.step + 1, total: total }) });
    return [
      el('div', { 'class': 'oqq-progress', role: 'progressbar', 'aria-valuenow': pct, 'aria-valuemin': 0, 'aria-valuemax': 100 }, [
        el('div', { 'class': 'oqq-progress-bar', style: 'width:' + pct + '%' }),
      ]),
      label,
    ];
  };

  App.prototype.render = function () {
    if (this.step === -1) return this.renderIntro();
    if (this.step === 'results') return this.renderResults();
    this.renderQuestion(this.cfg.questions[this.step]);
  };

  App.prototype.renderIntro = function () {
    var self = this, c = this.cfg.copy;
    this.shell([
      el('h2', { 'class': 'oqq-title', text: c.introTitle }),
      el('p', { 'class': 'oqq-intro-text', text: c.introText }),
      el('div', { 'class': 'oqq-center' }, [
        el('button', { 'class': 'oqq-cta', text: c.introButton, onclick: function () {
          track('start');
          self.step = 0; self.render();
        } }),
      ]),
    ]);
  };

  /* Choices can be hidden based on earlier answers via
     choice.hideWhen = { <questionId>: [<choiceId>, ...] }  */
  App.prototype.visibleChoices = function (q) {
    var self = this;
    return q.choices.filter(function (choice) {
      var hw = choice.hideWhen;
      if (!hw) return true;
      return !Object.keys(hw).some(function (qid) {
        var picked = asChoices(self.answers[qid]).map(function (c) { return c.id; });
        return hw[qid].some(function (id) { return picked.indexOf(id) !== -1; });
      });
    });
  };

  App.prototype.advance = function () {
    var next = this.step + 1;
    this.step = next >= this.cfg.questions.length ? 'results' : next;
    this.render();
  };

  App.prototype.renderQuestion = function (q) {
    var self = this, c = this.cfg.copy;
    var visible = this.visibleChoices(q);
    var selected = asChoices(this.answers[q.id]).filter(function (ch) {
      return visible.indexOf(ch) !== -1; // drop selections hidden by a changed earlier answer
    });
    var continueBtn = null;

    var choices = el('div', { 'class': 'oqq-choices' }, visible.map(function (choice) {
      var isOn = selected.indexOf(choice) !== -1;
      var btn = el('button', { 'class': 'oqq-choice', 'aria-pressed': q.multiSelect ? String(isOn) : null, onclick: function () {
        if (!q.multiSelect) {
          self.answers[q.id] = choice;
          track('answer', { question: q.id, choice: choice.id });
          self.advance();
          return;
        }
        var idx = selected.indexOf(choice);
        if (idx === -1) selected.push(choice); else selected.splice(idx, 1);
        btn.setAttribute('aria-pressed', String(idx === -1));
        self.answers[q.id] = selected.slice();
        if (continueBtn) continueBtn.disabled = selected.length === 0;
      } }, [
        el('span', { 'class': 'oqq-choice-dot', 'aria-hidden': 'true' }),
        el('span', { text: choice.label }),
      ]);
      return btn;
    }));

    var nav = [
      el('button', { 'class': 'oqq-back', text: this.step === 0 ? c.startOver : c.back, onclick: function () {
        self.step = self.step === 0 ? -1 : self.step - 1;
        self.render();
      } }),
    ];
    if (q.multiSelect) {
      continueBtn = el('button', { 'class': 'oqq-cta', text: c.continueLabel, onclick: function () {
        track('answer', { question: q.id, choice: selected.map(function (ch) { return ch.id; }).join('+') });
        self.answers[q.id] = selected.slice();
        self.advance();
      } });
      continueBtn.disabled = selected.length === 0;
      nav.unshift(el('div', { 'class': 'oqq-center', style: 'margin-bottom:10px' }, [continueBtn]));
    }

    this.shell([].concat(this.progress(), [
      el('h2', { 'class': 'oqq-title', text: q.title }),
      q.subtitle ? el('p', { 'class': 'oqq-subtitle', text: q.subtitle }) : null,
      choices,
      el('div', { 'class': 'oqq-nav' }, nav),
    ]));
  };

  App.prototype.renderResults = function () {
    var self = this, cfg = this.cfg, c = cfg.copy;
    var res = computeResults(cfg, this.products, this.answers);
    var cupsChoice = null;
    cfg.questions.forEach(function (q) { if (q.type === 'size') cupsChoice = self.answers[q.id]; });
    var rec = cupsChoice ? recommendSize(cfg, cupsChoice.cupsLow) : null;

    track('results', { count: res.list.length, relaxed: res.relaxedQuestions.join(',') });

    var head = [
      el('h2', { 'class': 'oqq-title', text: c.resultsTitle }),
      el('p', { 'class': 'oqq-results-intro', text: c.resultsIntro }),
    ];
    if (rec) {
      head.push(el('div', { 'class': 'oqq-size-note', text: fmt(c.sizeExplainer, {
        cupsLabel: cupsChoice.label, size: rec.size.label, serves: rec.serves, days: rec.days,
      }) }));
    }
    if (res.relaxedQuestions.length && res.list.length) {
      head.push(el('p', { 'class': 'oqq-fallback-note', text: c.fallbackNote }));
    }

    var body;
    if (!res.list.length) {
      body = el('div', { 'class': 'oqq-error' }, [
        el('p', { text: c.emptyNote }),
        el('p', {}, [el('a', { href: (this.onStore ? '' : 'https://' + cfg.dataSource.storeDomain) + '/collections/all-coffee', text: 'Browse all coffee' })]),
      ]);
    } else {
      body = el('div', { 'class': 'oqq-grid' }, res.list.map(function (p) {
        return self.card(p, rec);
      }));
    }

    this.shell([].concat(this.progress(), head, [body,
      el('div', { 'class': 'oqq-nav' }, [
        el('button', { 'class': 'oqq-back', text: c.startOver, onclick: function () {
          self.answers = {}; self.step = -1; self.render();
        } }),
      ]),
    ]));
  };

  App.prototype.badgesFor = function (product) {
    var cfg = this.cfg, out = [];
    /* highlight badges (Best Seller, Limited Release) first so they never
       get crowded out by origin/process badges */
    Object.keys(cfg.extraBadgeTags || {}).forEach(function (t) {
      if (product.tags.indexOf(t) !== -1) out.push({ text: cfg.extraBadgeTags[t], hot: true });
    });
    (cfg.badges || []).forEach(function (b) {
      product.tags.forEach(function (t) {
        if (t.indexOf(b.prefix) === 0) out.push({ text: fmt(b.format, { v: t.slice(b.prefix.length) }), hot: false });
      });
    });
    return out.slice(0, 4);
  };

  App.prototype.card = function (product, rec) {
    var self = this, cfg = this.cfg, c = cfg.copy;
    var selected = pickVariant(cfg, product, rec && rec.size);

    var priceEl = el('div', { 'class': 'oqq-price', text: selected ? '$' + selected.price.toFixed(2) : '' });
    var atc;

    /* size selector: one button per configured size that this product has */
    var sizeBtns = [];
    cfg.sizing.sizes.forEach(function (size) {
      /* first available variant of this size — must mirror pickVariant() so
         the preselected button matches the preselected variant */
      var variant = null;
      product.variants.forEach(function (v) {
        var s = sizeOfVariant(cfg, v);
        if (s && s.label === size.label) {
          if (!variant || (!variant.available && v.available)) variant = v;
        }
      });
      if (!variant) return;
      var isRec = rec && rec.size.label === size.label;
      var btn = el('button', {
        'class': 'oqq-size-btn',
        'aria-pressed': selected && variant.id === selected.id ? 'true' : 'false',
        disabled: variant.available ? null : 'disabled',
        onclick: function () {
          selected = variant;
          sizeBtns.forEach(function (b) { b.setAttribute('aria-pressed', 'false'); });
          btn.setAttribute('aria-pressed', 'true');
          priceEl.textContent = '$' + variant.price.toFixed(2);
          if (atc) syncAtc();
        },
      }, [
        el('span', { text: size.label }),
        isRec ? el('span', { 'class': 'oqq-size-rec', text: c.recommendedSize }) : null,
      ]);
      sizeBtns.push(btn);
    });

    function syncAtc() {
      atc.textContent = selected && selected.available ? c.addToCart : c.soldOut;
      if (selected && selected.available) atc.removeAttribute('disabled');
      else atc.setAttribute('disabled', 'disabled');
      atc.removeAttribute('data-state');
    }

    if (this.onStore) {
      atc = el('button', { 'class': 'oqq-atc', onclick: function () {
        if (!selected) return;
        atc.textContent = '…';
        addToCart(selected.id).then(function () {
          atc.textContent = c.added;
          atc.setAttribute('data-state', 'added');
          track('add_to_cart', { product: product.handle, variant: selected.id });
          setTimeout(syncAtc, 2500);
        }).catch(function (err) {
          atc.textContent = c.addToCart;
          alert(err.message || 'Could not add to cart');
        });
      } });
    } else {
      atc = el('a', { 'class': 'oqq-atc', target: '_top', href: this.productUrl(product, selected) });
    }
    syncAtc();

    var badges = this.badgesFor(product).map(function (b) {
      return el('span', { 'class': 'oqq-badge' + (b.hot ? ' oqq-badge--hot' : ''), text: b.text });
    });

    /* when the customer picked multiple brew methods, show which of THEIR
       methods this coffee suits */
    var methodChips = [];
    cfg.questions.forEach(function (q) {
      if (q.type !== 'filter' || !q.multiSelect) return;
      var answer = self.answers[q.id];
      if (asChoices(answer).length < 2) return;
      matchedChoices(product, answer).forEach(function (ch) {
        methodChips.push(el('span', { 'class': 'oqq-method-chip', text: '✓ ' + ch.label }));
      });
    });

    return el('div', { 'class': 'oqq-card' }, [
      el('a', { 'class': 'oqq-card-img', href: this.productUrl(product, selected), target: '_top' }, [
        product.image ? el('img', { src: product.image, alt: product.title, loading: 'lazy' }) : null,
      ]),
      el('div', { 'class': 'oqq-card-body' }, [
        badges.length ? el('div', { 'class': 'oqq-badges' }, badges) : null,
        el('h3', { 'class': 'oqq-card-title' }, [
          el('a', { href: this.productUrl(product, selected), target: '_top', text: product.title }),
        ]),
        methodChips.length ? el('div', { 'class': 'oqq-method-chips' }, methodChips) : null,
        sizeBtns.length ? el('div', { 'class': 'oqq-sizes' }, sizeBtns) : null,
        priceEl,
        atc,
        el('div', { 'class': 'oqq-card-links' }, [
          el('a', { href: this.productUrl(product, selected), target: '_top', text: c.viewProduct }),
        ]),
      ]),
    ]);
  };

  /* ==========================================================================
     BOOT
     ========================================================================== */
  /* HK Nova is the brand typeface but is commercially licensed; the CSS
     stack prefers it if the host page provides it, and we load Hanken
     Grotesk (same foundry, free) as the web fallback. */
  function loadFonts(cfg) {
    if (!cfg.brand || !cfg.brand.loadFonts) return;
    var have = false;
    document.querySelectorAll('link[href*="fonts.googleapis"]').forEach(function (l) {
      if (/Hanken/.test(l.href)) have = true;
    });
    if (have) return;
    var link = el('link', {
      rel: 'stylesheet',
      href: 'https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400;500;600;700;800&display=swap',
    });
    document.head.appendChild(link);
  }

  ready(function () {
    var mount = document.getElementById(MOUNT_ID);
    var cfg = window.OQ_QUIZ_CONFIG;
    if (!mount || !cfg) {
      if (window.console && !mount) console.warn('[OQ quiz] mount #' + MOUNT_ID + ' not found');
      return;
    }
    loadFonts(cfg);
    new App(mount, cfg).start();
  });
})();
