"""Lifecycle notifications for Feishu, DingTalk, WeCom, Slack and generic JSON.

Only the standard library is used, so the notifier works in a Kaggle notebook
without extra installs.  Lifecycle messages are queued to a daemon worker (see
``notify_async``) so a slow webhook never blocks startup or restart.  Sending is
best-effort: every failure is logged and ``notify()`` returns ``False`` instead
of raising, because a broken webhook must never take down the watchdog.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Callable, Dict, Mapping, Protocol, Tuple

from .config import WebhookConfig

log = logging.getLogger("kgcomfy")

_MAX_TEXT_CHARS = 4000
#: WeCom rejects text content larger than 2048 bytes.
_WECOM_MAX_BYTES = 2000

#: Field names are stable English keys; the text message shows Chinese labels.
_DETAIL_LABELS = {
    "pid": "PID",
    "url": "访问地址",
    "exit_code": "退出码",
    "uptime": "运行时长",
    "reason": "原因",
    "restarts": "累计重启",
    "restart_attempts": "窗口内重启次数",
    "startup_seconds": "启动耗时(秒)",
    "delay": "重启延迟",
    "attempt": "本次窗口重启次数",
    "max_restarts": "重启上限",
    "restart_window": "统计窗口(秒)",
    "last_error": "最后错误",
    "error": "错误",
    "log_path": "日志文件",
    "log_tail": "日志尾部",
    "provider": "平台",
}

#: Provider-specific payload builders/response checkers. Adding a platform
#: means adding one entry here plus its config alias.
_PAYLOAD_BUILDERS = {
    "feishu": "_build_feishu_payload",
    "dingtalk": "_build_dingtalk_payload",
    "wecom": "_build_wecom_payload",
    "slack": "_build_slack_payload",
    "generic": "_build_generic_payload",
}
_RESPONSE_CHECKERS = {
    "feishu": "_feishu_response_error",
    "dingtalk": "_errcode_response_error",
    "wecom": "_errcode_response_error",
    "slack": "_slack_response_error",
}


def detect_provider(url: str, provider: str = "auto") -> str:
    """Resolve ``auto`` to a platform name from the webhook URL."""
    chosen = (provider or "auto").strip().lower()
    if chosen != "auto":
        return chosen
    try:
        host = urllib.parse.urlsplit(url).netloc.lower()
    except ValueError:
        host = ""
    if "feishu" in host or "larksuite" in host:
        return "feishu"
    if "dingtalk" in host:
        return "dingtalk"
    if "qyapi.weixin.qq.com" in host:
        return "wecom"
    if "hooks.slack.com" in host:
        return "slack"
    return "generic"


def _sign(key: str, message: str) -> str:
    digest = hmac.new(
        key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def feishu_sign(timestamp: str, secret: str) -> str:
    """Return the Feishu custom-bot signature for ``timestamp``."""
    return _sign(f"{timestamp}\n{secret}", "")


def dingtalk_sign(timestamp: str, secret: str) -> str:
    """Return the DingTalk custom-bot signature for ``timestamp`` (ms)."""
    return _sign(secret, f"{timestamp}\n{secret}")


def append_query(url: str, params: Mapping[str, str]) -> str:
    """Merge ``params`` into the query string of ``url``."""
    parts = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    query.update(params)
    return urllib.parse.urlunsplit(
        parts._replace(query=urllib.parse.urlencode(query))
    )


class Notifier(Protocol):
    """Minimal notification sink used by the supervisor and app."""

    def notify(
        self,
        event: str,
        summary: str,
        details: Mapping[str, Any] | None = None,
    ) -> bool:
        ...

    def notify_async(
        self,
        event: str,
        summary: str,
        details: Mapping[str, Any] | None = None,
    ) -> bool:
        ...

    def flush(self, timeout: float | None = None) -> bool:
        ...


class WebhookNotifier:
    """Format and POST lifecycle notifications to one webhook endpoint."""

    def __init__(
        self,
        config: WebhookConfig,
        url: str | None = None,
        secret: str | None = None,
        context: Mapping[str, Any] | None = None,
        logger: logging.Logger | None = None,
        clock: Callable[[], float] = time.time,
        opener: Any | None = None,
    ):
        self.config = config
        # Explicit constructor values override the config; None means "use
        # whatever the config resolved to".
        self.url = (
            url if url is not None else (config.url or "")
        ).strip()
        self.secret = (
            secret if secret is not None else (config.secret or "")
        ).strip()
        self.provider = detect_provider(self.url, config.provider)
        self.context = dict(context or {})
        self.log = logger or log
        self._clock = clock
        self._opener = opener or urllib.request.build_opener()
        self._queue: queue.Queue | None = None
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()

    # ------------------------------------------------------------- public API
    def notify(
        self,
        event: str,
        summary: str,
        details: Mapping[str, Any] | None = None,
    ) -> bool:
        """Send one notification; never raises."""
        event_name = str(event or "").strip().lower()
        if not self._should_send(event_name):
            return False

        try:
            prepared = self._prepare_details(details)
            text = self._format(summary, prepared)
            target, payload = self._build_payload(event_name, text, prepared)
            return self._post(target, payload, event_name)
        except Exception:  # notifications must never break the caller
            self.log.warning(
                "Webhook notification (%s/%s) failed",
                self.provider,
                event_name,
                exc_info=True,
            )
            return False

    def notify_async(
        self,
        event: str,
        summary: str,
        details: Mapping[str, Any] | None = None,
    ) -> bool:
        """Queue a notification on the background sender; never blocks on HTTP."""
        event_name = str(event or "").strip().lower()
        if not self._should_send(event_name):
            return False

        self._ensure_worker()
        assert self._queue is not None
        self._queue.put((event_name, summary, details))
        return True

    def send_test(self) -> bool:
        """Send a test message synchronously, bypassing the event filter."""
        return self.notify(
            "test",
            "测试消息：webhook 配置可用",
            {"provider": self.provider},
        )

    def flush(self, timeout: float | None = None) -> bool:
        """Wait for queued notifications; useful for tests and shutdown."""
        if self._queue is None:
            return True
        if timeout is None:
            pending = max(1, self._queue.unfinished_tasks)
            timeout = min(30.0, pending * (self.config.timeout + 1.0))
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def _should_send(self, event: str) -> bool:
        if not self.config.enabled or not self.url:
            return False
        return event == "test" or self.accepts(event)

    def accepts(self, event: str) -> bool:
        events = self.config.events
        return not events or event in events

    def _ensure_worker(self) -> None:
        if self._worker is not None:
            return
        with self._worker_lock:
            if self._worker is not None:
                return
            self._queue = queue.Queue()
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="kgcomfy-webhook",
                daemon=True,
            )
            self._worker.start()

    def _worker_loop(self) -> None:
        assert self._queue is not None
        while True:
            event, summary, details = self._queue.get()
            try:
                self.notify(event, summary, details)
            finally:
                self._queue.task_done()

    # --------------------------------------------------------------- message
    def _prepare_details(
        self, details: Mapping[str, Any] | None
    ) -> Dict[str, Any]:
        prepared: Dict[str, Any] = dict(self.context)
        if details:
            prepared.update(details)

        tail = prepared.get("log_tail")
        if tail:
            if not self.config.include_log_tail or self.config.log_tail_lines <= 0:
                prepared.pop("log_tail", None)
            else:
                lines = tail.splitlines() if isinstance(tail, str) else list(tail)
                prepared["log_tail"] = lines[-self.config.log_tail_lines :]
        else:
            prepared.pop("log_tail", None)
        return prepared

    def _format(self, summary: str, details: Mapping[str, Any]) -> str:
        title = (self.config.title or "ComfyUI").strip()
        lines = [f"【{title}】{summary}", f"时间: {self._now_text()}"]
        for key, value in details.items():
            if not _has_value(value):
                continue
            label = _DETAIL_LABELS.get(key, key)
            if isinstance(value, (list, tuple)):
                lines.append(f"{label}:")
                lines.extend(f"  {item}" for item in value)
            else:
                lines.append(f"{label}: {value}")

        text = "\n".join(lines)
        if len(text) > _MAX_TEXT_CHARS:
            text = text[: _MAX_TEXT_CHARS - 20].rstrip() + "\n...(消息已截断)"
        return text

    def _now_text(self) -> str:
        return (
            datetime.fromtimestamp(self._clock())
            .astimezone()
            .strftime("%Y-%m-%d %H:%M:%S %Z")
            .strip()
        )

    # --------------------------------------------------------------- payload
    def _build_payload(
        self, event: str, text: str, details: Mapping[str, Any]
    ) -> Tuple[str, Dict[str, Any]]:
        builder_name = _PAYLOAD_BUILDERS.get(
            self.provider, "_build_generic_payload"
        )
        return getattr(self, builder_name)(event, text, details)

    def _build_feishu_payload(
        self, event: str, text: str, details: Mapping[str, Any]
    ) -> Tuple[str, Dict[str, Any]]:
        payload: Dict[str, Any] = {
            "msg_type": "text",
            "content": {"text": text},
        }
        if self.secret:
            timestamp = str(int(self._clock()))
            payload["timestamp"] = timestamp
            payload["sign"] = feishu_sign(timestamp, self.secret)
        return self.url, payload

    def _build_dingtalk_payload(
        self, event: str, text: str, details: Mapping[str, Any]
    ) -> Tuple[str, Dict[str, Any]]:
        payload = {"msgtype": "text", "text": {"content": text}}
        target = self.url
        if self.secret:
            timestamp = str(int(self._clock() * 1000))
            target = append_query(
                target,
                {
                    "timestamp": timestamp,
                    "sign": dingtalk_sign(timestamp, self.secret),
                },
            )
        return target, payload

    def _build_wecom_payload(
        self, event: str, text: str, details: Mapping[str, Any]
    ) -> Tuple[str, Dict[str, Any]]:
        content = _truncate_utf8(text, _WECOM_MAX_BYTES)
        return self.url, {"msgtype": "text", "text": {"content": content}}

    def _build_slack_payload(
        self, event: str, text: str, details: Mapping[str, Any]
    ) -> Tuple[str, Dict[str, Any]]:
        return self.url, {"text": text}

    def _build_generic_payload(
        self, event: str, text: str, details: Mapping[str, Any]
    ) -> Tuple[str, Dict[str, Any]]:
        return self.url, {
            "event": event,
            "title": self.config.title,
            "text": text,
            "timestamp": self._now_text(),
            "details": _jsonable(details),
        }

    # ------------------------------------------------------------------ HTTP
    def _post(
        self, url: str, payload: Mapping[str, Any], event: str
    ) -> bool:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with self._opener.open(
            request, timeout=self.config.timeout
        ) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            body = response.read(64 * 1024).decode("utf-8", "replace")

        if not 200 <= int(status or 0) < 300:
            self.log.warning(
                "Webhook notification (%s/%s) returned HTTP %s: %s",
                self.provider,
                event,
                status,
                body[:200],
            )
            return False

        error = self._response_error(body)
        if error:
            self.log.warning(
                "Webhook notification (%s/%s) was rejected: %s",
                self.provider,
                event,
                error,
            )
            return False

        self.log.info(
            "Sent webhook notification (%s, event=%s)", self.provider, event
        )
        return True

    def _response_error(self, body: str) -> str:
        if not body.strip():
            return ""
        try:
            data = json.loads(body)
        except ValueError:
            return ""
        if not isinstance(data, Mapping):
            return ""
        checker_name = _RESPONSE_CHECKERS.get(self.provider)
        if checker_name is None:
            return ""
        return getattr(self, checker_name)(data)

    def _feishu_response_error(self, data: Mapping[str, Any]) -> str:
        code = data.get("code", data.get("StatusCode"))
        if code in (None, 0, "0"):
            return ""
        message = data.get("msg", data.get("StatusMessage", ""))
        return f"code={code} msg={message}"

    def _errcode_response_error(self, data: Mapping[str, Any]) -> str:
        code = data.get("errcode")
        if code in (None, 0, "0"):
            return ""
        return f"errcode={code} errmsg={data.get('errmsg', '')}"

    def _slack_response_error(self, data: Mapping[str, Any]) -> str:
        if data.get("ok") is False:
            return f"ok=false error={data.get('error', '')}"
        return ""


def send_notification(
    notifier: Notifier | None,
    logger: logging.Logger,
    event: str,
    summary: str,
    details: Mapping[str, Any] | None = None,
) -> None:
    """Queue a notification through the notifier's background sender."""
    if notifier is None:
        return
    try:
        notifier.notify_async(event, summary, details)
    except Exception:
        logger.warning("Webhook notification %s failed", event, exc_info=True)


def flush_notifications(
    notifier: Notifier | None,
    logger: logging.Logger,
    timeout: float | None = None,
) -> None:
    """Wait for queued notifications before the process exits."""
    if notifier is None:
        return
    try:
        notifier.flush(timeout=timeout)
    except Exception:
        logger.warning("Webhook flush failed", exc_info=True)


def _truncate_utf8(text: str, limit: int) -> str:
    """Trim ``text`` to ``limit`` UTF-8 bytes without splitting a character."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    suffix = "\n...(消息已截断)"
    budget = max(0, limit - len(suffix.encode("utf-8")))
    return encoded[:budget].decode("utf-8", "ignore").rstrip() + suffix


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _jsonable(value: Any) -> Any:
    """Convert a details mapping to plain JSON types for generic payloads."""
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
