"""
Günlük Gaming + Yapay Zeka haber botu (Telegram)

- RSS kaynaklarından son 24 saatin haberlerini toplar (ücretsiz, API key gerekmez)
- İsteğe bağlı: LLM_API_KEY verirsen haberleri Türkçe özetletir
  (DeepSeek gibi OpenAI uyumlu herhangi bir API olur)
- Telegram'a gönderir

Gerekli ortam değişkenleri:
  TELEGRAM_BOT_TOKEN   BotFather'dan aldığın token
  TELEGRAM_CHAT_ID     Mesajın gideceği sohbet ID'si
İsteğe bağlı:
  LLM_API_KEY, LLM_BASE_URL (varsayılan: https://api.deepseek.com), LLM_MODEL (varsayılan: deepseek-chat)
"""

import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import feedparser
import requests

# ----------------------------- AYARLAR -----------------------------

FEEDS = {
    "🤖 Yapay Zeka": [
        "https://techcrunch.com/category/artificial-intelligence/feed/",
        "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
        "https://venturebeat.com/category/ai/feed/",
        "https://www.marktechpost.com/feed/",
        "https://huggingface.co/blog/feed.xml",
        "https://openai.com/news/rss.xml",
        "https://www.reddit.com/r/LocalLLaMA/top/.rss?t=day",
        "https://www.reddit.com/r/singularity/top/.rss?t=day",
    ],
    "🎮 Gaming": [
        "https://www.gamespot.com/feeds/news/",
        "https://www.pcgamer.com/rss/",
        "https://www.eurogamer.net/feed",
        "https://www.rockpapershotgun.com/feed",
        "https://www.polygon.com/rss/index.xml",
        "https://kotaku.com/rss",
        "https://www.reddit.com/r/Games/top/.rss?t=day",
        "https://www.reddit.com/r/pcgaming/top/.rss?t=day",
    ],
}

HOURS = 24               # Son kaç saatin haberleri
PER_FEED = 3             # Her kaynaktan en fazla kaç haber
MAX_PER_CATEGORY = 12    # Kategori başına toplam üst sınır

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
LLM_API_KEY = os.environ.get("LLM_API_KEY")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DailyNewsBot/1.0)"}

# ----------------------------- HABER TOPLAMA -----------------------------


def clean_text(text: str, limit: int = 300) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return text[:limit]


def entry_date(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def fetch_feed(url: str):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except Exception as exc:
        print(f"[uyarı] {url} alınamadı: {exc}", file=sys.stderr)
        return []

    source = clean_text(parsed.feed.get("title", ""), 40) or urlparse(url).netloc
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS)
    items = []
    for e in parsed.entries:
        date = entry_date(e)
        if date and date < cutoff:
            continue
        title = clean_text(e.get("title", ""), 200)
        link = e.get("link")
        if not title or not link:
            continue
        items.append(
            {
                "title": title,
                "link": link,
                "summary": clean_text(e.get("summary", "")),
                "source": source,
                "date": date or datetime.now(timezone.utc),
            }
        )
    items.sort(key=lambda x: x["date"], reverse=True)
    return items[:PER_FEED]


def collect(urls):
    seen_links, seen_titles, items = set(), set(), []
    for url in urls:
        for item in fetch_feed(url):
            key = re.sub(r"\W+", "", item["title"].lower())
            if item["link"] in seen_links or key in seen_titles:
                continue
            seen_links.add(item["link"])
            seen_titles.add(key)
            items.append(item)
    items.sort(key=lambda x: x["date"], reverse=True)
    return items[:MAX_PER_CATEGORY]


# ----------------------------- TÜRKÇE ÖZET (İSTEĞE BAĞLI) -----------------------------


def translate_items(items):
    """Her habere kısa bir Türkçe özet ekler. Hata olursa orijinal başlıklarla devam eder."""
    if not LLM_API_KEY or not items:
        return
    payload = [{"i": i, "title": it["title"], "summary": it["summary"]} for i, it in enumerate(items)]
    prompt = (
        "Aşağıdaki haberlerin her biri için tek cümlelik, akıcı bir Türkçe özet yaz. "
        "Sadece JSON dizisi döndür, başka hiçbir şey yazma. "
        'Biçim: [{"i": 0, "tr": "..."}, ...]\n\n' + json.dumps(payload, ensure_ascii=False)
    )
    try:
        resp = requests.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            json={"model": LLM_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3},
            timeout=90,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.M).strip()
        for row in json.loads(content):
            items[row["i"]]["tr"] = clean_text(row["tr"], 300)
    except Exception as exc:
        print(f"[uyarı] Türkçe özet alınamadı, orijinal başlıklar kullanılacak: {exc}", file=sys.stderr)


# ----------------------------- TELEGRAM -----------------------------


def build_message(sections):
    today = datetime.now().strftime("%d.%m.%Y")
    lines = [f"📰 <b>Günlük Haberler</b> — {today}", ""]
    for category, items in sections.items():
        lines.append(f"<b>{category}</b>")
        if not items:
            lines.append("Bugün yeni haber bulunamadı.")
        for it in items:
            text = html.escape(it.get("tr") or it["title"])
            link = html.escape(it["link"], quote=True)
            source = html.escape(it["source"])
            lines.append(f'• <a href="{link}">{text}</a> <i>({source})</i>')
        lines.append("")
    return "\n".join(lines).strip()


def send_telegram(text: str):
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > 3800:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current)

    for chunk in chunks:
        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        if not resp.ok:
            raise RuntimeError(f"Telegram hatası: {resp.status_code} {resp.text}")


def main():
    if not BOT_TOKEN or not CHAT_ID:
        sys.exit("TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID ortam değişkenleri gerekli.")

    sections = {}
    for category, urls in FEEDS.items():
        items = collect(urls)
        translate_items(items)
        sections[category] = items

    send_telegram(build_message(sections))
    print("Gönderildi.")


if __name__ == "__main__":
    main()
