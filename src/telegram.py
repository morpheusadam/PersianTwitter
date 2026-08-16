"""ارسال پست به کانال از طریق Bot API."""

import html
import json
import logging
from pathlib import Path

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


def build_keyboard(item: Item, comment_url: str | None = None) -> dict:
    """دکمه‌های زیر پست.

    هر دو از نوع url هستند، نه callback. یعنی هیچ backend زنده‌ای نمی‌خواهند و
    روی همین معماری‌ی cron کار می‌کنند. دکمه‌ی callback به handler نیاز داشت.
    """
    row = [{"text": "منبع", "url": item.url}]
    if comment_url:
        row.append({"text": "comment", "url": comment_url})
    return {"inline_keyboard": [row]}


def comment_link(channel: str, message_id: int) -> str:
    """لینک بخش کامنت همان پست.

    کانال یک گروه بحث لینک‌شده دارد، پس هر پست یک ترد کامنت دارد و این آدرس
    مستقیم بازش می‌کند. message_id فقط بعد از ارسال معلوم می‌شود، برای همین
    دکمه در مرحله‌ی دوم با editMessageReplyMarkup اضافه می‌شود.
    """
    return f"https://t.me/{channel.lstrip('@')}/{message_id}?comment=1"


def publish(
    item: Item,
    body: str,
    token: str,
    channel: str,
    client: httpx.Client,
    fallback: Path | None = None,
) -> None:
    """هر پست با عکس می‌رود.

    اگر تصویری پیدا شده باشد تلگرام خودش از URL برمی‌داردش، وگرنه فایل پیش‌فرض
    آپلود می‌شود. فقط وقتی به پیام متنی برمی‌گردیم که هر دو شکست بخورند، چون
    خبر بدون عکس بهتر از هیچ خبر است.

    دکمه‌ی comment بعد از ارسال اضافه می‌شود، چون آدرس ترد کامنت به message_id
    نیاز دارد و آن فقط در پاسخ همان ارسال برمی‌گردد.
    """
    keyboard = build_keyboard(item)
    caption = build_message(item.label, body, MAX_CAPTION)
    result = None

    if item.image_url:
        try:
            result = _call(
                "sendPhoto",
                {
                    "chat_id": channel,
                    "photo": item.image_url,
                    "caption": caption,
                    "parse_mode": "HTML",
                    "reply_markup": keyboard,
                },
                token,
                client,
            )
        except TelegramError as exc:
            log.warning("عکس از URL نرفت: %s", exc)

    if result is None and fallback and fallback.exists():
        try:
            result = _upload(
                (fallback.name, fallback.read_bytes()), channel, caption, keyboard, token, client
            )
        except TelegramError as exc:
            log.warning("عکس پیش‌فرض هم نرفت: %s", exc)

    if result is None:
        result = _call(
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

    _attach_comment(item, result, channel, token, client)


def _attach_comment(item, result, channel, token, client) -> None:
    """دکمه‌ی comment را روی پستِ تازه‌ارسال‌شده می‌نشاند."""
    message_id = (result or {}).get("message_id")
    if not message_id:
        return
    try:
        _call(
            "editMessageReplyMarkup",
            {
                "chat_id": channel,
                "message_id": message_id,
                "reply_markup": build_keyboard(item, comment_link(channel, message_id)),
            },
            token,
            client,
        )
    except TelegramError as exc:
        # پست رفته و سالم است؛ فقط یک دکمه کم دارد.
        log.warning("دکمه‌ی comment اضافه نشد: %s", exc)


def _call(method: str, payload: dict, token: str, client: httpx.Client) -> dict:
    resp = client.post(API.format(token=token, method=method), json=payload, timeout=60)
    if resp.status_code != 200:
        raise TelegramError(f"{method} {resp.status_code}: {resp.text[:300]}")
    result = resp.json().get("result")
    return result if isinstance(result, dict) else {}


def _upload(
    photo: tuple[str, bytes],
    channel: str,
    caption: str,
    keyboard: dict,
    token: str,
    client: httpx.Client,
) -> dict:
    """عکس را multipart آپلود می‌کند. در multipart همه‌ی فیلدها رشته‌اند."""
    name, blob = photo
    resp = client.post(
        API.format(token=token, method="sendPhoto"),
        data={
            "chat_id": channel,
            "caption": caption,
            "parse_mode": "HTML",
            "reply_markup": json.dumps(keyboard),
        },
        files={"photo": (name, blob, "image/jpeg")},
        timeout=90,
    )
    if resp.status_code != 200:
        raise TelegramError(f"sendPhoto(upload) {resp.status_code}: {resp.text[:300]}")
    return resp.json().get("result") or {}
