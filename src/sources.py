"""خواندن آیتم‌ها از Bluesky، Hacker News، Lobsters و هر فید RSS."""

import html
import logging
import re
from datetime import datetime, timezone

import feedparser
import httpx
from urllib.parse import urlparse

from .models import Item
from .scoring import weigh

log = logging.getLogger(__name__)

BLUESKY_FEED = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
HN_SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
HN_ITEM = "https://news.ycombinator.com/item?id={id}"
LOBSTERS_FEED = "https://lobste.rs/hottest.json"

UA = {"User-Agent": "Mozilla/5.0 (compatible; persian-tech-tube-bot)"}

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]*\n[ \t]*")
_IMG_SRC = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
# hnrss و فیدهای مشابه هر آیتم را با این سطرها پر می‌کنند؛ برای مدل فقط نویز است.
_BOILERPLATE = re.compile(
    r"^[ \t]*(Article URL|Comments URL|Points|# Comments)\s*:.*$",
    re.MULTILINE,
)


def fetch(source: dict, client: httpx.Client) -> list[Item]:
    kind = source.get("type")
    handler = {
        "bluesky": _fetch_bluesky,
        "hackernews": _fetch_hackernews,
        "lobsters": _fetch_lobsters,
        "rss": _fetch_rss,
    }.get(kind)
    if handler is None:
        raise ValueError(f"نوع منبع ناشناخته: {kind!r}")

    items = handler(source, client)

    # کف تعامل، تا منبع کم‌ترافیک منبع پرترافیک را زمین نزند. امتیاز ویروسی
    # سرعت را نسبت به baseline خودِ منبع می‌سنجد، پس یک پست ۳ امتیازی در جایی
    # که میانه ۱ است «۳ برابر عادی» حساب می‌شود و می‌تواند از یک بحث ۲۰۰ امتیازی
    # HN جلو بزند. این کف جلوی آن را می‌گیرد بدون اینکه نرمال‌سازی را خراب کند.
    floor = source.get("min_engagement", 0)
    if floor:
        items = [item for item in items if item.engagement >= floor]

    return items


# ─────────────────────────────────────────────────────────── منابع با عدد تعامل


def _fetch_bluesky(source: dict, client: httpx.Client) -> list[Item]:
    handle = source["handle"]
    label = source.get("label", handle)

    resp = client.get(
        BLUESKY_FEED,
        params={"actor": handle, "limit": 40, "filter": "posts_no_replies"},
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
        post_url = f"https://bsky.app/profile/{handle}/post/{rkey}"

        # پست‌های لینک‌دار: خبر مال جای دیگری است و خودِ پست فقط اشاره به آن.
        # بدون این، دکمه‌ی منبع به bsky.app می‌رفت و عکس هم از همان‌جا برداشته
        # می‌شد به‌جای کاور مقاله.
        external = (embed_of(post) or {}).get("external") or {}
        link = external.get("uri")
        if link:
            for extra in (external.get("title"), external.get("description")):
                if extra and extra.strip() not in text:
                    text = f"{text}\n\n{extra.strip()}"

        items.append(
            Item(
                uid=uri,
                label=label,
                text=text,
                url=link or post_url,
                discussion_url=post_url if link else None,
                publisher=_publisher(link),
                published=published,
                engagement=weigh(
                    like=post.get("likeCount"),
                    repost=post.get("repostCount"),
                    quote=post.get("quoteCount"),
                    reply=post.get("replyCount"),
                ),
                ranked=True,
                image_url=_bluesky_image(post),
            )
        )
    return items


def embed_of(post: dict) -> dict:
    """embed پست، چه مستقیم باشد چه داخل یک نقل‌قول."""
    embed = post.get("embed") or {}
    return embed.get("media") or embed


def _bluesky_image(post: dict) -> str | None:
    embed = embed_of(post)
    # پست معمولی با عکس.
    for image in embed.get("images") or []:
        url = image.get("fullsize") or image.get("thumb")
        if url:
            return url
    # پست لینک‌دار: thumb همان کاور مقاله است که Bluesky کش کرده. کوچک است، پس
    # فقط وقتی می‌ماند که رفتن سراغ خود مقاله چیزی بهتر ندهد.
    return ((embed.get("external") or {}).get("thumb")) or None


def _fetch_hackernews(source: dict, client: httpx.Client) -> list[Item]:
    """داستان‌های تازه‌ی HN بالای یک آستانه‌ی امتیاز.

    از search_by_date استفاده می‌کنیم نه front page، چون هدف ما گرفتن چیزهایی
    است که *دارند* بالا می‌آیند، نه آن‌هایی که قبلاً بالا آمده‌اند.
    """
    label = source.get("label", "Hacker News")
    min_points = source.get("min_points", 50)

    resp = client.get(
        HN_SEARCH,
        params={
            "tags": "story",
            "numericFilters": f"points>{min_points}",
            "hitsPerPage": 50,
        },
    )
    resp.raise_for_status()

    items = []
    for hit in resp.json().get("hits", []):
        title = (hit.get("title") or "").strip()
        if not title:
            continue

        published = _parse_time(hit.get("created_at"))
        if published is None:
            continue

        discussion = HN_ITEM.format(id=hit["objectID"])
        body = (hit.get("story_text") or "").strip()
        text = f"{title}\n\n{_clean(body)}" if body else title

        items.append(
            Item(
                uid=f"hn:{hit['objectID']}",
                label=label,
                text=text,
                # لینک مقاله‌ی اصلی مهم‌تر است؛ Ask HN لینک ندارد و به بحث می‌رود.
                url=hit.get("url") or discussion,
                published=published,
                engagement=weigh(
                    point=hit.get("points"),
                    comment=hit.get("num_comments"),
                ),
                ranked=True,
                discussion_url=discussion,
                publisher=_publisher(hit.get("url")),
            )
        )
    return items


def _fetch_lobsters(source: dict, client: httpx.Client) -> list[Item]:
    """Lobsters — کوچک‌تر از HN ولی سیگنالش برای امنیت و سیستم تمیزتر است."""
    label = source.get("label", "Lobsters")

    resp = client.get(LOBSTERS_FEED, headers=UA)
    resp.raise_for_status()

    items = []
    for story in resp.json():
        title = (story.get("title") or "").strip()
        published = _parse_time(story.get("created_at"))
        if not title or published is None:
            continue

        tags = ", ".join(story.get("tags") or [])
        text = f"{title}\n\n{tags}" if tags else title

        items.append(
            Item(
                uid=f"lobsters:{story.get('short_id')}",
                label=label,
                text=text,
                url=story.get("url") or story.get("comments_url", ""),
                published=published,
                engagement=weigh(
                    point=story.get("score"),
                    comment=story.get("comment_count"),
                ),
                ranked=True,
                discussion_url=story.get("comments_url"),
                publisher=_publisher(story.get("url")),
            )
        )
    return items


# ───────────────────────────────────────────────────────────────── فیدهای خبری


def _fetch_rss(source: dict, client: httpx.Client) -> list[Item]:
    url = source["url"]
    label = source.get("label", url)
    authority = source.get("authority", 0.5)
    max_age = source.get("max_age_hours")

    resp = client.get(url, headers=UA)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)

    # بعضی فیدها (OpenAI، HuggingFace) هزار آیتم می‌دهند و لزوماً مرتب نیستند.
    entries = sorted(
        feed.entries,
        key=lambda e: _entry_time(e) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[:25]

    items = []
    for entry in entries:
        link = entry.get("link")
        published = _entry_time(entry)
        if not link or published is None:
            continue

        # خلاصه معمولاً وسط جمله بریده می‌شود، پس عنوان را هم می‌دهیم تا مدل
        # زمینه‌ی کامل داشته باشد.
        title = _clean(entry.get("title", ""))
        summary = _clean(entry.get("summary", ""))
        if summary.startswith(title):
            text = summary
        else:
            text = "\n\n".join(part for part in (title, summary) if part)
        if not text:
            continue

        items.append(
            Item(
                uid=entry.get("id") or link,
                label=label,
                text=text,
                url=link,
                published=published,
                authority=authority,
                max_age_hours=max_age,
                image_url=_rss_image(entry),
            )
        )
    return items


def _rss_image(entry) -> str | None:
    """عکس را از تگ‌های استاندارد فید درمی‌آورد."""
    for media in entry.get("media_content", []) or []:
        if media.get("url") and str(media.get("medium", "image")) == "image":
            return media["url"]

    for thumb in entry.get("media_thumbnail", []) or []:
        if thumb.get("url"):
            return thumb["url"]

    for link in entry.get("links", []) or []:
        if link.get("rel") == "enclosure" and str(link.get("type", "")).startswith("image/"):
            return link.get("href")

    # بعضی فیدها عکس را فقط داخل HTML خلاصه می‌گذارند.
    match = _IMG_SRC.search(entry.get("summary", ""))
    return match.group(1) if match else None


# ────────────────────────────────────────────────────────────────────── کمکی‌ها


def _clean(raw: str) -> str:
    text = html.unescape(_TAG.sub(" ", raw))
    # اول خطوط را مرتب کن، بعد boilerplate را بردار — الگو به ابتدای خط تکیه دارد.
    text = _WS.sub("\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = _BOILERPLATE.sub("", text)
    return re.sub(r"\n{2,}", "\n", text).strip()


def _publisher(url: str | None) -> str | None:
    """دامنه‌ی ناشر واقعی، بدون www. برای پست‌های خودِ سایت (Ask HN) None."""
    if not url:
        return None
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return host or None


def _entry_time(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    return _parse_time(entry.get("published") or entry.get("updated"))


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        log.warning("تاریخ قابل خواندن نبود: %r", value)
        return None
