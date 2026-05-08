# /// script
# dependencies = ["httpx"]
# ///
"""
Fetch Catalog API results for each query in data/queries.json.
Caches one JSON file per query to data/catalog/{idx}.json.

Usage: uv run fetch_catalog.py [--force] [--limit N]
"""

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).parent
QUERIES_FILE = ROOT / "data" / "queries.json"
OUT_DIR = ROOT / "data" / "catalog"

V3_ENDPOINT = "https://catalog.shopify.com/api/ucp/mcp"
UCP_PROFILE = "https://shopify.dev/ucp/agent-profiles/2026-04-08/valid-with-capabilities.json"


def call_search(client: httpx.Client, query: str, limit: int = 10) -> dict:
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
    resp = client.post(V3_ENDPOINT, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def normalize_product(p: dict) -> dict:
    """Extract a common shape from a UCP product."""
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
    variant_count = len(variants)
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
        if v.get("availability", {}).get("available") if isinstance(v.get("availability"), dict) else v.get("availableForSale"):
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
        "variant_count": variant_count,
        "url": p.get("url", "") or (variants[0].get("checkout_url", "") if variants else ""),
        "rating": (p.get("rating") or {}).get("value"),
        "rating_count": (p.get("rating") or {}).get("count"),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Re-fetch even if cached")
    parser.add_argument("--limit", type=int, default=10, help="Top-N results per query")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    queries = json.loads(QUERIES_FILE.read_text())

    with httpx.Client() as client:
        for idx, q in enumerate(queries):
            out_path = OUT_DIR / f"{idx}.json"
            if out_path.exists() and not args.force:
                print(f"  [{idx:02d}] cached: {q[:60]}")
                continue

            print(f"  [{idx:02d}] fetching: {q[:60]}")
            try:
                raw = call_search(client, q, limit=args.limit)
                products_raw = (
                    raw.get("result", {})
                       .get("structuredContent", {})
                       .get("products", [])
                )
                products = [normalize_product(p) for p in products_raw[: args.limit]]
                out_path.write_text(json.dumps({
                    "query": q,
                    "fetched_at": time.time(),
                    "count": len(products),
                    "products": products,
                    "raw": raw,
                }, indent=2))
            except Exception as e:
                print(f"    ERROR: {e}", file=sys.stderr)
                out_path.write_text(json.dumps({
                    "query": q,
                    "fetched_at": time.time(),
                    "error": str(e),
                    "products": [],
                }, indent=2))
            time.sleep(0.3)  # gentle pacing

    print(f"\nDone. {len(queries)} queries cached in {OUT_DIR}")


if __name__ == "__main__":
    main()
