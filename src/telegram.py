"""ارسال پست به کانال از طریق Bot API."""

import html
import logging

import httpx

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"

# سقف تلگرام برای پیام متنی ۴۰۹۶ است و برای caption عکس فقط ۱۰۲۴.
MAX_TEXT = 3500
MAX_CAPTION = 900


class TelegramError(RuntimeError):
    pass


def build_message(label: str, body: str, url: str, limit: int) -> str:
    if len(body) > limit:
        body = body[:limit].rsplit(" ", 1)[0] + "…"

    return (
        f"<b>{html.escape(label)}</b>\n\n"
        f"{html.escape(body)}\n\n"
        f'<a href="{html.escape(url, quote=True)}">🔗 منبع</a>'
    )


def publish(
    label: str,
    body: str,
    url: str,
    image_url: str | None,
    token: str,
    channel: str,
    client: httpx.Client,
) -> None:
    """اگر عکس باشد با عکس می‌فرستد، وگرنه متنی.

    تلگرام خودش عکس را از URL برمی‌دارد، پس فایلی دانلود نمی‌کنیم. ولی گاهی به
    عکس نمی‌رسد یا فرمتش را نمی‌پذیرد — در آن حالت به پیام متنی برمی‌گردیم تا
    خبر به‌خاطر یک عکس از دست نرود.
    """
    if image_url:
        caption = build_message(label, body, url, MAX_CAPTION)
        try:
            _call(
                "sendPhoto",
                {
                    "chat_id": channel,
                    "photo": image_url,
                    "caption": caption,
                    "parse_mode": "HTML",
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
            "text": build_message(label, body, url, MAX_TEXT),
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        },
        token,
        client,
    )


def _call(method: str, payload: dict, token: str, client: httpx.Client) -> None:
    resp = client.post(API.format(token=token, method=method), json=payload, timeout=60)
    if resp.status_code != 200:
        raise TelegramError(f"{method} {resp.status_code}: {resp.text[:300]}")
