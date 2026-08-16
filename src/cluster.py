"""خوشه‌بندی خبرها، برای تشخیص اینکه کدام ماجرا واقعاً مهم است.

مسئله این بود: فیدهای خبری هیچ عدد تعاملی ندارند، پس تنها سیگنال اهمیتشان
`authority` بود که دستی تعیین شده. بات می‌دانست خبر از کجا آمده، ولی نمی‌دانست
مهم است یا نه.

قوی‌ترین سیگنال رایگانِ اهمیت، پوشش هم‌زمان چند رسانه‌ی مستقل است. خبری که
ظرف سه ساعت در هفت سایت بیاید مهم است؛ خبری که فقط یک سایت زده، نه. و اگر آن
هفت سایت خیلی سریع رویش پریده باشند، ماجرا جنجالی است. Techmeme و Google News
هم دقیقاً همین کار را می‌کنند.

خوشه‌بندی بین‌زبانی هم کار می‌کند: مقاله‌ی روسی درباره‌ی همان خبر، کلمات روسی
دارد ولی اسم‌های خاص را لاتین نگه می‌دارد — OpenAI، GPT، Cloudflare، شماره‌ی
CVE. امضای خوشه از همین توکن‌های لاتین و عددها ساخته می‌شود، پس Habr و The
Verge وقتی یک ماجرا را پوشش می‌دهند در یک خوشه می‌افتند.
"""

import logging
import re
from dataclasses import dataclass, field

from .models import Item

log = logging.getLogger(__name__)

# شباهت لازم برای اینکه دو خبر یک ماجرا حساب شوند. پایین‌تر از این، خبرهای
# نامرتبطِ هم‌موضوع به هم می‌چسبند؛ بالاتر، نسخه‌های مختلف یک خبر جدا می‌مانند.
SIMILARITY = 0.42

# امضا از توکن‌های معنادار ساخته می‌شود و اینها معنا ندارند.
STOP = {
    "the", "and", "for", "with", "from", "that", "this", "have", "has", "was",
    "are", "will", "can", "its", "his", "her", "you", "your", "our", "not",
    "but", "all", "new", "now", "how", "why", "what", "who", "when", "after",
    "before", "into", "over", "than", "then", "they", "them", "their", "been",
    "more", "most", "some", "such", "only", "just", "also", "make", "makes",
    "made", "says", "said", "say", "get", "gets", "got", "use", "uses", "used",
    "one", "two", "out", "off", "about", "against", "between", "during",
}

_TOKEN = re.compile(r"[a-z0-9][a-z0-9\-.]{2,}")


@dataclass
class Cluster:
    """یک ماجرا، و همه‌ی خبرهایی که رویش نوشته‌اند."""

    signature: set[str]
    items: list[Item] = field(default_factory=list)

    @property
    def sources(self) -> int:
        """تعداد رسانه‌های مستقل. همان سیگنال اهمیت است."""
        return len({item.label for item in self.items})

    @property
    def authority(self) -> float:
        values = [item.authority for item in self.items]
        return sum(values) / len(values) if values else 0.5

    @property
    def engagement(self) -> int:
        return max((item.engagement for item in self.items), default=0)

    def broke(self):
        """لحظه‌ای که ماجرا شکست: قدیمی‌ترین عضو."""
        return min(item.published for item in self.items)

    def latest(self):
        """آخرین باری که رسانه‌ای رویش نوشت.

        زوال زمانی باید بر اساس این باشد نه لحظه‌ی شکستن. ماجرایی که هنوز
        رسانه‌ها دارند رویش می‌نویسند زنده است، حتی اگر دیروز شروع شده باشد.
        """
        return max(item.published for item in self.items)

    def lead(self) -> Item:
        """نماینده‌ی خوشه، همان چیزی که پست می‌شود.

        مقاله‌ای که عکس دارد و از منبع معتبرتری است بهتر جواب می‌دهد؛ بین
        مساوی‌ها، آنکه تعامل بیشتری دارد.
        """
        return max(
            self.items,
            key=lambda i: (bool(i.image_url), i.authority, i.engagement),
        )


def build(items: list[Item]) -> list[Cluster]:
    """آیتم‌ها را به ماجراها گروه می‌کند.

    مقایسه‌ی همه با همه است ولی هر اجرا چند صد آیتم بیشتر نداریم، پس ارزان است.
    """
    clusters: list[Cluster] = []
    by_url: dict[str, Cluster] = {}

    for item in items:
        key = _url_key(item.url)
        found = by_url.get(key)

        if found is None:
            signature = _signature(item.text)
            found = _closest(signature, clusters)
            if found is None:
                found = Cluster(signature=signature)
                clusters.append(found)
            else:
                found.signature |= signature

        found.items.append(item)
        by_url.setdefault(key, found)

    multi = sum(1 for c in clusters if c.sources > 1)
    log.info("%d ماجرا از %d خبر، %d تایشان چندمنبعی", len(clusters), len(items), multi)
    return clusters


def _closest(signature: set[str], clusters: list[Cluster]) -> Cluster | None:
    best, best_score = None, 0.0
    for cluster in clusters:
        score = _similarity(signature, cluster.signature)
        if score > best_score:
            best, best_score = cluster, score
    return best if best_score >= SIMILARITY else None


def _similarity(left: set[str], right: set[str]) -> float:
    """اشتراک نسبت به کوچک‌ترین مجموعه، نه Jaccard.

    یک تیتر کوتاه و یک متن بلند درباره‌ی یک خبر، Jaccard پایینی می‌گیرند چون
    مخرج بزرگ می‌شود. آنچه اهمیت دارد این است که *همه‌ی* کلمات کلیدی تیتر در
    آن یکی هم باشند.
    """
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def _signature(text: str) -> set[str]:
    """توکن‌های لاتین و عددی از دو خط اول متن.

    فقط لاتین، چون همین باعث می‌شود خبر روسی و انگلیسیِ یک ماجرا به هم برسند:
    اسم شرکت و محصول و شماره‌ی CVE در هر دو زبان لاتین می‌ماند.
    """
    head = " ".join(text.splitlines()[:2]).lower()
    tokens = {t for t in _TOKEN.findall(head) if t not in STOP and not t.isdigit()}
    # کمتر از سه توکن یعنی امضا آن‌قدر ضعیف است که هر چیزی به آن می‌چسبد.
    return tokens if len(tokens) >= 3 else set()


def _url_key(url: str) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/").removesuffix("/amp").lower()
    return f"{host}{path}"
