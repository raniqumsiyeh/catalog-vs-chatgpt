# /// script
# dependencies = ["fastapi[standard]", "uvicorn[standard]", "httpx", "openai>=1.40"]
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

import asyncio
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

import httpx
import openai
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
CUSTOM_DIR = ROOT / "data" / "custom"
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


# ─────────────────────────────────────────────────────────────────────
# OpenAI integration (via Shopify LLM gateway)
# ─────────────────────────────────────────────────────────────────────

_openai_client: Optional[openai.AsyncOpenAI] = None


def _get_openai_client() -> Optional[openai.AsyncOpenAI]:
    """Lazily-built async OpenAI client. Prefers Shopify LLM gateway via
    `devx llm-gateway print-token`; falls back to OPENAI_API_KEY env var."""
    global _openai_client
    if _openai_client is not None:
        return _openai_client

    api_key = os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL")

    if not api_key:
        try:
            r = subprocess.run(
                ["/opt/dev/bin/user/devx", "llm-gateway", "print-token", "--key"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                api_key = r.stdout.strip()
                if not base_url:
                    base_url = "https://proxy.shopify.ai/vendors/openai/v1"
        except Exception:
            pass

    if not api_key:
        return None

    kwargs = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    _openai_client = openai.AsyncOpenAI(**kwargs)
    return _openai_client


_OAI_PROMPT = """You are a shopping research assistant. For the user query: "{query}"

Use web search to find {limit} real products from real online merchants. Prioritize:
- Real product pages from real stores (not editorial or review articles)
- Diverse merchants
- Products that match the query's intent (price range, brand, condition, etc.)

Return ONLY valid JSON, no preamble, no markdown fences. Shape:
{{
  "products": [
    {{
      "title": "<product name as shown on the merchant page>",
      "merchant": "<store name, e.g. 'Best Buy', 'Etsy seller name'>",
      "url": "<full product page URL>",
      "price": "<price as shown, e.g. '$49.99' or '€32.00'>",
      "image_url": "<full product image URL or empty string>"
    }}
  ]
}}

Return at least 5, up to {limit}. Each product must have a real URL. Keep titles concise.
"""


async def call_openai_shopping(query: str, limit: int = 10) -> dict:
    client = _get_openai_client()
    if client is None:
        return {
            "products": [],
            "error": "No OpenAI access. Run via Shopify LLM gateway, or set OPENAI_API_KEY (and optionally OPENAI_BASE_URL).",
        }

    prompt = _OAI_PROMPT.format(query=query, limit=limit)
    text = ""
    last_err = None

    # Try Responses API with web_search_preview first
    try:
        resp = await client.responses.create(
            model="gpt-4o",
            input=prompt,
            tools=[{"type": "web_search_preview"}],
        )
        text = (getattr(resp, "output_text", "") or "").strip()
    except Exception as e:
        last_err = f"responses api failed: {e}"

    # Fallback: Chat Completions with structured-output prompt (no web search)
    if not text:
        try:
            resp = await client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt + "\n\nNote: web search unavailable in this fallback path; use your training-data knowledge of real products and merchants."}],
                response_format={"type": "json_object"},
            )
            text = (resp.choices[0].message.content or "").strip()
        except Exception as e:
            return {"products": [], "error": f"{last_err or ''} | chat completions also failed: {e}"}

    if not text:
        return {"products": [], "error": last_err or "Empty response"}

    # Strip markdown fences if any slipped through
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)

    try:
        data = json.loads(text)
        raw_products = data.get("products", []) or []
    except Exception as e:
        return {"products": [], "error": f"Could not parse JSON: {e}", "raw": text[:600]}

    normalized = []
    for p in raw_products[:limit]:
        title = (p.get("title") or "").strip()
        merchant = (p.get("merchant") or "").strip()
        price = (p.get("price") or "").strip()
        normalized.append({
            "title": title,
            "url": (p.get("url") or "").strip(),
            "image_url": (p.get("image_url") or "").strip(),
            "price_text": price,
            "merchant": merchant,
            "text_full": " | ".join(filter(None, [title, merchant, price])),
            "source": "openai-api",
        })

    return {"products": normalized, "model": "gpt-4o", "tool": "web_search_preview"}


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

    # Also try matching against pending custom queue items — fill them in.
    matched_custom_id = None
    CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
    normalized_q2 = req.query.strip().lower()
    for path in CUSTOM_DIR.glob("*.json"):
        d = _load_json(path)
        if not d or d.get("status") == "complete":
            continue
        if (d.get("query") or "").strip().lower() == normalized_q2:
            d["status"] = "complete"
            d["chatgpt"] = {
                "products": products[:15],
                "reply_text": reply_text[:8000],
                "captured_at": ts,
            }
            path.write_text(json.dumps(d, indent=2))
            matched_custom_id = d.get("id")
            break

    return {
        "id": sid,
        "url": f"/?view=community&id={sid}",
        "matched_baseline_idx": matched_baseline_idx,
        "matched_custom_id": matched_custom_id,
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


class QueueReq(BaseModel):
    queries: list[str]


async def _fill_openai_for_custom(cid: str, query: str):
    """Background task: call OpenAI shopping search and update the queue record."""
    try:
        result = await call_openai_shopping(query, limit=10)
    except Exception as e:
        result = {"products": [], "error": str(e)}

    path = CUSTOM_DIR / f"{cid}.json"
    if not path.exists():
        return
    try:
        record = json.loads(path.read_text())
    except Exception:
        return
    record["chatgpt"] = {
        "products": result.get("products", []),
        "error": result.get("error"),
        "model": result.get("model"),
        "source": "openai-api",
        "completed_at": time.time(),
    }
    record["status"] = "complete" if not result.get("error") else "error"
    path.write_text(json.dumps(record, indent=2))


@app.post("/api/queue")
async def queue_custom_queries(req: QueueReq):
    """Add one or more user queries. Catalog API runs immediately (sync ~1s);
    OpenAI shopping search is kicked off in the background and fills in as it
    completes (~3-15s per query). Frontend polls /api/queue to show progress."""
    cleaned = [q.strip() for q in req.queries if q.strip()]
    if not cleaned:
        raise HTTPException(400, "no queries")

    CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for q in cleaned:
        ts = time.time()
        cid = _submission_id(q, ts)
        try:
            cat = await call_catalog_api(q, limit=10)
            catalog_payload = {"products": cat["products"], "raw_count": cat["raw_count"]}
        except Exception as e:
            catalog_payload = {"products": [], "error": str(e)}
        record = {
            "id": cid,
            "query": q,
            "created_at": ts,
            "status": "pending_chatgpt",
            "catalog": catalog_payload,
            "chatgpt": None,
        }
        (CUSTOM_DIR / f"{cid}.json").write_text(json.dumps(record, indent=2))
        out.append({"id": cid, "query": q, "status": record["status"]})
        # Fire-and-forget: OpenAI call updates the file when done
        asyncio.create_task(_fill_openai_for_custom(cid, q))
    return {"added": out}


@app.post("/api/queue/{cid}/retry")
async def retry_openai_for_custom(cid: str):
    """Re-run the OpenAI call for a queued item (e.g. after an error)."""
    path = CUSTOM_DIR / f"{cid}.json"
    record = _load_json(path)
    if not record:
        raise HTTPException(404, "not found")
    record["status"] = "pending_chatgpt"
    record["chatgpt"] = None
    path.write_text(json.dumps(record, indent=2))
    asyncio.create_task(_fill_openai_for_custom(cid, record["query"]))
    return {"id": cid, "status": "pending_chatgpt"}


@app.get("/api/queue")
async def list_queue(status: Optional[str] = None):
    """List custom queries (queue). status filter: 'pending_chatgpt' | 'complete' | None."""
    CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for path in sorted(CUSTOM_DIR.glob("*.json")):
        d = _load_json(path) or {}
        if not d:
            continue
        if status and d.get("status") != status:
            continue
        rows.append({
            "id": d.get("id"),
            "query": d.get("query"),
            "status": d.get("status"),
            "created_at": d.get("created_at"),
            "catalog_count": len((d.get("catalog") or {}).get("products") or []),
            "chatgpt_count": len(((d.get("chatgpt") or {}).get("products") or [])) if d.get("chatgpt") else 0,
        })
    rows.sort(key=lambda r: r.get("created_at") or 0)
    return {"total": len(rows), "queue": rows}


@app.get("/api/queue/{cid}")
async def get_queue_item(cid: str):
    path = CUSTOM_DIR / f"{cid}.json"
    d = _load_json(path)
    if not d:
        raise HTTPException(404, "not found")
    return d


@app.delete("/api/queue/{cid}")
async def delete_queue_item(cid: str):
    path = CUSTOM_DIR / f"{cid}.json"
    if path.exists():
        path.unlink()
    return {"id": cid, "deleted": True}


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


async def _fill_baseline_via_api(idx: int, query: str):
    """Background: call OpenAI for a baseline query and write data/chatgpt/{idx}.json."""
    result = await call_openai_shopping(query, limit=10)
    if result.get("error"):
        return  # Don't write a bad baseline
    CHATGPT_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "query": query,
        "scraped_at": time.time(),
        "source": "openai-api",
        "model": result.get("model"),
        "count": len(result["products"]),
        "products": result["products"],
        "reply_text": "",
    }
    (CHATGPT_DIR / f"{idx}.json").write_text(json.dumps(record, indent=2))


class FillBaselinesReq(BaseModel):
    overwrite: bool = False


@app.post("/api/baselines/fill-via-api")
async def fill_baselines_via_api(req: FillBaselinesReq):
    """Auto-fill any pending baseline ChatGPT slots via OpenAI API.
    With overwrite=true, also re-runs ones already captured."""
    queries = json.loads(QUERIES_FILE.read_text())
    targets = []
    for idx, q in enumerate(queries):
        path = CHATGPT_DIR / f"{idx}.json"
        if path.exists() and not req.overwrite:
            continue
        targets.append((idx, q))

    for idx, q in targets:
        asyncio.create_task(_fill_baseline_via_api(idx, q))
    return {"queued": len(targets), "total": len(queries)}


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
