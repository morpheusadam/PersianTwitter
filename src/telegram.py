"""ارسال پست به کانال از طریق Bot API."""

import html
import logging

import httpx

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"

# سقف تلگرام ۴۰۹۶ است؛ کمی حاشیه می‌گذاریم برای لینک و عنوان.
MAX_BODY = 3500


class TelegramError(RuntimeError):
    pass


def build_message(label: str, body: str, url: str) -> str:
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY].rsplit(" ", 1)[0] + "…"

    return (
        f"<b>{html.escape(label)}</b>\n\n"
        f"{html.escape(body)}\n\n"
        f'<a href="{html.escape(url, quote=True)}">🔗 منبع</a>'
    )


def send(text: str, token: str, channel: str, client: httpx.Client) -> None:
    resp = client.post(
        API.format(token=token, method="sendMessage"),
        json={
            "chat_id": channel,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise TelegramError(f"sendMessage {resp.status_code}: {resp.text[:300]}")
