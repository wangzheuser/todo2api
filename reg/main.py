#!/usr/bin/env python3
"""
Todofor.ai 批量注册机
=====================
通过可配置临时邮箱渠道 + Email OTP 登录 todofor.ai 并获取 API Key。
支持单线程/多线程批量操作，结果保存到同目录 get_apikey.txt。

用法:
  # 默认通过本机 MailPoolHub 临时邮箱 API 运行
  python main.py

  # 单线程注册 1 个
  python main.py -n 1

  # 5 线程并发注册 10 个
  python main.py -n 10 -t 5

  # 命令行覆盖 config 中的 Key
  python main.py -k AC-xxx -n 50 -t 10

依赖: pip install -r requirements.txt
"""

import os
import sys
import json
import time
import threading
import argparse
import re
import traceback
import requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Callable
from urllib.parse import unquote, urlparse
from uuid import uuid4

from mail_providers import (
    DEFAULT_MAILPOOLHUB_BASE_URL,
    MailPoolHubProvider,
    TemporaryMailProvider,
    YYDSMailProvider,
)

# ============================================================
# 全局常量
# ============================================================

TODO_BASE = "https://api.todofor.ai"
TODO_ORIGIN = "https://todofor.ai"
TURNSTILE_SITE_KEY = "0x4AAAAAAEP6KBMVDx9FVTzd"
MAILPOOLHUB_REGISTRATION_PROVIDERS = (
    "tempmail_lol",
)

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_FILE = SCRIPT_DIR / "config.json"
OUTPUT_FILE = SCRIPT_DIR / "get_apikey.txt"

# 线程安全锁
FILE_LOCK = threading.Lock()
PRINT_LOCK = threading.Lock()


def normalize_proxy_url(proxy_url: str) -> str:
    """兼容 Resin 的 ``scheme://platform.account:token:port`` 简写。"""
    value = str(proxy_url or "").strip()
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError:
        port = None
    if parsed.scheme in {"http", "https", "socks5"} and parsed.hostname and port:
        return value

    shorthand = re.fullmatch(
        r"(?P<scheme>https?|socks5)://(?P<username>[^:/@]+):(?P<password>[^:/@]+):(?P<port>\d+)",
        value,
    )
    if shorthand:
        port = int(shorthand.group("port"))
        if 1 <= port <= 65535:
            return (
                f"{shorthand.group('scheme')}://{shorthand.group('username')}:"
                f"{shorthand.group('password')}@127.0.0.1:{port}"
            )
    raise ValueError(
        "代理 URL 无效；请使用 http://用户:密码@主机:端口，"
        "或 Resin 简写 http://平台.{uuid}:口令:端口"
    )


def resolve_proxy_templates(proxies: dict | None) -> dict | None:
    """为单次注册解析共享的 Resin 粘性会话 UUID。"""
    if not proxies:
        return None
    session_id = uuid4().hex
    return {
        key: normalize_proxy_url(value).replace("{uuid}", session_id)
        for key, value in proxies.items()
    }


def solve_turnstile(
    proxy_url: str = "",
    *,
    site_key: str = TURNSTILE_SITE_KEY,
    browser_channel: str = "chrome",
    headless: bool = False,
    timeout: int = 45,
) -> str:
    """在目标页面生成 Turnstile token，并保持与业务请求相同的代理出口。"""
    try:
        from patchright.sync_api import sync_playwright
    except ImportError as error:
        raise RuntimeError("缺少 patchright，请执行 pip install -r requirements.txt") from error

    proxy_options = None
    if proxy_url:
        parsed = urlparse(proxy_url)
        if parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname or not parsed.port:
            raise ValueError("Turnstile 代理必须是完整 URL")
        proxy_options = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
        if parsed.username:
            proxy_options["username"] = unquote(parsed.username)
            proxy_options["password"] = unquote(parsed.password or "")

    script = f"""
    (() => {{
      const container = document.createElement('div');
      container.id = 'todo2api-turnstile';
      document.body.appendChild(container);
      const load = attempt => {{
        const script = document.createElement('script');
        script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit&retry=' + attempt;
        script.onload = () => {{
          try {{
            window.turnstile.render('#todo2api-turnstile', {{
              sitekey: {json.dumps(site_key)},
              callback: token => document.body.dataset.turnstileToken = token,
              'error-callback': code => document.body.dataset.turnstileError = 'error:' + code,
              'timeout-callback': () => document.body.dataset.turnstileError = 'timeout'
            }});
          }} catch (error) {{
            document.body.dataset.turnstileError = 'render:' + error;
          }}
        }};
        script.onerror = () => attempt < 5
          ? setTimeout(() => load(attempt + 1), 500)
          : document.body.dataset.turnstileError = 'load';
        document.head.appendChild(script);
      }};
      load(0);
    }})();
    """

    with sync_playwright() as playwright:
        launch_options = {"headless": headless}
        if browser_channel:
            launch_options["channel"] = browser_channel
        browser = playwright.chromium.launch(**launch_options)
        context_options = {"viewport": {"width": 1000, "height": 700}}
        if proxy_options:
            context_options["proxy"] = proxy_options
        context = browser.new_context(**context_options)
        page = context.new_page()
        try:
            response = page.goto(TODO_ORIGIN, wait_until="domcontentloaded", timeout=30_000)
            if not response or response.status != 200:
                raise RuntimeError(f"Turnstile 页面加载失败：HTTP {response.status if response else 0}")
            page.add_script_tag(content=script)
            deadline = time.monotonic() + max(10, timeout)
            while time.monotonic() < deadline:
                page.wait_for_timeout(1000)
                token = page.locator("body").get_attribute("data-turnstile-token")
                if token:
                    return token
                error = page.locator("body").get_attribute("data-turnstile-error")
                if error and error != "timeout":
                    raise RuntimeError(f"Turnstile 求解失败：{error}")
                for frame in page.frames:
                    if "challenges.cloudflare.com" not in frame.url:
                        continue
                    try:
                        frame.locator("input[type=checkbox]").first.click(timeout=500, force=True)
                    except Exception:
                        pass
            raise TimeoutError("Turnstile 求解超时")
        finally:
            browser.close()


# ============================================================
# 配置加载
# ============================================================

def load_config(config_path: Path | None = None) -> dict:
    """
    加载 config.json。
    返回顶层对象；文件不存在或格式错误时返回空对象。
    """
    path = config_path or DEFAULT_CONFIG_FILE
    if not path.exists():
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            return {}
        return cfg
    except (json.JSONDecodeError, OSError) as e:
        print(f"⚠️  读取配置文件失败 ({path}): {e}")
        return {}


def resolve_api_key(
    cli_key: str,
    config_path: Path | None = None,
    config: dict | None = None,
) -> str:
    """
    按优先级解析 YYDS API Key:
      1. 命令行 -k
      2. 环境变量 YYDS_API_KEY
      3. config.json 中的 yyds.api_key 或旧版 yyds_api_key
    返回解析到的 key，或空字符串。
    """
    # 1. 命令行
    if cli_key:
        return cli_key

    # 2. 环境变量
    env_key = os.environ.get("YYDS_API_KEY", "")
    if env_key:
        return env_key

    # 3. config 文件
    cfg = config if isinstance(config, dict) else load_config(config_path)
    yyds_cfg = cfg.get("yyds") if isinstance(cfg.get("yyds"), dict) else {}
    file_key = yyds_cfg.get("api_key") or cfg.get("yyds_api_key", "")
    if file_key:
        return file_key

    return ""


def create_mail_provider_factory(
    provider_name: str,
    config: dict,
    *,
    yyds_api_key: str = "",
    mailpoolhub_base_url: str = "",
    mailpoolhub_api_key: str = "",
    mailpoolhub_provider_name: str = "",
    proxies: dict | None = None,
) -> tuple[Callable[[], TemporaryMailProvider], str]:
    """根据配置创建线程隔离的临时邮箱 Provider 工厂。"""
    normalized_name = str(provider_name or "mailpoolhub").strip().lower()
    if normalized_name == "mailpoolhub":
        mailpoolhub_cfg = config.get("mailpoolhub")
        if not isinstance(mailpoolhub_cfg, dict):
            mailpoolhub_cfg = {}
        base_url = (
            mailpoolhub_base_url
            or os.environ.get("MAILPOOLHUB_BASE_URL", "")
            or mailpoolhub_cfg.get("base_url")
            or DEFAULT_MAILPOOLHUB_BASE_URL
        )
        api_key = (
            mailpoolhub_api_key
            or os.environ.get("MAILPOOLHUB_API_KEY", "")
            or mailpoolhub_cfg.get("api_key")
            or ""
        )
        if not str(api_key).strip():
            raise ValueError(
                "未找到 MailPoolHub API Key，请设置 MAILPOOLHUB_API_KEY 或 mailpoolhub.api_key"
            )
        request_timeout = int(mailpoolhub_cfg.get("request_timeout") or 30)
        poll_interval_seconds = int(mailpoolhub_cfg.get("poll_interval_seconds") or 5)
        ttl_seconds = int(mailpoolhub_cfg.get("ttl_seconds") or 900)
        forced_provider_name = str(mailpoolhub_provider_name or mailpoolhub_cfg.get("provider") or "")
        configured_provider_names = mailpoolhub_cfg.get("providers")
        if not isinstance(configured_provider_names, list):
            configured_provider_names = list(MAILPOOLHUB_REGISTRATION_PROVIDERS)
        provider_names = list(configured_provider_names)
        if forced_provider_name and forced_provider_name.lower() != "auto":
            provider_names.insert(0, forced_provider_name)

        def create_mailpoolhub_provider() -> TemporaryMailProvider:
            """为单个注册任务创建独立 MailPoolHub Session。"""
            return MailPoolHubProvider(
                base_url=str(base_url),
                api_key=str(api_key),
                request_timeout=request_timeout,
                poll_interval_seconds=poll_interval_seconds,
                ttl_seconds=ttl_seconds,
                provider_name=forced_provider_name,
                provider_names=provider_names,
            )

        return create_mailpoolhub_provider, f"MailPoolHub ({str(base_url).rstrip('/')})"

    if normalized_name == "yyds":
        if not yyds_api_key or not yyds_api_key.startswith("AC-"):
            raise ValueError("未找到有效的 YYDS Mail API Key（应以 AC- 开头）")

        def create_yyds_provider() -> TemporaryMailProvider:
            """为单个注册任务创建独立 YYDS Mail Session。"""
            return YYDSMailProvider(yyds_api_key, proxies=proxies)

        return create_yyds_provider, "YYDS Mail"

    raise ValueError(f"未知临时邮箱渠道：{normalized_name}")

# 通用 HTTP 请求头
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log(thread_id, msg):
    """线程安全的日志输出"""
    with PRINT_LOCK:
        print(f"[{ts()}][#{thread_id}] {msg}")


# ============================================================
# Todofor.ai 接口
# ============================================================

class TodoforAI:
    """Todofor.ai API 封装"""

    def __init__(self, proxies: dict | None = None):
        """创建目标业务 HTTP 会话并初始化最近一次 OTP 错误状态。"""
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.last_otp_error_code = ""
        self.last_otp_error_message = ""
        self.last_otp_status = 0
        self.proxy_url = str((proxies or {}).get("https") or (proxies or {}).get("http") or "")
        if proxies:
            self.session.proxies.update(proxies)

    def _api_headers(self):
        return {
            "Origin": TODO_ORIGIN,
            "Referer": f"{TODO_ORIGIN}/",
        }

    # ---- 会话初始化 ----

    def init_anonymous_session(self) -> bool:
        """
        初始化匿名会话，获取 __Secure-better-auth Cookie。
        ① 访问主站 ② 调用 apiKey.getDefault 触发匿名会话创建。
        """
        try:
            # 访问主站获取初始 Cookie
            self.session.get(TODO_ORIGIN, timeout=30, allow_redirects=True)
        except requests.RequestException:
            pass  # 即使失败也继续尝试

        try:
            # 调用 getDefault 完成会话初始化
            input_json = json.dumps({"0": {}}, separators=(",", ":"))
            self.session.get(
                f"{TODO_BASE}/trpc/cookie/apiKey.getDefault",
                params={"batch": "1", "input": input_json},
                headers=self._api_headers(),
                timeout=30,
            )
            return True
        except requests.RequestException as e:
            log(0, f"匿名会话初始化请求失败: {e}")
            return False

    # ---- 认证 ----

    def send_otp(self, email: str, captcha_token: str = "") -> bool:
        """发送邮箱登录验证码并保留服务端业务错误。"""
        self.last_otp_error_code = ""
        self.last_otp_error_message = ""
        self.last_otp_status = 0
        headers = {**self._api_headers(), "Content-Type": "application/json"}
        if captcha_token:
            headers["x-captcha-response"] = captcha_token
        resp = self.session.post(
            f"{TODO_BASE}/api/auth/email-otp/send-verification-otp",
            json={"email": email, "type": "sign-in"},
            headers=headers,
            timeout=30,
        )
        self.last_otp_status = resp.status_code
        try:
            body = resp.json()
        except json.JSONDecodeError:
            self.last_otp_error_code = "INVALID_RESPONSE"
            self.last_otp_error_message = f"HTTP {resp.status_code} 返回非 JSON 响应"
            return False

        if not isinstance(body, dict):
            self.last_otp_error_code = "INVALID_RESPONSE"
            self.last_otp_error_message = "OTP 接口响应结构无效"
            return False
        if body.get("success") is True:
            return True

        error_body = body.get("error") if isinstance(body.get("error"), dict) else {}
        self.last_otp_error_code = str(
            body.get("code") or error_body.get("code") or f"HTTP_{resp.status_code}"
        )
        self.last_otp_error_message = str(
            body.get("message") or error_body.get("message") or "OTP 发送失败"
        )
        return False

    def verify_otp(self, email: str, otp: str) -> dict | None:
        """
        提交 OTP 完成登录。
        成功后 Cookie 自动更新为正式用户会话。
        返回: {token, user: {id, name, email, isAnonymous, emailVerified, ...}} 或 None
        """
        resp = self.session.post(
            f"{TODO_BASE}/api/auth/sign-in/email-otp",
            json={"email": email, "otp": otp},
            headers={**self._api_headers(), "Content-Type": "application/json"},
            timeout=30,
        )
        try:
            data = resp.json()
        except json.JSONDecodeError:
            return None
        if "token" in data and "user" in data:
            return data
        return None

    def get_session(self) -> dict | None:
        """获取当前 Better Auth 会话信息"""
        resp = self.session.get(
            f"{TODO_BASE}/api/auth/get-session",
            headers=self._api_headers(),
            timeout=30,
        )
        try:
            return resp.json()
        except json.JSONDecodeError:
            return None

    # ---- tRPC 通用调用 ----

    def _trpc_get(self, procedures: str, input_obj: dict) -> list | dict:
        """
        tRPC GET 批量请求。
        返回: 解析后的 JSON（可能是 list 或 dict），出错返回空 list。
        """
        input_json = json.dumps(input_obj, separators=(",", ":"))
        resp = self.session.get(
            f"{TODO_BASE}/trpc/cookie/{procedures}",
            params={"batch": "1", "input": input_json},
            headers=self._api_headers(),
            timeout=30,
        )

        # 非 200 直接返回空
        if resp.status_code != 200:
            log(0, f"⚠️ tRPC {procedures} 返回 HTTP {resp.status_code}: {resp.text[:200]}")
            return []

        try:
            data = resp.json()
        except json.JSONDecodeError:
            log(0, f"⚠️ tRPC {procedures} 返回非 JSON: {resp.text[:200]}")
            return []

        return data

    @staticmethod
    def _unwrap_first(data: list | dict) -> dict | list | None:
        """
        从 tRPC 响应中取出第一个结果。
        支持两种格式:
          - 批处理: [{"result": {"data": ...}}, ...]
          - 单对象: {"result": {"data": ...}}
        """
        if isinstance(data, list) and len(data) > 0:
            first = data[0]
            if isinstance(first, dict):
                result = first.get("result", {})
                if isinstance(result, dict):
                    return result.get("data", result)
        if isinstance(data, dict):
            result = data.get("result", {})
            if isinstance(result, dict):
                return result.get("data", result)
        return None

    # ---- 业务查询 ----

    def get_api_keys(self) -> list[dict]:
        """获取 API Key 列表"""
        data = self._trpc_get("apiKey.list", {"0": {}})
        keys = self._unwrap_first(data)
        if isinstance(keys, list):
            return keys
        if isinstance(keys, dict):
            return [keys]
        return []

    def get_default_api_key(self) -> dict | None:
        """获取默认 API Key 详情"""
        data = self._trpc_get("apiKey.getDefault", {"0": {}})
        key = self._unwrap_first(data)
        return key if isinstance(key, dict) else None

    def get_user_profile(self) -> dict | None:
        """获取用户资料"""
        data = self._trpc_get("user.profile,user.profile", {"0": {}, "1": {}})
        profile = self._unwrap_first(data)
        return profile if isinstance(profile, dict) else None


# ============================================================
# 辅助函数
# ============================================================

def extract_api_key_value(key_obj: dict) -> str | None:
    """从 API Key 对象中提取实际 key 值"""
    if not isinstance(key_obj, dict):
        return str(key_obj)
    # 常见 key 值字段名
    for field in ("key", "apiKey", "api_key", "secret", "token", "value", "rawKey"):
        v = key_obj.get(field)
        if v and isinstance(v, str) and len(v) > 8:
            return v
    # todofor.ai: key 值在 "id" 字段（64 位 hex 字符串）
    id_val = key_obj.get("id", "")
    if id_val and isinstance(id_val, str) and len(id_val) >= 32:
        return id_val
    # 兜底：找任意非元数据字段
    meta_fields = {"name", "createdAt", "updatedAt", "isDefault", "lastUsed", "id"}
    non_meta = {k for k in key_obj if k not in meta_fields}
    if non_meta:
        return key_obj.get(next(iter(non_meta)), "")
    return None


def format_api_key_output(api_keys: list, default_key: dict | None) -> str:
    """格式化 API Key 输出"""
    lines = []
    seen_values = set()

    all_keys = list(api_keys)
    if default_key and default_key not in all_keys:
        all_keys.append(default_key)

    for i, k in enumerate(all_keys):
        val = extract_api_key_value(k)
        if val and val not in seen_values:
            seen_values.add(val)
            name = k.get("name", "")
            lines.append(f"API Key #{i+1}: {val}" + (f" ({name})" if name else ""))
        elif not val:
            lines.append(f"API Key #{i+1} (raw): {json.dumps(k, ensure_ascii=False)}")

    return "\n".join(lines) if lines else "(未能提取到 API Key)"


# ============================================================
# 单账号注册流程
# ============================================================

def process_one_account(
    thread_id: int,
    mail_provider_factory: Callable[[], TemporaryMailProvider],
    domain: str = "",
    exclude_domains: list[str] | None = None,
    proxies: dict | None = None,
    turnstile_solver: Callable[[str], str] | None = None,
    max_retries: int = 5,
    proxy_attempts: int = 5,
    initial_delay: float = 0,
    verbose: bool = False,
) -> dict | None:
    """
    执行单个账号完整流程:
      创建临时邮箱 → 匿名会话 → 发送 OTP → 等验证码 → 登录 → 获取 API Key

    如果 OTP 发送失败，自动换域名重试。
    """
    try:
        mail_provider = mail_provider_factory()
    except Exception as error:
        log(thread_id, f"❌ 临时邮箱渠道初始化失败: {error}")
        return None
    if initial_delay > 0:
        time.sleep(initial_delay)
    failed_domains: list[str] = []

    for attempt in range(1, max_retries + 1):
        account: dict | None = None
        todo: TodoforAI | None = None
        captcha_token = ""
        try:
            last_session_error: Exception | None = None
            for proxy_attempt in range(1, max(1, proxy_attempts if proxies else 1) + 1):
                candidate = TodoforAI(proxies=resolve_proxy_templates(proxies))
                try:
                    if not candidate.init_anonymous_session():
                        raise requests.RequestException("匿名会话初始化失败")
                    if turnstile_solver:
                        log(thread_id, "🧩 获取浏览器验证令牌...")
                        captcha_token = turnstile_solver(candidate.proxy_url)
                    todo = candidate
                    break
                except Exception as error:
                    last_session_error = error
                    session = getattr(candidate, "session", None)
                    if session is not None:
                        session.close()
                    if proxy_attempt < proxy_attempts:
                        log(thread_id, f"   当前代理或浏览器不可用，换 UUID... ({proxy_attempt}/{proxy_attempts})")
            if todo is None:
                raise last_session_error or requests.RequestException("匿名会话初始化失败")

            # ── 1. 创建临时邮箱 ──
            current_exclude = (exclude_domains or []) + failed_domains
            log(thread_id, f"📧 创建临时邮箱... (尝试 {attempt}/{max_retries})")
            account = mail_provider.create_account(
                domain=domain,
                exclude_domains=current_exclude if current_exclude else None,
            )
            email = account["address"]
            used_domain = account.get("domain", email.split("@")[-1])
            log(thread_id, f"   邮箱: {email}")
            if account.get("provider"):
                log(thread_id, f"   渠道: {account['provider']}")

            # ── 2. 发送验证码 ──
            log(thread_id, "📤 发送 OTP...")
            otp_sent = todo.send_otp(email, captcha_token)
            if not otp_sent and getattr(todo, "last_otp_error_code", "") == "CAPTCHA_REQUIRED" and turnstile_solver:
                log(thread_id, "🧩 浏览器令牌已过期，重新获取...")
                captcha_token = turnstile_solver(todo.proxy_url)
                otp_sent = todo.send_otp(email, captcha_token)

            if not otp_sent:
                error_code = str(getattr(todo, "last_otp_error_code", "") or "UNKNOWN")
                error_message = str(getattr(todo, "last_otp_error_message", "") or "OTP 发送失败")
                log(
                    thread_id,
                    f"❌ OTP 发送失败 [{error_code}] HTTP "
                    f"{getattr(todo, 'last_otp_status', 0)}: {error_message}",
                )

                # 浏览器校验和临时邮箱策略不会因更换域名或会话而消失。
                normalized_message = error_message.casefold()
                non_retryable = error_code in {"CAPTCHA_REQUIRED"} or any(
                    text in normalized_message
                    for text in ("temporary email", "browser verification", "captcha")
                )
                if non_retryable:
                    if error_code == "CAPTCHA_REQUIRED" and turnstile_solver and attempt < max_retries:
                        log(thread_id, "   浏览器令牌未通过，换代理重试...")
                        time.sleep(min(2 ** (attempt - 1), 10))
                        continue
                    log(thread_id, "   目标业务要求浏览器验证或永久邮箱，本轮停止域名重试")
                    return None

                log(thread_id, f"   域名 {used_domain} 可能被目标业务拒绝")
                failed_domains.append(used_domain)
                if attempt < max_retries:
                    log(thread_id, f"   换域名重试...")
                    time.sleep(min(2 ** (attempt - 1), 10))
                    continue
                return None

            # ── 4. 等待验证码 ──
            log(thread_id, "⏳ 等待验证码邮件...")
            otp = mail_provider.wait_for_code(account, timeout=120)
            if not otp:
                log(thread_id, "❌ 超时未收到验证码")
                if attempt < max_retries:
                    time.sleep(min(2 ** (attempt - 1), 10))
                    continue
                return None
            log(thread_id, "   已收到验证码")

            # ── 5. 提交 OTP 登录 ──
            log(thread_id, "🔐 登录中...")
            login_result = todo.verify_otp(email, otp)
            if not login_result:
                log(thread_id, "❌ 登录失败（OTP 错误或已过期）")
                if attempt < max_retries:
                    time.sleep(min(2 ** (attempt - 1), 10))
                    continue
                return None

            user = login_result["user"]
            log(thread_id, f"✅ 登录成功! user_id={user.get('id')}  isAnonymous={user.get('isAnonymous')}")

            # 短暂等待
            time.sleep(1)

            # ── 6. 获取 API Key ──
            log(thread_id, "🔑 获取 API Key...")

            try:
                api_keys = todo.get_api_keys()
                default_key = todo.get_default_api_key()

                # 日志只保留结构信息，避免输出完整 API Key。
                log(thread_id, f"   apiKey.list → {len(api_keys)} 个")
                if default_key:
                    log(thread_id, "   apiKey.getDefault → 已获取")
                else:
                    log(thread_id, "   apiKey.getDefault → null")

                # 如果 list 为空但 getDefault 有数据，使用 default
                if not api_keys and default_key:
                    api_keys = [default_key]
            except Exception as e:
                log(thread_id, f"⚠️ 获取 API Key 异常: {e}")
                api_keys = []
                default_key = None

            if not api_keys:
                log(thread_id, "❌ 登录成功但未取得 API Key")
                if attempt < max_retries:
                    time.sleep(min(2 ** (attempt - 1), 10))
                    continue
                return None

            # ── 7. 组装结果 ──
            result = {
                "thread_id": thread_id,
                "email": email,
                "user_id": user.get("id", ""),
                "user_name": user.get("name", ""),
                "email_verified": user.get("emailVerified", False),
                "api_keys": api_keys,
                "default_key": default_key,
                "token": login_result.get("token", ""),
                "created_at": user.get("createdAt", ""),
                "updated_at": user.get("updatedAt", ""),
            }

            key_count = len(api_keys)
            raw = extract_api_key_value(api_keys[0]) if api_keys else None
            key_preview = f"{raw[:6]}...{raw[-4:]}" if raw and len(raw) > 10 else "N/A"
            log(thread_id, f"✅ 完成! Keys: {key_count} 个, 首个: {key_preview}")
            return result

        except requests.RequestException as e:
            log(thread_id, f"❌ 网络异常 (尝试 {attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                time.sleep(min(2 ** (attempt - 1), 10))
                continue
            return None
        except Exception as e:
            log(thread_id, f"❌ 异常 (尝试 {attempt}/{max_retries}): {e}")
            if verbose:
                traceback.print_exc()
            if attempt < max_retries:
                time.sleep(min(2 ** (attempt - 1), 10))
                continue
            return None
        finally:
            if todo is not None:
                session = getattr(todo, "session", None)
                if session is not None:
                    session.close()
            if account:
                try:
                    mail_provider.close_account(account)
                except Exception as cleanup_error:
                    # 清理失败不覆盖注册流程的原始结果。
                    log(thread_id, f"⚠️ 临时邮箱清理失败: {cleanup_error}")

    return None


def save_result(result: dict):
    """线程安全地将 API Key 追加到 get_apikey.txt（一行一个）"""
    with FILE_LOCK:
        with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
            keys = result.get("api_keys", [])
            dk = result.get("default_key")
            all_keys = list(keys)
            if dk and dk not in all_keys:
                all_keys.append(dk)

            written = 0
            for k in all_keys:
                val = extract_api_key_value(k)
                if val:
                    f.write(f"{val}\n")
                    written += 1

    log(result["thread_id"], f"💾 已写入 {OUTPUT_FILE.name} ({written} 个 key)")


# ============================================================
# 主入口
# ============================================================

def main() -> int:
    """解析配置并执行注册任务，按最终业务结果返回进程退出码。"""
    parser = argparse.ArgumentParser(
        description="Todofor.ai 批量注册机 — 可配置临时邮箱 + OTP 登录 → 获取 API Key",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py                                      # 使用 config.json 中的渠道
  python main.py --mail-provider mailpoolhub -n 10 -t 5 # MailPoolHub 5 线程注册 10 个
  python main.py --mail-provider yyds -k AC-xxx -n 5 # 临时切换 YYDS Mail
  python main.py --check-mail-provider                # 检查临时邮箱渠道

MailPoolHub 配置优先级: 命令行 > 环境变量 > config.json > 本机默认地址
        """,
    )
    parser.add_argument("-n", "--count", type=int, default=1,
                        help="注册数量（默认: 1）")
    parser.add_argument("-t", "--threads", type=int, default=1,
                        help="并发线程数（默认: 1）")
    parser.add_argument("-k", "--api-key", type=str, default="",
                        help="YYDS Mail API Key（只在 yyds 渠道生效）")
    parser.add_argument("-c", "--config", type=str, default="",
                        help="配置文件路径（默认: 同目录 config.json）")
    parser.add_argument("-d", "--domain", type=str, default="",
                        help="指定临时邮箱域名（空则由渠道自动选择）")
    parser.add_argument("--mail-provider", choices=("mailpoolhub", "yyds"), default="",
                        help="临时邮箱渠道（默认读取 config.json）")
    parser.add_argument("--mailpoolhub-base-url", type=str, default="",
                        help="MailPoolHub 客户端 API 地址（默认: http://127.0.0.1:8080/api/v1）")
    parser.add_argument("--mailpoolhub-api-key", type=str, default="",
                        help="MailPoolHub API Key（优先推荐使用环境变量）")
    parser.add_argument("--mailpoolhub-provider", type=str, default="",
                        help="固定 MailPoolHub 下游渠道（推荐: mailgw）")
    parser.add_argument("--proxy-url", type=str, default="",
                        help="目标业务代理 URL，支持 {uuid} Resin 粘性会话模板")
    parser.add_argument("--max-retries", type=int, default=5,
                        help="每个账号最大尝试次数（默认: 5）")
    parser.add_argument("--turnstile-concurrency", type=int, default=0,
                        help="浏览器验证并发数（默认: min(线程数, 2)）")
    parser.add_argument("--check-mail-provider", action="store_true",
                        help="检查当前临时邮箱渠道并退出")
    parser.add_argument("--list-domains", action="store_true",
                        help="列出当前临时邮箱渠道可用域名并退出")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="显示详细调试信息")
    args = parser.parse_args()

    # ── 解析配置文件路径 ──
    config_path = Path(args.config) if args.config else None

    # ── 解析域名配置 ──
    cfg = load_config(config_path)
    domain = args.domain or cfg.get("domain", "")
    exclude_domains: list[str] = cfg.get("exclude_domains", [])
    if not isinstance(exclude_domains, list):
        exclude_domains = []

    # ── 解析代理配置 ──
    proxy_cfg = cfg.get("proxy", {})
    proxies: dict | None = None
    if args.proxy_url:
        proxies = {"http": args.proxy_url, "https": args.proxy_url}
    elif isinstance(proxy_cfg, dict) and proxy_cfg:
        # 支持 "http": "http://host:port" 格式
        proxies = {k: v for k, v in proxy_cfg.items() if v and isinstance(v, str)} or None
    if proxies:
        try:
            proxies = {key: normalize_proxy_url(value) for key, value in proxies.items()}
        except ValueError as error:
            print(f"❌ 代理配置错误：{error}")
            sys.exit(1)

    turnstile_cfg = cfg.get("turnstile") if isinstance(cfg.get("turnstile"), dict) else {}
    turnstile_solver = None
    if turnstile_cfg.get("enabled", True):
        turnstile_concurrency = args.turnstile_concurrency or int(
            turnstile_cfg.get("max_concurrency") or min(args.threads, 2)
        )
        turnstile_gate = threading.BoundedSemaphore(max(1, turnstile_concurrency))

        def turnstile_solver(proxy_url: str) -> str:
            with turnstile_gate:
                return solve_turnstile(
                    proxy_url,
                    site_key=str(turnstile_cfg.get("site_key") or TURNSTILE_SITE_KEY),
                    browser_channel=str(turnstile_cfg.get("browser_channel") or "chrome"),
                    headless=bool(turnstile_cfg.get("headless", False)),
                    timeout=int(turnstile_cfg.get("timeout") or 45),
                )

    # ── 解析邮箱渠道 ──
    provider_name = (
        args.mail_provider
        or os.environ.get("MAIL_PROVIDER", "")
        or cfg.get("mail_provider")
        or "mailpoolhub"
    )
    yyds_api_key = resolve_api_key(args.api_key, config_path, cfg)
    try:
        mail_provider_factory, provider_label = create_mail_provider_factory(
            str(provider_name),
            cfg,
            yyds_api_key=yyds_api_key,
            mailpoolhub_base_url=args.mailpoolhub_base_url,
            mailpoolhub_api_key=args.mailpoolhub_api_key,
            mailpoolhub_provider_name=args.mailpoolhub_provider,
            proxies=proxies,
        )
    except (TypeError, ValueError) as error:
        print(f"❌ 临时邮箱渠道配置错误：{error}")
        sys.exit(1)

    # 检查渠道时不创建输出文件，也不触发注册流程。
    if args.check_mail_provider:
        try:
            provider = mail_provider_factory()
            health_method = getattr(provider, "health", None)
            if callable(health_method):
                health = health_method()
                if health.get("configured") is False:
                    raise RuntimeError(str(health.get("message") or "临时邮箱渠道尚未配置"))
                print(f"✅ {provider_label} 临时邮箱渠道可用")
                print(json.dumps(health, ensure_ascii=False, indent=2))
            else:
                domains = provider.list_domains()
                print(f"✅ {provider_label} 临时邮箱渠道可用，可用域名 {len(domains)} 个")
        except Exception as error:
            print(f"❌ {provider_label} 临时邮箱渠道检查失败：{error}")
            sys.exit(1)
        sys.exit(0)

    if args.list_domains:
        try:
            provider = mail_provider_factory()
            domains = provider.list_domains()
            print(f"{provider_label} 可用域名 ({len(domains)} 个):\n")
            for available_domain in domains:
                print(f"  {available_domain}")
        except Exception as error:
            print(f"获取域名列表失败：{error}")
            sys.exit(1)
        sys.exit(0)

    if args.count <= 0 or args.threads <= 0 or args.max_retries <= 0 or args.turnstile_concurrency < 0:
        print("错误: 数量、线程数、重试数必须 > 0，浏览器并发数不能小于 0")
        sys.exit(1)

    # ── 准备 ──
    SCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text("")  # 清空旧结果

    print(f"{'=' * 55}")
    print(f"  Todofor.ai 批量注册机")
    print(f"{'=' * 55}")
    print(f"  邮箱渠道 : {provider_label}")
    print(f"  代理     : {'已配置' if proxies else '无（直连）'}")
    print(f"  注册数量 : {args.count}")
    print(f"  线程数   : {args.threads}")
    print(f"  重试次数 : {args.max_retries}")
    print(f"  输出文件 : {OUTPUT_FILE}")
    print(f"{'=' * 55}\n")

    # ── 执行 ──
    success = 0
    failed = 0
    start_time = time.time()

    workers = min(args.threads, args.count)

    if workers <= 1:
        # 单线程
        for i in range(1, args.count + 1):
            print(f"\n{'─' * 45}")
            print(f"  [{i}/{args.count}]")
            result = process_one_account(
                i,
                mail_provider_factory,
                domain=domain,
                exclude_domains=exclude_domains,
                proxies=proxies,
                turnstile_solver=turnstile_solver,
                max_retries=args.max_retries,
                verbose=args.verbose,
            )
            if result:
                save_result(result)
                success += 1
            else:
                failed += 1
            if i < args.count:
                time.sleep(2)
    else:
        # 多线程
        print(f"  启动 {workers} 个线程...\n")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {
                pool.submit(
                    process_one_account,
                    i,
                    mail_provider_factory,
                    domain=domain,
                    exclude_domains=exclude_domains,
                    proxies=proxies,
                    turnstile_solver=turnstile_solver,
                    max_retries=args.max_retries,
                    initial_delay=((i - 1) % workers) * 1.5,
                    verbose=args.verbose,
                ): i
                for i in range(1, args.count + 1)
            }

            done = 0
            for future in as_completed(future_map):
                done += 1
                tid = future_map[future]
                try:
                    result = future.result()
                except Exception as e:
                    log(tid, f"❌ 线程崩溃: {e}")
                    result = None

                if result:
                    save_result(result)
                    success += 1
                else:
                    failed += 1

                print(f"    总进度: {done}/{args.count}  成功: {success}  失败: {failed}")

    # ── 汇总 ──
    elapsed = time.time() - start_time
    print(f"\n{'=' * 55}")
    print(f"  执行完成")
    print(f"  耗时   : {elapsed:.1f}s")
    print(f"  成功   : {success}")
    print(f"  失败   : {failed}")
    print(f"  结果   : {OUTPUT_FILE}")
    print(f"{'=' * 55}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
