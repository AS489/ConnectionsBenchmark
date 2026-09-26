#!/usr/bin/env python3
"""Regenerate runner/models-free.yaml from OpenRouter's live model list.

Excluded by design: music-generation models (lyria-*), classifiers
(nemotron-3.5-content-safety), and openrouter/free — that one is a ROUTER which
resolves to a different underlying model per call, breaking the snapshot pinning
the benchmark depends on.
"""
import json
import urllib.request
from pathlib import Path

EXCLUDE = ("lyria", "content-safety", "openrouter/free")
ROOT = Path(__file__).resolve().parents[1]


def slug(mid: str) -> str:
    return "free__" + mid.split("/", 1)[1].replace(":free", "").replace(".", "-")


def main() -> None:
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/models", headers={"User-Agent": "connbench"}
    )
    data = json.load(urllib.request.urlopen(req, timeout=30))["data"]
    rows = []
    for m in data:
        p = m.get("pricing") or {}
        try:
            if float(p.get("prompt", 1)) != 0 or float(p.get("completion", 1)) != 0:
                continue
        except (TypeError, ValueError):
            continue
        if "text" not in ((m.get("architecture") or {}).get("input_modalities") or []):
            continue
        if any(x in m["id"] for x in EXCLUDE):
            continue
        rows.append((m["id"], "response_format" in (m.get("supported_parameters") or [])))
    rows.sort()
    print(f"{len(rows)} free models")
    for mid, _ in rows:
        print(" ", mid)


if __name__ == "__main__":
    main()
