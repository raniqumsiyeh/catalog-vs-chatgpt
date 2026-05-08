/* Catalog vs ChatGPT bookmarklet
 *
 * One-click capture: scrapes the current ChatGPT page for shopping product cards
 * and POSTs them to the comparison site. The site then runs the same query
 * against Shopify Catalog API and shows side-by-side.
 *
 * Drag this entire file's content (minified) into your bookmarks bar
 * with the 'javascript:' prefix. The site's setup page will generate the
 * minified version for you.
 *
 * Replaces the SITE_URL placeholder at install time with the deployed origin.
 */
(async () => {
  const SITE_URL = 'SITE_URL_PLACEHOLDER';

  // Helper: extract product cards from the active assistant turn
  function scrapeProducts() {
    function pushUnique(arr, p) {
      if (!p.title && !p.url && !p.image_url) return;
      const k = (p.url || '') + '|' + (p.title || '');
      if (arr.some(x => ((x.url || '') + '|' + (x.title || '')) === k)) return;
      arr.push(p);
    }

    const products = [];

    // Strategy 1 — testid-based product cards (most reliable when present)
    const testids = ['shopping-product-card', 'product-card', 'product-result',
                     'shopping-card', 'shopping-carousel-item', 'shopping-tile'];
    for (const tid of testids) {
      const cards = document.querySelectorAll(`[data-testid="${tid}"]`);
      for (const c of cards) {
        const img = c.querySelector('img');
        const link = c.querySelector('a[href]');
        const text = (c.innerText || '').split('\n').filter(Boolean);
        pushUnique(products, {
          title: text[0] || '',
          text_full: text.join(' | '),
          image_url: img ? (img.currentSrc || img.src) : '',
          url: link ? link.href : '',
          source: 'testid:' + tid,
        });
      }
    }

    // Strategy 2 — role=group containers
    if (products.length === 0) {
      const anchors = document.querySelectorAll('[role="group"] a[href*="://"], [role="group"] a[href^="/aclick"]');
      for (const a of anchors) {
        const card = a.closest('[role="group"]') || a.parentElement;
        if (!card) continue;
        const img = card.querySelector('img');
        const text = (card.innerText || '').split('\n').filter(Boolean);
        pushUnique(products, {
          title: text[0] || '',
          text_full: text.join(' | '),
          image_url: img ? (img.currentSrc || img.src) : '',
          url: a.href,
          source: 'role-group',
        });
      }
    }

    // Strategy 3 — heuristic: container with image + price-like text in latest turn
    if (products.length === 0) {
      const turns = document.querySelectorAll('[data-message-author-role="assistant"]');
      const last = turns[turns.length - 1];
      if (last) {
        const candidates = last.querySelectorAll('div, article, li');
        for (const el of candidates) {
          const img = el.querySelector('img');
          if (!img) continue;
          const txt = el.innerText || '';
          const hasPrice = /[$€£¥]\s?\d|\d+\s?(USD|EUR|GBP)/i.test(txt);
          const link = el.querySelector('a[href*="://"]');
          if (hasPrice && link) {
            const text = txt.split('\n').filter(Boolean);
            pushUnique(products, {
              title: text[0] || '',
              text_full: text.slice(0, 8).join(' | '),
              image_url: img.currentSrc || img.src,
              url: link.href,
              source: 'price-heuristic',
            });
          }
          if (products.length >= 12) break;
        }
      }
    }

    // Strategy 4 — fallback: every external link in latest assistant message
    if (products.length === 0) {
      const turns = document.querySelectorAll('[data-message-author-role="assistant"]');
      const last = turns[turns.length - 1];
      if (last) {
        const links = last.querySelectorAll('a[href*="://"]');
        const seen = new Set();
        for (const a of links) {
          if (seen.has(a.href)) continue;
          seen.add(a.href);
          pushUnique(products, {
            title: a.innerText || a.href,
            text_full: a.innerText || '',
            image_url: '',
            url: a.href,
            source: 'link-only',
          });
          if (products.length >= 10) break;
        }
      }
    }

    return products;
  }

  // Helper: pull the user's last query from the prompt history
  function detectQuery() {
    const userTurns = document.querySelectorAll('[data-message-author-role="user"]');
    const last = userTurns[userTurns.length - 1];
    if (!last) return '';
    const t = (last.innerText || '').trim();
    // If the user pasted the long prompt template, strip it
    const m = t.match(/Please show me product results to buy for:\s*(.+?)(?:\n|$)/i);
    return m ? m[1].trim() : t;
  }

  // Helper: pull the assistant's text reply (truncated)
  function getReplyText() {
    const turns = document.querySelectorAll('[data-message-author-role="assistant"]');
    const last = turns[turns.length - 1];
    return last ? (last.innerText || '').slice(0, 8000) : '';
  }

  const products = scrapeProducts();
  const query = detectQuery();
  const replyText = getReplyText();

  if (!query) {
    alert('Could not detect the query. Please run a search in ChatGPT first.');
    return;
  }

  // Quick UX: prompt for query confirmation + optional submitter name
  const confirmedQuery = window.prompt(
    `Captured ${products.length} products from ChatGPT.\n\nQuery (edit if wrong):`,
    query
  );
  if (!confirmedQuery) return;

  const submitter = window.prompt('Your name (optional, shown publicly):', '') || '';

  try {
    const resp = await fetch(SITE_URL + '/api/submit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        query: confirmedQuery,
        submitter,
        chatgpt: { products, reply_text: replyText },
      }),
    });
    if (!resp.ok) {
      alert('Submit failed: HTTP ' + resp.status);
      return;
    }
    const data = await resp.json();
    window.open(SITE_URL + data.url, '_blank');
  } catch (e) {
    alert('Submit failed: ' + e.message);
  }
})();
