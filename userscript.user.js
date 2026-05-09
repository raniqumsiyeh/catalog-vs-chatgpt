// ==UserScript==
// @name         Catalog vs ChatGPT — Auto-capture
// @namespace    https://github.com/raniqumsiyeh/catalog-vs-chatgpt
// @version      1.0
// @description  Auto-runs queued queries against ChatGPT and posts results to local comparison server. Pulls from the queue at SITE_URL/api/queue. Bypasses CSP via GM_xmlhttpRequest.
// @author       rani
// @match        https://chatgpt.com/*
// @match        https://chat.openai.com/*
// @grant        GM_xmlhttpRequest
// @connect      localhost
// @connect      127.0.0.1
// @run-at       document-idle
// ==/UserScript==

(function () {
  'use strict';

  // ── Config ──────────────────────────────────────────────────────────
  const SITE_URL = localStorage.getItem('cvg_site_url') || 'http://localhost:3458';
  const PACE_MS = 4000;          // gap between queries
  const RENDER_GRACE_MS = 3000;  // wait after stream ends for late shopping cards
  const MAX_WAIT_MS = 90_000;    // per-query response deadline

  // ── GM_xmlhttpRequest wrapper ───────────────────────────────────────
  function gmFetch(url, options = {}) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: options.method || 'GET',
        url,
        data: options.body,
        headers: options.headers || {},
        timeout: 30_000,
        onload: r => {
          let json;
          try { json = JSON.parse(r.responseText); } catch (_) { json = null; }
          resolve({ ok: r.status >= 200 && r.status < 300, status: r.status, json, text: r.responseText });
        },
        onerror: e => reject(new Error('Network error: ' + (e.error || 'unknown'))),
        ontimeout: () => reject(new Error('Timed out')),
      });
    });
  }

  function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

  // ── Scrape (mirror of console snippet's Strategy A) ─────────────────
  function scrapeProducts() {
    const products = [];
    const turns = document.querySelectorAll('[data-message-author-role="assistant"]');
    const last = turns[turns.length - 1];
    if (!last) return { products: [], replyText: '' };

    const seen = new WeakSet();
    last.querySelectorAll('img').forEach(img => {
      let card = img;
      for (let i = 0; i < 8; i++) {
        card = card.parentElement;
        if (!card) break;
        if ((card.className || '').toString().includes('cursor-pointer')) break;
      }
      if (!card || seen.has(card)) return;
      if (!(card.className || '').toString().includes('cursor-pointer')) return;
      seen.add(card);

      const text = (card.innerText || '').split('\n').map(t => t.trim()).filter(Boolean);
      const link = card.querySelector('a[href]');
      const priceLine = text.find(t => /[$€£¥]\s?\d/.test(t)) || '';
      const title = text.find(t => !/[$€£¥]\s?\d/.test(t)) || text[0] || '';
      products.push({
        title,
        text_full: text.slice(0, 6).join(' | '),
        price_text: priceLine,
        image_url: img.currentSrc || img.src,
        url: link ? link.href : '',
        source: 'card-walkup',
      });
    });

    // Fallback: testid-based
    if (products.length === 0) {
      ['shopping-product-card','product-card','product-result','shopping-card','shopping-carousel-item'].forEach(tid => {
        document.querySelectorAll(`[data-testid="${tid}"]`).forEach(c => {
          const img = c.querySelector('img'), link = c.querySelector('a[href]');
          const text = (c.innerText || '').split('\n').filter(Boolean);
          products.push({
            title: text[0] || '',
            text_full: text.join(' | '),
            image_url: img ? (img.currentSrc || img.src) : '',
            url: link ? link.href : '',
            source: 'testid:' + tid,
          });
        });
      });
    }

    return { products, replyText: (last.innerText || '').slice(0, 8000) };
  }

  // ── Page interaction ────────────────────────────────────────────────
  function findInput() {
    return document.querySelector('#prompt-textarea')
        || document.querySelector('textarea[placeholder*="Message"]')
        || document.querySelector('div[contenteditable="true"]');
  }

  async function waitFor(predicate, timeout = 15_000, interval = 200) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      const v = predicate();
      if (v) return v;
      await sleep(interval);
    }
    return null;
  }

  async function newChat() {
    // ChatGPT shortcut: Cmd/Ctrl+Shift+O
    const isMac = /Mac/.test(navigator.platform);
    document.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'O', code: 'KeyO', shiftKey: true,
      metaKey: isMac, ctrlKey: !isMac, bubbles: true,
    }));
    await sleep(1200);
    // Fallback: navigate
    if (!findInput()) {
      window.location.href = 'https://chatgpt.com/';
      await sleep(2500);
    }
  }

  async function sendQuery(query) {
    const input = await waitFor(findInput);
    if (!input) throw new Error('Could not find chat input');
    input.focus();

    if (input.tagName === 'TEXTAREA') {
      const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
      setter.call(input, query);
      input.dispatchEvent(new Event('input', { bubbles: true }));
    } else {
      // contenteditable: clear, then type via execCommand for compatibility
      input.innerHTML = '';
      const sel = window.getSelection();
      sel.removeAllRanges();
      const range = document.createRange();
      range.selectNodeContents(input);
      sel.addRange(range);
      document.execCommand('insertText', false, query);
    }
    await sleep(400);

    input.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'Enter', code: 'Enter', bubbles: true, which: 13, keyCode: 13,
    }));
  }

  async function waitForResponse() {
    // Wait for streaming to start, then complete
    const stopSelector = 'button[data-testid="stop-button"], button[aria-label*="Stop"]';
    const startDeadline = Date.now() + 10_000;
    let started = false;
    while (Date.now() < startDeadline) {
      if (document.querySelector(stopSelector)) { started = true; break; }
      await sleep(200);
    }
    const endDeadline = Date.now() + MAX_WAIT_MS;
    while (Date.now() < endDeadline) {
      if (!document.querySelector(stopSelector)) {
        if (started) {
          await sleep(RENDER_GRACE_MS);
          return true;
        }
      } else {
        started = true;
      }
      await sleep(400);
    }
    return false;
  }

  // ── Queue runner ────────────────────────────────────────────────────
  let running = false;
  let stopRequested = false;

  async function processQueue() {
    if (running) return;
    running = true;
    stopRequested = false;
    setBtnState('running');

    let processed = 0;
    let errors = 0;

    try {
      while (!stopRequested) {
        // Fetch the next pending item
        const r = await gmFetch(SITE_URL + '/api/queue?status=pending_chatgpt');
        if (!r.ok || !r.json) {
          setStatus(`Server unreachable at ${SITE_URL}. Is the local server running?`);
          break;
        }
        const queue = (r.json.queue || []).filter(q => q.status === 'pending_chatgpt');
        if (queue.length === 0) {
          setStatus(`Done. Processed ${processed}, errors ${errors}.`);
          break;
        }

        const next = queue[0];
        setStatus(`(${processed + 1}) ${next.query.slice(0, 50)}`);

        try {
          await newChat();
          await sendQuery(next.query);
          const ok = await waitForResponse();
          if (!ok) throw new Error('Response did not complete in time');

          const { products, replyText } = scrapeProducts();
          await gmFetch(SITE_URL + '/api/submit', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              query: next.query,
              submitter: 'userscript',
              chatgpt: { products, reply_text: replyText },
            }),
          });
          processed++;
        } catch (e) {
          console.error('[catalog-vs-chatgpt] error on query:', next.query, e);
          errors++;
          // Mark as complete with empty products so it doesn't loop forever
          await gmFetch(SITE_URL + '/api/submit', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              query: next.query,
              submitter: 'userscript-error',
              chatgpt: { products: [], reply_text: 'Error: ' + e.message },
            }),
          });
        }

        await sleep(PACE_MS);
      }
    } finally {
      running = false;
      setBtnState('idle');
    }
  }

  // ── UI panel ────────────────────────────────────────────────────────
  function makePanel() {
    const panel = document.createElement('div');
    Object.assign(panel.style, {
      position: 'fixed', bottom: '20px', right: '20px', zIndex: '999999',
      background: '#fff', border: '1px solid #d1d5db', borderRadius: '12px',
      padding: '12px', boxShadow: '0 8px 24px rgba(0,0,0,0.15)',
      fontFamily: 'system-ui, -apple-system, sans-serif', fontSize: '13px',
      width: '320px',
    });
    panel.innerHTML = `
      <div style="display:flex; align-items:center; gap:8px; margin-bottom:8px;">
        <span style="background:#5c6ac4; color:#fff; padding:2px 8px; border-radius:6px; font-size:10px; font-weight:600; letter-spacing:0.05em;">CVG</span>
        <strong>Auto-capture</strong>
        <button id="cvg-toggle-min" style="margin-left:auto; background:transparent; border:0; cursor:pointer; color:#6b7280;">—</button>
      </div>
      <div id="cvg-body">
        <div style="font-size:11px; color:#6b7280; margin-bottom:8px;">
          Server: <span id="cvg-url">${SITE_URL}</span>
          <button id="cvg-edit-url" style="background:transparent; border:0; color:#5c6ac4; cursor:pointer; font-size:11px;">edit</button>
        </div>
        <div style="display:flex; gap:6px;">
          <button id="cvg-start" style="flex:1; padding:8px; background:#5c6ac4; color:#fff; border:0; border-radius:6px; font-weight:600; cursor:pointer;">▶ Start</button>
          <button id="cvg-stop" style="padding:8px 12px; background:#fff; border:1px solid #d1d5db; border-radius:6px; cursor:pointer; display:none;">Stop</button>
        </div>
        <div id="cvg-status" style="margin-top:8px; color:#6b7280; font-size:12px; min-height:18px;"></div>
      </div>
    `;
    document.body.appendChild(panel);

    document.getElementById('cvg-start').addEventListener('click', () => processQueue().catch(e => alert(e.message)));
    document.getElementById('cvg-stop').addEventListener('click', () => { stopRequested = true; setStatus('Stopping after current query...'); });
    document.getElementById('cvg-edit-url').addEventListener('click', () => {
      const u = prompt('Server URL', SITE_URL);
      if (u) { localStorage.setItem('cvg_site_url', u); location.reload(); }
    });
    document.getElementById('cvg-toggle-min').addEventListener('click', () => {
      const b = document.getElementById('cvg-body');
      b.style.display = b.style.display === 'none' ? '' : 'none';
    });
  }

  function setBtnState(state) {
    const start = document.getElementById('cvg-start');
    const stop = document.getElementById('cvg-stop');
    if (!start) return;
    if (state === 'running') {
      start.disabled = true; start.style.opacity = 0.5;
      stop.style.display = '';
    } else {
      start.disabled = false; start.style.opacity = 1;
      stop.style.display = 'none';
    }
  }
  function setStatus(t) {
    const el = document.getElementById('cvg-status');
    if (el) el.textContent = t;
  }

  // Wait until the page is ready, then mount
  function mount() {
    if (document.body) makePanel();
    else setTimeout(mount, 200);
  }
  mount();
})();
