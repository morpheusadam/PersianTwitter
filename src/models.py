from dataclasses import dataclass
from datetime import datetime


@dataclass
class Item:
    """یک آیتم از هر منبعی، بعد از نرمال‌سازی."""

    uid: str  # کلید یکتا برای dedup
    label: str  # نام منبع، همان چیزی که در config آمده
    text: str  # متن اصلی، انگلیسی
    url: str
    published: datetime  # همیشه timezone-aware
