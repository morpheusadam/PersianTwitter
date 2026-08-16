from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Item:
    """یک آیتم از هر منبعی، بعد از نرمال‌سازی."""

    uid: str  # کلید یکتا برای dedup
    label: str  # نام منبع، همان چیزی که در config آمده
    text: str  # متن اصلی، انگلیسی
    url: str
    published: datetime  # همیشه timezone-aware

    # منابعی مثل Bluesky و HN عدد تعامل می‌دهند و امتیاز ویروسی می‌گیرند.
    # فیدهای خبری هیچ عددی ندارند و در استخر editorial رتبه‌بندی می‌شوند.
    engagement: int = 0
    ranked: bool = False

    # اعتبار منبع، فقط برای استخر editorial. از config می‌آید.
    authority: float = 0.5

    image_url: str | None = None
    # صفحه‌ی بحث، جدا از خود مقاله. HN و Lobsters هر دو را دارند.
    discussion_url: str | None = None
    score: float = 0.0
    breakdown: dict = field(default_factory=dict)

    def age_hours(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return max((now - self.published).total_seconds() / 3600, 0.05)
