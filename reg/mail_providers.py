"""临时邮箱渠道实现。"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Protocol
from urllib.parse import quote, urlparse
from uuid import uuid4

import requests


YYDS_BASE_URL = "https://maliapi.215.im/v1"
DEFAULT_MAILPOOLHUB_BASE_URL = "http://127.0.0.1:8080/api/v1"


class MailProviderError(RuntimeError):
    """临时邮箱渠道请求错误。"""

    def __init__(self, message: str, *, status: int = 0, code: str = ""):
        """保存便于上层区分的 HTTP 状态和业务错误码。"""
        super().__init__(message)
        self.status = status
        self.code = code


class TemporaryMailProvider(Protocol):
    """注册流程所需的最小临时邮箱接口。"""

    def list_domains(self) -> list[str]:
        """返回当前渠道支持的邮箱域名。"""

    def create_account(
        self,
        domain: str = "",
        exclude_domains: list[str] | None = None,
    ) -> dict:
        """创建临时邮箱并返回标准化账户信息。"""

    def wait_for_code(self, account: dict, timeout: int = 90) -> str | None:
        """等待账户收到验证码，超时返回 None。"""

    def close_account(self, account: dict) -> None:
        """释放临时邮箱资源。"""


def extract_verification_code(message: dict) -> str | None:
    """从标准字段、标题、纯文本或 HTML 中提取验证码。"""
    if not isinstance(message, dict):
        return None

    for field in ("verification_code", "verificationCode", "otp", "code"):
        value = message.get(field)
        normalized_value = str(value or "").strip()
        if re.fullmatch(r"\d{4,8}", normalized_value):
            return normalized_value

    html_value = message.get("html")
    if isinstance(html_value, list):
        html = " ".join(str(part) for part in html_value)
    else:
        html = str(html_value or "")

    combined = " ".join(
        [
            str(message.get("subject") or ""),
            str(message.get("text") or ""),
            html,
            str(message.get("rawJson") or message.get("raw_json") or ""),
        ]
    )
    patterns = [
        r"(?:verification\s*(?:code|token)|otp|code|验证码|确认码)\s*[:：\s]*(\d{4,8})",
        r"\b(\d{6})\b",
        r"\b(\d{4})\b",
        r"\b(\d{8})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, combined, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _decode_json_response(response: requests.Response, provider_name: str) -> dict:
    """解析渠道 JSON 响应并保留可操作的错误信息。"""
    try:
        body = response.json()
    except (json.JSONDecodeError, requests.exceptions.JSONDecodeError) as error:
        raise MailProviderError(
            f"{provider_name} 返回了无法解析的响应（HTTP {response.status_code}）",
            status=response.status_code,
            code="INVALID_JSON_RESPONSE",
        ) from error

    if not isinstance(body, dict):
        raise MailProviderError(
            f"{provider_name} 返回的数据结构无效",
            status=response.status_code,
            code="INVALID_RESPONSE_SHAPE",
        )
    return body


class YYDSMailProvider:
    """YYDS Mail 临时邮箱渠道。"""

    def __init__(self, api_key: str, proxies: dict | None = None):
        """创建使用独立 HTTP Session 的 YYDS Mail 客户端。"""
        self.api_key = api_key
        self.session = requests.Session()
        if proxies:
            self.session.proxies.update(proxies)

    def _headers(self) -> dict[str, str]:
        """生成 YYDS Mail 认证请求头。"""
        return {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def list_domains(self) -> list[str]:
        """读取 YYDS Mail 可用域名。"""
        response = self.session.get(f"{YYDS_BASE_URL}/domains", timeout=15)
        response.raise_for_status()
        body = _decode_json_response(response, "YYDS Mail")
        data = body.get("data")
        if body.get("success") and isinstance(data, list):
            return [domain for domain in data if isinstance(domain, str)]
        return []

    def create_account(
        self,
        domain: str = "",
        exclude_domains: list[str] | None = None,
    ) -> dict:
        """创建 YYDS Mail 临时邮箱。"""
        payload: dict = {}
        if domain:
            payload["domain"] = domain
        if exclude_domains:
            payload["excludeDomains"] = exclude_domains

        response = self.session.post(
            f"{YYDS_BASE_URL}/accounts",
            headers=self._headers(),
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        body = _decode_json_response(response, "YYDS Mail")
        account = body.get("data")
        if not body.get("success") or not isinstance(account, dict):
            raise MailProviderError(
                str(body.get("error") or body.get("message") or "YYDS Mail 创建邮箱失败"),
                status=response.status_code,
                code=str(body.get("code") or "CREATE_ACCOUNT_FAILED"),
            )
        if not account.get("address"):
            raise MailProviderError("YYDS Mail 返回的邮箱地址为空", code="INVALID_ACCOUNT")
        return account

    def wait_for_code(self, account: dict, timeout: int = 90) -> str | None:
        """长轮询 YYDS Mail，直到收到验证码或达到总超时。"""
        address = str(account.get("address") or "").strip()
        if not address:
            raise MailProviderError("YYDS Mail 邮箱缺少 address", code="INVALID_ACCOUNT")

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            wait_seconds = min(30, max(1, int(remaining)))
            try:
                response = self.session.get(
                    f"{YYDS_BASE_URL}/messages/next",
                    params={"address": address, "wait": wait_seconds},
                    headers={"X-API-Key": self.api_key},
                    timeout=wait_seconds + 15,
                )
            except requests.RequestException:
                time.sleep(min(3, max(0, deadline - time.monotonic())))
                continue

            if response.status_code == 204:
                continue
            response.raise_for_status()
            body = _decode_json_response(response, "YYDS Mail")
            message = (body.get("data") or {}).get("message") if body.get("success") else None
            code = extract_verification_code(message or {})
            if code:
                return code
        return None

    def close_account(self, account: dict) -> None:
        """YYDS Mail 现有接口没有邮箱删除能力，无需额外处理。"""


def _parse_timestamp(value: object) -> datetime | None:
    """解析 MailPoolHub 使用的 RFC 3339 时间并统一为 UTC。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_candidate_message(summary: dict, account: dict) -> bool:
    """按邮箱地址和创建时间过滤本次流程收到的邮件摘要。"""
    address = str(account.get("address") or "").strip().casefold()
    recipient = str(summary.get("to") or "").strip().casefold()
    if recipient and address and address not in recipient:
        return False

    created_at = _parse_timestamp(account.get("createdAt"))
    received_at = _parse_timestamp(summary.get("receivedAt"))
    if created_at and received_at and received_at < created_at - timedelta(seconds=60):
        return False
    return True


class MailPoolHubProvider:
    """MailPoolHub 客户端 REST API 临时邮箱渠道。"""

    def __init__(
        self,
        base_url: str = DEFAULT_MAILPOOLHUB_BASE_URL,
        api_key: str = "",
        request_timeout: int = 30,
        poll_interval_seconds: int = 5,
        ttl_seconds: int = 900,
        provider_name: str = "",
        provider_names: list[str] | None = None,
        provider_start: int = 0,
    ):
        """创建 MailPoolHub 客户端并隔离本机 API 与系统代理。"""
        normalized_url = str(base_url or DEFAULT_MAILPOOLHUB_BASE_URL).strip().rstrip("/")
        parsed = urlparse(normalized_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("MailPoolHub base_url 必须是完整的 HTTP(S) 地址")
        normalized_key = str(api_key or "").strip()
        if not normalized_key:
            raise ValueError("未找到 MailPoolHub API Key")

        self.base_url = normalized_url
        self.api_key = normalized_key
        self.request_timeout = max(1, int(request_timeout))
        self.poll_interval_seconds = max(1, int(poll_interval_seconds))
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.provider_name = str(provider_name or "").strip()
        self.provider_names = list(
            dict.fromkeys(
                name.strip()
                for name in (provider_names or [])
                if isinstance(name, str) and name.strip()
            )
        )
        self.provider_cursor = max(0, int(provider_start))
        self.available_provider_names: list[str] | None = None
        self.session = requests.Session()
        # 本机 API 不应被 HTTP_PROXY/HTTPS_PROXY 转发到外部代理。
        self.session.trust_env = False

    def _headers(self) -> dict[str, str]:
        """生成 MailPoolHub 客户端 API 请求头。"""
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    def _request_json(
        self,
        method: str,
        pathname: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
        timeout: int | None = None,
    ) -> dict:
        """请求 MailPoolHub JSON API 并统一映射错误信封。"""
        headers = self._headers()
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = self.session.request(
                method,
                f"{self.base_url}{pathname}",
                headers=headers,
                json=json_body,
                params=params,
                timeout=timeout or self.request_timeout,
            )
        except requests.RequestException as error:
            raise MailProviderError(
                f"连接 MailPoolHub 失败：{error}",
                code="MAILPOOLHUB_UNREACHABLE",
            ) from error

        body = _decode_json_response(response, "MailPoolHub")
        if not response.ok:
            error_body = body.get("error") if isinstance(body.get("error"), dict) else {}
            raise MailProviderError(
                str(
                    error_body.get("message")
                    or body.get("message")
                    or f"MailPoolHub 请求失败（HTTP {response.status_code}）"
                ),
                status=response.status_code,
                code=str(
                    error_body.get("code")
                    or body.get("code")
                    or "MAILPOOLHUB_REQUEST_FAILED"
                ),
            )
        return body

    def health(self) -> dict:
        """通过受鉴权的 Provider 列表检查客户端 API 是否可用。"""
        body = self._request_json("GET", "/providers")
        providers = body.get("providers")
        if not isinstance(providers, list):
            raise MailProviderError(
                "MailPoolHub Provider 列表结构无效",
                code="INVALID_PROVIDER_LIST",
            )
        healthy_count = sum(
            1
            for provider in providers
            if isinstance(provider, dict)
            and str(provider.get("healthStatus") or "").lower() in {"healthy", "ok"}
        )
        return {
            "configured": True,
            "providerCount": len(providers),
            "healthyProviderCount": healthy_count,
        }

    def list_domains(self) -> list[str]:
        """汇总 MailPoolHub 可用 Provider 公布的域名。"""
        body = self._request_json("GET", "/providers")
        providers = body.get("providers")
        if not isinstance(providers, list):
            return []

        domains: set[str] = set()
        for provider in providers:
            domain_options = provider.get("domains") if isinstance(provider, dict) else None
            if not isinstance(domain_options, dict) or not domain_options.get("selectable"):
                continue
            values = domain_options.get("domains")
            if isinstance(values, list):
                domains.update(
                    value.strip()
                    for value in values
                    if isinstance(value, str) and value.strip()
                )
        return sorted(domains)

    def _provider_candidates(self) -> list[str]:
        """按运行时健康清单过滤并轮换注册渠道。"""
        if not self.provider_names:
            return [self.provider_name] if self.provider_name else [""]
        if self.available_provider_names is None:
            body = self._request_json("GET", "/providers")
            providers = body.get("providers")
            if not isinstance(providers, list):
                raise MailProviderError(
                    "MailPoolHub Provider 列表结构无效",
                    code="INVALID_PROVIDER_LIST",
                )
            available = [
                str(provider.get("name") or "").strip()
                for provider in providers
                if isinstance(provider, dict)
                and str(provider.get("name") or "").strip()
                and str(provider.get("healthStatus") or "").lower() in {"healthy", "ok"}
            ]
            preferred = [name for name in self.provider_names if name in available]
            self.available_provider_names = preferred or available
        candidates = self.available_provider_names
        if not candidates:
            raise MailProviderError(
                "MailPoolHub 没有可用邮箱渠道",
                code="NO_HEALTHY_PROVIDER",
            )
        start = self.provider_cursor % len(candidates)
        self.provider_cursor += 1
        return candidates[start:] + candidates[:start]

    def create_account(
        self,
        domain: str = "",
        exclude_domains: list[str] | None = None,
    ) -> dict:
        """通过 MailPoolHub 创建临时邮箱并返回邮箱会话。"""
        base_payload: dict = {
            "ttlSeconds": self.ttl_seconds,
            "tags": {
                "scene": "todo2api-reg",
                "requestId": uuid4().hex,
            },
        }
        if domain:
            base_payload["domain"] = domain
        # MailPoolHub 当前由服务端调度渠道，没有 excludeDomains 请求字段。
        _ = exclude_domains

        last_error: MailProviderError | None = None
        body: dict | None = None
        for provider_name in self._provider_candidates():
            payload = dict(base_payload)
            payload["tags"] = dict(base_payload["tags"])
            if provider_name:
                payload["provider"] = provider_name
            try:
                body = self._request_json(
                    "POST",
                    "/mailboxes",
                    json_body=payload,
                )
                break
            except MailProviderError as error:
                last_error = error
        if body is None:
            raise last_error or MailProviderError(
                "MailPoolHub 创建邮箱失败",
                code="CREATE_ACCOUNT_FAILED",
            )
        account = body

        mailbox_id = str(account.get("id") or "").strip()
        address = str(account.get("address") or "").strip()
        if not mailbox_id or not address:
            raise MailProviderError(
                "MailPoolHub 返回的邮箱缺少 id 或 address",
                code="INVALID_ACCOUNT",
            )
        if not account.get("domain") and "@" in address:
            account["domain"] = address.rsplit("@", 1)[-1]
        return account

    def wait_for_code(self, account: dict, timeout: int = 90) -> str | None:
        """轮询 MailPoolHub 邮件列表并从详情中提取验证码。"""
        mailbox_id = str(account.get("id") or "").strip()
        if not mailbox_id:
            raise MailProviderError("MailPoolHub 邮箱缺少 id", code="INVALID_ACCOUNT")

        deadline = time.monotonic() + timeout
        encoded_id = quote(mailbox_id, safe="")
        inspected_message_ids: set[str] = set()
        while time.monotonic() < deadline:
            try:
                body = self._request_json(
                    "GET",
                    f"/mailboxes/{encoded_id}/messages",
                    params={"refresh": "true"},
                )
            except MailProviderError as error:
                if error.status and error.status < 500:
                    raise
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(self.poll_interval_seconds, remaining))
                continue
            messages = body.get("messages")
            if not isinstance(messages, list):
                raise MailProviderError(
                    "MailPoolHub 邮件列表结构无效",
                    code="INVALID_MESSAGE_LIST",
                )

            candidates = [
                message
                for message in messages
                if isinstance(message, dict)
                and str(message.get("id") or "") not in inspected_message_ids
                and _is_candidate_message(message, account)
            ]
            candidates.sort(key=lambda message: str(message.get("receivedAt") or ""), reverse=True)

            for summary in candidates:
                message_id = str(summary.get("id") or "").strip()
                if not message_id:
                    continue
                try:
                    detail = self._request_json(
                        "GET",
                        f"/mailboxes/{encoded_id}/messages/{quote(message_id, safe='')}",
                    )
                except MailProviderError as error:
                    if error.status and error.status < 500:
                        raise
                    continue
                inspected_message_ids.add(message_id)
                code = extract_verification_code(detail)
                if code:
                    return code

            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(self.poll_interval_seconds, remaining))
        return None

    def close_account(self, account: dict) -> None:
        """删除 MailPoolHub 邮箱，确保成功、失败和超时路径均释放资源。"""
        mailbox_id = str(account.get("id") or "").strip()
        if not mailbox_id:
            return
        try:
            self._request_json(
                "DELETE",
                f"/mailboxes/{quote(mailbox_id, safe='')}",
            )
        except MailProviderError as error:
            # 重复清理已不存在的邮箱时保持幂等。
            if error.status != 404:
                raise
