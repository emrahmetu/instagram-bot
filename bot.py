#!/usr/bin/env python3
"""
Birleşik Krallık Gündemi — Instagram otomatik paylaşım botu.

Komutlar:
  python bot.py prepare            RSS'ten son 24 saatin haberlerini çeker, Claude ile
                                   Türkçe özet hazırlar, carousel görsellerini üretir.
  python bot.py prepare --from-json ornek.json
                                   Haberleri RSS/Claude yerine bir JSON dosyasından alır
                                   (test veya elle haber girmek için).
  python bot.py publish            Hazırlanan görselleri Instagram'a carousel olarak gönderir.
  python bot.py breaking           Son 2 saatin yeni başlıklarına bakar; gerçekten son dakika
                                   niteliğinde bir haber varsa tek görsellik kart hazırlar.
  python bot.py breaking-publish   Hazırlanan son dakika kartını Instagram'a gönderir.
  python bot.py whoami             Token'ın eriştiği Facebook Sayfalarını ve bağlı Instagram
                                   hesaplarını (kullanıcı adı + IG_USER_ID) listeler.

Tüm ayarlar ortam değişkenleriyle yapılır (bkz. KURULUM.md).
"""
import argparse
import calendar
import datetime as dt
import html
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# --------------------------------------------------------------------------- #
# Ayarlar
# --------------------------------------------------------------------------- #
TZ = ZoneInfo("Europe/London")
OUT_ROOT = Path("output")

HISTORY_FILE = OUT_ROOT / "history.json"   # daha önce paylaşılan haberler (tekrarı önler)
CHECKED_FILE = OUT_ROOT / "checked.json"   # son dakika kontrolünde zaten değerlendirilen başlıklar

# Son dakika (ara paylaşım) ayarları
BREAKING_START_HOUR = int(os.environ.get("BREAKING_START_HOUR", "8"))    # Londra saati
BREAKING_END_HOUR = int(os.environ.get("BREAKING_END_HOUR", "22"))       # bu saatten sonra sessiz
BREAKING_MAX_PER_DAY = int(os.environ.get("BREAKING_MAX_PER_DAY", "2"))  # günlük üst sınır
BREAKING_WINDOW_HOURS = int(os.environ.get("BREAKING_WINDOW_HOURS", "2"))

# Yanlış hesaba paylaşmayı önlemek için (isteğe bağlı): ör. "emresterling"
EXPECTED_IG_USERNAME = os.environ.get("EXPECTED_IG_USERNAME", "").lstrip("@").strip().lower()

# Paylaşım saati: Londra saatiyle bu saatten önce çalışırsa atlar.
# (GitHub saatleri UTC'dir; yaz/kış saati farkı için iş iki kez tetiklenir, doğru olanı bu kontrol seçer.)
POST_HOUR_LONDON = int(os.environ.get("POST_HOUR_LONDON", "8"))
FORCE = os.environ.get("FORCE", "0") == "1"        # elle çalıştırmada saat kontrolünü atla
COVER_LABEL = os.environ.get("COVER_LABEL", "SABAH BÜLTENİ")

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
NEWS_COUNT = max(1, min(9, int(os.environ.get("NEWS_COUNT", "8"))))  # kapak + en fazla 9 = 10 görsel
BRAND_TITLE = os.environ.get("BRAND_TITLE", "Birleşik Krallık Gündemi")
BRAND_HANDLE = os.environ.get("BRAND_HANDLE", "@emresterling")

GRAPH_API_BASE = os.environ.get("GRAPH_API_BASE", "https://graph.facebook.com").rstrip("/")
GRAPH_API_VERSION = os.environ.get("GRAPH_API_VERSION", "v23.0")
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

# Ücretsiz RSS/Atom kaynakları: (kaynak adı, adres, kaç saat geriye bakılacak)
# Bir kaynak çalışmazsa bot onu atlar ve diğerleriyle devam eder.
FEEDS = [
    # Genel haber
    ("BBC", "https://feeds.bbci.co.uk/news/uk/rss.xml", 24),
    ("BBC", "https://feeds.bbci.co.uk/news/politics/rss.xml", 24),
    ("BBC", "https://feeds.bbci.co.uk/news/business/rss.xml", 24),
    ("Sky News", "https://feeds.skynews.com/feeds/rss/uk.xml", 24),
    ("Sky News", "https://feeds.skynews.com/feeds/rss/politics.xml", 24),
    ("Sky News", "https://feeds.skynews.com/feeds/rss/business.xml", 24),
    ("Guardian", "https://www.theguardian.com/uk-news/rss", 24),
    ("Guardian", "https://www.theguardian.com/money/rss", 24),
    ("The Independent", "https://www.independent.co.uk/news/uk/rss", 24),
    ("Evening Standard", "https://www.standard.co.uk/news/uk/rss", 24),
    ("Metro", "https://metro.co.uk/news/uk/feed/", 24),
    ("Nation.Cymru", "https://nation.cymru/feed/", 24),
    # Göç ve vize (resmi kaynaklar seyrek yayın yaptığı için 3 gün geriye bakılır)
    ("GOV.UK İçişleri", "https://www.gov.uk/government/organisations/home-office.atom", 72),
    ("GOV.UK Vize ve Göç", "https://www.gov.uk/government/organisations/uk-visas-and-immigration.atom", 72),
    ("Free Movement", "https://freemovement.org.uk/feed/", 72),
]

MONTHS_TR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz",
             "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
DAYS_TR = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]

# Görsel tasarımı
W, H = 1080, 1350                      # Instagram 4:5 dikey format
BG = (11, 31, 58)                      # lacivert
PANEL = (18, 44, 80)
ACCENT = (200, 16, 46)                 # kırmızı
TEXT = (255, 255, 255)
MUTED = (169, 182, 200)
MARGIN = 80


def log(msg):
    print(msg, flush=True)


def today_london():
    return dt.datetime.now(TZ).date()


def date_tr(d):
    return f"{d.day} {MONTHS_TR[d.month - 1]} {d.year}, {DAYS_TR[d.weekday()]}"


# --------------------------------------------------------------------------- #
# 1) Haberleri çek
# --------------------------------------------------------------------------- #
def strip_html(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s or ""))


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def title_key(title):
    return re.sub(r"\W+", "", (title or "").lower())


def load_keys(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_keys(path, keys, keep):
    data = load_keys(path) + [k for k in keys if k]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data[-keep:], ensure_ascii=False), encoding="utf-8")


def load_history():
    return load_keys(HISTORY_FILE)


def save_history(keys):
    save_keys(HISTORY_FILE, keys, keep=1000)


def fetch_news(limit=120, max_hours=None, exclude=()):
    import feedparser

    seen = set(load_history()) | set(exclude)  # önceden paylaşılanlar tekrar gelmesin
    items = []
    for source, url, hours in FEEDS:
        if max_hours:
            hours = min(hours, max_hours)
        cutoff = time.time() - hours * 3600
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (uk-gundem-bot)"})
            r.raise_for_status()
            feed = feedparser.parse(r.content)
        except Exception as e:  # bir kaynak çökerse diğerleriyle devam et
            log(f"[uyarı] {source} okunamadı: {e}")
            continue
        for e in feed.entries:
            t = e.get("published_parsed") or e.get("updated_parsed")
            if not t:
                continue
            ts = calendar.timegm(t)
            if ts < cutoff:
                continue
            title = clean(e.get("title", ""))
            key = title_key(title)
            if not title or key in seen:
                continue
            seen.add(key)
            items.append({
                "source": source,
                "title": title,
                "summary": clean(strip_html(e.get("summary", "")))[:400],
                "published": dt.datetime.fromtimestamp(ts, TZ).strftime("%d.%m.%Y %H:%M"),
                "_ts": ts,
                "_priority": 1 if hours > 24 else 0,  # göç kaynakları liste kısaltılırken elenmesin
            })
    items.sort(key=lambda x: (x["_priority"], x["_ts"]), reverse=True)
    for it in items:
        it.pop("_ts", None)
        it.pop("_priority", None)
    log(f"{len(items)} güncel haber başlığı bulundu.")
    return items[:limit]


# --------------------------------------------------------------------------- #
# 2) Claude ile Türkçe özet
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = """Sen İngiltere'de yaşayan Türk topluluğu için Türkçe haber özeti hazırlayan bir editörsün.

Kurallar:
- YALNIZCA sana verilen başlık ve özetlerdeki bilgileri kullan. Rakam, isim, tarih, alıntı UYDURMA. Emin olmadığın ayrıntıyı yazma, genel ifade et.
- Göç, iltica, vize, oturum izni haberi varsa mutlaka seç.
- Öncelik sırası: göç/vize, yaşam maliyeti/vergi/sosyal yardım/emeklilik, siyaset, sağlık/NHS, ekonomi/iş, güvenlik, sonra ilgi çekici haberler.
- Şunları SEÇME: cinsel suçlar, çocuk istismarı, intihar/kendine zarar, bireysel cinayet ve şiddet ayrıntıları, ünlü/kraliyet dedikodusu, diyet/zayıflama.
- Aynı olayı iki kez yazma.
- Terimler: "Chancellor" = "Maliye Bakanı" (asla "şansölye" yazma). "Prime Minister" = "Başbakan". "Home Office" = "İçişleri Bakanlığı". "DWP" = "DWP (Çalışma ve Emeklilik Bakanlığı)". "Budget" = "bütçe".
- Dil sade, tarafsız, haber dili. Para birimini "sterlin" ya da "£" ile yaz.

Çıktı: SADECE geçerli JSON, başka hiçbir metin yok:
{"items":[{"ref":listedeki sıra numarası (tam sayı),"emoji":"tek emoji","title":"en fazla 70 karakter başlık","body":"2 cümle, en fazla 260 karakter","source":"kaynak adı"}],
 "hashtags":["#ornek", "..."]}
hashtags: 8-12 adet, ilgili Türkçe ve İngilizce etiketler (ör. #İngiltere #Londra #UKNews)."""

USER_TEMPLATE = """Aşağıda İngiltere'nin güncel haber başlıkları var (genel haberler son 24 saat, resmi göç kaynakları son 3 gün). Bunlardan en önemli {n} haberi seç ve kurallara göre JSON üret. Her haber için "ref" alanına listedeki numarasını yaz.

{listing}"""


def parse_json(text):
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("Claude yanıtında JSON bulunamadı")
    return json.loads(text[start:end + 1])


def summarize(items, n):
    import anthropic

    if not items:
        return {"items": [], "hashtags": []}
    listing = "\n".join(
        f"{i + 1}. [{it['source']} | {it['published']}] {it['title']} — {it['summary']}"
        for i, it in enumerate(items)
    )
    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY ortam değişkenini kullanır
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": USER_TEMPLATE.format(n=n, listing=listing)}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return parse_json(text)


BREAKING_PROMPT = """Sen İngiltere'de yaşayan Türk topluluğu için çalışan bir haber editörüsün. Görevin, sana verilen YENİ başlıklar arasında ara paylaşımı (son dakika) GERÇEKTEN hak eden bir haber olup olmadığına karar vermek.

Yalnızca şunlar son dakika sayılır:
- Göç, vize, oturum izni ya da vatandaşlıkta yeni kural veya karar
- Başbakan ya da hükümetle ilgili büyük gelişme (istifa, erken seçim kararı, hükümetin düşmesi)
- Bütçe açıklaması, faiz kararı, vergi ya da sosyal yardımda geniş kitleyi etkileyen ani değişiklik
- Ülke çapında ulaşım, grev, sınır ya da havalimanı aksaması
- Kırmızı ya da amber hava uyarısı, büyük sel, ulusal acil durum
- Büyük bir ulusal güvenlik olayı (yalnızca resmi bilgiye dayanarak, ayrıntıya girmeden)
- Kraliyet ailesinde tarihi nitelikte gelişme

Son dakika SAYILMAZ: sıradan siyasi tartışmalar, anketler, yorum yazıları, bireysel suç ve mahkeme haberleri, cinsel suçlar, intihar, ünlü ve magazin haberleri, spor.
Günde en fazla birkaç ara paylaşım yapılabildiği için çıta çok yüksek. Emin değilsen paylaşma.

Kurallar: YALNIZCA verilen başlık ve özetteki bilgileri kullan, rakam, isim, alıntı uydurma. "Chancellor" = "Maliye Bakanı". Dil sade ve tarafsız.

Çıktı SADECE JSON:
Paylaşılacak haber yoksa: {"breaking": false}
Varsa: {"breaking": true, "ref": listedeki numara, "emoji": "tek emoji", "title": "en fazla 70 karakter", "body": "2-3 cümle, en fazla 300 karakter", "source": "kaynak adı", "hashtags": ["#SonDakika", "..."]}"""


def judge_breaking(items):
    import anthropic

    listing = "\n".join(
        f"{i + 1}. [{it['source']} | {it['published']}] {it['title']} — {it['summary']}"
        for i, it in enumerate(items)
    )
    msg = anthropic.Anthropic().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1000,
        system=BREAKING_PROMPT,
        messages=[{"role": "user", "content": f"Yeni başlıklar:\n\n{listing}"}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return parse_json(text)


def normalize(data, n):
    out = []
    for it in data.get("items", [])[:n]:
        title = clean(it.get("title", ""))[:90]
        body = clean(it.get("body", ""))[:320]
        if title and body:
            ref = it.get("ref")
            out.append({
                "ref": ref if isinstance(ref, int) else None,
                "emoji": clean(it.get("emoji", "📰"))[:4] or "📰",
                "title": title,
                "body": body,
                "source": clean(it.get("source", ""))[:30],
            })
    tags = [t if t.startswith("#") else f"#{t}" for t in (data.get("hashtags") or [])]
    tags = [re.sub(r"\s+", "", t) for t in tags if len(t) > 1][:20]
    return out, tags


# --------------------------------------------------------------------------- #
# 3) Görselleri üret
# --------------------------------------------------------------------------- #
FONT_CANDIDATES = {
    "bold": ["fonts/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "regular": ["fonts/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
}


def font(kind, size):
    from PIL import ImageFont

    for path in FONT_CANDIDATES[kind]:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    sys.exit("Font bulunamadı. Ubuntu'da: sudo apt-get install -y fonts-dejavu-core")


def no_emoji(s):
    """Kart üzerindeki metinden emojileri çıkarır (fontta yoklar, kutu olarak görünürler)."""
    out = []
    for ch in s:
        if ord(ch) > 0xFFFF or ch in "‍️︎" or unicodedata.category(ch) == "So":
            continue
        out.append(ch)
    return clean("".join(out))


def wrap(draw, text, fnt, max_w):
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=fnt) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def fit_text(draw, text, kind, max_w, max_h, start, minimum, spacing=1.25):
    """Metni kutuya sığana kadar font boyutunu küçültür."""
    size = start
    while size >= minimum:
        fnt = font(kind, size)
        lines = wrap(draw, text, fnt, max_w)
        line_h = int(size * spacing)
        if line_h * len(lines) <= max_h:
            return fnt, lines, line_h
        size -= 2
    fnt = font(kind, minimum)
    lines = wrap(draw, text, fnt, max_w)
    line_h = int(minimum * spacing)
    keep = max(1, max_h // line_h)
    if len(lines) > keep:
        lines = lines[:keep]
        lines[-1] = lines[-1].rstrip(".,;: ") + "…"
    return fnt, lines, line_h


def draw_lines(draw, x, y, lines, fnt, line_h, fill):
    for ln in lines:
        draw.text((x, y), ln, font=fnt, fill=fill)
        y += line_h
    return y


def base_canvas():
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 16], fill=ACCENT)
    return img, d


def footer(d, left_text):
    y = H - 110
    d.line([MARGIN, y, W - MARGIN, y], fill=PANEL, width=3)
    f = font("regular", 30)
    d.text((MARGIN, y + 30), left_text, font=f, fill=MUTED)
    fb = font("bold", 30)
    tw = d.textlength(BRAND_HANDLE, font=fb)
    d.text((W - MARGIN - tw, y + 30), BRAND_HANDLE, font=fb, fill=TEXT)


def render_cover(items, day, path):
    img, d = base_canvas()
    max_w = W - 2 * MARGIN
    y = 110
    d.text((MARGIN, y), COVER_LABEL, font=font("bold", 34), fill=ACCENT)
    y += 70
    fnt, lines, lh = fit_text(d, BRAND_TITLE, "bold", max_w, 260, 96, 60, 1.12)
    y = draw_lines(d, MARGIN, y, lines, fnt, lh, TEXT)
    y += 10
    d.text((MARGIN, y), date_tr(day), font=font("regular", 38), fill=MUTED)
    y += 90
    d.rectangle([MARGIN, y, MARGIN + 140, y + 8], fill=ACCENT)
    y += 50

    # Başlık listesi: kutuya sığacak şekilde font küçültülür
    box_bottom = H - 150
    for size in range(36, 23, -2):
        f_num, f_t = font("bold", size), font("regular", size)
        lh = int(size * 1.3)
        blocks = []
        total = 0
        for i, it in enumerate(items, 1):
            ls = wrap(d, no_emoji(it["title"]), f_t, max_w - 70)[:2]
            blocks.append((i, ls))
            total += lh * len(ls) + int(size * 0.55)
        if y + total <= box_bottom:
            break
    for i, ls in blocks:
        d.text((MARGIN, y), f"{i:02d}", font=f_num, fill=ACCENT)
        y = draw_lines(d, MARGIN + 70, y, ls, f_t, lh, TEXT) + int(size * 0.55)

    footer(d, "Detaylar için kaydırın  →")
    img.save(path, "JPEG", quality=92, optimize=True)


def upper_tr(s):
    return s.replace("i", "İ").replace("ı", "I").upper()


def render_item(it, idx, total, path):
    img, d = base_canvas()
    top = 100
    d.text((MARGIN, top), f"{idx:02d} / {total:02d}", font=font("bold", 34), fill=ACCENT)
    label = upper_tr(BRAND_TITLE)
    fl = font("regular", 26)
    d.text((W - MARGIN - d.textlength(label, font=fl), top + 6), label, font=fl, fill=MUTED)
    draw_story(d, it, top + 100, H - 170)
    footer(d, f"Kaynak: {it['source']}" if it.get("source") else BRAND_TITLE)
    img.save(path, "JPEG", quality=92, optimize=True)


def render_breaking(it, when, path):
    img, d = base_canvas()
    d.rectangle([0, 0, W, 16], fill=ACCENT)
    label = "SON DAKİKA"
    f = font("bold", 44)
    tw = d.textlength(label, font=f)
    d.rectangle([MARGIN, 100, MARGIN + tw + 48, 178], fill=ACCENT)
    d.text((MARGIN + 24, 112), label, font=f, fill=TEXT)
    stamp = f"{when.day} {MONTHS_TR[when.month - 1]} {when.year}, {when:%H:%M}"
    d.text((MARGIN, 205), stamp, font=font("regular", 32), fill=MUTED)
    draw_story(d, it, 280, H - 170)
    footer(d, f"Kaynak: {it['source']}" if it.get("source") else BRAND_TITLE)
    img.save(path, "JPEG", quality=92, optimize=True)


def draw_story(d, it, area_top, area_bottom):
    """Başlık + kırmızı çizgi + gövde metnini verilen alana sığdırıp dikeyde ortalar."""
    max_w = W - 2 * MARGIN
    t_fnt, t_lines, t_lh = fit_text(d, no_emoji(it["title"]), "bold", max_w, 420, 72, 44, 1.15)
    title_h = t_lh * len(t_lines)
    gap = 30 + 8 + 55  # başlık altı boşluk + çizgi + gövde üstü boşluk
    b_fnt, b_lines, b_lh = fit_text(d, no_emoji(it["body"]), "regular", max_w,
                                    (area_bottom - area_top) - title_h - gap, 52, 30, 1.42)
    block_h = title_h + gap + b_lh * len(b_lines)

    # İçeriği boş alanda dikey olarak ortala (kısa haberlerde alt yarı boş kalmasın)
    y = area_top + max(0, (area_bottom - area_top - block_h) // 2)
    y = draw_lines(d, MARGIN, y, t_lines, t_fnt, t_lh, TEXT)
    y += 30
    d.rectangle([MARGIN, y, MARGIN + 140, y + 8], fill=ACCENT)
    y += 63
    draw_lines(d, MARGIN, y, b_lines, b_fnt, b_lh, (225, 232, 240))


def build_breaking_caption(it, tags, when):
    tags = (["#SonDakika"] + [t for t in tags if t.lower() != "#sondakika"])[:30]
    text = "\n".join([
        f"🔴 SON DAKİKA | {it['emoji']} {it['title']}",
        "",
        it["body"],
        "",
        f"Kaynak: {it['source']}" if it.get("source") else "",
        f"{BRAND_TITLE} | {when.day} {MONTHS_TR[when.month - 1]} {when:%H:%M}",
        "",
        " ".join(tags),
    ])
    return text[:2200].strip()


def build_caption(items, tags, day):
    lines = [f"🇬🇧 {BRAND_TITLE} | {date_tr(day)}", ""]
    for i, it in enumerate(items, 1):
        lines.append(f"{i}. {it['emoji']} {it['title']}")
    sources = []
    for it in items:
        if it.get("source") and it["source"] not in sources:
            sources.append(it["source"])
    src_txt = ", ".join(sources) if sources else "İngiliz basını"
    lines += ["", "Detaylar için kaydırın 👉",
              f"Kaynaklar: {src_txt}. Haberler Türkçe özet olarak derlenmiştir.", ""]
    caption = "\n".join(lines)
    tag_line = " ".join(tags[:30])
    if len(caption) + len(tag_line) + 1 > 2200:  # Instagram açıklama sınırı
        caption = caption[: 2200 - len(tag_line) - 2]
    return f"{caption}\n{tag_line}".strip()


# --------------------------------------------------------------------------- #
# Komutlar
# --------------------------------------------------------------------------- #
def too_early():
    """Londra saatiyle paylaşım saatinden önceyse True (elle çalıştırmada FORCE=1 ile atlanır)."""
    now = dt.datetime.now(TZ)
    if FORCE or now.hour >= POST_HOUR_LONDON:
        return False
    log(f"Londra saati {now:%H:%M}, paylaşım saati {POST_HOUR_LONDON:02d}:00. Henüz erken, atlanıyor.")
    return True


def cmd_prepare(args):
    day = today_london()
    out = OUT_ROOT / day.isoformat()
    if (out / "published.json").exists():
        log("Bugünün postu zaten yayınlanmış, atlanıyor.")
        return
    if too_early():
        return
    out.mkdir(parents=True, exist_ok=True)

    source_items = []
    if args.from_json:
        data = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
    else:
        source_items = fetch_news()
        data = summarize(source_items, NEWS_COUNT)
    items, tags = normalize(data, NEWS_COUNT)
    history_keys = [title_key(source_items[it["ref"] - 1]["title"]) for it in items
                    if it.get("ref") and 1 <= it["ref"] <= len(source_items)]
    if not items:
        log("Paylaşılacak haber çıkmadı, bugün post yok.")
        return

    for old in out.glob("*.jpg"):
        old.unlink()
    images = []
    p = out / "00.jpg"
    render_cover(items, day, p)
    images.append(p.as_posix())
    for i, it in enumerate(items, 1):
        p = out / f"{i:02d}.jpg"
        render_item(it, i, len(items), p)
        images.append(p.as_posix())

    post = {"date": day.isoformat(), "caption": build_caption(items, tags, day),
            "images": images, "items": items, "history_keys": history_keys}
    (out / "post.json").write_text(json.dumps(post, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"{len(images)} görsel hazırlandı: {out}")


def graph(method, path, **params):
    token = os.environ["IG_ACCESS_TOKEN"]
    url = f"{GRAPH_API_BASE}/{GRAPH_API_VERSION}/{path}"
    params["access_token"] = token
    if method == "GET":
        r = requests.get(url, params=params, timeout=60)
    else:
        r = requests.post(url, data=params, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"Instagram API hatası ({r.status_code}): {r.text}")
    return r.json()


def wait_ready(container_id):
    for _ in range(36):
        status = graph("GET", container_id, fields="status_code").get("status_code")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Medya hazırlanamadı ({container_id}): {status}")
        time.sleep(5)
    raise TimeoutError(f"Medya zamanında hazır olmadı: {container_id}")


def wait_public(urls):
    """Görseller GitHub'a yeni yüklendiyse birkaç saniye erişilemeyebilir."""
    for url in urls:
        for _ in range(24):
            try:
                r = requests.head(url, timeout=20, allow_redirects=True)
                if r.status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(5)
        else:
            raise RuntimeError(f"Görsel herkese açık değil: {url} (depo public mi?)")


def cmd_publish(args):
    day = today_london().isoformat()
    out = OUT_ROOT / day
    post_file = out / "post.json"
    if not post_file.exists():
        log("Bugün için hazırlanmış post yok, yayın atlanıyor.")
        return
    if (out / "published.json").exists():
        log("Bugünün postu zaten yayınlanmış.")
        return
    if too_early():
        return
    post = json.loads(post_file.read_text(encoding="utf-8"))
    base = os.environ.get("IMAGE_BASE_URL", "").rstrip("/")
    if not base:
        sys.exit("IMAGE_BASE_URL tanımlı değil.")
    urls = [f"{base}/{p}" for p in post["images"]]

    if DRY_RUN:
        log("DRY_RUN açık: Instagram'a gönderilmedi.\n" + "\n".join(urls) + "\n\n" + post["caption"])
        return

    check_account()
    wait_public(urls)
    ig_user = os.environ["IG_USER_ID"]
    children = []
    for url in urls:
        cid = graph("POST", f"{ig_user}/media", image_url=url, is_carousel_item="true")["id"]
        wait_ready(cid)
        children.append(cid)
    carousel = graph("POST", f"{ig_user}/media", media_type="CAROUSEL",
                     children=",".join(children), caption=post["caption"])["id"]
    wait_ready(carousel)
    media_id = graph("POST", f"{ig_user}/media_publish", creation_id=carousel)["id"]
    (out / "published.json").write_text(json.dumps(
        {"media_id": media_id, "published_at": dt.datetime.now(TZ).isoformat()}, indent=2))
    save_history(post.get("history_keys", []))
    log(f"Yayınlandı. Instagram medya ID: {media_id}")


def check_account():
    """Paylaşımdan önce hedef hesabın kullanıcı adını yazar; beklenen hesap değilse durdurur."""
    info = graph("GET", os.environ["IG_USER_ID"], fields="username")
    user = (info.get("username") or "").lower()
    log(f"Paylaşım yapılacak Instagram hesabı: @{user}")
    if EXPECTED_IG_USERNAME and user != EXPECTED_IG_USERNAME:
        sys.exit(f"Hesap uyuşmuyor: beklenen @{EXPECTED_IG_USERNAME}, bulunan @{user}. Paylaşım iptal edildi.")


def cmd_whoami(args):
    """Token'ın hangi sayfa/Instagram hesabına eriştiğini farklı yollardan arar ve sorunu açıklar."""
    def safe(path, **params):
        try:
            return graph("GET", path, **params), None
        except Exception as e:
            return None, str(e)[:300]

    ig_fields = "id,name,instagram_business_account{id,username}"
    found = {}  # ig_id -> (username, sayfa adı)

    me, err = safe("me", fields="id,name")
    log("=" * 70)
    log(f"Token sahibi: {me.get('name')} (ID {me.get('id')})" if me else f"Token okunamadı: {err}")

    # 1) Token'a (sistem kullanıcısına) doğrudan atanmış sayfalar
    acc, err = safe("me/accounts", fields=ig_fields)
    pages = (acc or {}).get("data", [])
    log(f"\n[1] Sistem kullanıcısına atanmış Facebook Sayfaları: {len(pages)}" + (f"  (hata: {err})" if err else ""))
    for pg in pages:
        ig = pg.get("instagram_business_account")
        log(f"    - {pg['name']}  →  " + (f"Instagram @{ig.get('username')} (ID {ig['id']})" if ig else "bağlı Instagram YOK"))
        if ig:
            found[ig["id"]] = (ig.get("username"), pg["name"])

    # 2) İşletme hesabının sahip olduğu sayfalar ve Instagram hesapları
    biz_ids = []
    if os.environ.get("BUSINESS_ID"):
        biz_ids.append((os.environ["BUSINESS_ID"], "BUSINESS_ID ayarı"))
    bz, err = safe("me/businesses", fields="id,name")
    biz_ids += [(b["id"], b.get("name", "")) for b in (bz or {}).get("data", [])]
    log(f"\n[2] Görülen işletme hesapları: {len(biz_ids)}" + (f"  (hata: {err})" if err and not biz_ids else ""))
    seen_biz = set()
    for bid, bname in biz_ids:
        if bid in seen_biz:
            continue
        seen_biz.add(bid)
        log(f"    İşletme: {bname} (ID {bid})")
        op, err = safe(f"{bid}/owned_pages", fields=ig_fields)
        opages = (op or {}).get("data", [])
        log(f"      Sahip olunan sayfalar: {len(opages)}" + (f"  (hata: {err})" if err else ""))
        for pg in opages:
            ig = pg.get("instagram_business_account")
            log(f"        - {pg['name']}  →  " + (f"Instagram @{ig.get('username')} (ID {ig['id']})" if ig else "bağlı Instagram YOK"))
            if ig:
                found.setdefault(ig["id"], (ig.get("username"), pg["name"]))
        oi, err = safe(f"{bid}/owned_instagram_accounts", fields="id,username")
        igs = (oi or {}).get("data", [])
        log(f"      Sahip olunan Instagram hesapları: {len(igs)}" + (f"  (hata: {err})" if err else ""))
        for ig in igs:
            log(f"        - @{ig.get('username')} (ID {ig['id']})")

    # 3) Bulunan her Instagram hesabına token gerçekten erişebiliyor mu?
    log("\n[3] Erişim testi")
    ok = []
    for ig_id, (user, page) in found.items():
        test, err = safe(ig_id, fields="username")
        if test:
            ok.append((ig_id, test.get("username") or user, page))
            log(f"    ✓ @{test.get('username') or user}: token bu hesaba ERİŞEBİLİYOR")
        else:
            log(f"    ✗ @{user}: hesap var ama token erişemiyor ({err})")

    log("\n" + "=" * 70)
    if ok:
        ig_id, user, page = ok[0]
        log(f"SONUÇ: Hazır. Instagram: @{user} (Facebook Sayfası: {page})")
        log(f"       GitHub Secrets'a ekle →  IG_USER_ID = {ig_id}")
        log(f"       GitHub Variables'a ekle → EXPECTED_IG_USERNAME = {user}")
    elif found:
        log("SONUÇ: Instagram hesabı bulundu ama sistem kullanıcısına atanmamış.")
        log("       business.facebook.com/settings → Hesaplar → Sayfalar ve Instagram hesapları →")
        log("       her birinde 'Kişi ata' → listeden SİSTEM KULLANICISINI (instagram-bot) seç → Tam kontrol.")
    elif not biz_ids:
        log("SONUÇ: Token işletme hesabını da göremiyor. Bu çıktının ekran görüntüsünü gönder;")
        log("       bir sonraki adımda işletme ID'sini elle tanımlayacağız.")
    else:
        log("SONUÇ: İşletme hesabında Instagram'a bağlı bir Facebook Sayfası görünmüyor.")
        log("       Kontrol et: (a) Sayfa bu işletme hesabına ekli mi? (b) Instagram bu sayfaya bağlı mı?")
        log("       (Instagram → Profili düzenle → Sayfa satırında sayfanın adı görünmeli.)")


# --------------------------- Son dakika (ara paylaşım) --------------------------- #
def breaking_posts(out):
    posts = []
    for p in sorted(out.glob("breaking-*.json")):
        try:
            posts.append((p, json.loads(p.read_text(encoding="utf-8"))))
        except json.JSONDecodeError:
            continue
    return posts


def cmd_breaking(args):
    now = dt.datetime.now(TZ)
    out = OUT_ROOT / now.date().isoformat()
    if not FORCE and not (BREAKING_START_HOUR <= now.hour < BREAKING_END_HOUR):
        log(f"Londra saati {now:%H:%M}: sessiz saatler ({BREAKING_END_HOUR:02d}:00–{BREAKING_START_HOUR:02d}:00), kontrol yok.")
        return
    posts = breaking_posts(out)
    if sum(1 for _, j in posts if j.get("published")) >= BREAKING_MAX_PER_DAY:
        log(f"Bugün için ara paylaşım sınırına ({BREAKING_MAX_PER_DAY}) ulaşıldı.")
        return
    if any(not j.get("published") and not j.get("dry_run") for _, j in posts):
        log("Yayın bekleyen bir son dakika kartı var, yeni kontrol yapılmadı.")
        return

    new_items = []
    if args.from_json:
        data = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
    else:
        checked = load_keys(CHECKED_FILE)
        new_items = fetch_news(limit=60, max_hours=BREAKING_WINDOW_HOURS, exclude=checked)
        if not new_items:
            log("Son kontrolden bu yana yeni başlık yok.")  # Claude'a hiç gidilmez, maliyet sıfır
            return
        data = judge_breaking(new_items)
        save_keys(CHECKED_FILE, [title_key(i["title"]) for i in new_items], keep=3000)

    if not data.get("breaking"):
        log(f"{len(new_items)} yeni başlık değerlendirildi, son dakika niteliğinde haber yok.")
        return
    items, tags = normalize({"items": [data], "hashtags": data.get("hashtags", [])}, 1)
    if not items:
        return
    it = items[0]
    ref = it.get("ref")
    hkey = title_key(new_items[ref - 1]["title"]) if ref and 1 <= ref <= len(new_items) else None

    out.mkdir(parents=True, exist_ok=True)
    stamp, n = now.strftime("%H%M%S"), 1
    while (out / f"breaking-{stamp}.json").exists():  # aynı anda iki kart üst üste yazılmasın
        stamp, n = f"{now:%H%M%S}-{n}", n + 1
    img = out / f"breaking-{stamp}.jpg"
    render_breaking(it, now, img)
    post = {"type": "breaking", "created_at": now.isoformat(), "item": it,
            "caption": build_breaking_caption(it, tags, now), "images": [img.as_posix()],
            "history_keys": [hkey] if hkey else [], "published": False}
    (out / f"breaking-{stamp}.json").write_text(json.dumps(post, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"SON DAKİKA kartı hazırlandı: {it['title']}")


def cmd_breaking_publish(args):
    out = OUT_ROOT / today_london().isoformat()
    pending = [(p, j) for p, j in breaking_posts(out) if not j.get("published") and not j.get("dry_run")]
    if not pending:
        log("Yayın bekleyen son dakika kartı yok.")
        return
    path, post = pending[-1]
    base = os.environ.get("IMAGE_BASE_URL", "").rstrip("/")
    if not base:
        sys.exit("IMAGE_BASE_URL tanımlı değil.")
    url = f"{base}/{post['images'][0]}"

    if DRY_RUN:
        post["dry_run"] = True  # deneme kartı sonradan kendiliğinden yayınlanmasın
        path.write_text(json.dumps(post, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"DRY_RUN açık: Instagram'a gönderilmedi.\n{url}\n\n{post['caption']}")
        return

    check_account()
    wait_public([url])
    ig_user = os.environ["IG_USER_ID"]
    cid = graph("POST", f"{ig_user}/media", image_url=url, caption=post["caption"])["id"]
    wait_ready(cid)
    media_id = graph("POST", f"{ig_user}/media_publish", creation_id=cid)["id"]
    post.update(published=True, media_id=media_id, published_at=dt.datetime.now(TZ).isoformat())
    path.write_text(json.dumps(post, ensure_ascii=False, indent=2), encoding="utf-8")
    save_history(post.get("history_keys", []))
    log(f"Son dakika yayınlandı. Instagram medya ID: {media_id}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--from-json", help="Haberleri bu JSON dosyasından al (RSS ve Claude atlanır)")
    sub.add_parser("publish")
    b = sub.add_parser("breaking")
    b.add_argument("--from-json", help="Son dakika kararını bu JSON dosyasından al (test için)")
    sub.add_parser("breaking-publish")
    sub.add_parser("whoami")
    args = ap.parse_args()
    {"prepare": cmd_prepare, "publish": cmd_publish, "breaking": cmd_breaking,
     "breaking-publish": cmd_breaking_publish, "whoami": cmd_whoami}[args.cmd](args)


if __name__ == "__main__":
    main()
