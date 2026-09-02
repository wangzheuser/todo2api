#!/usr/bin/env python3
"""交互式启动批量注册，并记住上次输入。"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = SCRIPT_DIR / "start_reg.settings.json"
WORKING_CHANNELS_FILE = SCRIPT_DIR / "working_mail_channels.json"
LOG_FILE = SCRIPT_DIR / "start_reg.log"
MAX_LOG_BYTES = 16 * 1024 * 1024


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


def prompt_bool(label: str, current: bool) -> bool:
    default = "Y" if current else "N"
    while True:
        value = input(f"{label} (Y/N) [{default}]: ").strip().lower()
        if not value:
            return current
        if value in {"y", "yes", "1", "true"}:
            return True
        if value in {"n", "no", "0", "false"}:
            return False
        print("请输入 Y 或 N")


def load_working_channels(path: Path | None = None) -> list[dict]:
    path = path or WORKING_CHANNELS_FILE
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    channels = body.get("channels") if isinstance(body, dict) else None
    return channels if isinstance(channels, list) else []


def prompt_mail_provider(current: str) -> str:
    channels = load_working_channels()
    providers = [str(item.get("provider") or "") for item in channels if item.get("provider")]
    if not providers:
        return prompt_text("MailPoolHub 邮箱渠道", current or "auto")
    default = current if current == "random" or current in providers else "random"
    print("\n已实测可用邮箱渠道：")
    print("  0. random（默认，每个账号随机）")
    for index, item in enumerate(channels, 1):
        domains = ", ".join(
            str(domain.get("domain") or "")
            for domain in item.get("domains") or []
        )
        print(
            f"  {index}. {item['provider']} "
            f"({item.get('successes', 0)}/{item.get('attempts', 0)})"
            + (f" [{domains}]" if domains else "")
        )
    while True:
        value = input(f"选择邮箱渠道 [{default}]: ").strip()
        if not value:
            return default
        if value in {"0", "random"}:
            return "random"
        if value.isdigit() and 1 <= int(value) <= len(providers):
            return providers[int(value) - 1]
        if value in providers:
            return value
        print("请输入列表编号、渠道名或直接回车")


def build_command(settings: dict) -> list[str]:
    command = [
        sys.executable,
        str(SCRIPT_DIR / "main.py"),
        "--mail-provider", "mailpoolhub",
        "--mailpoolhub-base-url", settings["mailpoolhub_base_url"],
        "--mailpoolhub-provider", settings["mailpoolhub_provider"],
        "--proxy-url", settings["proxy_url"],
        "--proxy-platforms", settings["proxy_platforms"],
        "--max-retries", str(settings["max_retries"]),
        "--turnstile-concurrency", str(settings["turnstile_concurrency"]),
        "--turnstile-mode", settings["turnstile_mode"],
        "--otp-interval", str(settings["otp_interval_seconds"]),
        "--otp-cooldown", str(settings["otp_cooldown_seconds"]),
        "--network-cooldown", str(settings["network_cooldown_seconds"]),
        "--count", str(settings["count"]),
        "--threads", str(settings["threads"]),
    ]
    if settings.get("cloud_sync_enabled", False):
        command.extend(
            [
                "--cloud-sync",
                "--cloud-sync-url", str(settings["cloud_sync_base_url"]),
                "--cloud-sync-username", str(settings["cloud_sync_admin_username"]),
            ]
        )
    return command


def relay_output(stream, path: Path = LOG_FILE, max_bytes: int = MAX_LOG_BYTES) -> None:
    """将注册输出同步到控制台和定长轮转日志。"""
    logger = logging.getLogger(f"todo2api.reg.{id(stream)}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = RotatingFileHandler(
        path,
        maxBytes=max_bytes,
        backupCount=1,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    try:
        logger.info("===== registration started %s =====", datetime.now().isoformat(timespec="seconds"))
        for line in stream:
            print(line, end="", flush=True)
            logger.info(line.rstrip("\r\n"))
    finally:
        logger.info("===== registration output closed =====")
        logger.removeHandler(handler)
        handler.close()


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
    environment = dict(environment)
    environment["PYTHONUNBUFFERED"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    popen_options = {
        "env": environment,
        "cwd": SCRIPT_DIR,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "bufsize": 1,
    }
    if os.name == "nt":
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_options["start_new_session"] = True
    process = subprocess.Popen(command, **popen_options)
    relay = threading.Thread(target=relay_output, args=(process.stdout,), daemon=True)
    relay.start()
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
    finally:
        relay.join(timeout=3)


def main() -> int:
    saved = load_settings()
    defaults = {
        "count": 5,
        "threads": 2,
        "max_retries": 5,
        "turnstile_concurrency": 2,
        "turnstile_mode": "always",
        "otp_interval_seconds": 40,
        "otp_cooldown_seconds": 3600,
        "network_cooldown_seconds": 60,
        "mailpoolhub_base_url": "http://127.0.0.1:8080/api/v1",
        "mailpoolhub_provider": "auto",
        "mailpoolhub_api_key": os.environ.get("MAILPOOLHUB_API_KEY", ""),
        "proxy_url": os.environ.get("TODO_PROXY_URL", ""),
        "proxy_platforms": "node,jp,us,de,github,baokemeng",
        "cloud_sync_enabled": False,
        "cloud_sync_base_url": "",
        "cloud_sync_admin_username": "",
        "cloud_sync_admin_password": "",
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
    while True:
        settings["turnstile_mode"] = prompt_text(
            "浏览器验证模式（always/auto/off）",
            str(settings["turnstile_mode"]),
        ).lower()
        if settings["turnstile_mode"] in {"always", "auto", "off"}:
            break
        print("请输入 always、auto 或 off")
    settings["otp_interval_seconds"] = prompt_int(
        "OTP 提交最小间隔秒数",
        int(settings["otp_interval_seconds"]),
        minimum=0,
    )
    settings["otp_cooldown_seconds"] = prompt_int(
        "HTTP 429 全局冷却秒数",
        int(settings["otp_cooldown_seconds"]),
        minimum=0,
    )
    settings["network_cooldown_seconds"] = prompt_int(
        "全部代理不可用时冷却秒数",
        int(settings["network_cooldown_seconds"]),
        minimum=0,
    )
    settings["mailpoolhub_base_url"] = prompt_text(
        "MailPoolHub API 地址", str(settings["mailpoolhub_base_url"])
    )
    settings["mailpoolhub_provider"] = prompt_mail_provider(
        str(settings["mailpoolhub_provider"])
    )
    settings["proxy_url"] = prompt_text("Resin 代理 URL", str(settings["proxy_url"]))
    settings["proxy_platforms"] = prompt_text(
        "Resin 平台轮换列表", str(settings["proxy_platforms"])
    )
    settings["mailpoolhub_api_key"] = prompt_text(
        "MailPoolHub API Key", str(settings["mailpoolhub_api_key"])
    )
    settings["cloud_sync_enabled"] = prompt_bool(
        "注册成功后同步到云端账号池",
        bool(settings["cloud_sync_enabled"]),
    )
    if settings["cloud_sync_enabled"]:
        settings["cloud_sync_base_url"] = prompt_text(
            "云端项目访问地址", str(settings["cloud_sync_base_url"])
        )
        settings["cloud_sync_admin_username"] = prompt_text(
            "云端管理员账号", str(settings["cloud_sync_admin_username"])
        )
        settings["cloud_sync_admin_password"] = prompt_text(
            "云端管理员密码", str(settings["cloud_sync_admin_password"])
        )

    if not settings["mailpoolhub_api_key"]:
        print("MailPoolHub API Key 不能为空")
        return 2
    if not settings["proxy_url"]:
        print("Resin 代理 URL 不能为空")
        return 2
    if settings["cloud_sync_enabled"] and not all(
        str(settings[key]).strip()
        for key in (
            "cloud_sync_base_url",
            "cloud_sync_admin_username",
            "cloud_sync_admin_password",
        )
    ):
        print("启用云端同步时，项目地址、管理员账号和密码不能为空")
        return 2
    settings["threads"] = min(settings["threads"], settings["count"])
    settings["turnstile_concurrency"] = min(
        settings["turnstile_concurrency"], settings["threads"]
    )
    save_settings(settings)

    environment = os.environ.copy()
    environment["MAILPOOLHUB_API_KEY"] = settings["mailpoolhub_api_key"]
    if settings["cloud_sync_enabled"]:
        environment["TODO2API_CLOUD_ADMIN_PASSWORD"] = settings[
            "cloud_sync_admin_password"
        ]
    print(
        f"\n启动注册：总数={settings['count']}，并发={settings['threads']}，"
        f"浏览器并发={settings['turnstile_concurrency']}，渠道={settings['mailpoolhub_provider']}\n"
        f"云端同步={'已启用' if settings['cloud_sync_enabled'] else '未启用'}\n"
        f"日志文件：{LOG_FILE}（单文件最大 16 MiB）\n"
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
