"""خواندن آیتم‌ها از Bluesky، Hacker News، Lobsters و هر فید RSS."""

import html
import logging
import re
from datetime import datetime, timezone

import feedparser
import httpx

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
_OG_IMAGE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']'
    r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image["\']',
    re.IGNORECASE,
)

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
        items.append(
            Item(
                uid=uri,
                label=label,
                text=text,
                url=f"https://bsky.app/profile/{handle}/post/{rkey}",
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


def _bluesky_image(post: dict) -> str | None:
    embed = post.get("embed") or {}
    # پست معمولی با عکس، یا نقل‌قولی که عکس را در media گذاشته.
    images = embed.get("images") or (embed.get("media") or {}).get("images") or []
    for image in images:
        url = image.get("fullsize") or image.get("thumb")
        if url:
            return url
    return None


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
            )
        )
    return items


# ───────────────────────────────────────────────────────────────── فیدهای خبری


def _fetch_rss(source: dict, client: httpx.Client) -> list[Item]:
    url = source["url"]
    label = source.get("label", url)
    authority = source.get("authority", 0.5)

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


def fetch_og_image(url: str, client: httpx.Client) -> str | None:
    """og:image صفحه‌ی مقاله — برای فیدهایی که خودشان عکس نمی‌دهند.

    فقط برای آیتم‌هایی صدا زده می‌شود که واقعاً قرار است پست شوند، وگرنه هر اجرا
    ده‌ها درخواست اضافه می‌شد.
    """
    try:
        resp = client.get(url, headers=UA, timeout=15)
        resp.raise_for_status()
        # صفحه‌های خبری سنگین‌اند و og:image همیشه در head است.
        match = _OG_IMAGE.search(resp.text[:200_000])
    except Exception as exc:
        log.debug("og:image خوانده نشد (%s): %s", url, exc)
        return None

    if not match:
        return None
    found = match.group(1) or match.group(2)
    return found if found and found.startswith("http") else None


# ────────────────────────────────────────────────────────────────────── کمکی‌ها


def _clean(raw: str) -> str:
    text = html.unescape(_TAG.sub(" ", raw))
    # اول خطوط را مرتب کن، بعد boilerplate را بردار — الگو به ابتدای خط تکیه دارد.
    text = _WS.sub("\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = _BOILERPLATE.sub("", text)
    return re.sub(r"\n{2,}", "\n", text).strip()


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
