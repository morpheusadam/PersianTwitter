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
    fallback: Path | None = None,
    cover_jpeg: bytes | None = None,
) -> None:
    """هر پست با عکس می‌رود.

    اگر تصویری پیدا شده باشد تلگرام خودش از URL برمی‌داردش. اگر نه، فایل
    پیش‌فرض را آپلود می‌کنیم. فقط وقتی به پیام متنی برمی‌گردیم که هر دو شکست
    بخورند، چون خبر بدون عکس بهتر از هیچ خبر است.
    """
    keyboard = build_keyboard(item)
    caption = build_message(item.label, body, MAX_CAPTION)

    # کاور شیشه‌ای از قبل ساخته شده و بایت است، پس مستقیم آپلود می‌شود.
    if cover_jpeg:
        try:
            _upload(("cover.jpg", cover_jpeg), channel, caption, keyboard, token, client)
            return
        except TelegramError as exc:
            log.warning("کاور نرفت: %s", exc)

    if item.image_url:
        try:
            _call(
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
            return
        except TelegramError as exc:
            log.warning("عکس از URL نرفت: %s", exc)

    if fallback and fallback.exists():
        try:
            _upload((fallback.name, fallback.read_bytes()), channel, caption, keyboard, token, client)
            return
        except TelegramError as exc:
            log.warning("عکس پیش‌فرض هم نرفت: %s", exc)

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


def _upload(
    photo: tuple[str, bytes],
    channel: str,
    caption: str,
    keyboard: dict,
    token: str,
    client: httpx.Client,
) -> None:
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
