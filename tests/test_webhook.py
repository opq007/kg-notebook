import json
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from kgcomfy.config import WebhookConfig
from kgcomfy.webhook import (
    WebhookNotifier,
    append_query,
    detect_provider,
    dingtalk_sign,
    feishu_sign,
)


def webhook_config(**overrides):
    values = {
        "enabled": True,
        "url": "https://example.com/hook",
        "timeout": 2.0,
        "events": [],
    }
    values.update(overrides)
    return WebhookConfig(**values)


class FakeResponse:
    def __init__(self, status=200, body=b'{"code":0}'):
        self.status = status
        self._body = body

    def read(self, size=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class RecordingOpener:
    def __init__(self, status=200, body=b'{"code":0}', error=None):
        self.status = status
        self.body = body
        self.error = error
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        if self.error is not None:
            raise self.error
        return FakeResponse(self.status, self.body)


def payload_of(request):
    return json.loads(request.data.decode("utf-8"))


class ProviderTests(unittest.TestCase):
    def test_detection_from_url(self):
        cases = {
            "https://open.feishu.cn/open-apis/bot/v2/hook/x": "feishu",
            "https://open.larksuite.com/open-apis/bot/v2/hook/x": "feishu",
            "https://oapi.dingtalk.com/robot/send?access_token=x": "dingtalk",
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x": "wecom",
            "https://hooks.slack.com/services/x": "slack",
            "https://example.com/hook": "generic",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(detect_provider(url, "auto"), expected)

    def test_explicit_provider_wins(self):
        self.assertEqual(
            detect_provider("https://example.com/hook", "feishu"), "feishu"
        )

    def test_append_query_preserves_existing_parameters(self):
        self.assertEqual(
            append_query(
                "https://example.com/hook?access_token=abc",
                {"timestamp": "1", "sign": "a+b"},
            ),
            "https://example.com/hook?access_token=abc&timestamp=1&sign=a%2Bb",
        )


class PayloadTests(unittest.TestCase):
    def test_feishu_text_payload_with_signature(self):
        opener = RecordingOpener(body=b'{"code":0,"msg":"success"}')
        notifier = WebhookNotifier(
            webhook_config(
                provider="feishu",
                url="https://open.feishu.cn/open-apis/bot/v2/hook/x",
                secret="s3cret",
            ),
            opener=opener,
            clock=lambda: 1700000000.0,
        )

        self.assertTrue(notifier.notify("started", "ComfyUI 已启动", {"pid": 42}))

        request, timeout = opener.requests[0]
        payload = payload_of(request)
        self.assertEqual(payload["msg_type"], "text")
        self.assertIn("【ComfyUI】ComfyUI 已启动", payload["content"]["text"])
        self.assertIn("PID: 42", payload["content"]["text"])
        self.assertEqual(payload["timestamp"], "1700000000")
        self.assertEqual(payload["sign"], feishu_sign("1700000000", "s3cret"))
        self.assertEqual(timeout, 2.0)
        self.assertEqual(
            request.full_url, "https://open.feishu.cn/open-apis/bot/v2/hook/x"
        )
        self.assertIn("application/json", request.get_header("Content-type"))

    def test_dingtalk_signature_is_added_to_url(self):
        opener = RecordingOpener(body=b'{"errcode":0,"errmsg":"ok"}')
        url = "https://oapi.dingtalk.com/robot/send?access_token=abc"
        notifier = WebhookNotifier(
            webhook_config(provider="dingtalk", url=url, secret="SECxyz"),
            opener=opener,
            clock=lambda: 1700000000.5,
        )

        self.assertTrue(
            notifier.notify("crashed", "ComfyUI 意外停止", {"exit_code": 1})
        )

        request, _ = opener.requests[0]
        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(request.full_url).query
        )
        timestamp = str(int(1700000000.5 * 1000))
        self.assertEqual(query["access_token"], ["abc"])
        self.assertEqual(query["timestamp"], [timestamp])
        self.assertEqual(query["sign"], [dingtalk_sign(timestamp, "SECxyz")])

        payload = payload_of(request)
        self.assertEqual(payload["msgtype"], "text")
        self.assertIn("退出码: 1", payload["text"]["content"])

    def test_wecom_payload(self):
        opener = RecordingOpener(body=b'{"errcode":0,"errmsg":"ok"}')
        notifier = WebhookNotifier(
            webhook_config(
                provider="wecom",
                url="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x",
            ),
            opener=opener,
        )

        self.assertTrue(notifier.notify("restarted", "ComfyUI 已重启", {}))

        payload = payload_of(opener.requests[0][0])
        self.assertEqual(payload["msgtype"], "text")
        self.assertIn("ComfyUI 已重启", payload["text"]["content"])

    def test_slack_payload(self):
        opener = RecordingOpener(body=b"ok")
        notifier = WebhookNotifier(
            webhook_config(
                provider="slack",
                url="https://hooks.slack.com/services/x",
            ),
            opener=opener,
        )

        self.assertTrue(notifier.notify("failed", "ComfyUI 重启失败", {}))

        payload = payload_of(opener.requests[0][0])
        self.assertEqual(set(payload), {"text"})
        self.assertIn("ComfyUI 重启失败", payload["text"])

    def test_generic_payload_contains_structured_details(self):
        opener = RecordingOpener(body=b"{}")
        notifier = WebhookNotifier(
            webhook_config(
                provider="generic",
                title="TTS",
                include_log_tail=True,
            ),
            opener=opener,
        )

        self.assertTrue(
            notifier.notify(
                "crashed",
                "ComfyUI 意外停止",
                {"pid": 7, "log_tail": ["line1", "line2"]},
            )
        )

        payload = payload_of(opener.requests[0][0])
        self.assertEqual(payload["event"], "crashed")
        self.assertEqual(payload["title"], "TTS")
        self.assertEqual(payload["details"]["pid"], 7)
        self.assertEqual(payload["details"]["log_tail"], ["line1", "line2"])
        self.assertIn("ComfyUI 意外停止", payload["text"])
        self.assertTrue(payload["timestamp"])

    def test_context_is_merged_into_messages(self):
        opener = RecordingOpener(body=b'{"errcode":0,"errmsg":"ok"}')
        notifier = WebhookNotifier(
            webhook_config(provider="wecom"),
            context={"url": "http://tts.example.com"},
            opener=opener,
        )

        self.assertTrue(notifier.notify("started", "ComfyUI 已启动", {"pid": 5}))

        text = payload_of(opener.requests[0][0])["text"]["content"]
        self.assertIn("访问地址: http://tts.example.com", text)
        self.assertIn("PID: 5", text)

    def test_long_message_is_truncated(self):
        opener = RecordingOpener(body=b'{"errcode":0,"errmsg":"ok"}')
        notifier = WebhookNotifier(
            webhook_config(provider="wecom"), opener=opener
        )

        notifier.notify("crashed", "x" * 5000, {})

        text = payload_of(opener.requests[0][0])["text"]["content"]
        self.assertIn("消息已截断", text)
        self.assertLessEqual(len(text), 4000)
        self.assertLessEqual(len(text.encode("utf-8")), 2000)


class FilteringTests(unittest.TestCase):
    def test_unlisted_event_is_not_sent(self):
        opener = RecordingOpener()
        notifier = WebhookNotifier(
            webhook_config(events=["failed"]), opener=opener
        )

        self.assertFalse(notifier.notify("started", "ComfyUI 已启动"))
        self.assertEqual(opener.requests, [])

    def test_empty_event_list_means_all(self):
        opener = RecordingOpener()
        notifier = WebhookNotifier(webhook_config(events=[]), opener=opener)

        self.assertTrue(notifier.notify("started", "ComfyUI 已启动"))
        self.assertEqual(len(opener.requests), 1)

    def test_test_event_bypasses_the_filter(self):
        opener = RecordingOpener()
        notifier = WebhookNotifier(
            webhook_config(events=["failed"]), opener=opener
        )

        self.assertTrue(notifier.send_test())
        self.assertEqual(len(opener.requests), 1)

    def test_disabled_notifier_sends_nothing(self):
        opener = RecordingOpener()
        notifier = WebhookNotifier(
            webhook_config(enabled=False, events=[]), opener=opener
        )

        self.assertFalse(notifier.notify("started", "ComfyUI 已启动"))
        self.assertEqual(opener.requests, [])

    def test_async_notification_is_delivered_by_worker(self):
        opener = RecordingOpener()
        notifier = WebhookNotifier(
            webhook_config(provider="wecom"), opener=opener
        )

        self.assertTrue(
            notifier.notify_async("started", "ComfyUI 已启动", {"pid": 1})
        )
        self.assertTrue(notifier.flush(timeout=2))

        self.assertEqual(len(opener.requests), 1)
        text = payload_of(opener.requests[0][0])["text"]["content"]
        self.assertIn("PID: 1", text)

    def test_async_notification_respects_the_event_filter(self):
        opener = RecordingOpener()
        notifier = WebhookNotifier(
            webhook_config(events=["failed"]), opener=opener
        )

        self.assertFalse(notifier.notify_async("started", "ComfyUI 已启动"))
        self.assertTrue(notifier.flush(timeout=0))
        self.assertEqual(opener.requests, [])

    def test_log_tail_is_opt_in_and_trimmed(self):
        opener = RecordingOpener()
        notifier = WebhookNotifier(
            webhook_config(provider="wecom"), opener=opener
        )
        notifier.notify(
            "crashed", "ComfyUI 意外停止", {"log_tail": ["line1", "line2"]}
        )
        text = payload_of(opener.requests[0][0])["text"]["content"]
        self.assertNotIn("日志尾部", text)
        self.assertNotIn("line1", text)

        opener = RecordingOpener()
        notifier = WebhookNotifier(
            webhook_config(
                provider="wecom",
                include_log_tail=True,
                log_tail_lines=1,
            ),
            opener=opener,
        )
        notifier.notify(
            "crashed", "ComfyUI 意外停止", {"log_tail": ["line1", "line2"]}
        )
        text = payload_of(opener.requests[0][0])["text"]["content"]
        self.assertIn("日志尾部", text)
        self.assertIn("line2", text)
        self.assertNotIn("line1", text)


class FailureTests(unittest.TestCase):
    def _notifier(self, opener, provider="wecom"):
        return WebhookNotifier(
            webhook_config(provider=provider), opener=opener
        )

    def test_non_2xx_status_returns_false(self):
        opener = RecordingOpener(status=500, body=b"oops")
        self.assertFalse(
            self._notifier(opener).notify("started", "ComfyUI 已启动")
        )

    def test_business_error_returns_false(self):
        cases = [
            (
                "dingtalk",
                b'{"errcode":310000,"errmsg":"invalid"}',
            ),
            (
                "feishu",
                b'{"code":19021,"msg":"sign error"}',
            ),
            (
                "slack",
                b'{"ok":false,"error":"invalid_token"}',
            ),
        ]
        for provider, body in cases:
            with self.subTest(provider=provider):
                opener = RecordingOpener(body=body)
                self.assertFalse(
                    self._notifier(opener, provider).notify(
                        "started", "ComfyUI 已启动"
                    )
                )

    def test_network_error_returns_false(self):
        opener = RecordingOpener(error=urllib.error.URLError("down"))
        self.assertFalse(
            self._notifier(opener).notify("started", "ComfyUI 已启动")
        )

    def test_malformed_response_body_is_tolerated(self):
        opener = RecordingOpener(body=b"not json")
        self.assertTrue(
            self._notifier(opener).notify("started", "ComfyUI 已启动")
        )


class _WebhookHandler(BaseHTTPRequestHandler):
    received = []
    response = b'{"code":0,"msg":"success"}'

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        type(self).received.append((self.path, body))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(type(self).response)

    def log_message(self, *args):
        pass


class HttpEndToEndTests(unittest.TestCase):
    def test_real_http_post(self):
        _WebhookHandler.received = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), _WebhookHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({})
            )
            notifier = WebhookNotifier(
                webhook_config(
                    provider="feishu",
                    url=f"http://127.0.0.1:{port}/hook",
                ),
                opener=opener,
                clock=lambda: 1700000000.0,
            )

            self.assertTrue(
                notifier.notify("started", "ComfyUI 已启动", {"pid": 1})
            )

            self.assertEqual(len(_WebhookHandler.received), 1)
            path, body = _WebhookHandler.received[0]
            self.assertEqual(path, "/hook")
            self.assertEqual(json.loads(body)["msg_type"], "text")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
