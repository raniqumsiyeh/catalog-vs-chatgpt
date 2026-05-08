# /// script
# dependencies = ["playwright"]
# ///
"""
Scrape ChatGPT shopping results for each query in data/queries.json.

Usage:
  # First-time setup (opens browser, you log in, press ENTER in terminal):
  uv run scrape_chatgpt.py --setup

  # Scrape all queries:
  uv run scrape_chatgpt.py

  # Scrape a single query for testing:
  uv run scrape_chatgpt.py --idx 0

  # Re-scrape even if cached:
  uv run scrape_chatgpt.py --force

Notes:
- Uses a persistent Chrome profile at chrome-profile/ so you only log in once.
- Runs headed by default (you can watch). --headless to hide.
- 30-40 queries should take ~5-8 min depending on response time.
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

ROOT = Path(__file__).parent
QUERIES_FILE = ROOT / "data" / "queries.json"
OUT_DIR = ROOT / "data" / "chatgpt"
PROFILE_DIR = ROOT / "chrome-profile"

CHATGPT_URL = "https://chatgpt.com/"
SHOPPING_PROMPT = (
    "Please show me product results to buy for: {q}\n\n"
    "Return shopping results from real merchants. Return at least 5 products if available."
)


def setup_browser():
    """First-time setup: open chatgpt, let user log in."""
    print("\n=== ChatGPT Setup ===")
    print("Opening Chrome — please log into ChatGPT in the browser window.")
    print("After you're logged in and on chatgpt.com, come back here and press ENTER.\n")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(CHATGPT_URL)
        input("Press ENTER once you're logged in and ChatGPT is loaded...")
        ctx.close()
    print("Setup complete. Profile saved to:", PROFILE_DIR)


def scrape_query(page, query: str) -> dict:
    """Send a query to ChatGPT and scrape product results from the response."""
    # Navigate to a fresh chat
    page.goto(CHATGPT_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)

    # Find prompt textarea — ChatGPT uses #prompt-textarea
    prompt = page.locator("#prompt-textarea, textarea[placeholder*='Message'], div[contenteditable='true']").first
    prompt.wait_for(state="visible", timeout=15_000)
    prompt.click()
    prompt.fill("")  # clear if anything

    full_prompt = SHOPPING_PROMPT.format(q=query)
    prompt.type(full_prompt, delay=10)
    page.wait_for_timeout(300)
    # Submit (Enter)
    prompt.press("Enter")

    # Wait for response to begin streaming
    # ChatGPT shows a stop button while generating
    page.wait_for_timeout(2000)

    # Wait for response to complete (no stop button visible)
    completed = False
    deadline = time.time() + 90
    while time.time() < deadline:
        stop_btn = page.locator("button[data-testid='stop-button'], button[aria-label*='Stop']")
        if stop_btn.count() == 0:
            # Wait an extra moment for late-rendering shopping cards
            page.wait_for_timeout(2500)
            completed = True
            break
        page.wait_for_timeout(500)

    if not completed:
        print(f"    [WARN] response did not complete within 90s")

    # Extract products. ChatGPT shopping cards have evolved — try multiple strategies.
    products = page.evaluate("""() => {
        function pushUnique(arr, p) {
            if (!p.title && !p.url && !p.image_url) return;
            // Dedup by url+title
            const k = (p.url || '') + '|' + (p.title || '');
            if (arr.some(x => ((x.url || '') + '|' + (x.title || '')) === k)) return;
            arr.push(p);
        }

        const products = [];

        // Strategy 1 — look for shopping/product testids
        const testids = ['shopping-product-card', 'product-card', 'product-result',
                         'shopping-card', 'shopping-carousel-item', 'shopping-tile'];
        for (const tid of testids) {
            const cards = document.querySelectorAll(`[data-testid="${tid}"]`);
            for (const c of cards) {
                const img = c.querySelector('img');
                const link = c.querySelector('a[href]');
                const text = (c.innerText || '').split('\\n').filter(Boolean);
                pushUnique(products, {
                    title: text[0] || '',
                    text_full: text.join(' | '),
                    image_url: img ? (img.currentSrc || img.src) : '',
                    url: link ? link.href : '',
                    source: 'testid:' + tid,
                });
            }
        }

        // Strategy 2 — look for any anchor with role=group / shopping pattern
        if (products.length === 0) {
            const groups = document.querySelectorAll('[role="group"] a[href*="://"], [role="group"] a[href^="/aclick"]');
            for (const a of groups) {
                const card = a.closest('[role="group"]') || a.parentElement;
                if (!card) continue;
                const img = card.querySelector('img');
                const text = (card.innerText || '').split('\\n').filter(Boolean);
                pushUnique(products, {
                    title: text[0] || '',
                    text_full: text.join(' | '),
                    image_url: img ? (img.currentSrc || img.src) : '',
                    url: a.href,
                    source: 'role-group',
                });
            }
        }

        // Strategy 3 — assistant message: look for any container with img + heading + price-like text
        if (products.length === 0) {
            const assistantTurns = document.querySelectorAll('[data-message-author-role="assistant"]');
            const lastTurn = assistantTurns[assistantTurns.length - 1];
            if (lastTurn) {
                // Look for elements that have an image and a $ price string
                const candidates = lastTurn.querySelectorAll('div, article, li');
                for (const el of candidates) {
                    const img = el.querySelector('img');
                    if (!img) continue;
                    const txt = el.innerText || '';
                    const hasPrice = /[$€£¥]\\s?\\d|\\d+\\s?(USD|EUR|GBP)/i.test(txt);
                    const link = el.querySelector('a[href*="://"]');
                    if (hasPrice && link) {
                        const text = txt.split('\\n').filter(Boolean);
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

        // Strategy 4 — last-resort: every external link in the last assistant message
        if (products.length === 0) {
            const assistantTurns = document.querySelectorAll('[data-message-author-role="assistant"]');
            const lastTurn = assistantTurns[assistantTurns.length - 1];
            if (lastTurn) {
                const links = lastTurn.querySelectorAll('a[href*="://"]');
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
    }""")

    # Also grab the assistant's full text reply, in case we want to inspect it
    reply_text = page.evaluate("""() => {
        const turns = document.querySelectorAll('[data-message-author-role="assistant"]');
        const last = turns[turns.length - 1];
        return last ? (last.innerText || '') : '';
    }""")

    return {
        "products": products[:10],
        "reply_text": reply_text[:5000],
    }


def parse_price(text: str) -> dict:
    """Try to extract a price string from a snippet of card text."""
    m = re.search(r"([$€£¥])\s?(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?)", text or "")
    if m:
        return {"currency": m.group(1), "amount": m.group(2)}
    m = re.search(r"(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?)\s?(USD|EUR|GBP|CAD|AUD|JPY)", text or "")
    if m:
        return {"currency": m.group(2), "amount": m.group(1)}
    return {"currency": "", "amount": ""}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup", action="store_true",
                        help="Open browser for login (do this once)")
    parser.add_argument("--idx", type=int, default=None,
                        help="Scrape only one query by index")
    parser.add_argument("--force", action="store_true",
                        help="Re-scrape even if cached")
    parser.add_argument("--headless", action="store_true",
                        help="Run headless (default: headed so you can watch)")
    args = parser.parse_args()

    if args.setup:
        setup_browser()
        return

    if not PROFILE_DIR.exists():
        print("ERROR: no chrome profile yet. Run with --setup first.", file=sys.stderr)
        sys.exit(1)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    queries = json.loads(QUERIES_FILE.read_text())

    indices = [args.idx] if args.idx is not None else range(len(queries))

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=args.headless,
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        for idx in indices:
            q = queries[idx]
            out_path = OUT_DIR / f"{idx}.json"
            if out_path.exists() and not args.force:
                print(f"  [{idx:02d}] cached: {q[:60]}")
                continue

            print(f"  [{idx:02d}] scraping: {q[:60]}")
            try:
                result = scrape_query(page, q)
                # Annotate parsed prices
                for prod in result["products"]:
                    prod["parsed_price"] = parse_price(prod.get("text_full", ""))
                out_path.write_text(json.dumps({
                    "query": q,
                    "scraped_at": time.time(),
                    "count": len(result["products"]),
                    "products": result["products"],
                    "reply_text": result["reply_text"],
                }, indent=2))
                print(f"    -> {len(result['products'])} products")
            except Exception as e:
                print(f"    ERROR: {e}", file=sys.stderr)
                out_path.write_text(json.dumps({
                    "query": q,
                    "scraped_at": time.time(),
                    "error": str(e),
                    "products": [],
                }, indent=2))
            # Pace requests so we don't hammer ChatGPT
            time.sleep(2.0)

        ctx.close()

    print(f"\nDone. Outputs in {OUT_DIR}")


if __name__ == "__main__":
    main()
