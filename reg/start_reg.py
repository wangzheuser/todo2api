#!/usr/bin/env python3
"""交互式启动批量注册，并记住上次输入。"""

from __future__ import annotations

import getpass
import json
import os
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = SCRIPT_DIR / "start_reg.settings.json"


def load_settings(path: Path = SETTINGS_FILE) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(settings: dict, path: Path = SETTINGS_FILE) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def prompt_text(label: str, current: str, *, secret: bool = False) -> str:
    if secret:
        prompt = f"{label} [{'已保存，回车复用' if current else '必填'}]: "
        value = getpass.getpass(prompt).strip()
    else:
        value = input(f"{label} [{current}]: ").strip()
    return value or current


def prompt_int(label: str, current: int, *, minimum: int = 1) -> int:
    while True:
        value = input(f"{label} [{current}]: ").strip()
        try:
            result = int(value) if value else current
            if result >= minimum:
                return result
        except ValueError:
            pass
        print(f"请输入不小于 {minimum} 的整数")


def build_command(settings: dict) -> list[str]:
    return [
        sys.executable,
        str(SCRIPT_DIR / "main.py"),
        "--mail-provider", "mailpoolhub",
        "--mailpoolhub-base-url", settings["mailpoolhub_base_url"],
        "--mailpoolhub-provider", settings["mailpoolhub_provider"],
        "--proxy-url", settings["proxy_url"],
        "--max-retries", str(settings["max_retries"]),
        "--turnstile-concurrency", str(settings["turnstile_concurrency"]),
        "--count", str(settings["count"]),
        "--threads", str(settings["threads"]),
    ]


def main() -> int:
    saved = load_settings()
    defaults = {
        "count": 5,
        "threads": 2,
        "max_retries": 5,
        "turnstile_concurrency": 2,
        "mailpoolhub_base_url": "http://127.0.0.1:8080/api/v1",
        "mailpoolhub_provider": "mailgw",
        "mailpoolhub_api_key": os.environ.get("MAILPOOLHUB_API_KEY", ""),
        "proxy_url": os.environ.get("TODO_PROXY_URL", ""),
    }
    settings = {**defaults, **saved}

    print("Todofor.ai 批量注册")
    print("直接按回车使用方括号中的上次配置。\n")
    if not settings["proxy_url"]:
        print("代理示例：http://jp.{uuid}:代理口令@127.0.0.1:9200\n")
    settings["count"] = prompt_int("预期注册总数量", int(settings["count"]))
    settings["threads"] = prompt_int("并发数", int(settings["threads"]))
    settings["max_retries"] = prompt_int("每个账号最大尝试次数", int(settings["max_retries"]))
    settings["turnstile_concurrency"] = prompt_int(
        "浏览器验证并发数",
        min(int(settings["turnstile_concurrency"]), int(settings["threads"])),
    )
    settings["mailpoolhub_base_url"] = prompt_text(
        "MailPoolHub API 地址", str(settings["mailpoolhub_base_url"])
    )
    settings["mailpoolhub_provider"] = prompt_text(
        "MailPoolHub 邮箱渠道", str(settings["mailpoolhub_provider"])
    )
    settings["proxy_url"] = prompt_text("Resin 代理 URL", str(settings["proxy_url"]))
    settings["mailpoolhub_api_key"] = prompt_text(
        "MailPoolHub API Key", str(settings["mailpoolhub_api_key"]), secret=True
    )

    if not settings["mailpoolhub_api_key"]:
        print("MailPoolHub API Key 不能为空")
        return 2
    if not settings["proxy_url"]:
        print("Resin 代理 URL 不能为空")
        return 2
    settings["threads"] = min(settings["threads"], settings["count"])
    settings["turnstile_concurrency"] = min(
        settings["turnstile_concurrency"], settings["threads"]
    )
    save_settings(settings)

    environment = os.environ.copy()
    environment["MAILPOOLHUB_API_KEY"] = settings["mailpoolhub_api_key"]
    print(
        f"\n启动注册：总数={settings['count']}，并发={settings['threads']}，"
        f"浏览器并发={settings['turnstile_concurrency']}，渠道={settings['mailpoolhub_provider']}\n"
    )
    try:
        return subprocess.run(build_command(settings), env=environment, cwd=SCRIPT_DIR).returncode
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
