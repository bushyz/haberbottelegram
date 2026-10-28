"""
Günlük Gaming + Yapay Zeka haber botu (Telegram) - v2

- RSS kaynaklarından son 24 saatin haberlerini toplar
- LLM_API_KEY varsa her habere Türkçe başlık + kısa açıklama yazar
  (yapay zeka haberleri teknik olmayan biri için sadeleştirilir)
- LLM_API_KEY yoksa orijinal başlık + kaynaktaki kısa özet gönderilir
- Telegram'a gönderir

Gerekli ortam değişkenleri:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Önerilen:
  LLM_API_KEY  (Google AI Studio'dan ücretsiz Gemini anahtarı olur)
İsteğe bağlı:
  LLM_BASE_URL (varsayılan: Gemini), LLM_MODEL (varsayılan: gemini-2.5-flash)
"""

import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import feedparser
import requests

# ----------------------------- AYARLAR -----------------------------

AI = "🤖 Yapay Zeka"
GAMING = "🎮 Gaming"

FEEDS = {
    AI: [
        "https://techcrunch.com/category/artificial-intelligence/feed/",
        "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
        "https://venturebeat.com/category/ai/feed/",
        "https://www.marktechpost.com/feed/",
        "https://huggingface.co/blog/feed.xml",
        "https://openai.com/news/rss.xml",
        "https://www.reddit.com/r/LocalLLaMA/top/.rss?t=day",
        "https://www.reddit.com/r/singularity/top/.rss?t=day",
    ],
    GAMING: [
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

# Kategoriye göre LLM'e verilen yazım tarzı
STYLE = {
    AI: (
        "Okuyucu yapay zeka konusunda teknik biri DEĞİL. Açıklamayı çok basit, günlük bir Türkçeyle yaz. "
        "Teknik terim kullanma; kullanman gerekirse parantez içinde bir cümleyle açıkla "
        "(ör. 'açık kaynak: kodu herkese açık'). Haberin 'bana ne faydası var / neden önemli' kısmını "
        "mümkünse vurgula. Açıklama 1-2 kısa cümle olsun."
    ),
    GAMING: (
        "Açıklamayı akıcı, samimi bir Türkçeyle yaz. Haberin ne hakkında olduğunu 1-2 kısa cümlede anlat. "
        "Oyun, stüdyo ve platform adlarını orijinal haliyle bırak."
    ),
}

HOURS = 24               # Son kaç saatin haberleri
PER_FEED = 3             # Her kaynaktan en fazla kaç haber
MAX_PER_CATEGORY = 10    # Kategori başına toplam üst sınır

SOURCE_NAMES = {
    "techcrunch.com": "TechCrunch",
    "theverge.com": "The Verge",
    "venturebeat.com": "VentureBeat",
    "marktechpost.com": "MarkTechPost",
    "huggingface.co": "Hugging Face",
    "openai.com": "OpenAI",
    "gamespot.com": "GameSpot",
    "pcgamer.com": "PC Gamer",
    "eurogamer.net": "Eurogamer",
    "rockpapershotgun.com": "Rock Paper Shotgun",
    "polygon.com": "Polygon",
    "kotaku.com": "Kotaku",
}

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
LLM_API_KEY = os.environ.get("LLM_API_KEY")
# "or" kullanıyoruz: GitHub'da secret tanımlı değilse boş string gelir, o zaman varsayılan kullanılsın
LLM_BASE_URL = (os.environ.get("LLM_BASE_URL") or "https://generativelanguage.googleapis.com/v1beta/openai").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL") or "gemini-2.5-flash"

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


def source_name(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    if "reddit.com" in host:
        match = re.search(r"/r/([^/]+)/", parsed.path)
        return f"Reddit r/{match.group(1)}" if match else "Reddit"
    return SOURCE_NAMES.get(host, host)


def fetch_feed(url: str):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except Exception as exc:
        print(f"[uyarı] {url} alınamadı: {exc}", file=sys.stderr)
        return []

    source = source_name(url)
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


# ----------------------------- TÜRKÇE BAŞLIK + AÇIKLAMA (LLM) -----------------------------


def enrich_items(category, items):
    """Her habere Türkçe başlık ve kısa açıklama ekler. Hata olursa sessizce orijinal haliyle devam eder."""
    if not LLM_API_KEY or not items:
        return
    payload = [{"i": i, "title": it["title"], "summary": it["summary"]} for i, it in enumerate(items)]
    prompt = (
        "Sen bir Türkçe haber editörüsün. Aşağıdaki haberlerin her biri için şunları yaz:\n"
        '- "baslik": Haberin doğal, kısa Türkçe başlığı.\n'
        '- "aciklama": Haberin ne hakkında olduğunu anlatan kısa açıklama.\n\n'
        f"Yazım tarzı: {STYLE[category]}\n\n"
        "Sadece verilen bilgilere dayan, bilgi uydurma. Emin olmadığın bir şeyi yazma. "
        "Sadece JSON dizisi döndür, başka hiçbir şey yazma. "
        'Biçim: [{"i": 0, "baslik": "...", "aciklama": "..."}, ...]\n\n'
        + json.dumps(payload, ensure_ascii=False)
    )
    for attempt in range(2):
        try:
            resp = requests.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json={"model": LLM_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3},
                timeout=120,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.M).strip()
            for row in json.loads(content):
                item = items[row["i"]]
                item["tr_title"] = clean_text(row.get("baslik", ""), 200)
                item["tr_desc"] = clean_text(row.get("aciklama", ""), 400)
            return
        except Exception as exc:
            print(f"[uyarı] LLM denemesi {attempt + 1} başarısız: {exc}", file=sys.stderr)
            time.sleep(5)


# ----------------------------- TELEGRAM -----------------------------


def format_item(it) -> str:
    title = html.escape(it.get("tr_title") or it["title"])
    link = html.escape(it["link"], quote=True)
    source = html.escape(it["source"])
    # LLM açıklaması yoksa kaynaktaki kısa özeti kullan (genelde İngilizce)
    desc = it.get("tr_desc") or (it["summary"][:160] + "…" if it["summary"] else "")
    lines = [f'<b><a href="{link}">{title}</a></b>']
    if desc:
        lines.append(html.escape(desc))
    lines.append(f"<i>📌 {source}</i>")
    return "\n".join(lines)


def build_blocks(sections):
    """Mesajı, bölünürse bile bir haberin ortasından kesilmeyecek parçalara ayırır."""
    today = datetime.now().strftime("%d.%m.%Y")
    blocks = [f"📰 <b>Günlük Haberler</b> — {today}"]
    for category, items in sections.items():
        blocks.append(f"━━━━━━━━━━━━━━\n<b>{category}</b>\n━━━━━━━━━━━━━━")
        if not items:
            blocks.append("Bugün yeni haber bulunamadı.")
        for it in items:
            blocks.append(format_item(it))
    return blocks


def send_telegram(blocks):
    chunks, current = [], ""
    for block in blocks:
        if current and len(current) + len(block) + 2 > 3800:
            chunks.append(current)
            current = ""
        current += ("\n\n" if current else "") + block
    if current:
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
        enrich_items(category, items)
        sections[category] = items

    send_telegram(build_blocks(sections))
    print("Gönderildi.")


if __name__ == "__main__":
    main()
