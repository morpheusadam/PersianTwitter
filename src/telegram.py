"""ارسال پست به کانال از طریق Bot API."""

import html
import logging

import httpx

from .models import Item

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"

# سقف تلگرام برای پیام متنی ۴۰۹۶ است و برای caption عکس فقط ۱۰۲۴.
MAX_TEXT = 3500
MAX_CAPTION = 900


class TelegramError(RuntimeError):
    pass


def build_message(label: str, body: str, limit: int) -> str:
    if len(body) > limit:
        body = body[:limit].rsplit(" ", 1)[0] + "…"
    return f"<b>{html.escape(label)}</b>\n\n{html.escape(body)}"


def build_keyboard(item: Item) -> dict:
    """دکمه‌های زیر پست.

    هر دو از نوع url هستند، نه callback. یعنی هیچ backend زنده‌ای نمی‌خواهند و
    روی همین معماری‌ی cron کار می‌کنند. دکمه‌ی callback به handler نیاز داشت.
    """
    row = [{"text": "📄 منبع", "url": item.url}]
    if item.discussion_url and item.discussion_url != item.url:
        row.append({"text": "💬 بحث", "url": item.discussion_url})
    return {"inline_keyboard": [row]}


def publish(
    item: Item,
    body: str,
    token: str,
    channel: str,
    client: httpx.Client,
) -> None:
    """اگر عکس باشد با عکس می‌فرستد، وگرنه متنی.

    تلگرام خودش عکس را از URL برمی‌دارد، پس فایلی دانلود نمی‌کنیم. ولی گاهی به
    عکس نمی‌رسد یا فرمتش را نمی‌پذیرد؛ در آن حالت به پیام متنی برمی‌گردیم تا خبر
    به‌خاطر یک عکس از دست نرود.
    """
    keyboard = build_keyboard(item)

    if item.image_url:
        try:
            _call(
                "sendPhoto",
                {
                    "chat_id": channel,
                    "photo": item.image_url,
                    "caption": build_message(item.label, body, MAX_CAPTION),
                    "parse_mode": "HTML",
                    "reply_markup": keyboard,
                },
                token,
                client,
            )
            return
        except TelegramError as exc:
            log.warning("عکس فرستاده نشد، متنی می‌فرستم: %s", exc)

    _call(
        "sendMessage",
        {
            "chat_id": channel,
            "text": build_message(item.label, body, MAX_TEXT),
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
            "reply_markup": keyboard,
        },
        token,
        client,
    )


def _call(method: str, payload: dict, token: str, client: httpx.Client) -> None:
    resp = client.post(API.format(token=token, method=method), json=payload, timeout=60)
    if resp.status_code != 200:
        raise TelegramError(f"{method} {resp.status_code}: {resp.text[:300]}")
