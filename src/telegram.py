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


def build_message(item: Item, body: str, limit: int) -> str:
    if len(body) > limit:
        body = body[:limit].rsplit(" ", 1)[0] + "…"

    # وقتی ناشر با منبع فرق دارد هر دو می‌آیند. Hacker News فقط لینک جمع می‌کند
    # و نوشتن «Hacker News» بالای خبری که مال wptv.com است گمراه‌کننده بود.
    head = html.escape(item.publisher or item.label)
    if item.publisher:
        head += f"  ·  via {html.escape(item.label)}"
    return f"<b>{head}</b>\n\n{html.escape(body)}"


def build_keyboard(item: Item) -> dict:
    """دکمه‌ی زیر پست.

    یک دکمه‌ی url، نه callback، پس هیچ backend زنده‌ای نمی‌خواهد و روی همین
    معماری‌ی cron کار می‌کند.

    دکمه‌ی کامنت اینجا نیست: کانال گروه بحث لینک‌شده دارد و خود تلگرام ردیف
    کامنت را زیر هر پست می‌گذارد. ساختن نسخه‌ی خودمان هم شدنی نبود، چون آدرس
    ترد کامنت به شناسه‌ی پیام در گروه نیاز دارد و Bot API موقع ارسال به کانال
    آن را برنمی‌گرداند.
    """
    return {"inline_keyboard": [[{"text": "source", "url": item.url}]]}


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
    """
    keyboard = build_keyboard(item)
    caption = build_message(item, body, MAX_CAPTION)
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
                "text": build_message(item, body, MAX_TEXT),
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
                "reply_markup": keyboard,
            },
            token,
            client,
        )


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
        files={"photo": (name, blob, None)},
        timeout=90,
    )
    if resp.status_code != 200:
        raise TelegramError(f"sendPhoto(upload) {resp.status_code}: {resp.text[:300]}")
    return resp.json().get("result") or {}
