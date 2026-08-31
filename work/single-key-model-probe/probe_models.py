#!/usr/bin/env python3
"""Probe every model exposed by a local single-key todo2api gateway."""

import json
import time
from pathlib import Path

import requests


BASE_URL = "http://127.0.0.1:18080"
HEADERS = {
    "Authorization": "Bearer sk-single-key-model-probe",
    "Content-Type": "application/json",
}
RESULTS_FILE = Path(__file__).with_name("model_probe_results.json")


def load_results() -> dict:
    if not RESULTS_FILE.exists():
        return {}
    return {
        item["model"]: item
        for item in json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
    }


def save_results(results: dict) -> None:
    RESULTS_FILE.write_text(
        json.dumps(list(results.values()), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


models = requests.get(f"{BASE_URL}/v1/models", headers=HEADERS, timeout=15).json()["data"]
results = load_results()
for index, model in enumerate(models, 1):
    model_id = model["id"]
    if results.get(model_id, {}).get("status") == "ok":
        continue
    started = time.monotonic()
    try:
        response = requests.post(
            f"{BASE_URL}/v1/chat/completions",
            headers=HEADERS,
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": "Reply with exactly OK."}],
                "max_tokens": 8,
            },
            timeout=200,
        )
        elapsed = round(time.monotonic() - started, 3)
        try:
            body = response.json()
        except ValueError:
            body = {"raw": response.text[:500]}
        content = ""
        if response.ok:
            content = str((((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""))
        results[model_id] = {
            "model": model_id,
            "status": "ok" if response.ok and content else "failed",
            "http_status": response.status_code,
            "latency_seconds": elapsed,
            "response_preview": content[:120] if content else str(body)[:500],
            "declared_context": model.get("context_length", 0),
            "declared_max_completion": model.get("max_completion_tokens", 0),
        }
    except Exception as error:
        results[model_id] = {
            "model": model_id,
            "status": "exception",
            "latency_seconds": round(time.monotonic() - started, 3),
            "error": str(error)[:500],
            "declared_context": model.get("context_length", 0),
            "declared_max_completion": model.get("max_completion_tokens", 0),
        }
    save_results(results)
    item = results[model_id]
    print(
        f"[{index}/{len(models)}] {model_id}: {item['status']} "
        f"HTTP={item.get('http_status', 0)} {item['latency_seconds']}s",
        flush=True,
    )
    if item.get("http_status") == 503:
        raise SystemExit(75)
