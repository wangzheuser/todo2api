#!/usr/bin/env python3
"""交互式启动批量注册，并记住上次输入。"""

from __future__ import annotations

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


def prompt_text(label: str, current: str) -> str:
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


def terminate_process_tree(process: subprocess.Popen) -> None:
    """强制终止注册进程及其浏览器子进程。"""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()


def run_registration(command: list[str], environment: dict) -> int:
    popen_options = {"env": environment, "cwd": SCRIPT_DIR}
    if os.name == "nt":
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_options["start_new_session"] = True
    process = subprocess.Popen(command, **popen_options)
    try:
        while True:
            try:
                return process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                continue
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在强制结束所有注册和浏览器进程...", flush=True)
        terminate_process_tree(process)
        return 130


def main() -> int:
    saved = load_settings()
    defaults = {
        "count": 5,
        "threads": 2,
        "max_retries": 5,
        "turnstile_concurrency": 2,
        "mailpoolhub_base_url": "http://127.0.0.1:8080/api/v1",
        "mailpoolhub_provider": "auto",
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
        "MailPoolHub API Key", str(settings["mailpoolhub_api_key"])
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
    return run_registration(build_command(settings), environment)


if __name__ == "__main__":
    try:
        exit_code = main()
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，强制结束。", flush=True)
        exit_code = 130
    if "--pause" in sys.argv[1:] and exit_code != 130:
        try:
            input("\n按回车关闭窗口...")
        except EOFError:
            pass
    raise SystemExit(exit_code)
