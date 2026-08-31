#!/usr/bin/env python3
"""Benchmark every live MailPoolHub provider against the full registration flow."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[2]
REG_DIR = ROOT / "reg"
sys.path.insert(0, str(REG_DIR))

import main as registration
from mail_providers import MailPoolHubProvider


SETTINGS_FILE = REG_DIR / "start_reg.settings.json"
RESULTS_FILE = Path(__file__).with_name("results.json")
ATTEMPTS_PER_PROVIDER = 10
CONCURRENCY = 2
PROXY_ATTEMPTS = 6
PROXY_PLATFORMS = ("jp", "us", "de", "node", "github", "baokemeng")
RATE_LIMIT_WAIT = 10 * 60
MIN_ATTEMPT_INTERVAL = 20
WRITE_LOCK = threading.Lock()


def load_settings() -> dict:
    return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))


def provider_names(settings: dict) -> list[str]:
    response = requests.get(
        settings["mailpoolhub_base_url"].rstrip("/") + "/providers",
        headers={"Authorization": f"Bearer {settings['mailpoolhub_api_key']}"},
        timeout=15,
    )
    response.raise_for_status()
    return [item["name"] for item in response.json()["providers"]]


def load_results() -> dict[tuple[str, int], dict]:
    if not RESULTS_FILE.exists():
        return {}
    return {
        (item["provider"], int(item["attempt"])): item
        for item in json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
    }


def save_results(results: dict[tuple[str, int], dict]) -> None:
    with WRITE_LOCK:
        temporary = RESULTS_FILE.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(list(results.values()), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(RESULTS_FILE)


def new_todo(settings: dict) -> registration.TodoforAI:
    template = os.environ.get("BENCHMARK_PROXY_URL") or settings["proxy_url"]
    last_error: Exception | None = None
    for attempt in range(PROXY_ATTEMPTS):
        candidate_template = re.sub(
            r"(?<=://)[^.]+(?=\.)",
            PROXY_PLATFORMS[attempt % len(PROXY_PLATFORMS)],
            template,
            count=1,
        )
        todo = registration.TodoforAI(
            proxies=registration.resolve_proxy_templates(
                {"http": candidate_template, "https": candidate_template}
            )
        )
        if todo.init_anonymous_session():
            return todo
        todo.session.close()
        last_error = RuntimeError("anonymous session failed")
    raise last_error or RuntimeError("no usable proxy")


def classify_send_failure(todo: registration.TodoforAI) -> str:
    message = todo.last_otp_error_message.casefold()
    if "temporary email" in message:
        return "rejected_temp_email"
    if todo.last_otp_error_code == "CAPTCHA_REQUIRED" or "captcha" in message or "browser verification" in message:
        return "captcha_required"
    if todo.last_otp_status == 429:
        return "rate_limited"
    return "otp_send_failed"


def test_once(provider_name: str, attempt: int, settings: dict) -> dict:
    started = time.monotonic()
    provider = MailPoolHubProvider(
        base_url=settings["mailpoolhub_base_url"],
        api_key=settings["mailpoolhub_api_key"],
        request_timeout=45,
        poll_interval_seconds=5,
        ttl_seconds=900,
        provider_name=provider_name,
    )
    account = None
    todo = None
    result = {
        "provider": provider_name,
        "attempt": attempt,
        "status": "",
        "domain": "",
        "detail": "",
        "elapsed_seconds": 0,
    }
    try:
        account = provider.create_account()
        address = str(account.get("address") or "")
        result["domain"] = str(account.get("domain") or address.rsplit("@", 1)[-1]).lower()
        if result["domain"] in registration.load_rejected_domains():
            result["status"] = "known_rejected_temp_email"
            return result
        todo = new_todo(settings)
        otp_sent = todo.send_otp(address)
        if not otp_sent:
            status = classify_send_failure(todo)
            if status != "captcha_required":
                result["status"] = status
                result["detail"] = todo.last_otp_error_message[:240]
                if status == "rejected_temp_email":
                    registration.record_rejected_domain(result["domain"])
                return result

            last_error: Exception | None = None
            for _ in range(PROXY_ATTEMPTS):
                try:
                    token = registration.solve_turnstile(todo.proxy_url, timeout=45)
                    if todo.send_otp(address, token):
                        break
                    if classify_send_failure(todo) == "rate_limited":
                        result["status"] = "rate_limited"
                        return result
                    last_error = RuntimeError(todo.last_otp_error_message)
                except Exception as error:
                    last_error = error
                todo.session.close()
                todo = new_todo(settings)
            else:
                result["status"] = "captcha_failed"
                result["detail"] = str(last_error or "captcha failed")[:240]
                return result

        code = provider.wait_for_code(account, timeout=60)
        if not code:
            result["status"] = "no_code"
            return result
        login = todo.verify_otp(address, code)
        if not login:
            result["status"] = "verify_failed"
            return result
        keys = todo.get_api_keys()
        default_key = todo.get_default_api_key()
        if not keys and not default_key:
            result["status"] = "no_api_key"
            return result
        result["status"] = "ok"
        return result
    except Exception as error:
        result["status"] = "mailbox_create_failed" if account is None else "exception"
        result["detail"] = str(error)[:240]
        return result
    finally:
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        if todo is not None:
            todo.session.close()
        if account is not None:
            try:
                provider.close_account(account)
            except Exception:
                pass
        remaining_gap = MIN_ATTEMPT_INTERVAL - (time.monotonic() - started)
        if remaining_gap > 0:
            time.sleep(remaining_gap)


def main() -> int:
    settings = load_settings()
    names = provider_names(settings)
    results = load_results()
    print(f"providers={len(names)} attempts={ATTEMPTS_PER_PROVIDER} concurrency={CONCURRENCY}")
    for provider_index, name in enumerate(names, 1):
        missing = [
            attempt
            for attempt in range(1, ATTEMPTS_PER_PROVIDER + 1)
            if (name, attempt) not in results
        ]
        if missing:
            with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
                futures = {
                    pool.submit(test_once, name, attempt, settings): attempt
                    for attempt in missing
                }
                for future in as_completed(futures):
                    item = future.result()
                    results[(name, item["attempt"])] = item
                    save_results(results)
                    print(
                        f"[{provider_index}/{len(names)}] {name} "
                        f"{item['attempt']}/{ATTEMPTS_PER_PROVIDER}: {item['status']} "
                        f"domain={item['domain']} {item['elapsed_seconds']}s",
                        flush=True,
                    )
        provider_results = [results[(name, attempt)] for attempt in range(1, ATTEMPTS_PER_PROVIDER + 1)]
        counts = Counter(item["status"] for item in provider_results)
        print(f"SUMMARY {name}: {dict(counts)}", flush=True)
        if missing and counts.get("rate_limited"):
            print(f"RATE LIMIT COOLDOWN {RATE_LIMIT_WAIT}s", flush=True)
            time.sleep(RATE_LIMIT_WAIT)
        else:
            time.sleep(2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
