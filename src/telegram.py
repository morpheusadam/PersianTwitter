"""ارسال پست به کانال از طریق Bot API."""

import html
import logging

import httpx

from .models import Item

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(RuntimeError):
    pass


# سقف پیام متنی ۴۰۹۶ است. خلاصه‌ی ۲۵۰ کلمه‌ای فارسی حدود ۱۵۰۰ کاراکتر می‌شود،
# پس جا هست. caption عکس فقط ۱۰۲۴ می‌گیرد و همین باعث شد از sendPhoto به
# sendMessage با link preview بزرگ برویم: هم عکس بالای متن می‌آید هم متن کامل جا
# می‌شود.
MAX_TEXT = 3500

# عکس پیش‌فرض از خودِ repo عمومی سرو می‌شود تا مثل بقیه یک URL داشته باشد و در
# همان link preview بنشیند.
FALLBACK_URL = (
    "https://raw.githubusercontent.com/morpheusadam/persian-tech-tube-bot"
    "/main/assets/fallback.png"
)


def build_message(
    item: Item,
    title: str,
    body: str,
    tag: str = "",
    channel: str = "",
) -> str:
    """تیتر، متن، خط منبع، و ردیف هشتگ و امضا."""
    source = html.escape(item.publisher or item.label)
    if item.publisher and item.publisher != item.label:
        source += f"  ·  via {html.escape(item.label)}"

    footer = "  ".join(part for part in (tag, _handle(channel)) if part)
    fixed = len(source) + len(footer) + len(title) + 12
    body = body.strip()
    if len(body) > MAX_TEXT - fixed:
        body = body[: MAX_TEXT - fixed].rsplit(" ", 1)[0] + "…"

    parts = []
    if title:
        parts.append(f"<b>{html.escape(title)}</b>")
    parts.append(html.escape(body))
    parts.append(f"<i>{source}</i>")
    if footer:
        parts.append(html.escape(footer))
    return "\n\n".join(parts)


def _handle(channel: str) -> str:
    channel = (channel or "").strip()
    return channel if channel.startswith("@") else ""


def build_keyboard(item: Item) -> dict:
    """دکمه‌ی زیر پست.

    یک دکمه‌ی url، نه callback، پس هیچ backend زنده‌ای نمی‌خواهد و روی همین
    معماری‌ی cron کار می‌کند.

    دکمه‌ی کامنت اینجا نیست: کانال گروه بحث لینک‌شده دارد و خود تلگرام ردیف
    کامنت را زیر هر پست می‌گذارد.
    """
    return {"inline_keyboard": [[{"text": "source", "url": item.url}]]}


def publish(
    item: Item,
    title: str,
    body: str,
    token: str,
    channel: str,
    client: httpx.Client,
    tag: str = "",
) -> None:
    """پست را با عکس بزرگ بالای متن می‌فرستد.

    از sendPhoto استفاده نمی‌کنیم چون caption اش به ۱۰۲۴ کاراکتر محدود است و
    خلاصه‌های ما بلندترند. link preview همان عکس را بزرگ نشان می‌دهد بدون آن
    محدودیت.
    """
    text = build_message(item, title, body, tag, channel)
    preview = {
        "url": item.image_url or FALLBACK_URL,
        "prefer_large_media": True,
        "show_above_text": True,
    }

    try:
        _call(
            "sendMessage",
            {
                "chat_id": channel,
                "text": text,
                "parse_mode": "HTML",
                "link_preview_options": preview,
                "reply_markup": build_keyboard(item),
            },
            token,
            client,
        )
        return
    except TelegramError as exc:
        log.warning("ارسال با عکس نشد، بدون عکس تلاش می‌کنم: %s", exc)

    _call(
        "sendMessage",
        {
            "chat_id": channel,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
            "reply_markup": build_keyboard(item),
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


def send_digest(text: str, token: str, channel: str, client: httpx.Client) -> None:
    """پست خلاصه‌ی هفتگی. متنی است چون چند لینک دارد، نه یک خبر واحد."""
    _call(
        "sendMessage",
        {
            "chat_id": channel,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        },
        token,
        client,
    )
