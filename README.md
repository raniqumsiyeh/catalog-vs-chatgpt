# Catalog vs ChatGPT

Side-by-side comparison of Shopify Catalog Search API results vs ChatGPT shopping responses. Built for visual inspection of ranking quality, coverage, and merchant overlap.

**Live modes:**
- **Browse 40** — pre-scraped baseline of 40 queries from the eval sheet (`PDP Error Scrape` tab `20260504`)
- **Compare your own** — bookmarklet captures any ChatGPT search you run; the site fetches the same query against Catalog API and renders side-by-side
- **Community** — every bookmarklet submission is saved and visible to all visitors

## Local development

```bash
# 1. Catalog baseline (one-shot, ~30 sec)
uv run /tmp/catalog-vs-chatgpt/fetch_catalog.py

# 2. ChatGPT baseline scrape (one-shot, ~5–8 min)
uv run /tmp/catalog-vs-chatgpt/scrape_chatgpt.py --setup   # first-time login
uv run /tmp/catalog-vs-chatgpt/scrape_chatgpt.py

# 3. Run the server
uv run /tmp/catalog-vs-chatgpt/server.py
# → http://localhost:3458
```

## Files

```
catalog-vs-chatgpt/
├── server.py              # FastAPI: serves UI + APIs
├── index.html             # SPA with 3 tabs
├── bookmarklet.js         # client-side ChatGPT scraper (drop into bookmarks bar)
├── fetch_catalog.py       # one-shot baseline catalog fetch
├── scrape_chatgpt.py      # one-shot baseline ChatGPT scrape (Playwright)
├── data/
│   ├── queries.json       # the 40 baseline queries
│   ├── catalog/           # cached catalog results, one per query (idx-keyed)
│   ├── chatgpt/           # cached chatgpt results, one per query
│   └── submissions/       # community-submitted comparisons (id-keyed)
└── README.md
```

## API endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | UI |
| GET | `/bookmarklet.js` | Bookmarklet source (with `SITE_URL_PLACEHOLDER` substituted client-side) |
| GET | `/api/queries` | List 40 baseline queries with cache status |
| GET | `/api/result/{idx}` | Get baseline result for one query |
| POST | `/api/catalog` | Live Catalog API call: `{query, limit?}` → `{products[]}` |
| POST | `/api/submit` | Save comparison: `{query, submitter?, chatgpt: {products[], reply_text?}}` → `{id, url}` |
| GET | `/api/submissions` | List all community submissions |
| GET | `/api/submissions/{id}` | Fetch one submission |

## Deployment notes (for River / quick site)

- **Runtime**: Python 3.10+ FastAPI. PEP-723 inline deps in `server.py` (`fastapi[standard]`, `uvicorn[standard]`, `httpx`). `uv run server.py` is the entry point.
- **Port**: reads `PORT` env var, defaults to 3458.
- **Persistence**: file-based — `data/submissions/*.json`. For production, swap to a managed store (BigQuery, Cloud SQL, or the Shopify quick-site equivalent). Single-instance file storage is fine for the prototype.
- **Auth**: site can run behind IAP. The bookmarklet POSTs from `chatgpt.com`, so CORS is wide open on `/api/submit`. If IAP is enforced, the user's IAP cookie will be sent on the cross-origin POST, which is fine for IAP-internal tooling.
- **External calls**: server hits `https://catalog.shopify.com/api/ucp/mcp` (UCP MCP endpoint). No auth header — public agent profile.
- **State directory**: `data/submissions/` is the only writable path needed. Mount as a volume or bind to a persistent disk.
- **No background jobs**: everything is request-driven. No cron, no queues.

### Building the bookmarklet at the deployed origin

The bookmarklet source has `SITE_URL_PLACEHOLDER` baked in. The UI fetches `/bookmarklet.js`, substitutes `window.location.origin`, minifies, and wraps with `javascript:(function(){…})()` for the user to drag to their bookmarks bar. No build step required.

### Refreshing the baseline

The 40-query baseline is committed in `data/catalog/` and `data/chatgpt/`. To refresh:
- Catalog: re-run `fetch_catalog.py --force` and commit
- ChatGPT: re-run `scrape_chatgpt.py --force` (requires login) and commit. Cannot run on the deployed quick site — Playwright + ChatGPT login is a local dev-only step.

## Iteration tips

- Re-scrape one baseline query: `uv run scrape_chatgpt.py --idx 5 --force`
- Test with smaller catalog limit: `uv run fetch_catalog.py --limit 5`
- Inspect a bad submission: read `data/submissions/{id}.json` directly
