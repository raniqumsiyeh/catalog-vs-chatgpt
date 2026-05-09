# /// script
# dependencies = ["fastapi[standard]", "uvicorn[standard]", "httpx"]
# ///
"""
FastAPI server: side-by-side comparison of Shopify Catalog Search vs ChatGPT.

Routes:
  GET  /                        — main UI (tabs: Browse / Compare / Community)
  GET  /bookmarklet.js          — bookmarklet source (drop-in JS)
  GET  /api/queries             — pre-scraped baseline list (40 queries)
  GET  /api/result/{idx}        — pre-scraped baseline result for one query

  POST /api/catalog             — live Catalog API call: { query }
  POST /api/submit              — save a community comparison: { query, chatgpt: { products[], reply_text? } }
                                  → calls Catalog API, saves combined record, returns submission id
  GET  /api/submissions         — list community submissions
  GET  /api/submissions/{id}    — fetch one submission

Run locally:  uv run server.py    (port 3458)
"""

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Optional

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).parent
QUERIES_FILE = ROOT / "data" / "queries.json"
CATALOG_DIR = ROOT / "data" / "catalog"
CHATGPT_DIR = ROOT / "data" / "chatgpt"
SUBMISSIONS_DIR = ROOT / "data" / "submissions"
INDEX_HTML = ROOT / "index.html"
BOOKMARKLET_JS = ROOT / "bookmarklet.js"

V3_ENDPOINT = "https://catalog.shopify.com/api/ucp/mcp"
UCP_PROFILE = "https://shopify.dev/ucp/agent-profiles/2026-04-08/valid-with-capabilities.json"

app = FastAPI()
# CORS open so the bookmarklet can POST from chatgpt.com / chat.openai.com
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────

def _load_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _normalize_catalog_product(p: dict) -> dict:
    media = p.get("media") or []
    image_url = ""
    if media:
        first = media[0] if isinstance(media[0], dict) else {}
        image_url = first.get("url", "")

    variants = p.get("variants") or []
    seller_name = ""
    seller_url = ""
    price_min = None
    price_max = None
    available_any = False
    for v in variants:
        seller = v.get("seller") or v.get("shop") or {}
        if isinstance(seller, dict):
            seller_name = seller_name or seller.get("name", "")
            seller_url = seller_url or seller.get("url", "") or seller.get("domain", "")
        price = v.get("price") or {}
        amt = price.get("amount") if isinstance(price, dict) else None
        if amt is not None:
            try:
                amt = float(amt) / 100.0
            except (TypeError, ValueError):
                amt = None
            if amt is not None:
                price_min = amt if price_min is None else min(price_min, amt)
                price_max = amt if price_max is None else max(price_max, amt)
        avail = v.get("availability")
        if isinstance(avail, dict) and avail.get("available"):
            available_any = True
        elif v.get("availableForSale"):
            available_any = True

    return {
        "id": p.get("id", ""),
        "title": p.get("title", ""),
        "image_url": image_url,
        "seller_name": seller_name,
        "seller_url": seller_url,
        "price_min": price_min,
        "price_max": price_max,
        "available": available_any,
        "variant_count": len(variants),
        "url": p.get("url", "") or (variants[0].get("checkout_url", "") if variants else ""),
        "rating": (p.get("rating") or {}).get("value"),
        "rating_count": (p.get("rating") or {}).get("count"),
    }


async def call_catalog_api(query: str, limit: int = 10) -> dict:
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {
            "name": "search_catalog",
            "arguments": {
                "meta": {"ucp-agent": {"profile": UCP_PROFILE}},
                "catalog": {"query": query, "limit": limit},
            },
        },
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(V3_ENDPOINT, json=payload)
        resp.raise_for_status()
        data = resp.json()
    raw_products = data.get("result", {}).get("structuredContent", {}).get("products", [])
    return {
        "products": [_normalize_catalog_product(p) for p in raw_products[:limit]],
        "raw_count": len(raw_products),
    }


def _submission_id(query: str, ts: float) -> str:
    h = hashlib.sha1(f"{query}|{ts}".encode()).hexdigest()[:12]
    return h


# ─────────────────────────────────────────────────────────────────────
# routes — UI / static
# ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML.read_text()


@app.get("/bookmarklet.js", response_class=PlainTextResponse)
async def bookmarklet_source():
    """Return the source of the bookmarklet (for inspection / paste-as-bookmark)."""
    return BOOKMARKLET_JS.read_text()


# ─────────────────────────────────────────────────────────────────────
# routes — pre-scraped baseline (the 40)
# ─────────────────────────────────────────────────────────────────────

@app.get("/api/queries")
async def list_queries():
    queries = json.loads(QUERIES_FILE.read_text())
    out = []
    for idx, q in enumerate(queries):
        cat = _load_json(CATALOG_DIR / f"{idx}.json")
        chat = _load_json(CHATGPT_DIR / f"{idx}.json")
        out.append({
            "idx": idx,
            "query": q,
            "catalog_count": (cat or {}).get("count", 0),
            "chatgpt_count": (chat or {}).get("count", 0),
            "has_catalog": cat is not None and not (cat or {}).get("error"),
            "has_chatgpt": chat is not None and not (chat or {}).get("error"),
        })
    return {"total": len(queries), "queries": out}


@app.get("/api/result/{idx}")
async def get_result(idx: int):
    queries = json.loads(QUERIES_FILE.read_text())
    if idx < 0 or idx >= len(queries):
        raise HTTPException(404, "index out of range")

    q = queries[idx]
    cat = _load_json(CATALOG_DIR / f"{idx}.json") or {"products": [], "missing": True}
    chat = _load_json(CHATGPT_DIR / f"{idx}.json") or {"products": [], "missing": True}

    return {
        "idx": idx,
        "query": q,
        "total": len(queries),
        "catalog": {
            "products": cat.get("products", []),
            "error": cat.get("error"),
            "missing": cat.get("missing", False),
            "fetched_at": cat.get("fetched_at"),
        },
        "chatgpt": {
            "products": chat.get("products", []),
            "reply_text": chat.get("reply_text", ""),
            "error": chat.get("error"),
            "missing": chat.get("missing", False),
            "scraped_at": chat.get("scraped_at"),
        },
    }


# ─────────────────────────────────────────────────────────────────────
# routes — live + community
# ─────────────────────────────────────────────────────────────────────

class CatalogReq(BaseModel):
    query: str
    limit: int = 10


@app.post("/api/catalog")
async def live_catalog(req: CatalogReq):
    if not req.query.strip():
        raise HTTPException(400, "query required")
    try:
        r = await call_catalog_api(req.query, limit=min(req.limit, 10))
        return {"query": req.query, **r}
    except Exception as e:
        raise HTTPException(502, f"catalog API error: {e}")


class SubmitReq(BaseModel):
    query: str
    chatgpt: dict = Field(..., description="{ products: [...], reply_text?: string }")
    submitter: Optional[str] = None
    notes: Optional[str] = None


@app.post("/api/submit")
async def submit_comparison(req: SubmitReq, request: Request):
    """Save a community comparison.

    The bookmarklet POSTs the ChatGPT-scraped products + query.
    Server fetches Catalog API server-side, combines, saves.
    """
    if not req.query.strip():
        raise HTTPException(400, "query required")

    products = (req.chatgpt or {}).get("products") or []
    reply_text = (req.chatgpt or {}).get("reply_text") or ""

    # Catalog side — best-effort, don't fail submission if catalog blips
    try:
        cat = await call_catalog_api(req.query, limit=10)
        catalog_payload = {"products": cat["products"], "raw_count": cat["raw_count"]}
    except Exception as e:
        catalog_payload = {"products": [], "error": str(e)}

    ts = time.time()
    sid = _submission_id(req.query, ts)
    record = {
        "id": sid,
        "query": req.query,
        "submitted_at": ts,
        "submitter": (req.submitter or "anonymous").strip()[:80],
        "notes": (req.notes or "").strip()[:1000],
        "user_agent": request.headers.get("user-agent", "")[:300],
        "catalog": catalog_payload,
        "chatgpt": {"products": products[:15], "reply_text": reply_text[:8000]},
    }

    SUBMISSIONS_DIR.mkdir(parents=True, exist_ok=True)
    (SUBMISSIONS_DIR / f"{sid}.json").write_text(json.dumps(record, indent=2))

    # Bonus: if this query matches one of the 40 baseline queries (case-insensitive,
    # whitespace-trimmed), also save it as the baseline ChatGPT file so the
    # "Browse 40" view picks it up. Lets us populate the baseline by clicking
    # the bookmarklet on each query, no Playwright needed.
    matched_baseline_idx = None
    try:
        baselines = json.loads(QUERIES_FILE.read_text())
        normalized_q = req.query.strip().lower()
        for i, bq in enumerate(baselines):
            if bq.strip().lower() == normalized_q:
                matched_baseline_idx = i
                break
    except Exception:
        pass

    if matched_baseline_idx is not None:
        CHATGPT_DIR.mkdir(parents=True, exist_ok=True)
        baseline_record = {
            "query": baselines[matched_baseline_idx],
            "scraped_at": ts,
            "source": "bookmarklet",
            "count": len(products[:15]),
            "products": products[:15],
            "reply_text": reply_text[:8000],
        }
        (CHATGPT_DIR / f"{matched_baseline_idx}.json").write_text(
            json.dumps(baseline_record, indent=2)
        )

    return {
        "id": sid,
        "url": f"/?view=community&id={sid}",
        "matched_baseline_idx": matched_baseline_idx,
    }


@app.get("/api/submissions")
async def list_submissions():
    SUBMISSIONS_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for path in sorted(SUBMISSIONS_DIR.glob("*.json")):
        d = _load_json(path) or {}
        if not d:
            continue
        rows.append({
            "id": d.get("id"),
            "query": d.get("query"),
            "submitter": d.get("submitter"),
            "submitted_at": d.get("submitted_at"),
            "catalog_count": len((d.get("catalog") or {}).get("products") or []),
            "chatgpt_count": len((d.get("chatgpt") or {}).get("products") or []),
        })
    rows.sort(key=lambda r: r.get("submitted_at") or 0, reverse=True)
    return {"total": len(rows), "submissions": rows}


@app.delete("/api/baseline/{idx}")
async def reset_baseline(idx: int):
    """Delete a captured baseline so it shows as pending again."""
    queries = json.loads(QUERIES_FILE.read_text())
    if idx < 0 or idx >= len(queries):
        raise HTTPException(404, "index out of range")
    path = CHATGPT_DIR / f"{idx}.json"
    if path.exists():
        path.unlink()
    return {"idx": idx, "cleared": True}


@app.get("/api/submissions/{sid}")
async def get_submission(sid: str):
    path = SUBMISSIONS_DIR / f"{sid}.json"
    d = _load_json(path)
    if not d:
        raise HTTPException(404, "submission not found")
    return d


# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "3458"))
    uvicorn.run(app, host="0.0.0.0", port=port, reload=False)
