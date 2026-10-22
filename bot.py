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
import io
import json
import os
import random
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
BREAKING_MAX_PER_DAY = int(os.environ.get("BREAKING_MAX_PER_DAY", "4"))  # günlük üst sınır
BREAKING_MIN_GAP_MIN = int(os.environ.get("BREAKING_MIN_GAP_MIN", "60"))  # iki son dakika arası en az (dakika)
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
POST_STORY = os.environ.get("POST_STORY", "1") == "1"   # post sonrası hikâye de paylaşılsın mı

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
    # Spor, magazin ve kraliyet
    ("BBC Sport", "https://feeds.bbci.co.uk/sport/rss.xml", 24),
    ("BBC", "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml", 24),
    ("Sky News", "https://feeds.skynews.com/feeds/rss/entertainment.xml", 24),
    ("Guardian", "https://www.theguardian.com/uk/monarchy/rss", 24),
    # Göç ve vize (resmi kaynaklar seyrek yayın yaptığı için 3 gün geriye bakılır)
    ("GOV.UK Home Office", "https://www.gov.uk/government/organisations/home-office.atom", 72),
    ("GOV.UK UKVI", "https://www.gov.uk/government/organisations/uk-visas-and-immigration.atom", 72),
    ("Free Movement", "https://freemovement.org.uk/feed/", 72),
]

MONTHS_TR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz",
             "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
DAYS_TR = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]

# Görsel tasarımı
W, H = 1080, 1350                      # Instagram 4:5 dikey format
# Açık "editoryal" tema: kırık beyaz zemin (Pantone 2026 Cloud Dancer'a yakın), koyu mürekkep mavisi metin,
# her haber kategorisine az sayıda, kolay ayırt edilen ve hafif kısık tonlu bir renk.
BG_TOP = (243, 240, 234)
BG_BOTTOM = (235, 231, 223)
BG = BG_BOTTOM
PANEL = (226, 220, 209)                # ince çizgiler, kutular
INK = (22, 36, 64)                     # başlıklar
TEXT = INK
BODY = (55, 66, 88)
MUTED = (112, 119, 136)
WHITE = (255, 255, 255)
BREAKING = (200, 64, 52)               # son dakika: kısık tuğla kırmızısı
ACCENT = (37, 78, 160)                 # varsayılan vurgu: kraliyet mavisi

CATEGORIES = {                         # anahtar: (etiket, renk)
    "goc":      ("GÖÇ & VİZE", (13, 115, 119)),      # petrol yeşili
    "ekonomi":  ("EKONOMİ", (39, 122, 78)),          # orman yeşili
    "siyaset":  ("SİYASET", (37, 78, 160)),          # kraliyet mavisi
    "saglik":   ("SAĞLIK", (190, 82, 108)),          # gül kurusu
    "guvenlik": ("GÜVENLİK", (74, 86, 112)),         # arduvaz
    "hava":     ("HAVA & ÇEVRE", (35, 128, 186)),    # gök mavisi
    "spor":     ("SPOR", (106, 72, 168)),            # mor
    "magazin":  ("MAGAZİN", (190, 120, 28)),         # hardal
    "kraliyet": ("KRALİYET", (140, 62, 120)),        # erik
    "dunya":    ("DÜNYA", (24, 98, 150)),            # derin mavi
    "gundem":   ("GÜNDEM", (60, 72, 140)),           # çivit
}


def cat_of(it):
    key = (it.get("category") or "").lower()
    return CATEGORIES.get(key, CATEGORIES["gundem"])


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


# Hesabın yayın ilkesi: bu konulardaki haberler hiç seçilmez (Claude'a bile gönderilmez).
SENSITIVE_EN = re.compile(
    r"\b(rape[ds]?|raping|rapist|sexual(ly)?|sex (attack|offen\w*|abuse)|indecen\w*|paedophil\w*|pedophil\w*|"
    r"grooming|child abuse|molest\w*|upskirt\w*|suicide|self-harm|murder\w*|manslaughter|stabb\w*|stabbed|"
    r"strangl\w*|dismember\w*|porn\w*)\b", re.I)
SENSITIVE_TR = re.compile(
    r"(tecavüz|cinsel|taciz|istismar|müstehcen|pornograf|intihar|kendine zarar|cinayet|bıçakla|bıçaklan|boğarak)", re.I)


def is_sensitive(*texts):
    t = " ".join(x or "" for x in texts)
    return bool(SENSITIVE_EN.search(t) or SENSITIVE_TR.search(t))


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
            if is_sensitive(title, e.get("summary", "")):
                continue
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
TERMS = """İsim ve terim kuralları (çok önemli, yanlış çeviri hesabı küçük düşürür):
- Kişi, kurum, kuruluş, şirket, parti, program, yasa, vize ve sosyal yardım ADLARINI ÇEVİRME; orijinal İngilizce halini aynen yaz.
  Gerekirse ilk geçtiği yerde parantez içinde kısa Türkçe açıklama ekle.
  Örnekler: "Home Office (İçişleri Bakanlığı)", "DWP (Çalışma ve Emeklilik Bakanlığı)", "HMRC (vergi idaresi)", "NHS", "Ofgem",
  "Ofcom", "Met Police", "Bank of England", "Reform UK", "Labour", "Conservatives", "Lib Dems", "SNP", "Plaid Cymru",
  "Universal Credit", "Pension Credit", "PIP", "Skilled Worker visa", "triple lock", "Budget" yerine "bütçe" olabilir.
- Unvanlardan YALNIZCA yerleşik Türkçe karşılığı olanları çevir: "Prime Minister" = "Başbakan", "Chancellor" (Chancellor of the Exchequer) = "Maliye Bakanı"
  (asla "şansölye" yazma), "Home Secretary" = "İçişleri Bakanı", "Foreign Secretary" = "Dışişleri Bakanı", "Health Secretary" = "Sağlık Bakanı".
  Karşılığından emin olmadığın unvanı İngilizce bırak (ör. "Shadow Chancellor", "Speaker", "Black Rod").
- Yer adlarında yalnızca yerleşik Türkçe adları kullan: Londra, İngiltere, Birleşik Krallık, İskoçya, Galler, Kuzey İrlanda, Manş Denizi.
  Diğerlerini İngilizce bırak: Westminster, Downing Street, Holyrood, Senedd, Stormont, Heathrow, Gatwick vb.
- Emin olmadığın hiçbir ismi ya da terimi tahmin ederek çevirme; İngilizce bırak."""


SYSTEM_PROMPT = """Sen İngiltere'de yaşayan Türk topluluğu için Türkçe haber özeti hazırlayan bir editörsün.

Kurallar:
- YALNIZCA sana verilen başlık ve özetlerdeki bilgileri kullan. Rakam, isim, tarih, alıntı UYDURMA. Emin olmadığın ayrıntıyı yazma, genel ifade et.
- Göç, iltica, vize, oturum izni haberi varsa mutlaka seç.
- Öncelik sırası: göç/vize, yaşam maliyeti/vergi/sosyal yardım/emeklilik, siyaset, sağlık/NHS, ekonomi/iş, güvenlik, sonra ilgi çekici haberler.
- Listenin sonuna, varsa 1-2 ilgi çekici spor, magazin ya da kraliyet haberi ekle (ör. İngiliz takımlarının ve milli takımların önemli maçları, büyük transferler, tanınmış İngiliz isimlerle ilgili önemli gelişmeler, kraliyet ailesinin resmi açıklamaları ve ziyaretleri).
- Şunları SEÇME: cinsel suçlar ve cinsel suç iddiaları, çocuk istismarı, intihar/kendine zarar, bireysel cinayet ve şiddet ayrıntıları, doğrulanmamış dedikodu ve söylentiler, diyet/zayıflama.
- Aynı olayı iki kez yazma.
- Dil sade, tarafsız, haber dili. Para birimini "sterlin" ya da "£" ile yaz.
- category: haberin kategorisi; şunlardan BİRİ: goc, ekonomi, siyaset, saglik, guvenlik, hava, spor, magazin, kraliyet, dunya, gundem.
- photo_query: haberi temsil eden bir MEKÂN ya da NESNE fotoğrafı için İngilizce arama terimi (ör. "UK parliament", "passport stamp", "hospital corridor", "London bus", "pound coins"). Gerçek kişilerin adını, yüzü ön planda olan insanları, şiddet ya da suç içeren görselleri arama.

Çıktı: SADECE geçerli JSON, başka hiçbir metin yok:
{"items":[{"ref":listedeki sıra numarası (tam sayı),"emoji":"tek emoji","title":"en fazla 70 karakter başlık","body":"2 cümle, en fazla 260 karakter","source":"kaynak adı","category":"kategori anahtarı","photo_query":"Pexels için 2-4 kelimelik İNGİLİZCE fotoğraf arama terimi"}],
 "hashtags":["#ornek", "..."]}
hashtags: 8-12 adet, ilgili Türkçe ve İngilizce etiketler (ör. #İngiltere #Londra #UKNews)."""

SYSTEM_PROMPT += "\n\n" + TERMS

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

Şunlar son dakika sayılır (geniş kitleyi ilgilendiren, yeni ve resmi/kesin gelişmeler):
- Göç, vize, oturum izni, vatandaşlık ya da iltica politikasında yeni kural, karar veya resmi açıklama; büyük göç verisi
- Başbakan, bakanlar ya da hükümetle ilgili büyük gelişme (istifa, görevden alma, kabine değişikliği, erken seçim, önemli parti ayrılıkları)
- Ekonomi: enflasyon, faiz, işsizlik gibi resmi veri açıklamaları; bütçe ve vergi kararları; enerji fiyat tavanı
- Sosyal yardım, emeklilik, konut, kira, ulaşım ücretleri gibi geniş kitleyi etkileyen resmi değişiklikler
- Büyük şirket haberleri: 1.000'den fazla kişiyi etkileyen işten çıkarma, büyük bir şirketin iflası, ülke çapında hizmet kesintisi
- NHS'te grev, ulusal sağlık uyarısı ya da büyük sağlık politikası kararı
- Ülke çapında ulaşım, grev, sınır ya da havalimanı aksaması
- Amber ya da kırmızı hava uyarısı, isimli fırtına, büyük sel, ulusal acil durum
- Büyük ulusal güvenlik veya savunma gelişmesi (yalnızca resmi bilgiye dayanarak, ayrıntıya girmeden)
- Hükümet politikasını etkileyen önemli mahkeme kararları
- Kraliyet ailesinden resmi açıklama ya da önemli gelişme (doğum, nişan, sağlık açıklaması, tarihi ziyaret, unvan değişikliği)
- Spor: İngiliz, İskoç ya da Galler milli takımlarının önemli maç sonuçları, büyük kupa finalleri, Premier League'de çok büyük gelişmeler (rekor transfer, teknik direktör ayrılığı/ataması, şampiyonluk), Türk futbolcu ya da takımlarla ilgili önemli İngiltere gelişmeleri
- Magazin: çok tanınmış İngiliz isimlerle ilgili büyük ve doğrulanmış gelişmeler (ör. önemli ödül, ünlü bir ismin vefatı, büyük bir turne/film duyurusu)

Son dakika SAYILMAZ: yorum ve köşe yazıları, anketler, spekülasyon ve sızıntılar, sıradan siyasi atışmalar, bireysel suç, soruşturma ve mahkeme haberleri, cinsel suç iddiaları (kim hakkında olursa olsun), intihar, doğrulanmamış dedikodu ve söylentiler, sıradan maç haberleri ve transfer söylentileri, yerel haberler.
Günde birkaç ara paylaşım yapılabiliyor; çıta yüksek ama makul. Gerçekten yeni ve önemli bir gelişme varsa paylaş, emin değilsen paylaşma.

Kurallar: YALNIZCA verilen başlık ve özetteki bilgileri kullan, rakam, isim, alıntı uydurma. Dil sade ve tarafsız. photo_query: haberi temsil eden MEKÂN ya da NESNE için İngilizce arama terimi; kişi adı, yüz, şiddet içeren görsel arama.

Çıktı SADECE JSON:
Paylaşılacak haber yoksa: {"breaking": false}
Varsa: {"breaking": true, "ref": listedeki numara, "emoji": "tek emoji", "title": "en fazla 70 karakter", "body": "2-3 cümle, en fazla 300 karakter", "source": "kaynak adı", "category": "goc|ekonomi|siyaset|saglik|guvenlik|hava|spor|magazin|kraliyet|dunya|gundem", "photo_query": "2-4 kelimelik İNGİLİZCE fotoğraf arama terimi", "hashtags": ["#SonDakika", "..."]}"""


BREAKING_PROMPT += "\n\n" + TERMS


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
    for it in data.get("items", []):
        if len(out) >= n:
            break
        title = clean(it.get("title", ""))[:90]
        body = clean(it.get("body", ""))[:320]
        if is_sensitive(title, body):
            log(f"[filtre] Hassas konu nedeniyle elendi: {title}")
            continue
        if title and body:
            ref = it.get("ref")
            out.append({
                "ref": ref if isinstance(ref, int) else None,
                "emoji": clean(it.get("emoji", "📰"))[:4] or "📰",
                "title": title,
                "body": body,
                "source": clean(it.get("source", ""))[:30],
                "photo_query": clean(it.get("photo_query", ""))[:60],
                "category": clean(it.get("category", "")).lower()[:12],
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


def bg_at(y, total):
    """Arka plan geçişinde verilen satırın rengi."""
    t = max(0.0, min(1.0, y / max(1, total - 1)))
    return tuple(int(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM))


def gradient_canvas(w, h):
    from PIL import Image

    col = Image.new("RGB", (1, h))
    col.putdata([bg_at(y, h) for y in range(h)])
    return col.resize((w, h))


def base_canvas(accent=ACCENT):
    from PIL import ImageDraw

    img = gradient_canvas(W, H)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 12], fill=accent)
    return img, d


def footer(d, left_text):
    y = H - 110
    d.line([MARGIN, y, W - MARGIN, y], fill=PANEL, width=3)
    d.text((MARGIN, y + 30), left_text, font=font("regular", 30), fill=MUTED)
    fb = font("bold", 30)
    tw = d.textlength(BRAND_HANDLE, font=fb)
    d.text((W - MARGIN - tw, y + 30), BRAND_HANDLE, font=fb, fill=INK)


def chip(d, x, y, text, color, size=28, pad_x=22, pad_y=12, fg=WHITE):
    """Yuvarlak köşeli renkli etiket; sağ kenarının x'ini döndürür."""
    f = font("bold", size)
    tw = d.textlength(text, font=f)
    h = size + 2 * pad_y
    d.rounded_rectangle([x, y, x + tw + 2 * pad_x, y + h], radius=h // 2, fill=color)
    d.text((x + pad_x, y + pad_y - 2), text, font=f, fill=fg)
    return x + tw + 2 * pad_x


def render_cover(items, day, path):
    img, d = base_canvas(INK)
    max_w = W - 2 * MARGIN
    y = 110
    chip(d, MARGIN, y, COVER_LABEL, INK, size=28)
    y += 95
    fnt, lines, lh = fit_text(d, BRAND_TITLE, "bold", max_w, 260, 96, 60, 1.12)
    y = draw_lines(d, MARGIN, y, lines, fnt, lh, INK)
    y += 10
    d.text((MARGIN, y), date_tr(day), font=font("regular", 38), fill=MUTED)
    y += 80
    # her haberin kategori rengiyle küçük renk şeridi
    seg = max_w // max(1, len(items))
    for i, it in enumerate(items):
        d.rectangle([MARGIN + i * seg, y, MARGIN + (i + 1) * seg - 6, y + 8], fill=cat_of(it)[1])
    y += 50

    box_bottom = H - 150
    for size in range(36, 23, -2):
        f_num, f_t = font("bold", size), font("regular", size)
        lh = int(size * 1.3)
        blocks, total = [], 0
        for i, it in enumerate(items, 1):
            ls = wrap(d, no_emoji(it["title"]), f_t, max_w - 70)[:2]
            blocks.append((i, it, ls))
            total += lh * len(ls) + int(size * 0.55)
        if y + total <= box_bottom:
            break
    for i, it, ls in blocks:
        d.text((MARGIN, y), f"{i:02d}", font=f_num, fill=cat_of(it)[1])
        y = draw_lines(d, MARGIN + 70, y, ls, f_t, lh, INK) + int(size * 0.55)

    footer(d, "Detaylar için kaydırın  →")
    img.save(path, "JPEG", quality=92, optimize=True)


def upper_tr(s):
    return s.replace("i", "İ").replace("ı", "I").upper()


PHOTO_H = 540  # kartlarda fotoğraf bandının yüksekliği


def render_item(it, idx, total, path, photo=None):
    label, color = cat_of(it)
    img, d = base_canvas(color)
    top = 62
    d.text((MARGIN, top), f"{idx:02d} / {total:02d}", font=font("bold", 32), fill=color)
    brand = upper_tr(BRAND_TITLE)
    fl = font("regular", 26)
    d.text((W - MARGIN - d.textlength(brand, font=fl), top + 5), brand, font=fl, fill=MUTED)
    y = top + 70
    if photo is not None:
        img, d = draw_photo(img, photo, (MARGIN - 32, y, W - MARGIN + 32, y + PHOTO_H - 120))
        y += PHOTO_H - 120 + 34
    else:
        y += 40
    draw_story(d, it, y, H - 160, accent=color, label=label, label_color=color)
    footer(d, f"Kaynak: {it['source']}" if it.get("source") else BRAND_TITLE)
    img.save(path, "JPEG", quality=92, optimize=True)


def render_breaking(it, when, path, photo=None):
    label, color = cat_of(it)
    img, d = base_canvas(BREAKING)
    y = 62
    x = chip(d, MARGIN, y - 6, "SON DAKİKA", BREAKING, size=34, pad_x=26, pad_y=14)
    stamp = f"{when.day} {MONTHS_TR[when.month - 1]} {when.year}, {when:%H:%M}"
    d.text((x + 24, y + 10), stamp, font=font("bold", 30), fill=MUTED)
    y += 92
    if photo is not None:
        img, d = draw_photo(img, photo, (MARGIN - 32, y, W - MARGIN + 32, y + PHOTO_H - 140))
        y += PHOTO_H - 140 + 34
    else:
        y += 40
    draw_story(d, it, y, H - 160, accent=BREAKING, label=label, label_color=color)
    footer(d, f"Kaynak: {it['source']}" if it.get("source") else BRAND_TITLE)
    img.save(path, "JPEG", quality=92, optimize=True)


# ------------------------- Fotoğraflar (Pixabay / Pexels) ------------------------- #
PHOTOS_FILE = OUT_ROOT / "photos_used.json"   # aynı fotoğraf sık tekrar etmesin
UK_FALLBACK_QUERIES = [
    "London skyline", "Big Ben London", "Tower Bridge", "Westminster London", "London red bus",
    "London street", "British countryside", "Edinburgh city", "Cardiff Wales", "Manchester city",
    "Thames river London", "London underground", "English village", "Scottish highlands",
    "Union Jack flag", "London taxi", "Belfast city", "Brighton seafront",
]


_PHOTO_WARNED = set()


def _photo_warn(msg):
    """Aynı uyarıyı bir kez, GitHub Annotations kutusunda görünecek şekilde yazar."""
    if msg not in _PHOTO_WARNED:
        _PHOTO_WARNED.add(msg)
        print(f"::warning::{msg}", flush=True)


def photo_provider():
    """Hangi fotoğraf servisi kullanılacak: Pixabay anahtarı varsa Pixabay, yoksa Pexels, ikisi de yoksa None."""
    if os.environ.get("PIXABAY_API_KEY", "").strip():
        return "Pixabay"
    if os.environ.get("PEXELS_API_KEY", "").strip():
        return "Pexels"
    return None


def _get_json(name, url, **kw):
    try:
        r = requests.get(url, timeout=20, **kw)
        if r.status_code != 200:
            log(f"[uyarı] {name} araması başarısız ({r.status_code})")
            _photo_warn(f"{name} araması başarısız (HTTP {r.status_code}). 400/401/403 ise {name.upper()}_API_KEY yanlış.")
            return None
        return r.json()
    except Exception as e:
        log(f"[uyarı] {name}'e ulaşılamadı: {e}")
        _photo_warn(f"{name}'e ulaşılamadı: {type(e).__name__}")
        return None


def photo_search(query, orientation="landscape"):
    """Ortak biçimde sonuç döndürür: [{'id','url','photographer','page'}]."""
    prov = photo_provider()
    if not prov or not query:
        return []
    if prov == "Pixabay":
        data = _get_json("Pixabay", "https://pixabay.com/api/", params={
            "key": os.environ["PIXABAY_API_KEY"].strip(), "q": query[:100], "image_type": "photo",
            "orientation": "vertical" if orientation == "portrait" else "horizontal",
            "safesearch": "true", "per_page": 15, "lang": "en"})
        return [{"id": f"pb:{h['id']}", "url": h.get("largeImageURL") or h.get("webformatURL"),
                 "photographer": h.get("user"), "page": h.get("pageURL")}
                for h in (data or {}).get("hits", []) if h.get("largeImageURL") or h.get("webformatURL")]
    data = _get_json("Pexels", "https://api.pexels.com/v1/search",
                     params={"query": query, "per_page": 15, "orientation": orientation},
                     headers={"Authorization": os.environ["PEXELS_API_KEY"].strip()})
    out = []
    for p in (data or {}).get("photos", []):
        src = p.get("src", {})
        out.append({"id": str(p["id"]), "url": src.get("large2x") or src.get("large") or src.get("original"),
                    "photographer": p.get("photographer"), "page": p.get("url")})
    return out


def load_photo(url):
    from PIL import Image

    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        return Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception as e:
        log(f"[uyarı] Fotoğraf indirilemedi: {e}")
        return None


def photo_for(it, used, orientation="landscape"):
    """Önce habere uygun fotoğraf, bulunamazsa rastgele bir İngiltere fotoğrafı. Hiçbiri yoksa None."""
    prov = photo_provider()
    if not prov:
        _photo_warn("PIXABAY_API_KEY (ya da PEXELS_API_KEY) tanımlı değil; kartlar fotoğrafsız üretildi.")
        return None
    queries = [q for q in [it.get("photo_query")] if q] + random.sample(UK_FALLBACK_QUERIES, 3)
    for q in queries:
        candidates = [p for p in photo_search(q, orientation) if p["id"] not in used and p.get("url")]
        random.shuffle(candidates)
        for p in candidates[:3]:
            im = load_photo(p["url"])
            if im is not None:
                used.add(p["id"])
                it["photo"] = {"id": p["id"], "provider": prov, "query": q, "photographer": p.get("photographer"),
                               "page": p.get("page"), "fallback": q != it.get("photo_query")}
                return im
    return None


def draw_photo(img, photo, box, total_h=None):
    """Fotoğrafı kutuya kırpıp yuvarlak köşeli bir çerçeve içinde yerleştirir (hafif gölgeyle)."""
    from PIL import Image, ImageDraw, ImageFilter, ImageOps

    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    fitted = ImageOps.fit(photo, (w, h), method=Image.LANCZOS, centering=(0.5, 0.45))
    radius = 34
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
    base = img.convert("RGBA")
    shadow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle([x0 + 4, y0 + 14, x1 + 4, y1 + 14], radius=radius, fill=(22, 36, 64, 60))
    base = Image.alpha_composite(base, shadow.filter(ImageFilter.GaussianBlur(16)))
    base.paste(fitted, (x0, y0), mask)
    img = base.convert("RGB")
    return img, ImageDraw.Draw(img)


def draw_story(d, it, area_top, area_bottom, title_start=72, accent=ACCENT, width=None, label=None, label_color=None):
    """Başlık + renkli çizgi + gövde metnini verilen alana sığdırıp dikeyde ortalar."""
    max_w = (width or W) - 2 * MARGIN
    t_fnt, t_lines, t_lh = fit_text(d, no_emoji(it["title"]), "bold", max_w, 420, title_start, 40, 1.15)
    title_h = t_lh * len(t_lines)
    gap = 30 + 8 + 50
    chip_h = 80 if label else 0          # kategori etiketi başlığın hemen üstünde
    area_top += chip_h
    b_fnt, b_lines, b_lh = fit_text(d, no_emoji(it["body"]), "regular", max_w,
                                    (area_bottom - area_top) - title_h - gap, 50, 30, 1.42)
    block_h = title_h + gap + b_lh * len(b_lines)

    y = area_top + max(0, (area_bottom - area_top - block_h) // 2)
    if label:
        chip(d, MARGIN, y - chip_h, label, label_color or accent)
    y = draw_lines(d, MARGIN, y, t_lines, t_fnt, t_lh, INK)
    y += 30
    d.rounded_rectangle([MARGIN, y, MARGIN + 120, y + 8], radius=4, fill=accent)
    y += 58
    draw_lines(d, MARGIN, y, b_lines, b_fnt, b_lh, BODY)


def build_breaking_caption(it, tags, when):
    tags = (["#SonDakika"] + [t for t in tags if t.lower() != "#sondakika"])[:30]
    text = "\n".join([
        f"🔴 SON DAKİKA | {it['emoji']} {it['title']}",
        "",
        it["body"],
        "",
        (f"Kaynak: {it['source']}" if it.get("source") else "") + (f" | Görsel: {it['photo'].get('provider', 'Pexels')}" if it.get("photo") else ""),
        f"{BRAND_TITLE} | {when.day} {MONTHS_TR[when.month - 1]} {when:%H:%M}",
        "",
        " ".join(tags),
    ])
    return text[:2200].strip()


SW, SH = 1080, 1920  # hikâye (story) formatı 9:16
STORY_SAFE_TOP, STORY_SAFE_BOTTOM = 250, SH - 260  # Instagram'ın üst/alt arayüzünün kapattığı alanlar


def story_canvas(accent=INK):
    from PIL import ImageDraw

    img = gradient_canvas(SW, SH)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, SW, 12], fill=accent)
    d.rectangle([0, SH - 12, SW, SH], fill=accent)
    return img, d


def story_cta(d, line1="Tüm haberler ve detaylar", line2="profildeki son gönderide", accent=INK):
    """Alttaki yönlendirme kutusu (API ile link/çıkartma eklenemediği için)."""
    top, bottom = STORY_SAFE_BOTTOM - 250, STORY_SAFE_BOTTOM
    d.rounded_rectangle([MARGIN, top, SW - MARGIN, bottom], radius=32, fill=INK)
    d.rounded_rectangle([MARGIN, top, MARGIN + 16, bottom], radius=8, fill=accent)
    f1, f2, f3 = font("bold", 46), font("regular", 40), font("bold", 40)
    d.text((MARGIN + 54, top + 40), line1, font=f1, fill=WHITE)
    d.text((MARGIN + 54, top + 105), line2, font=f2, fill=(196, 204, 220))
    d.text((MARGIN + 54, top + 170), BRAND_HANDLE, font=f3, fill=WHITE)


def render_story(items, day, path):
    img, d = story_canvas(INK)
    max_w = SW - 2 * MARGIN
    y = STORY_SAFE_TOP
    chip(d, MARGIN, y, COVER_LABEL, INK, size=32)
    y += 100
    fnt, lines, lh = fit_text(d, BRAND_TITLE, "bold", max_w, 260, 100, 64, 1.1)
    y = draw_lines(d, MARGIN, y, lines, fnt, lh, INK) + 10
    d.text((MARGIN, y), date_tr(day), font=font("regular", 42), fill=MUTED)
    y += 90
    shown = items[:6]
    seg = max_w // max(1, len(items))
    for i, it in enumerate(items):
        d.rectangle([MARGIN + i * seg, y, MARGIN + (i + 1) * seg - 6, y + 8], fill=cat_of(it)[1])
    y += 60

    box_bottom = STORY_SAFE_BOTTOM - 340
    for size in range(44, 27, -2):
        f_num, f_t = font("bold", size), font("regular", size)
        lh = int(size * 1.3)
        blocks, total = [], 0
        for i, it in enumerate(shown, 1):
            ls = wrap(d, no_emoji(it["title"]), f_t, max_w - 80)[:2]
            blocks.append((i, it, ls))
            total += lh * len(ls) + int(size * 0.7)
        if y + total <= box_bottom:
            break
    for i, it, ls in blocks:
        d.text((MARGIN, y), f"{i:02d}", font=f_num, fill=cat_of(it)[1])
        y = draw_lines(d, MARGIN + 80, y, ls, f_t, lh, INK) + int(size * 0.7)
    if len(items) > len(shown):
        d.text((MARGIN + 80, y), f"+ {len(items) - len(shown)} haber daha", font=font("regular", 34), fill=MUTED)

    story_cta(d)
    img.save(path, "JPEG", quality=92, optimize=True)


def render_breaking_story(it, when, path, photo=None):
    label, color = cat_of(it)
    img, d = story_canvas(BREAKING)
    y = STORY_SAFE_TOP
    chip(d, MARGIN, y, "SON DAKİKA", BREAKING, size=44, pad_x=30, pad_y=16)
    stamp = f"{when.day} {MONTHS_TR[when.month - 1]} {when.year}, {when:%H:%M}"
    d.text((MARGIN, y + 105), stamp, font=font("regular", 38), fill=MUTED)
    y += 175
    if photo is not None:
        img, d = draw_photo(img, photo, (MARGIN - 20, y, SW - MARGIN + 20, y + 400))
        y += 430
    draw_story(d, it, y, STORY_SAFE_BOTTOM - 285, title_start=60, accent=BREAKING, width=SW, label=label, label_color=color)
    story_cta(d, "Ayrıntılar", "profildeki son gönderide", accent=BREAKING)
    img.save(path, "JPEG", quality=92, optimize=True)


CAPTION_LIMIT = 2200  # Instagram açıklama sınırı


def first_sentence(text):
    return re.split(r"(?<=[.!?])\s+", text.strip(), maxsplit=1)[0]


def build_caption(items, tags, day):
    """Başlık + her haberin özeti. Sınıra sığmazsa önce ilk cümleye, sonra kısaltmaya düşer."""
    sources = []
    for it in items:
        if it.get("source") and it["source"] not in sources:
            sources.append(it["source"])
    src_txt = ", ".join(sources) if sources else "İngiliz basını"
    head = f"🇬🇧 {BRAND_TITLE} | {date_tr(day)}\n\n"
    provs = sorted({it["photo"].get("provider", "Pexels") for it in items if it.get("photo")})
    photo_note = f" Görseller: {', '.join(provs)}." if provs else ""
    foot = f"\nKaynaklar: {src_txt}. Haberler Türkçe özet olarak derlenmiştir.{photo_note}\n\n" + " ".join(tags[:30])

    def compose(bodies):
        blocks = [f"{i}. {it['emoji']} {it['title']}\n{b}" for i, (it, b) in enumerate(zip(items, bodies), 1)]
        return head + "\n\n".join(blocks) + "\n" + foot

    # 1) Tam özetler  2) Her haberden ilk cümle  3) İlk cümleler eşit uzunlukta kısaltılır
    full = [it["body"] for it in items]
    short = [first_sentence(b) for b in full]
    for bodies in (full, short):
        text = compose(bodies)
        if len(text) <= CAPTION_LIMIT:
            return text.strip()
    budget = CAPTION_LIMIT - len(compose([""] * len(items)))
    per = max(40, budget // max(1, len(items)))
    cut = [b if len(b) <= per else b[: per - 1].rstrip(" ,.;:") + "…" for b in short]
    return compose(cut)[:CAPTION_LIMIT].strip()


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
    seen_photos = set(load_keys(PHOTOS_FILE))
    used_photos = set(seen_photos)
    images = []
    p = out / "00.jpg"
    render_cover(items, day, p)
    images.append(p.as_posix())
    for i, it in enumerate(items, 1):
        p = out / f"{i:02d}.jpg"
        render_item(it, i, len(items), p, photo=photo_for(it, used_photos))
        images.append(p.as_posix())

    save_keys(PHOTOS_FILE, list(used_photos - seen_photos), keep=400)
    story = out / "story.jpg"
    render_story(items, day, story)
    post = {"date": day.isoformat(), "caption": build_caption(items, tags, day),
            "images": images, "story_image": story.as_posix(),
            "items": items, "history_keys": history_keys}
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
    record = {"media_id": media_id, "published_at": dt.datetime.now(TZ).isoformat()}
    (out / "published.json").write_text(json.dumps(record, indent=2))
    save_history(post.get("history_keys", []))
    log(f"Yayınlandı. Instagram medya ID: {media_id}")

    story_id = publish_story(post, base)
    if story_id:
        record["story_id"] = story_id
        (out / "published.json").write_text(json.dumps(record, indent=2))


def publish_story(post, base):
    """Post yayınlandıktan sonra dikey hikâye görselini paylaşır. Hata olursa postu bozmaz."""
    if not POST_STORY or not post.get("story_image"):
        return None
    url = f"{base}/{post['story_image']}"
    try:
        wait_public([url])
        ig_user = os.environ["IG_USER_ID"]
        cid = graph("POST", f"{ig_user}/media", image_url=url, media_type="STORIES")["id"]
        wait_ready(cid)
        story_id = graph("POST", f"{ig_user}/media_publish", creation_id=cid)["id"]
        log(f"Hikâye paylaşıldı. Instagram medya ID: {story_id}")
        return story_id
    except Exception as e:
        log(f"[uyarı] Post paylaşıldı ama hikâye paylaşılamadı: {e}")
        return None


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
    last = max((j.get("created_at", "") for _, j in posts if j.get("published")), default="")
    if last and not FORCE:
        mins = (now - dt.datetime.fromisoformat(last)).total_seconds() / 60
        if mins < BREAKING_MIN_GAP_MIN:
            log(f"Son ara paylaşımdan bu yana {mins:.0f} dakika geçti (en az {BREAKING_MIN_GAP_MIN}), bekleniyor.")
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
    seen_photos = set(load_keys(PHOTOS_FILE))
    used_photos = set(seen_photos)
    photo = photo_for(it, used_photos)
    render_breaking(it, now, img, photo=photo)
    story = out / f"breaking-{stamp}-story.jpg"
    render_breaking_story(it, now, story, photo=photo)
    save_keys(PHOTOS_FILE, list(used_photos - seen_photos), keep=400)
    post = {"type": "breaking", "created_at": now.isoformat(), "item": it,
            "caption": build_breaking_caption(it, tags, now), "images": [img.as_posix()],
            "story_image": story.as_posix(),
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

    story_id = publish_story(post, base)
    if story_id:
        post["story_id"] = story_id
        path.write_text(json.dumps(post, ensure_ascii=False, indent=2), encoding="utf-8")


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


def _annotate_error(msg):
    """Hatayı GitHub'ın 'Annotations' kutusunda görünür yapar (token'ı gizleyerek)."""
    msg = str(msg)
    tok = os.environ.get("IG_ACCESS_TOKEN")
    if tok:
        msg = msg.replace(tok, "***")
    msg = re.sub(r"access_token=[^&\s'\"]+", "access_token=***", msg)
    msg = msg.replace("%", "%25").replace("\r", "").replace("\n", " ")[:900]
    print(f"::error::{msg}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code not in (None, 0):
            if not isinstance(e.code, int):
                _annotate_error(e.code)
        raise
    except Exception as e:
        _annotate_error(f"{type(e).__name__}: {e}")
        raise
