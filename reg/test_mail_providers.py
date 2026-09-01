"""临时邮箱渠道与注册流程测试。"""

from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import main
import start_reg
from mail_providers import MailPoolHubProvider, MailProviderError, extract_verification_code


class MailPoolHubContractHandler(BaseHTTPRequestHandler):
    """实现 MailPoolHub 客户端 API 最小契约的真实 HTTP 测试服务。"""

    message_requests = 0
    last_create_payload: dict = {}
    authorization_headers: list[str] = []
    deleted_mailboxes: list[str] = []

    def log_message(self, _format: str, *_args) -> None:
        """关闭测试 HTTP 服务的标准错误日志。"""

    def _write_json(self, status: int, payload: dict) -> None:
        """写入 UTF-8 JSON 响应。"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record_authorization(self) -> None:
        """记录客户端传入的 Bearer Token 请求头。"""
        type(self).authorization_headers.append(self.headers.get("Authorization", ""))

    def do_GET(self) -> None:
        """处理 Provider、邮件列表和邮件详情接口。"""
        self._record_authorization()
        if self.path == "/api/v1/providers":
            self._write_json(
                200,
                {
                    "providers": [
                        {
                            "name": "fixture",
                            "displayName": "Fixture",
                            "healthStatus": "healthy",
                            "domains": {
                                "selectable": True,
                                "domains": ["mail.test"],
                                "observedDomains": ["observed.test"],
                            },
                        }
                    ]
                },
            )
            return
        if self.path == "/api/v1/mailboxes/mailbox-1/messages?refresh=true":
            type(self).message_requests += 1
            if type(self).message_requests == 1:
                self._write_json(502, {"error": {"code": "UPSTREAM_EOF", "message": "upstream EOF"}})
                return
            self._write_json(
                200,
                {
                    "mailboxId": "mailbox-1",
                    "messages": [
                        {
                            "id": "message-1",
                            "from": "login@example.test",
                            "to": "fixture@mail.test",
                            "subject": "登录验证码",
                            "receivedAt": "2026-08-27T15:01:00Z",
                            "hasText": True,
                            "hasHtml": False,
                        }
                    ],
                },
            )
            return
        if self.path == "/api/v1/mailboxes/mailbox-1/messages/message-1":
            self._write_json(
                200,
                {
                    "id": "message-1",
                    "from": "login@example.test",
                    "to": "fixture@mail.test",
                    "subject": "登录验证码",
                    "text": "验证码：481205",
                    "html": "",
                    "receivedAt": "2026-08-27T15:01:00Z",
                },
            )
            return
        self._write_json(
            404,
            {"error": {"code": "MAILBOX_NOT_FOUND", "message": "mailbox not found"}},
        )

    def do_POST(self) -> None:
        """处理创建临时邮箱接口。"""
        self._record_authorization()
        if self.path != "/api/v1/mailboxes":
            self._write_json(
                404,
                {"error": {"code": "INVALID_REQUEST", "message": "unknown path"}},
            )
            return
        content_length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(content_length) or b"{}")
        type(self).last_create_payload = payload
        self._write_json(
            201,
            {
                "id": "mailbox-1",
                "provider": "fixture",
                "address": "fixture@mail.test",
                "status": "active",
                "capabilities": {
                    "createMailbox": True,
                    "listMessages": True,
                    "getMessage": True,
                },
                "createdAt": "2026-08-27T15:00:00Z",
                "expiresAt": "2026-08-27T15:15:00Z",
            },
        )

    def do_DELETE(self) -> None:
        """处理删除临时邮箱接口。"""
        self._record_authorization()
        if self.path == "/api/v1/mailboxes/mailbox-1":
            type(self).deleted_mailboxes.append("mailbox-1")
            self._write_json(200, {"success": True})
            return
        self._write_json(
            404,
            {"error": {"code": "MAILBOX_NOT_FOUND", "message": "mailbox not found"}},
        )


class MailPoolHubProviderTest(unittest.TestCase):
    """通过真实本地 TCP/HTTP 验证 MailPoolHub Provider 契约。"""

    @classmethod
    def setUpClass(cls) -> None:
        """启动随机端口的 MailPoolHub 契约服务。"""
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MailPoolHubContractHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address
        cls.base_url = f"http://{host}:{port}/api/v1"

    @classmethod
    def tearDownClass(cls) -> None:
        """关闭本地 MailPoolHub 契约服务。"""
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def setUp(self) -> None:
        """重置各测试共享的请求记录。"""
        MailPoolHubContractHandler.message_requests = 0
        MailPoolHubContractHandler.last_create_payload = {}
        MailPoolHubContractHandler.authorization_headers = []
        MailPoolHubContractHandler.deleted_mailboxes = []

    def test_real_http_contract_flow(self) -> None:
        """完整验证鉴权检查、域名、邮箱、轮询详情和删除流程。"""
        provider = MailPoolHubProvider(
            base_url=self.base_url,
            api_key="local-test-token",
            request_timeout=3,
            poll_interval_seconds=1,
            ttl_seconds=900,
        )

        health = provider.health()
        domains = provider.list_domains()
        account = provider.create_account("", ["blocked.test"])
        code = provider.wait_for_code(account, timeout=3)
        provider.close_account(account)

        self.assertTrue(health["configured"])
        self.assertEqual(1, health["healthyProviderCount"])
        self.assertEqual(["mail.test"], domains)
        self.assertEqual("fixture@mail.test", account["address"])
        self.assertEqual("mail.test", account["domain"])
        self.assertEqual("481205", code)
        self.assertEqual(900, MailPoolHubContractHandler.last_create_payload["ttlSeconds"])
        self.assertEqual("todo2api-reg", MailPoolHubContractHandler.last_create_payload["tags"]["scene"])
        self.assertNotIn("excludeDomains", MailPoolHubContractHandler.last_create_payload)
        self.assertEqual(["mailbox-1"], MailPoolHubContractHandler.deleted_mailboxes)
        self.assertTrue(MailPoolHubContractHandler.authorization_headers)
        self.assertTrue(
            all(
                value == "Bearer local-test-token"
                for value in MailPoolHubContractHandler.authorization_headers
            )
        )

    def test_error_envelope_is_preserved(self) -> None:
        """验证 MailPoolHub 错误信封会保留状态和错误码。"""
        provider = MailPoolHubProvider(base_url=self.base_url, api_key="local-test-token")
        with self.assertRaises(MailProviderError) as context:
            provider._request_json("GET", "/mailboxes/missing")
        self.assertEqual(404, context.exception.status)
        self.assertEqual("MAILBOX_NOT_FOUND", context.exception.code)

    def test_unavailable_preference_falls_back_to_runtime_provider(self) -> None:
        """验证已下线的保存渠道会被运行时健康清单替换。"""
        provider = MailPoolHubProvider(
            base_url=self.base_url,
            api_key="local-test-token",
            provider_name="removed-provider",
            provider_names=["removed-provider", "fixture"],
            request_timeout=3,
        )
        account = provider.create_account()
        provider.close_account(account)
        self.assertEqual("fixture", MailPoolHubContractHandler.last_create_payload["provider"])

    def test_excluded_mailbox_domain_is_deleted_and_retried(self) -> None:
        """验证 MailPoolHub 客户端会跳过持久黑名单域名。"""
        provider = MailPoolHubProvider(
            base_url=self.base_url,
            api_key="local-test-token",
            provider_name="fixture",
        )
        created = iter(
            [
                {"id": "blocked", "address": "first@blocked.test"},
                {"id": "allowed", "address": "second@allowed.test"},
            ]
        )
        deleted: list[str] = []

        def request(method: str, pathname: str, **_kwargs) -> dict:
            if method == "POST":
                return next(created)
            if method == "DELETE":
                deleted.append(pathname)
                return {"success": True}
            raise AssertionError((method, pathname))

        with patch.object(provider, "_request_json", side_effect=request):
            account = provider.create_account(exclude_domains=["blocked.test"])
        self.assertEqual("second@allowed.test", account["address"])
        self.assertEqual(["/mailboxes/blocked"], deleted)


class VerificationCodeTest(unittest.TestCase):
    """验证码字段与正文兜底提取测试。"""

    def test_extracts_normalized_field(self) -> None:
        """优先读取标准验证码字段。"""
        self.assertEqual("123456", extract_verification_code({"verification_code": "123456"}))

    def test_extracts_code_from_html_list(self) -> None:
        """兼容 YYDS HTML 数组并从正文提取验证码。"""
        message = {"subject": "Sign in", "html": ["<p>Verification code: 876543</p>"]}
        self.assertEqual("876543", extract_verification_code(message))

    def test_extracts_code_from_mailpoolhub_raw_json(self) -> None:
        """在正文为空时从 MailPoolHub rawJson 兜底提取验证码。"""
        message = {"rawJson": '{"content":"OTP: 443322"}'}
        self.assertEqual("443322", extract_verification_code(message))

    def test_ignores_non_numeric_code_field(self) -> None:
        """忽略邮件元数据中不是验证码的 code 字段。"""
        message = {"code": "MESSAGE_OK", "text": "OTP: 112233"}
        self.assertEqual("112233", extract_verification_code(message))


class RegistrationFlowTest(unittest.TestCase):
    """邮箱 Provider 注入后的单账号注册流程测试。"""

    def test_process_one_account_uses_and_closes_provider_account(self) -> None:
        """验证主流程读取完整邮箱账户并在成功后删除邮箱。"""
        provider = _FakeMailProvider()

        with patch.object(main, "TodoforAI", _FakeTodoforAI), patch.object(
            main, "load_rejected_domains", return_value=set()
        ), patch.object(main.time, "sleep"):
            result = main.process_one_account(
                1,
                lambda: provider,
                domain="mail.test",
                exclude_domains=["blocked.test"],
                max_retries=1,
            )

        self.assertIsNotNone(result)
        self.assertEqual("fixture@mail.test", result["email"])
        self.assertEqual("mailbox-1", provider.waited_account["id"])
        self.assertEqual("mail.test", provider.created_domain)
        self.assertEqual(["blocked.test"], provider.created_exclude_domains)
        self.assertEqual(["mailbox-1"], provider.closed_account_ids)

    def test_request_interval_gate_spaces_concurrent_submissions(self) -> None:
        """验证 OTP 门按配置间隔排队。"""
        gate = main.RequestIntervalGate(7)
        with patch.object(
            main.time,
            "monotonic",
            side_effect=[10, 10, 10, 12, 12, 17, 17],
        ), patch.object(
            main.time, "sleep"
        ) as sleep:
            gate.wait()
            gate.wait()
        sleep.assert_called_once_with(5)

    def test_request_interval_gate_reports_cooldown_wait(self) -> None:
        """验证长冷却可通知注册流程刷新已过期邮箱。"""
        gate = main.RequestIntervalGate(40)
        gate.cooldown_until = 100
        with patch.object(
            main.time, "monotonic", side_effect=[0, 0, 100, 100]
        ), patch.object(main.time, "sleep") as sleep:
            waited = gate.wait_for_cooldown()
        self.assertEqual(100, waited)
        sleep.assert_called_once_with(100)

    def test_concurrent_failures_are_replaced_until_success_target(self) -> None:
        """验证失败任务会补位，且在途任务不会超过剩余成功数。"""
        calls: list[int] = []

        def worker(task_id: int, _initial_delay: float) -> dict | None:
            calls.append(task_id)
            return None if task_id <= 2 else {"thread_id": task_id}

        with patch.object(main, "save_result") as save:
            success, failed = main.run_concurrent_accounts(3, 3, worker)

        self.assertEqual((3, 2), (success, failed))
        self.assertEqual(5, len(calls))
        self.assertEqual(3, save.call_count)

    def test_exhausted_proxy_platforms_trigger_network_cooldown(self) -> None:
        """验证全部代理失败时触发共享网络熔断。"""
        gate = Mock()
        gate.wait_for_cooldown.return_value = 0

        with patch.object(main, "TodoforAI", _AlwaysInitFailsTodoforAI), patch.object(
            main.time, "sleep"
        ):
            result = main.process_one_account(
                1,
                _FakeMailProvider,
                proxies={"https": "http://node.{uuid}:token@127.0.0.1:9200"},
                network_gate=gate,
                network_cooldown_seconds=60,
                proxy_attempts=2,
                max_retries=1,
            )

        self.assertIsNone(result)
        gate.defer.assert_called_once_with(60)

    def test_failure_also_closes_provider_account(self) -> None:
        """验证 OTP 发送失败时同样删除已创建邮箱。"""
        provider = _FakeMailProvider()

        with patch.object(main, "TodoforAI", _RejectingTodoforAI):
            result = main.process_one_account(1, lambda: provider, max_retries=1)

        self.assertIsNone(result)
        self.assertEqual(["mailbox-1"], provider.closed_account_ids)

    def test_non_retryable_captcha_error_stops_domain_rotation(self) -> None:
        """验证浏览器校验错误不会继续创建无效邮箱。"""
        provider = _FakeMailProvider()

        with patch.object(main, "TodoforAI", _CaptchaRequiredTodoforAI):
            result = main.process_one_account(1, lambda: provider, max_retries=3)

        self.assertIsNone(result)
        self.assertEqual(1, provider.create_count)
        self.assertEqual(["mailbox-1"], provider.closed_account_ids)

    def test_browser_token_is_requested_after_protocol_challenge(self) -> None:
        """验证纯协议失败后才通过同一代理取得浏览器令牌。"""
        provider = _FakeMailProvider()
        solver = Mock(
            side_effect=lambda _proxy_url: (
                self.assertEqual(1, provider.create_count) or "turnstile-token"
            )
        )

        with patch.object(main, "TodoforAI", _CaptchaThenSuccessTodoforAI), patch.object(main.time, "sleep"):
            result = main.process_one_account(
                1,
                lambda: provider,
                proxies={"https": "http://us.{uuid}:token@127.0.0.1:9200"},
                turnstile_solver=solver,
                max_retries=1,
            )

        self.assertIsNotNone(result)
        self.assertEqual(1, solver.call_count)
        proxy_url = solver.call_args.args[0]
        self.assertNotIn("{uuid}", proxy_url)
        self.assertTrue(proxy_url.startswith("http://us."))

    def test_browser_is_skipped_when_protocol_otp_succeeds(self) -> None:
        """验证目标接受纯协议请求时不会启动浏览器。"""
        solver = Mock(return_value="unused-token")

        with patch.object(main, "TodoforAI", _FakeTodoforAI), patch.object(main.time, "sleep"):
            result = main.process_one_account(
                1,
                _FakeMailProvider,
                turnstile_solver=solver,
                max_retries=1,
            )

        self.assertIsNotNone(result)
        solver.assert_not_called()

    def test_always_mode_sends_first_otp_with_browser_token(self) -> None:
        """验证 always 模式不会先发送必然失败的无令牌请求。"""
        tokens: list[str] = []

        class BrowserFirstTodoforAI(_FakeTodoforAI):
            def send_otp(self, email: str, captcha_token: str = "") -> bool:
                tokens.append(captcha_token)
                return captcha_token == "turnstile-token"

        otp_gate = Mock()
        otp_gate.wait_for_cooldown.return_value = 0
        otp_gate.wait.return_value = 0
        with patch.object(main, "TodoforAI", BrowserFirstTodoforAI), patch.object(
            main.time, "sleep"
        ):
            result = main.process_one_account(
                1,
                _FakeMailProvider,
                turnstile_solver=Mock(return_value="turnstile-token"),
                turnstile_mode="always",
                otp_gate=otp_gate,
                max_retries=1,
            )

        self.assertIsNotNone(result)
        self.assertEqual(["turnstile-token"], tokens)
        self.assertEqual(2, otp_gate.wait_for_cooldown.call_count)
        otp_gate.wait.assert_called_once_with()

    def test_anonymous_session_failure_rotates_before_creating_mailbox(self) -> None:
        """验证坏代理会在创建邮箱前换 UUID。"""
        provider = _FakeMailProvider()
        _InitFailsOnceTodoforAI.instances = 0

        with patch.object(main, "TodoforAI", _InitFailsOnceTodoforAI), patch.object(main.time, "sleep"):
            result = main.process_one_account(
                1,
                lambda: provider,
                proxies={"https": "http://jp.{uuid}:token@127.0.0.1:9200"},
                max_retries=2,
            )

        self.assertIsNotNone(result)
        self.assertEqual(2, _InitFailsOnceTodoforAI.instances)
        self.assertEqual(1, provider.create_count)
        self.assertEqual(1, len(provider.closed_account_ids))

    def test_turnstile_failure_rotates_proxy_and_retries(self) -> None:
        """验证浏览器失败时同一邮箱换代理继续尝试。"""
        provider = _FakeMailProvider()
        solver = Mock(side_effect=[TimeoutError("probe timeout"), "turnstile-token"])

        with patch.object(main, "TodoforAI", _CaptchaThenSuccessTodoforAI), patch.object(main.time, "sleep"):
            result = main.process_one_account(
                1,
                lambda: provider,
                proxies={"https": "http://jp.{uuid}:token@127.0.0.1:9200"},
                proxy_platforms=["jp", "us"],
                turnstile_solver=solver,
                max_retries=1,
            )

        self.assertIsNotNone(result)
        self.assertEqual(2, solver.call_count)
        self.assertEqual(1, provider.create_count)
        self.assertNotEqual(solver.call_args_list[0].args[0], solver.call_args_list[1].args[0])
        self.assertTrue(main.urlparse(solver.call_args_list[0].args[0]).username.startswith("jp."))
        self.assertTrue(main.urlparse(solver.call_args_list[1].args[0]).username.startswith("us."))

    def test_missing_api_key_is_not_counted_as_success(self) -> None:
        """验证登录成功但无 Key 时会重试而不是返回伪成功。"""
        provider = _FakeMailProvider()
        _NoKeysThenSuccessTodoforAI.instances = 0

        with patch.object(main, "TodoforAI", _NoKeysThenSuccessTodoforAI), patch.object(main.time, "sleep"):
            result = main.process_one_account(1, lambda: provider, max_retries=2)

        self.assertIsNotNone(result)
        self.assertEqual(2, provider.create_count)

    def test_default_api_key_request_is_skipped_when_list_has_key(self) -> None:
        """验证列表已有 Key 时不再发送冗余的默认 Key 请求。"""
        with patch.object(main, "TodoforAI", _FakeTodoforAI), patch.object(
            _FakeTodoforAI,
            "get_default_api_key",
            side_effect=AssertionError("redundant getDefault request"),
        ) as get_default, patch.object(main.time, "sleep"):
            result = main.process_one_account(1, _FakeMailProvider, max_retries=1)

        self.assertIsNotNone(result)
        get_default.assert_not_called()

    def test_default_api_key_remains_fallback_when_list_is_empty(self) -> None:
        """验证列表为空时仍通过默认 Key 完成注册。"""
        default_key = {"key": "fixture-api-key-value"}
        with patch.object(main, "TodoforAI", _FakeTodoforAI), patch.object(
            _FakeTodoforAI, "get_api_keys", return_value=[]
        ), patch.object(
            _FakeTodoforAI, "get_default_api_key", return_value=default_key
        ) as get_default, patch.object(main.time, "sleep"):
            result = main.process_one_account(1, _FakeMailProvider, max_retries=1)

        self.assertEqual([default_key], result["api_keys"])
        get_default.assert_called_once_with()

    def test_delayed_api_key_is_polled_without_recreating_account(self) -> None:
        """验证 Key 延迟生成时短轮询而不是重新注册。"""
        provider = _FakeMailProvider()

        class DelayedKeyTodoforAI(_FakeTodoforAI):
            calls = 0

            def get_api_keys(self) -> list[dict]:
                type(self).calls += 1
                return [] if type(self).calls == 1 else super().get_api_keys()

            def get_default_api_key(self) -> dict | None:
                return None

        with patch.object(main, "TodoforAI", DelayedKeyTodoforAI), patch.object(
            main.time, "sleep"
        ):
            result = main.process_one_account(1, lambda: provider, max_retries=1)

        self.assertIsNotNone(result)
        self.assertEqual(2, DelayedKeyTodoforAI.calls)
        self.assertEqual(1, provider.create_count)

    def test_rate_limit_retries_without_blacklisting_domain(self) -> None:
        """验证 429 会退避重试但不会误判邮箱域名。"""
        provider = _FakeMailProvider()
        _RateLimitThenSuccessTodoforAI.instances = 0
        otp_gate = Mock()
        otp_gate.wait.return_value = 0

        with patch.object(main, "TodoforAI", _RateLimitThenSuccessTodoforAI), patch.object(
            main.time, "sleep"
        ):
            result = main.process_one_account(
                1,
                lambda: provider,
                otp_gate=otp_gate,
                otp_cooldown_seconds=600,
                max_retries=2,
            )

        self.assertIsNotNone(result)
        self.assertNotIn("mail.test", provider.created_exclude_domains or [])
        otp_gate.defer.assert_called_once_with(600)

    def test_cooldown_race_refreshes_mailbox_before_otp_submission(self) -> None:
        """验证冷却在 token 生成期间触发时不会提交过期邮箱。"""
        todo = _CaptchaThenSuccessTodoforAI
        otp_gate = Mock()
        otp_gate.wait_for_cooldown.return_value = 0
        otp_gate.wait.return_value = 3600

        with patch.object(main, "TodoforAI", todo), patch.object(main.time, "sleep"):
            result = main.process_one_account(
                1,
                _FakeMailProvider,
                turnstile_solver=Mock(return_value="turnstile-token"),
                turnstile_mode="always",
                otp_gate=otp_gate,
                max_retries=1,
            )

        self.assertIsNone(result)

    def test_save_result_appends_to_existing_key_file(self) -> None:
        """验证新一轮注册不会覆盖之前保存的 API Key。"""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "get_apikey.txt"
            output.write_text("existing-api-key\n", encoding="utf-8")
            with patch.object(main, "OUTPUT_FILE", output):
                main.save_result(
                    {
                        "thread_id": 1,
                        "api_keys": [{"key": "new-api-key-value"}],
                        "default_key": None,
                    }
                )
            self.assertEqual(
                ["existing-api-key", "new-api-key-value"],
                output.read_text(encoding="utf-8").splitlines(),
            )

    def test_rejected_domain_file_appends_once(self) -> None:
        """验证明确拒绝域名会持久化且自动去重。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rejected_domains.txt"
            with patch.object(main, "REJECTED_DOMAINS_FILE", path):
                self.assertTrue(main.record_rejected_domain("Blocked.Test"))
                self.assertFalse(main.record_rejected_domain("blocked.test"))
                self.assertEqual({"blocked.test"}, main.load_rejected_domains())

    def test_temporary_email_rejection_is_persisted_before_retry(self) -> None:
        """验证目标拒绝后，重试创建邮箱时已带上持久黑名单。"""
        provider = _FakeMailProvider()
        addresses = iter(["first@blocked.test", "second@allowed.test"])
        seen_exclusions: list[set[str]] = []

        def create_account(
            domain: str = "", exclude_domains: list[str] | None = None
        ) -> dict:
            address = next(addresses)
            seen_exclusions.append(set(exclude_domains or []))
            return {"id": address, "address": address, "domain": address.rsplit("@", 1)[-1]}

        provider.create_account = create_account

        class RejectOnce(_FakeTodoforAI):
            attempts = 0

            def send_otp(self, email: str, captcha_token: str = "") -> bool:
                type(self).attempts += 1
                if type(self).attempts == 1:
                    self.last_otp_error_code = "HTTP_400"
                    self.last_otp_error_message = "Temporary email addresses are not allowed."
                    self.last_otp_status = 400
                    return False
                return True

            def verify_otp(self, email: str, otp: str) -> dict | None:
                return super().verify_otp("fixture@mail.test", otp)

        with tempfile.TemporaryDirectory() as directory, patch.object(
            main, "REJECTED_DOMAINS_FILE", Path(directory) / "rejected_domains.txt"
        ), patch.object(main, "TodoforAI", RejectOnce), patch.object(main.time, "sleep"):
            result = main.process_one_account(1, lambda: provider, max_retries=2)

        self.assertIsNotNone(result)
        self.assertIn("blocked.test", seen_exclusions[1])

    def test_send_otp_preserves_business_error(self) -> None:
        """验证 OTP 接口错误码和错误消息会传递给注册流程。"""
        todo = main.TodoforAI()
        response = Mock(status_code=400)
        response.headers = {"Retry-After": "1200", "X-RateLimit-Reset": "fixture-reset"}
        response.json.return_value = {
            "code": "CAPTCHA_REQUIRED",
            "message": "Browser verification failed.",
        }
        todo.session.post = Mock(return_value=response)

        self.assertFalse(todo.send_otp("fixture@mail.test"))
        self.assertEqual("CAPTCHA_REQUIRED", todo.last_otp_error_code)
        self.assertEqual("Browser verification failed.", todo.last_otp_error_message)
        self.assertEqual(400, todo.last_otp_status)
        self.assertEqual(1200, todo.last_otp_retry_after)
        self.assertEqual("fixture-reset", todo.last_otp_rate_limit_headers["X-RateLimit-Reset"])

    def test_send_otp_adds_captcha_header(self) -> None:
        """验证浏览器令牌通过目标网页使用的请求头提交。"""
        todo = main.TodoforAI()
        response = Mock(status_code=200)
        response.headers = {}
        response.json.return_value = {"success": True}
        todo.session.post = Mock(return_value=response)

        self.assertTrue(todo.send_otp("fixture@mail.test", "turnstile-token"))
        headers = todo.session.post.call_args.kwargs["headers"]
        self.assertEqual("turnstile-token", headers["x-captcha-response"])

    def test_proxy_template_uses_one_uuid_per_session(self) -> None:
        """验证 HTTP 和 HTTPS 代理共享同一个 Resin 粘性身份。"""
        proxies = main.resolve_proxy_templates(
            {
                "http": "http://us.{uuid}:token@127.0.0.1:9200",
                "https": "http://us.{uuid}:token@127.0.0.1:9200",
            }
        )
        self.assertEqual(proxies["http"], proxies["https"])
        self.assertNotIn("{uuid}", proxies["http"])

    def test_resin_proxy_shorthand_is_normalized(self) -> None:
        """验证启动器历史保存的 Resin 简写能转换成标准代理 URL。"""
        shorthand = "http://node.{uuid}:fixture-token:9200"
        normalized = main.normalize_proxy_url(shorthand)
        self.assertEqual(
            "http://node.{uuid}:fixture-token@127.0.0.1:9200",
            normalized,
        )
        resolved = main.resolve_proxy_templates({"https": shorthand})["https"]
        parsed = main.urlparse(resolved)
        self.assertTrue(parsed.username.startswith("node."))
        self.assertEqual("fixture-token", parsed.password)
        self.assertEqual("127.0.0.1", parsed.hostname)
        self.assertEqual(9200, parsed.port)

    def test_standard_proxy_url_is_unchanged(self) -> None:
        """验证标准代理 URL 不被重复改写。"""
        proxy = "http://node.{uuid}:fixture-token@127.0.0.1:9200"
        self.assertEqual(proxy, main.normalize_proxy_url(proxy))

    def test_resin_platform_is_replaced_before_uuid_resolution(self) -> None:
        """验证同一 Resin 模板可以轮换平台并保持粘性 UUID。"""
        proxies = main.resolve_proxy_templates(
            {"https": "http://node.{uuid}:fixture-token@127.0.0.1:9200"},
            "jp",
        )
        parsed = main.urlparse(proxies["https"])
        self.assertTrue(parsed.username.startswith("jp."))
        self.assertNotIn("{uuid}", parsed.username)

    def test_invalid_proxy_url_is_rejected(self) -> None:
        """验证无法识别的代理配置会在批量任务启动前失败。"""
        with self.assertRaisesRegex(ValueError, "代理 URL 无效"):
            main.normalize_proxy_url("http://missing-port")

    def test_interactive_launcher_builds_expected_command(self) -> None:
        """验证交互启动器完整传递批量注册关键参数。"""
        command = start_reg.build_command(
            {
                "count": 10,
                "threads": 3,
                "max_retries": 6,
                "turnstile_concurrency": 2,
                "turnstile_mode": "always",
                "otp_interval_seconds": 40,
                "otp_cooldown_seconds": 3600,
                "network_cooldown_seconds": 60,
                "mailpoolhub_base_url": "http://127.0.0.1:8080/api/v1",
                "mailpoolhub_provider": "mailgw",
                "proxy_url": "http://jp.{uuid}:token@127.0.0.1:9200",
                "proxy_platforms": "node,jp,us",
            }
        )
        self.assertIn("--count", command)
        self.assertEqual("10", command[command.index("--count") + 1])
        self.assertEqual("3", command[command.index("--threads") + 1])
        self.assertEqual("mailgw", command[command.index("--mailpoolhub-provider") + 1])
        self.assertEqual("always", command[command.index("--turnstile-mode") + 1])
        self.assertEqual("40", command[command.index("--otp-interval") + 1])
        self.assertEqual("3600", command[command.index("--otp-cooldown") + 1])
        self.assertEqual("60", command[command.index("--network-cooldown") + 1])
        self.assertEqual("node,jp,us", command[command.index("--proxy-platforms") + 1])

    def test_interactive_launcher_remembers_settings(self) -> None:
        """验证启动器配置可原子保存并在下次启动复用。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            settings = {"count": 10, "threads": 3, "mailpoolhub_api_key": "fixture-key"}
            start_reg.save_settings(settings, path)
            self.assertEqual(settings, start_reg.load_settings(path))

    def test_stale_saved_provider_defaults_to_random(self) -> None:
        """验证旧渠道不在成功清单时回车会切换为随机。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "working.json"
            path.write_text(
                json.dumps(
                    {
                        "channels": [
                            {
                                "provider": "verified",
                                "attempts": 10,
                                "successes": 8,
                                "domains": [{"domain": "mail.test", "successes": 8}],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(start_reg, "WORKING_CHANNELS_FILE", path), patch(
                "builtins.input", return_value=""
            ):
                self.assertEqual("random", start_reg.prompt_mail_provider("removed"))

    def test_interactive_launcher_force_stops_child_on_ctrl_c(self) -> None:
        """验证 Ctrl+C 会终止注册及浏览器进程树并返回 130。"""
        process = Mock()
        process.stdout = io.StringIO("")
        process.wait.side_effect = KeyboardInterrupt
        with patch.object(start_reg.subprocess, "Popen", return_value=process) as popen, patch.object(
            start_reg, "terminate_process_tree"
        ) as terminate, patch.object(start_reg, "relay_output"):
            result = start_reg.run_registration(["python", "main.py"], {})
        self.assertEqual(130, result)
        terminate.assert_called_once_with(process)
        popen_options = popen.call_args.kwargs
        self.assertEqual(start_reg.subprocess.CREATE_NEW_PROCESS_GROUP, popen_options["creationflags"])

    def test_registration_log_rotates_at_configured_size(self) -> None:
        """验证注册日志达到上限后轮转且单文件不超过限制。"""
        with tempfile.TemporaryDirectory() as directory, patch("builtins.print"):
            path = Path(directory) / "start_reg.log"
            start_reg.relay_output(io.StringIO(("x" * 32 + "\n") * 20), path, 256)
            files = [path, path.with_name(path.name + ".1")]
            self.assertTrue(all(file.exists() for file in files))
            self.assertTrue(all(file.stat().st_size <= 256 for file in files))

    def test_windows_launcher_is_ascii_crlf(self) -> None:
        """防止 cmd.exe 将 UTF-8/LF 批处理错误解析为残缺命令。"""
        data = Path(start_reg.__file__).with_name("start_reg.bat").read_bytes()
        data.decode("ascii")
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))
        self.assertIn(
            b'start "Todofor.ai Registration" python "%~dp0start_reg.py" --pause\r\n',
            data,
        )
        self.assertNotIn(b"/wait /b", data)

    def test_factory_returns_independent_mailpoolhub_sessions(self) -> None:
        """验证并发任务不会共享 requests.Session。"""
        factory, label = main.create_mail_provider_factory(
            "mailpoolhub",
            {
                "mailpoolhub": {
                    "base_url": "http://127.0.0.1:8080/api/v1",
                    "api_key": "local-test-token",
                    "poll_interval_seconds": 1,
                }
            },
        )
        first = factory()
        second = factory()
        self.assertIn("MailPoolHub", label)
        self.assertIsNot(first, second)
        self.assertIsNot(first.session, second.session)
        self.assertFalse(first.session.trust_env)
        self.assertLess(first.provider_cursor, len(first.provider_names))
        self.assertLess(second.provider_cursor, len(second.provider_names))

    def test_factory_accepts_mailpoolhub_provider_override(self) -> None:
        """验证启动器可固定到已经实测成功的下游渠道。"""
        factory, _ = main.create_mail_provider_factory(
            "mailpoolhub",
            {"mailpoolhub": {"api_key": "local-test-token"}},
            mailpoolhub_provider_name="mailgw",
        )
        provider = factory()
        self.assertEqual("mailgw", provider.provider_name)
        self.assertEqual("mailgw", provider.provider_names[0])

    def test_factory_rejects_missing_mailpoolhub_api_key(self) -> None:
        """验证缺少 MailPoolHub API Key 时在启动阶段给出配置错误。"""
        with self.assertRaisesRegex(ValueError, "MailPoolHub API Key"):
            main.create_mail_provider_factory(
                "mailpoolhub",
                {"mailpoolhub": {"base_url": "http://127.0.0.1:8080/api/v1"}},
            )


class _FakeMailProvider:
    """注册主流程使用的内存邮箱渠道。"""

    def __init__(self):
        """初始化调用记录。"""
        self.created_domain = ""
        self.created_exclude_domains: list[str] | None = None
        self.waited_account: dict = {}
        self.closed_account_ids: list[str] = []
        self.create_count = 0

    def list_domains(self) -> list[str]:
        """返回测试域名。"""
        return ["mail.test"]

    def create_account(self, domain: str = "", exclude_domains: list[str] | None = None) -> dict:
        """记录参数并返回测试邮箱。"""
        self.created_domain = domain
        self.created_exclude_domains = exclude_domains
        self.create_count += 1
        return {"id": "mailbox-1", "address": "fixture@mail.test", "domain": "mail.test"}

    def wait_for_code(self, account: dict, timeout: int = 90) -> str | None:
        """记录完整账户并返回测试验证码。"""
        self.waited_account = account
        return "481205"

    def close_account(self, account: dict) -> None:
        """记录已清理邮箱。"""
        self.closed_account_ids.append(str(account.get("id") or ""))


class _FakeTodoforAI:
    """隔离外部站点调用的注册流程测试替身。"""

    def __init__(self, proxies: dict | None = None):
        """保存代理参数用于接口兼容。"""
        self.proxies = proxies
        self.proxy_url = str((proxies or {}).get("https") or (proxies or {}).get("http") or "")

    def init_anonymous_session(self) -> bool:
        """模拟匿名会话创建成功。"""
        return True

    def send_otp(self, email: str, captcha_token: str = "") -> bool:
        """模拟向测试邮箱发送 OTP。"""
        return email == "fixture@mail.test"

    def verify_otp(self, email: str, otp: str) -> dict | None:
        """模拟使用测试 OTP 登录成功。"""
        if email != "fixture@mail.test" or otp != "481205":
            return None
        return {
            "token": "test-session-token",
            "user": {
                "id": "user-1",
                "name": "Fixture",
                "emailVerified": True,
                "isAnonymous": False,
                "createdAt": "",
                "updatedAt": "",
            },
        }

    def get_api_keys(self) -> list[dict]:
        """返回测试 API Key 对象。"""
        return [{"key": "fixture-api-key-value"}]

    def get_default_api_key(self) -> dict | None:
        """返回与列表相同的默认 Key。"""
        return {"key": "fixture-api-key-value"}


class _RejectingTodoforAI(_FakeTodoforAI):
    """模拟目标站拒绝发送 OTP 的客户端。"""

    def send_otp(self, email: str, captcha_token: str = "") -> bool:
        """模拟 OTP 发送失败。"""
        return False


class _CaptchaRequiredTodoforAI(_FakeTodoforAI):
    """模拟目标业务要求浏览器验证。"""

    last_otp_error_code = "CAPTCHA_REQUIRED"
    last_otp_error_message = "Browser verification failed."
    last_otp_status = 400

    def send_otp(self, email: str, captcha_token: str = "") -> bool:
        """模拟纯 HTTP 会话缺少浏览器验证令牌。"""
        return False


class _CaptchaThenSuccessTodoforAI(_FakeTodoforAI):
    """模拟浏览器令牌到达后允许发送 OTP。"""

    last_otp_error_code = ""
    last_otp_error_message = ""
    last_otp_status = 0

    def send_otp(self, email: str, captcha_token: str = "") -> bool:
        if captcha_token == "turnstile-token":
            self.last_otp_error_code = ""
            self.last_otp_error_message = ""
            self.last_otp_status = 200
            return True
        self.last_otp_error_code = "CAPTCHA_REQUIRED"
        self.last_otp_error_message = "Browser verification failed."
        self.last_otp_status = 400
        return False


class _InitFailsOnceTodoforAI(_FakeTodoforAI):
    """模拟首个代理出口初始化失败。"""

    instances = 0

    def __init__(self, proxies: dict | None = None):
        super().__init__(proxies)
        type(self).instances += 1

    def init_anonymous_session(self) -> bool:
        return type(self).instances > 1


class _AlwaysInitFailsTodoforAI(_FakeTodoforAI):
    """模拟所有代理平台均无法初始化会话。"""

    def init_anonymous_session(self) -> bool:
        return False


class _NoKeysThenSuccessTodoforAI(_FakeTodoforAI):
    """模拟首次登录后 API Key 尚未创建。"""

    instances = 0

    def __init__(self, proxies: dict | None = None):
        super().__init__(proxies)
        type(self).instances += 1

    def get_api_keys(self) -> list[dict]:
        return [] if type(self).instances == 1 else super().get_api_keys()

    def get_default_api_key(self) -> dict | None:
        return None if type(self).instances == 1 else super().get_default_api_key()


class _RateLimitThenSuccessTodoforAI(_FakeTodoforAI):
    """模拟首个会话触发 OTP 限速、第二个会话成功。"""

    instances = 0

    def __init__(self, proxies: dict | None = None):
        super().__init__(proxies)
        type(self).instances += 1

    def send_otp(self, email: str, captcha_token: str = "") -> bool:
        if type(self).instances > 1:
            return True
        self.last_otp_error_code = "HTTP_429"
        self.last_otp_error_message = "Too many sign-in attempts."
        self.last_otp_status = 429
        return False


if __name__ == "__main__":
    unittest.main()
