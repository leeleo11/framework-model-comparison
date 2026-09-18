#!/usr/bin/env python
"""Query the OSIS/pyosis knowledge base (Weknora) from the command line.

Configuration comes from the environment:
    WEKNORA_BASE_URL  (default: https://knowledge.osisbim.com/api/v1)
    WEKNORA_API_KEY   (required)
    WEKNORA_KB_IDS    (comma-separated knowledge-base ids, required)

Usage:
    python weknora_search.py "create_rect 矩形截面 参数 签名"
"""

import json
import os
import sys
import urllib.request


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: weknora_search.py QUERY", file=sys.stderr)
        return 2
    base = os.environ.get("WEKNORA_BASE_URL", "https://knowledge.osisbim.com/api/v1").rstrip("/")
    key = os.environ.get("WEKNORA_API_KEY", "")
    ids = [x.strip() for x in os.environ.get("WEKNORA_KB_IDS", "").split(",") if x.strip()]
    if not key or not ids:
        print("TOOL_ERROR: WEKNORA_API_KEY / WEKNORA_KB_IDS 未配置", file=sys.stderr)
        return 1
    payload = json.dumps({"query": sys.argv[1][:2000], "knowledge_base_ids": ids}).encode("utf-8")
    request = urllib.request.Request(
        base + "/knowledge-search", data=payload,
        headers={
            "X-API-Key": key,
            "Content-Type": "application/json",
            "User-Agent": "osis-framework-benchmark/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.loads(response.read().decode("utf-8"))
    data = body.get("data")
    chunks = data if isinstance(data, list) else (data or {}).get("chunks") or []
    for index, chunk in enumerate(chunks[:5], 1):
        source = chunk.get("document_name") or chunk.get("knowledge_name") or ""
        print(f"--- [{index}] {source} ---")
        print((chunk.get("content") or "").strip()[:3000])
        print()
    if not chunks:
        print("知识库无命中")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
