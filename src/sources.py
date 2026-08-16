"""خواندن آیتم‌ها از Bluesky و RSS."""

import html
import logging
import re
from datetime import datetime, timezone

import feedparser
import httpx

from .models import Item

log = logging.getLogger(__name__)

BLUESKY_FEED = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]*\n[ \t]*")


# hnrss هر آیتم را با این سطرهای تکراری پر می‌کند؛ برای مدل فقط نویز است.
_BOILERPLATE = re.compile(
    r"^[ \t]*(Article URL|Comments URL|Points|# Comments)\s*:.*$",
    re.MULTILINE,
)


def _clean(raw: str) -> str:
    text = html.unescape(_TAG.sub(" ", raw))
    # اول خطوط را مرتب کن، بعد boilerplate را بردار — الگو به ابتدای خط تکیه دارد.
    text = _WS.sub("\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = _BOILERPLATE.sub("", text)
    return re.sub(r"\n{2,}", "\n", text).strip()


def fetch(source: dict, client: httpx.Client) -> list[Item]:
    kind = source.get("type")
    if kind == "bluesky":
        return _fetch_bluesky(source, client)
    if kind == "rss":
        return _fetch_rss(source, client)
    raise ValueError(f"نوع منبع ناشناخته: {kind!r}")


def _fetch_bluesky(source: dict, client: httpx.Client) -> list[Item]:
    handle = source["handle"]
    label = source.get("label", handle)

    resp = client.get(
        BLUESKY_FEED,
        params={"actor": handle, "limit": 30, "filter": "posts_no_replies"},
    )
    resp.raise_for_status()

    items = []
    for entry in resp.json().get("feed", []):
        # ریپوست‌ها متن خودشان را ندارند، ردشان کن.
        if entry.get("reason"):
            continue

        post = entry.get("post", {})
        record = post.get("record", {})
        text = (record.get("text") or "").strip()
        uri = post.get("uri") or ""
        if not text or not uri:
            continue

        published = _parse_time(record.get("createdAt") or post.get("indexedAt"))
        if published is None:
            continue

        rkey = uri.rsplit("/", 1)[-1]
        items.append(
            Item(
                uid=uri,
                label=label,
                text=text,
                url=f"https://bsky.app/profile/{handle}/post/{rkey}",
                published=published,
            )
        )
    return items


def _fetch_rss(source: dict, client: httpx.Client) -> list[Item]:
    url = source["url"]
    label = source.get("label", url)

    resp = client.get(url, headers={"User-Agent": "Mozilla/5.0 (persian-tech-tube-bot)"})
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)

    items = []
    for entry in feed.entries[:30]:
        link = entry.get("link")
        if not link:
            continue

        # فیدهای خبری عنوان و خلاصه را جدا می‌دهند و خلاصه معمولاً وسط جمله بریده
        # می‌شود؛ هر دو را می‌دهیم تا مدل زمینه‌ی کامل داشته باشد. xcancel کل متن
        # توییت را در title می‌گذارد و summary اش تکراری است.
        title = _clean(entry.get("title", ""))
        summary = _clean(entry.get("summary", ""))
        if summary.startswith(title):
            text = summary
        else:
            text = "\n\n".join(part for part in (title, summary) if part)
        if not text:
            continue

        published = _parse_struct_time(entry) or _parse_time(entry.get("published"))
        if published is None:
            continue

        items.append(
            Item(
                uid=entry.get("id") or link,
                label=label,
                text=text,
                url=link,
                published=published,
            )
        )
    return items


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # fromisoformat در 3.11+ با Z هم کنار می‌آید، ولی محض احتیاط.
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        log.warning("تاریخ قابل خواندن نبود: %r", value)
        return None


def _parse_struct_time(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=timezone.utc)
