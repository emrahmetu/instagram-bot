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
# Dil: "tr" (varsayılan, @emresterling) ya da "en" (İngilizce hesap, @emresterlin).
# İki hesap aynı depoda çalışır; her dilin kayıtları ve görselleri ayrı klasördedir.
LANG = os.environ.get("BOT_LANG", "tr").strip().lower()
EN = LANG == "en"
OUT_ROOT = Path("output-en" if EN else "output")


def L(tr, en):
    """Dile göre metin seçer."""
    return en if EN else tr

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
COVER_LABEL = os.environ.get("COVER_LABEL", L("SABAH BÜLTENİ", "MORNING BRIEFING"))

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")   # haber seçimi (ucuz, hızlı)
WRITER_MODEL = os.environ.get("WRITER_MODEL", "claude-sonnet-5-5")           # Türkçe yazım ve editörlük
NEWS_COUNT = max(1, min(9, int(os.environ.get("NEWS_COUNT", "8"))))  # kapak + en fazla 9 = 10 görsel
BRAND_TITLE = os.environ.get("BRAND_TITLE", L("Birleşik Krallık Gündemi", "UK Daily Briefing"))
BRAND_HANDLE = os.environ.get("BRAND_HANDLE", L("@emresterling", "@emresterlin"))

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
if EN:
    _EN_LABELS = {"goc": "IMMIGRATION", "ekonomi": "ECONOMY", "siyaset": "POLITICS", "saglik": "HEALTH",
                  "guvenlik": "SECURITY", "hava": "WEATHER", "spor": "SPORT", "magazin": "ENTERTAINMENT",
                  "kraliyet": "ROYALS", "dunya": "WORLD", "gundem": "NEWS"}
    CATEGORIES = {k: (_EN_LABELS[k], c) for k, (_, c) in CATEGORIES.items()}
MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July",
             "August", "September", "October", "November", "December"]
DAYS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def cat_of(it):
    key = (it.get("category") or "").lower()
    return CATEGORIES.get(key, CATEGORIES["gundem"])


MARGIN = 80


def log(msg):
    print(msg, flush=True)


def today_london():
    return dt.datetime.now(TZ).date()


def date_tr(d):
    if EN:
        return f"{DAYS_EN[d.weekday()]}, {d.day} {MONTHS_EN[d.month - 1]} {d.year}"
    return f"{d.day} {MONTHS_TR[d.month - 1]} {d.year}, {DAYS_TR[d.weekday()]}"


def stamp_of(when, year=True):
    """Son dakika kartları için tarih + saat."""
    months = MONTHS_EN if EN else MONTHS_TR
    return f"{when.day} {months[when.month - 1]}" + (f" {when.year}" if year else "") + f", {when:%H:%M}"


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
- Emin olmadığın hiçbir ismi ya da terimi tahmin ederek çevirme; İngilizce bırak.
- DİL: "title" ve "body" alanları HER ZAMAN TÜRKÇE olmalı. Kaynak haber İngilizce olsa bile cümleleri Türkçeye çevir;
  yalnızca yukarıdaki özel isimler, kurum adları ve terimler İngilizce kalır. İngilizce cümle yazmak kesinlikle yasak."""


STYLE = """Türkçe yazım ve üslup (bir Türk okurun doğal bulacağı dil):
- Türk haber sitelerinin dilini kullan: kısa, net, etken cümleler. Çeviri kokan kalıplardan kaçın
  ("-mektedir/-maktadır" yığınları, "değerlendirilmektedir", "olarak görülmektedir", "söz konusu" tekrarı, İngilizce cümle dizilimi).
  Örnek: "Hükümet yeni kuralları açıkladı." (iyi)  /  "Yeni kurallar hükümet tarafından açıklanmış bulunmaktadır." (kötü)
- Başlık özne + yüklem içeren, tek bakışta anlaşılan bir haber cümlesi olsun. Başlığı kesik ya da bozuk cümle yapma.
- Zaman doğru olsun: henüz olmamış şeyi olmuş gibi yazma ("açıklanacak", "planlanıyor", "bekleniyor" ile "açıkladı" farkına dikkat).
- Birimler: "mph" = "saatte ... mil" (ASLA kilometreye çevirme, ASLA "milyon" yazma). "£" = "... sterlin". "bn" = milyar, "m" = milyon.
  Sayıları kaynaktaki değerle birebir aynı yaz; yuvarlama ya da tahmin yapma. Ondalık için virgül kullan (3,5).
- Kişilerin unvanını YALNIZCA kaynakta yazıyorsa kullan; kendi bilginden unvan ekleme (görevler değişmiş olabilir).
- İngilizce deyimleri kelimesi kelimesine çevirme; anlamını düz Türkçe ile ver (ör. "cap in hand" = "avuç açmak").
- Kurum adları İngilizce kalır: Met Office, Met Police, Home Office, Ofcom, Ofgem, NHS, HMRC, DWP vb.
  ("Meteoroloji Dairesi", "Metropoliten Polis" gibi çeviriler YASAK).
- Yasal/idari terimleri yanlış eşleme: "legalisation/apostille" noter onayı değildir, "apostil" yaz;
  "council" = belediye, "benefit" = sosyal yardım, "settlement / ILR" = süresiz oturum izni (ILR)."""


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

SYSTEM_PROMPT += "\n\n" + TERMS + "\n\n" + STYLE

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


BREAKING_PROMPT += "\n\n" + TERMS + "\n\n" + STYLE


def judge_breaking(items, already=()):
    import anthropic

    listing = "\n".join(
        f"{i + 1}. [{it['source']} | {it['published']}] {it['title']} — {it['summary']}"
        for i, it in enumerate(items)
    )
    prev = ""
    if already:
        prev = (L("Bugün ZATEN son dakika olarak paylaşılanlar (aynı olayı tekrar paylaşma; ancak olayda çok büyük ve "
                  "yeni bir gelişme varsa, örneğin can kaybı ya da kırmızı uyarı, paylaşabilirsin):\n",
                  "ALREADY posted as breaking today (do not repeat the same event, unless there is a major new "
                  "development such as deaths or a red warning):\n")
                + "\n".join(f"- {t}" for t in already) + "\n\n")
    msg = anthropic.Anthropic().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1000,
        system=BREAKING_PROMPT,
        messages=[{"role": "user", "content": f"{prev}{L('Yeni başlıklar', 'New headlines')}:\n\n{listing}"}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return parse_json(text)


TR_CHARS = set("çğıöşüÇĞİÖŞÜ")
EN_WORDS = {"the", "and", "of", "to", "in", "for", "is", "are", "as", "with", "on", "has", "have", "after",
            "from", "by", "will", "be", "was", "were", "its", "their", "amid", "over", "a", "an"}
TR_WORDS = {"ve", "bir", "için", "ile", "bu", "da", "de", "olarak", "olan", "en", "çok", "daha", "sonra", "yeni"}


def is_turkish(text):
    """Basit dil kontrolü: Türkçe harf/kelime var mı, İngilizce bağlaç yoğunluğu ne kadar?"""
    words = re.findall(r"[A-Za-zÇĞİÖŞÜçğıöşü']+", text.lower())
    if not words:
        return True
    en = sum(w in EN_WORDS for w in words)
    tr = sum(w in TR_WORDS for w in words) + sum(ch in TR_CHARS for ch in text)
    if tr == 0:
        return False
    return not (en >= 3 and en / len(words) >= 0.15)


TRANSLATE_PROMPT = """Aşağıdaki haber başlığı ve metnini Türkçeye çevir. Anlamı değiştirme, bilgi ekleme.
Özel isimler, kurum ve kuruluş adları, yasa/vize/yardım adları İngilizce kalır (ör. Home Office, NHS, Met Office, Storm Austen).
Çıktı SADECE JSON: {"title": "en fazla 70 karakter", "body": "en fazla 300 karakter"}"""


def ensure_turkish(it):
    """Başlık ya da metin İngilizce kaldıysa Claude'a bir kez çevirtir; yine olmazsa False döner."""
    if EN or (is_turkish(it["title"] + " " + it["body"]) and is_turkish(it["body"])):
        return True
    log(f"[dil] Türkçe olmayan metin bulundu, çevriliyor: {it['title']}")
    try:
        import anthropic

        msg = anthropic.Anthropic().messages.create(
            model=WRITER_MODEL, max_tokens=600, system=TRANSLATE_PROMPT + "\n\n" + TERMS + "\n\n" + STYLE,
            messages=[{"role": "user", "content": json.dumps({"title": it["title"], "body": it["body"]}, ensure_ascii=False)}],
        )
        out = parse_json("".join(b.text for b in msg.content if getattr(b, "type", "") == "text"))
        title, body = clean(out.get("title", ""))[:90], clean(out.get("body", ""))[:320]
        if title and body and is_turkish(title + " " + body) and is_turkish(body):
            it["title"], it["body"] = title, body
            return True
    except Exception as e:
        log(f"[dil] Çeviri başarısız: {e}")
    log(f"[dil] Türkçeye çevrilemediği için elendi: {it['title']}")
    return False


_MULT = {"bin": 1e3, "milyon": 1e6, "milyar": 1e9, "trilyon": 1e12, "k": 1e3, "m": 1e6, "bn": 1e9,
         "million": 1e6, "billion": 1e9, "trillion": 1e12, "thousand": 1e3}


def _num_values(text):
    """Metindeki sayıları olası değerleriyle çıkarır (Türkçe ve İngilizce ayraçlara göre iki yorum)."""
    out = []
    for m in re.finditer(r"(\d+(?:[.,]\d+)*)(?:\s*(bin|milyon|milyar|trilyon|thousand|million|billion|trillion|bn|k|m)\b)?",
                         text, re.I):
        raw, unit = m.group(1), (m.group(2) or "").lower()
        cands = set()
        for dec, thou in ((",", "."), (".", ",")):
            t = raw.replace(thou, "").replace(dec, ".")
            try:
                cands.add(float(t))
            except ValueError:
                pass
        mult = _MULT.get(unit, 1)
        vals = {round(v * mult, 6) for v in cands} | {round(v, 6) for v in cands}
        out.append((m.group(0).strip(), vals, max(cands) if cands else 0))
    return out


def unknown_numbers(text, source):
    """Türkçe metinde geçip kaynakta karşılığı olmayan sayılar (10 ve üstü; uydurma ya da birim hatası işareti)."""
    src_vals = set()
    for _, vals, _ in _num_values(source):
        src_vals |= vals
    bad = []
    for shown, vals, biggest in _num_values(text):
        if biggest < 10:
            continue
        if not (vals & src_vals):
            bad.append(shown)
    if re.search(r"mph", source, re.I):   # rüzgâr hızı mil olmalı; km'ye çevrilmiş ya da "milyon" yazılmışsa hata
        bad += re.findall(r"\d+\s*(?:kilometre|km)\b[^.]{0,12}|\d+\s*milyon\s+hız\w*", text, re.I)
    return bad


EDITOR_PROMPT = """Sen deneyimli bir Türk haber editörüsün. Sana İngilizce kaynak haberler ve bunlardan hazırlanmış Türkçe başlık/metinler verilecek.
Görevin her Türkçe metni kaynakla karşılaştırıp düzeltmek ve bir Türk okurun gazetede okuyacağı doğallıkta yeniden yazmak.

Kontrol et ve düzelt:
1. Doğruluk: Kaynakta olmayan bilgi, rakam, unvan, tarih varsa çıkar. Rakamlar ve birimler kaynakla birebir aynı olsun.
2. Zaman: Olmamış şeyi olmuş gibi yazma (planlanan/beklenen ≠ yapıldı).
3. Dil: Çeviri kokan, bozuk ya da yarım cümleleri düzelt; doğal, akıcı, kısa Türkçe cümleler kur.
4. İsim kuralları: Kurum ve özel adlar İngilizce kalır; yalnızca yerleşik karşılığı olan unvanlar çevrilir.
Anlamı değiştirme, haberi uzatma. Başlık en fazla 70, metin en fazla 300 karakter.

Çıktı SADECE JSON: {"items":[{"id": verilen numara, "title": "...", "body": "..."}]}"""


# ------------------------- İngilizce hesap için talimatlar ------------------------- #
if EN:
    TERMS = """Names and terms:
- Keep official names exactly as in the source (Home Office, DWP, HMRC, NHS, Ofgem, Met Office, Met Police, Reform UK, Universal Credit, Skilled Worker visa...).
- Use a person's job title ONLY if the source states it; never add a title from your own knowledge (roles change).
- Never guess a name, figure or detail that is not in the source."""

    STYLE = """Writing style (British English, for an Instagram news account):
- Clear, neutral, concise news English in British spelling (organise, programme, labour market, £).
- Titles: a complete headline sentence with subject and verb, sentence case, no clickbait, no ALL CAPS.
- Tense must be right: do not report something planned or expected as if it has already happened.
- Numbers and units exactly as in the source (mph stays mph, £ stays £; "bn" = billion, "m" = million). No rounding, no estimates.
- No opinions, no adjectives the source does not support, no emojis inside title or body.
- Explain jargon briefly only if a general reader would not understand it."""

    SYSTEM_PROMPT = """You are an editor writing a short daily UK news briefing for an English-language Instagram account.
Readers live in the UK or follow UK affairs: they want the stories that affect their money, rights, daily life and the national conversation.

Rules:
- Use ONLY information in the headlines and summaries you are given. Do not invent figures, names, dates or quotes. If unsure, be general.
- Priorities: cost of living / tax / benefits / pensions / housing, immigration and visas, politics and government, health / NHS, economy and business, security, weather and transport, then notable stories.
- At the end, if available, add 1-2 notable sport, entertainment or royal stories (major results for UK teams, big transfers, significant news about well-known British names, official royal news).
- Do NOT select: sexual offences or allegations, child abuse, suicide/self-harm, details of individual murders or violence, unverified gossip and rumours, diet/weight loss.
- Never cover the same event twice.
- category: exactly ONE of: goc (immigration), ekonomi (economy), siyaset (politics), saglik (health), guvenlik (security), hava (weather/environment), spor (sport), magazin (entertainment), kraliyet (royals), dunya (world), gundem (general).
- photo_query: an English search term for a PLACE or OBJECT photo that represents the story (e.g. "UK parliament", "passport stamp", "hospital corridor", "London bus", "pound coins"). No real people's names, no faces in close-up, no violence or crime imagery.

Output: ONLY valid JSON, nothing else:
{"items":[{"ref":number from the list (integer),"emoji":"one emoji","title":"max 70 characters","body":"2 sentences, max 260 characters","source":"source name","category":"category key","photo_query":"2-4 word English photo search term"}],
 "hashtags":["#example", "..."]}
hashtags: 8-12 relevant English hashtags (e.g. #UKNews #London #Britain)."""
    SYSTEM_PROMPT += "\n\n" + TERMS + "\n\n" + STYLE

    USER_TEMPLATE = """Below are today's UK headlines (general news from the last 24 hours, official immigration sources from the last 3 days). Pick the {n} most important stories and produce JSON following the rules. Put each story's list number in "ref".

{listing}"""

    BREAKING_PROMPT = """You are a news editor for an English-language UK news Instagram account. Decide whether any of the NEW headlines below truly deserves a breaking-news post.

Counts as breaking (new, confirmed developments that matter to a wide audience):
- New immigration, visa, settlement, citizenship or asylum rules, decisions or official announcements; major migration data
- Major developments involving the Prime Minister, ministers or the government (resignation, sacking, reshuffle, early election, major party splits)
- Economy: official inflation, interest rate or unemployment data; Budget and tax decisions; energy price cap
- Official changes to benefits, pensions, housing, rents or fares that affect many people
- Big company news: job losses affecting over 1,000 people, collapse of a major company, nationwide service outage
- NHS strikes, national health alerts or major health policy decisions
- Nationwide transport, strike, border or airport disruption
- Amber or red weather warnings, named storms, major flooding, national emergencies
- Major national security or defence developments (official information only, no speculation)
- Important court rulings affecting government policy
- Official royal announcements or major royal news (births, engagements, health statements, historic visits, title changes)
- Sport: major results for the England, Scotland or Wales national teams, cup finals, very big Premier League news (record transfer, manager departure/appointment, title win)
- Entertainment: major confirmed news about very well-known British names (major award, death of a famous figure, big tour/film announcement)

NOT breaking: opinion and columns, polls, speculation and leaks, routine political rows, individual crime, investigations and court cases, sexual offence allegations (about anyone), suicide, unverified gossip and rumours, routine match reports and transfer rumours, local news.
Several breaking posts a day are possible; the bar is high but reasonable. If unsure, do not post.

Rules: use ONLY information in the headline and summary; do not invent figures, names or quotes. photo_query: English search term for a PLACE or OBJECT; no names, faces or violence.

Output ONLY JSON:
If nothing qualifies: {"breaking": false}
Otherwise: {"breaking": true, "ref": list number, "emoji": "one emoji", "title": "max 70 characters", "body": "2-3 sentences, max 300 characters", "source": "source name", "category": "goc|ekonomi|siyaset|saglik|guvenlik|hava|spor|magazin|kraliyet|dunya|gundem", "photo_query": "2-4 word English photo search term", "hashtags": ["#BreakingNews", "..."]}"""
    BREAKING_PROMPT += "\n\n" + TERMS + "\n\n" + STYLE

    EDITOR_PROMPT = """You are an experienced British news sub-editor. You will get English source stories and the headline/body written from each.
Check each one against its source and rewrite it so it reads like a quality UK news outlet.

Check and fix:
1. Accuracy: remove any fact, figure, job title or date that is not in the source. Figures and units must match the source exactly.
2. Tense: do not report planned or expected events as done.
3. Language: clear, natural, concise British English; fix clumsy or incomplete sentences.
Do not change the meaning or lengthen the story. Title max 70, body max 300 characters.

Output ONLY JSON: {"items":[{"id": given number, "title": "...", "body": "..."}]}"""


def polish_items(items, sources, note=""):
    """Türkçe metinleri kaynakla karşılaştırıp WRITER_MODEL ile düzeltir. sources: her öğe için kaynak metin."""
    if not items or not os.environ.get("ANTHROPIC_API_KEY"):
        return items
    import anthropic

    blocks = "\n\n".join((f"### {i}\nSOURCE: {src}\nHEADLINE: {it['title']}\nBODY: {it['body']}" if EN else
                           f"### {i}\nKAYNAK: {src}\nTÜRKÇE BAŞLIK: {it['title']}\nTÜRKÇE METİN: {it['body']}")
                          for i, (it, src) in enumerate(zip(items, sources), 1))
    try:
        msg = anthropic.Anthropic().messages.create(
            model=WRITER_MODEL, max_tokens=3000,
            system=EDITOR_PROMPT + "\n\n" + TERMS + "\n\n" + STYLE,
            messages=[{"role": "user", "content": (note + "\n\n" if note else "") + blocks}],
        )
        out = parse_json("".join(b.text for b in msg.content if getattr(b, "type", "") == "text"))
    except Exception as e:
        log(f"[editör] Düzeltme yapılamadı, ilk metin kullanılıyor: {e}")
        return items
    for o in out.get("items", []):
        try:
            it = items[int(o["id"]) - 1]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        t, b = clean(o.get("title", ""))[:90], clean(o.get("body", ""))[:320]
        if t and b:
            it["title"], it["body"] = t, b
    return items


def verify_items(items, sources):
    """Editör sonrası rakam kontrolü: kaynakta olmayan rakam kalırsa bir kez daha düzelttirir, yine kalırsa haberi eler."""
    items = polish_items(items, sources)
    bad = {i: unknown_numbers(it["title"] + " " + it["body"], src) for i, (it, src) in enumerate(zip(items, sources))}
    bad = {i: b for i, b in bad.items() if b}
    if bad:
        idx = list(bad)
        note = L("DİKKAT: Aşağıdaki metinlerde kaynakta OLMAYAN rakamlar var, bunları kaynağa göre düzelt ya da çıkar: ",
                 "WARNING: these texts contain figures that are NOT in the source; correct them from the source or remove them: ") + "; ".join(
            f"{n + 1}. {L('metin', 'text')}: {', '.join(bad[i])}" for n, i in enumerate(idx))
        sub = polish_items([items[i] for i in idx], [sources[i] for i in idx], note)
        for i, it in zip(idx, sub):
            items[i] = it
    keep = []
    for it, src in zip(items, sources):
        left = unknown_numbers(it["title"] + " " + it["body"], src)
        if left:
            log(f"[doğrulama] Kaynakta olmayan rakam ({', '.join(left)}) nedeniyle elendi: {it['title']}")
            continue
        keep.append(it)
    return keep


def same_story(title, earlier):
    """Başlıktaki özel isimlerden (büyük harfle başlayan kelimeler) en az ikisi daha önceki bir başlıkta geçiyorsa aynı olay say."""
    caps = lambda t: {w.lower() for w in re.findall(r"\b[A-ZÇĞİÖŞÜ][\wçğıöşü']{2,}", t)}
    common = {"ingiltere", "i̇ngiltere", "birleşik", "krallık", "londra", "başbakan", "hükümet", "bakan", "son", "yeni",
              "the", "britain", "british", "london", "england", "government", "minister", "new", "after", "over"}
    mine = caps(title) - common
    return any(len(mine & (caps(e) - common)) >= 2 for e in earlier)


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
    fb = font("bold", 23)                     # hesap adı küçük ve sade
    tw = d.textlength(BRAND_HANDLE, font=fb)
    d.text((W - MARGIN - tw, y + 35), BRAND_HANDLE, font=fb, fill=MUTED)


def chip(d, x, y, text, color, size=28, pad_x=22, pad_y=12, fg=WHITE):
    """Yuvarlak köşeli renkli etiket; sağ kenarının x'ini döndürür."""
    f = font("bold", size)
    tw = d.textlength(text, font=f)
    h = size + 2 * pad_y
    d.rounded_rectangle([x, y, x + tw + 2 * pad_x, y + h], radius=h // 2, fill=color)
    d.text((x + pad_x, y + pad_y - 2), text, font=f, fill=fg)
    return x + tw + 2 * pad_x


# ------------------------- Sade (modern) kart tasarımı ------------------------- #
# Tarih/saat ve kategori etiketi kartlarda YOK. Renk yalnızca ince vurgu çizgisinde.
def clean_canvas(w=None, h=None):
    from PIL import ImageDraw

    img = gradient_canvas(w or W, h or H)
    return img, ImageDraw.Draw(img)


def tracked(d, xy, text, fnt, fill, spacing=4):
    """Harf aralığı açılmış küçük başlık (ör. MORNING BRIEFING)."""
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=fnt, fill=fill)
        x += d.textlength(ch, font=fnt) + spacing
    return x


def bleed_photo(img, photo, h, width=None):
    """Fotoğrafı kartın üst kısmına kenardan kenara yerleştirir."""
    from PIL import Image, ImageDraw, ImageOps

    w = width or img.width
    img.paste(ImageOps.fit(photo, (w, h), method=Image.LANCZOS, centering=(0.5, 0.45)), (0, 0))
    return img, ImageDraw.Draw(img)


def numbered_list(d, items, y, bottom, max_w, sizes, num_w):
    """Kapak ve hikâyedeki numaralı başlık listesi, aralarında ince çizgilerle."""
    for size in sizes:
        f_num, f_t = font("bold", size), font("regular", size)
        lh, gap = int(size * 1.3), int(size * 0.9)
        blocks = [wrap(d, no_emoji(it["title"]), f_t, max_w - num_w)[:2] for it in items]
        total = sum(lh * len(ls) + 2 * gap for ls in blocks)
        if y + total <= bottom:
            break
    for i, ls in enumerate(blocks, 1):
        y += gap
        d.text((MARGIN, y), f"{i:02d}", font=f_num, fill=MUTED)
        y = draw_lines(d, MARGIN + num_w, y, ls, f_t, lh, INK) + gap
        if i < len(blocks):
            d.line([MARGIN + num_w, y, MARGIN + max_w, y], fill=PANEL, width=2)
    return y


def render_cover(items, day, path):
    img, d = clean_canvas()
    max_w = W - 2 * MARGIN
    y = 120
    tracked(d, (MARGIN, y), COVER_LABEL, font("bold", 26), MUTED)
    y += 70
    fnt, lines, lh = fit_text(d, BRAND_TITLE, "bold", max_w, 260, 104, 64, 1.08)
    y = draw_lines(d, MARGIN, y, lines, fnt, lh, INK) + 36
    d.rectangle([MARGIN, y, MARGIN + 90, y + 6], fill=INK)
    y += 50
    numbered_list(d, items, y, H - 150, max_w, range(36, 23, -2), 70)
    footer(d, L("Detaylar için kaydırın  →", "Swipe for details  →"))
    img.save(path, "JPEG", quality=92, optimize=True)


def upper_tr(s):
    if EN:
        return s.upper()
    return s.replace("i", "İ").replace("ı", "I").upper()


PHOTO_H = 520  # kartlarda üstteki fotoğrafın yüksekliği


def _news_card(it, path, photo, accent, top_text, top_color, pill=None):
    img, d = clean_canvas()
    if photo is not None:
        img, d = bleed_photo(img, photo, PHOTO_H)
        y = PHOTO_H + 56
    else:
        y = 150
    if top_text:
        f = font("bold", 26)
        tracked(d, (MARGIN, y), top_text, f, top_color, spacing=3)
        y += 62
    draw_story(d, it, y, H - 150, title_start=64 if photo is not None else 72, accent=accent,
               label=pill, label_color=BREAKING, center=False)
    footer(d, f"{L('Kaynak', 'Source')}: {it['source']}" if it.get("source") else BRAND_TITLE)
    img.save(path, "JPEG", quality=92, optimize=True)


def render_item(it, idx, total, path, photo=None, label=None):
    _news_card(it, path, photo, cat_of(it)[1], f"{idx:02d} / {total:02d}", MUTED)


def render_breaking(it, when, path, photo=None):
    _news_card(it, path, photo, BREAKING, None, None, pill=L("SON DAKİKA", "BREAKING"))


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


def draw_story(d, it, area_top, area_bottom, title_start=72, accent=ACCENT, width=None, label=None, label_color=None,
               center=True):
    """Başlık + renkli çizgi + gövde metnini verilen alana sığdırıp dikeyde ortalar."""
    max_w = (width or W) - 2 * MARGIN
    t_max = min(420, int((area_bottom - area_top) * 0.5))
    t_fnt, t_lines, t_lh = fit_text(d, no_emoji(it["title"]), "bold", max_w, t_max, title_start, 40, 1.15)
    title_h = t_lh * len(t_lines)
    gap = 30 + 8 + 50
    chip_h = 80 if label else 0          # kategori etiketi başlığın hemen üstünde
    area_top += chip_h
    b_fnt, b_lines, b_lh = fit_text(d, no_emoji(it["body"]), "regular", max_w,
                                    (area_bottom - area_top) - title_h - gap, 50, 28, 1.4)
    block_h = title_h + gap + b_lh * len(b_lines)

    y = area_top + (max(0, (area_bottom - area_top - block_h) // 2) if center else 0)
    if label:
        chip(d, MARGIN, y - chip_h, label, label_color or accent)
    y = draw_lines(d, MARGIN, y, t_lines, t_fnt, t_lh, INK)
    y += 30
    d.rectangle([MARGIN, y, MARGIN + 80, y + 6], fill=accent)
    y += 56
    draw_lines(d, MARGIN, y, b_lines, b_fnt, b_lh, BODY)


def build_breaking_caption(it, tags, when):
    first = L("#SonDakika", "#BreakingNews")
    tags = ([first] + [t for t in tags if t.lower() != first.lower()])[:30]
    text = "\n".join([
        f"🔴 {L('SON DAKİKA', 'BREAKING')} | {it['emoji']} {it['title']}",
        "",
        it["body"],
        "",
        (f"{L('Kaynak', 'Source')}: {it['source']}" if it.get("source") else "")
        + (f" | {L('Görsel', 'Image')}: {it['photo'].get('provider', 'Pexels')}" if it.get("photo") else ""),
        f"{BRAND_TITLE} | {stamp_of(when, year=False)}",
        "",
        " ".join(tags),
    ])
    return text[:2200].strip()


SW, SH = 1080, 1920  # hikâye (story) formatı 9:16
STORY_SAFE_TOP, STORY_SAFE_BOTTOM = 250, SH - 260  # Instagram'ın üst/alt arayüzünün kapattığı alanlar


def story_canvas(accent=INK):
    return clean_canvas(SW, SH)


def story_cta(d, line1=None, line2=None, accent=INK):
    """Alttaki yönlendirme kutusu (API ile link/çıkartma eklenemediği için)."""
    line1 = line1 or L("Tüm haberler ve detaylar", "All stories and details")
    line2 = line2 or L("profildeki son gönderide", "in the latest post on our profile")
    top, bottom = STORY_SAFE_BOTTOM - 230, STORY_SAFE_BOTTOM
    d.rounded_rectangle([MARGIN, top, SW - MARGIN, bottom], radius=28, fill=INK)
    f1, f2, f3 = font("bold", 44), font("regular", 36), font("bold", 28)
    d.text((MARGIN + 48, top + 38), line1, font=f1, fill=WHITE)
    d.text((MARGIN + 48, top + 100), line2, font=f2, fill=(196, 204, 220))
    d.text((MARGIN + 48, top + 158), BRAND_HANDLE, font=f3, fill=WHITE)


def render_story(items, day, path):
    img, d = story_canvas()
    max_w = SW - 2 * MARGIN
    y = STORY_SAFE_TOP
    tracked(d, (MARGIN, y), COVER_LABEL, font("bold", 30), MUTED)
    y += 80
    fnt, lines, lh = fit_text(d, BRAND_TITLE, "bold", max_w, 260, 108, 64, 1.06)
    y = draw_lines(d, MARGIN, y, lines, fnt, lh, INK) + 40
    d.rectangle([MARGIN, y, MARGIN + 90, y + 6], fill=INK)
    y += 50
    shown = items[:6]
    y = numbered_list(d, shown, y, STORY_SAFE_BOTTOM - 330, max_w, range(44, 27, -2), 80)
    if len(items) > len(shown):
        d.text((MARGIN + 80, y + 20), L(f"+ {len(items) - len(shown)} haber daha", f"+ {len(items) - len(shown)} more stories"),
               font=font("regular", 34), fill=MUTED)
    story_cta(d)
    img.save(path, "JPEG", quality=92, optimize=True)


def render_breaking_story(it, when, path, photo=None):
    img, d = story_canvas()
    y = STORY_SAFE_TOP
    if photo is not None:
        img, d = bleed_photo(img, photo, 760, SW)
        y = 820
    draw_story(d, it, y, STORY_SAFE_BOTTOM - 270, title_start=64, accent=BREAKING, width=SW,
               label=L("SON DAKİKA", "BREAKING"), label_color=BREAKING, center=False)
    story_cta(d, L("Ayrıntılar", "Full story"), L("profildeki son gönderide", "in the latest post on our profile"))
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
    src_txt = ", ".join(sources) if sources else L("İngiliz basını", "UK media")
    head = f"🇬🇧 {BRAND_TITLE} | {date_tr(day)}\n\n"
    provs = sorted({it["photo"].get("provider", "Pexels") for it in items if it.get("photo")})
    photo_note = f" {L('Görseller', 'Images')}: {', '.join(provs)}." if provs else ""
    foot = (f"\n{L('Kaynaklar', 'Sources')}: {src_txt}. "
            f"{L('Haberler Türkçe özet olarak derlenmiştir.', 'Stories summarised from UK media reports.')}{photo_note}\n\n"
            + " ".join(tags[:30]))

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
    if source_items:
        srcs = [(lambda s_: f"{s_['title']} — {s_['summary']}")(source_items[it["ref"] - 1])
                if it.get("ref") and 1 <= it["ref"] <= len(source_items) else "" for it in items]
        pairs = [(it, s_) for it, s_ in zip(items, srcs) if s_]
        items = verify_items([p[0] for p in pairs], [p[1] for p in pairs])
    items = [it for it in items if ensure_turkish(it)]
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
    media_id = publish_carousel(urls, post["caption"])
    record = {"media_id": media_id, "published_at": dt.datetime.now(TZ).isoformat()}
    (out / "published.json").write_text(json.dumps(record, indent=2))
    save_history(post.get("history_keys", []))
    log(f"Yayınlandı. Instagram medya ID: {media_id}")

    story_id = publish_story(post, base)
    if story_id:
        record["story_id"] = story_id
        (out / "published.json").write_text(json.dumps(record, indent=2))


def publish_carousel(urls, caption):
    """Görsel adreslerinden Instagram carousel postu oluşturup yayınlar, medya ID'sini döndürür."""
    wait_public(urls)
    ig_user = os.environ["IG_USER_ID"]
    children = []
    for url in urls:
        cid = graph("POST", f"{ig_user}/media", image_url=url, is_carousel_item="true")["id"]
        wait_ready(cid)
        children.append(cid)
    carousel = graph("POST", f"{ig_user}/media", media_type="CAROUSEL",
                     children=",".join(children), caption=caption)["id"]
    wait_ready(carousel)
    return graph("POST", f"{ig_user}/media_publish", creation_id=carousel)["id"]


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
    if not os.environ.get("IG_USER_ID", "").strip():
        sys.exit("IG_USER_ID tanımlı değil" + L(".", " (İngilizce hesap için GitHub Secret adı: IG_USER_ID_EN)."))
    if EN and not EXPECTED_IG_USERNAME:
        sys.exit("İngilizce hesapta güvenlik için EXPECTED_IG_USERNAME_EN değişkeni zorunlu (ör. emresterlin).")
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
        log(f"SONUÇ: Token {len(ok)} Instagram hesabına erişebiliyor:")
        for ig_id, user, page in ok:
            log(f"   @{user} (Facebook Sayfası: {page})  →  IG_USER_ID = {ig_id}")
        log("       Türkçe hesap için: Secret IG_USER_ID, Variable EXPECTED_IG_USERNAME")
        log("       İngilizce hesap için: Secret IG_USER_ID_EN, Variable EXPECTED_IG_USERNAME_EN")
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
        already = [j.get("item", {}).get("title", "") for _, j in posts if j.get("published")]
        data = judge_breaking(new_items, already)
        save_keys(CHECKED_FILE, [title_key(i["title"]) for i in new_items], keep=3000)

    if not data.get("breaking"):
        log(f"{len(new_items)} yeni başlık değerlendirildi, son dakika niteliğinde haber yok.")
        return
    items, tags = normalize({"items": [data], "hashtags": data.get("hashtags", [])}, 1)
    if items and new_items and items[0].get("ref") and 1 <= items[0]["ref"] <= len(new_items):
        src = new_items[items[0]["ref"] - 1]
        items = verify_items(items, [f"{src['title']} — {src['summary']}"])
    if not items or not ensure_turkish(items[0]):
        return
    it = items[0]
    earlier = [j.get("item", {}).get("title", "") for _, j in posts]
    if same_story(it["title"], earlier) and not FORCE:
        log(f"Bugün aynı olay zaten paylaşıldı, tekrar paylaşılmıyor: {it['title']}")
        return
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



# --------------------------------------------------------------------------- #
# GOV.UK günlük rehber postu (öğlen)
# --------------------------------------------------------------------------- #
GUIDE_HOUR, GUIDE_MINUTE = (int(x) for x in os.environ.get("GUIDE_TIME_LONDON", "12:30").split(":"))
GUIDE_START = dt.date(2026, 9, 11)   # e-postadaki "GOV.UK Günlük Bilgi Notu" ile aynı sıralama
GUIDE_CATEGORIES = [                 # (GOV.UK yolu, kart etiketi, kategori rengi anahtarı)
    ("benefits", "SOSYAL YARDIMLAR", "ekonomi"),
    ("births-deaths-marriages", "DOĞUM, ÖLÜM, EVLİLİK", "gundem"),
    ("childcare-parenting", "ÇOCUK BAKIMI VE EBEVEYNLİK", "saglik"),
    ("citizenship", "VATANDAŞLIK", "goc"),
    ("driving", "SÜRÜCÜLÜK VE ULAŞIM", "hava"),
    ("education", "EĞİTİM", "spor"),
    ("employing-people", "İŞÇİ ÇALIŞTIRMA", "magazin"),
    ("housing-local-services", "KONUT VE YEREL HİZMETLER", "kraliyet"),
    ("tax", "VERGİ", "siyaset"),
    ("abroad", "YURT DIŞI", "dunya"),
    ("visas-immigration", "VİZE VE GÖÇ", "goc"),
    ("working", "ÇALIŞMA HAYATI", "guvenlik"),
]
GOVUK = "https://www.gov.uk"
GUIDE_FORMATS = {"guide", "answer", "transaction", "simple_smart_answer", "local_transaction", "place", "calculator"}


def govuk_json(path, **params):
    r = requests.get(f"{GOVUK}{path}", params=params, timeout=30,
                     headers={"User-Agent": "Mozilla/5.0 (uk-gundem-bot)", "Accept": "application/json"})
    r.raise_for_status()
    return r.json()


def guide_plan(day):
    """Bugünün kategorisi ve 2 alt başlığı: e-postadaki görevle aynı tarih formülü."""
    diff = max(0, (day - GUIDE_START).days)
    slug, label, color_key = GUIDE_CATEGORIES[diff % len(GUIDE_CATEGORIES)]
    cycle = diff // len(GUIDE_CATEGORIES)
    page = govuk_json(f"/api/content/browse/{slug}")
    links = page.get("links", {})
    subs = (links.get("ordered_second_level_browse_pages") or links.get("second_level_browse_pages")
            or links.get("children") or [])
    subs = [x for x in subs if x.get("base_path")]
    if not subs:
        raise RuntimeError(f"GOV.UK alt başlıkları bulunamadı: /browse/{slug}")
    start = (cycle * 2) % len(subs)
    picked = [subs[(start + i) % len(subs)] for i in range(min(2, len(subs)))]
    return slug, label, color_key, picked


def html_text(html_s):
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html_s or "", flags=re.S | re.I)
    t = re.sub(r"</(p|li|h\d|tr)>", "\n", t, flags=re.I)
    return re.sub(r"[ \t]+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t))).strip()


def item_body(content):
    det = content.get("details", {})
    chunks = []
    for part in det.get("parts", []) or []:
        chunks.append(f"## {part.get('title', '')}\n" + html_text(part.get("body", "")))
    for key in ("body", "introductory_paragraph", "introduction", "more_information", "need_to_know"):
        val = det.get(key)
        if isinstance(val, str):
            chunks.append(html_text(val))
        elif isinstance(val, list):
            chunks.extend(html_text(v.get("content", "")) for v in val if isinstance(v, dict))
    return "\n\n".join(c for c in chunks if c.strip())


def fetch_guide(sub):
    """Alt başlıktaki ilk gerçek rehber sayfasını bulur ve metnini getirir."""
    bp = sub["base_path"].removeprefix("/browse/")
    res = govuk_json("/api/search.json", filter_mainstream_browse_pages=bp, count=20,
                     fields=["title", "link", "format", "description"])
    results = [r for r in res.get("results", []) if r.get("format") in GUIDE_FORMATS] or res.get("results", [])
    for r in results:
        try:
            c = govuk_json(f"/api/content{r['link']}")
        except Exception:
            continue
        text = item_body(c)
        if len(text) > 300:
            return {"sub_title": sub.get("title", ""), "title": c.get("title", r.get("title", "")),
                    "description": c.get("description", ""), "url": GOVUK + r["link"], "text": text[:7000]}
    raise RuntimeError(f"Okunabilir rehber bulunamadı: {sub.get('base_path')}")


GUIDE_PROMPT = """Sen İngiltere'de yaşayan Türk topluluğu için Instagram'da İNFOGRAFİK içerik hazırlayan bir editörsün.
Sana GOV.UK'den alınmış iki resmi rehber metni verilecek. Her rehber için iki infografik kartın içeriğini hazırla.

Kurallar:
- YALNIZCA verilen GOV.UK metnindeki bilgileri kullan. Rakam, tarih, ücret, şart UYDURMA; metinde yoksa yazma.
- İnfografik dili: kısa, net, taranabilir ifadeler. Tam cümle şart değil; her madde en fazla 55 karakter.
- "key_fact": metindeki en dikkat çekici tek rakam/bilgi (ör. "£4,000", "8 hafta", "18 yaş"); "value" en fazla 12 karakter,
  "label" en fazla 70 karakter. Metinde uygun rakam yoksa key_fact'i null yap.
- "sections": TAM 4 kutu. Her kutuda "icon" (şunlardan biri: kim, sart, para, sure, belge, bilgi, basvuru, ev, iletisim,
  hukuk, saglik, seyahat), "heading" (en fazla 28 karakter) ve 2-3 "points" (her biri en fazla 55 karakter).
  Uygun başlıklar: Nedir?, Kimler yararlanabilir?, Şartlar, Ne kadar?, Ne kadar sürer?, Gerekli belgeler, Dikkat edin.
- "steps": nasıl başvurulur/ne yapılır adımları, 3-5 adım; her adımda "heading" (en fazla 32 karakter) ve "text" (en fazla 100 karakter).
  Metinde süreç anlatılmıyorsa GOV.UK'deki bilgiye dayanan mantıklı sıralı adımlar çıkar, ama bilgi uydurma.
- "warning": okuyucunun bilmesi gereken tek önemli uyarı (en fazla 140 karakter) ya da null.
- "title": en fazla 60 karakter, ilgi çekici ama abartısız. "summary": açıklama metni için 2-3 cümle.
- Dil sade Türkçe. Hukuki tavsiye verme.

Çıktı SADECE JSON:
{"guides":[{"title":"...","summary":"...","key_fact":{"value":"...","label":"..."},
  "sections":[{"icon":"kim","heading":"...","points":["...","..."]}, ... 4 adet],
  "steps":[{"heading":"...","text":"..."}, ...],
  "warning":"..."}],
 "hashtags":["#İngiltere","..."]}
hashtags: 8-12 adet, ilgili Türkçe ve İngilizce etiketler."""
GUIDE_PROMPT += "\n\n" + TERMS


def summarize_guides(guides, label):
    import anthropic

    blocks = "\n\n".join(f"### REHBER {i + 1}: {g['title']} ({g['url']})\nAlt başlık: {g['sub_title']}\n"
                         f"Açıklama: {g['description']}\n\n{g['text']}" for i, g in enumerate(guides))
    msg = anthropic.Anthropic().messages.create(
        model=WRITER_MODEL, max_tokens=4000, system=GUIDE_PROMPT + "\n\n" + STYLE,
        messages=[{"role": "user", "content": f"Bugünün kategorisi: {label}\n\n{blocks}"}],
    )
    return parse_json("".join(b.text for b in msg.content if getattr(b, "type", "") == "text"))


def guide_too_early():
    now = dt.datetime.now(TZ)
    if FORCE or (now.hour, now.minute) >= (GUIDE_HOUR, GUIDE_MINUTE):
        return False
    log(f"Londra saati {now:%H:%M}, rehber postu {GUIDE_HOUR:02d}:{GUIDE_MINUTE:02d}'da. Henüz erken.")
    return True


def render_guide_cover(gs, label, color, day, path):
    img, d = base_canvas(color)
    max_w = W - 2 * MARGIN
    y = 110
    x = chip(d, MARGIN, y, "GÜNÜN REHBERİ", INK, size=28)
    chip(d, x + 14, y, label, color, size=28)
    y += 100
    fnt, lines, lh = fit_text(d, "İngiltere'de Bilmeniz Gerekenler", "bold", max_w, 260, 88, 56, 1.12)
    y = draw_lines(d, MARGIN, y, lines, fnt, lh, INK) + 40
    d.rounded_rectangle([MARGIN, y, MARGIN + 140, y + 8], radius=4, fill=color)
    y += 60
    for i, g in enumerate(gs, 1):
        d.text((MARGIN, y), f"{i:02d}", font=font("bold", 44), fill=color)
        fnt, lines, lh = fit_text(d, no_emoji(g["title"]), "bold", max_w - 90, 150, 44, 32, 1.2)
        y = draw_lines(d, MARGIN + 90, y, lines, fnt, lh, INK) + 14
        sf = font("regular", 30)
        y = draw_lines(d, MARGIN + 90, y, wrap(d, no_emoji(g["summary"]), sf, max_w - 90)[:3], sf, 42, BODY) + 44
    footer(d, "Kaydırın  →   Kaynak: GOV.UK")
    img.save(path, "JPEG", quality=92, optimize=True)


def title_tr(s):
    """Türkçe kurallarıyla kelime başlarını büyük yapar (VERGİ -> Vergi, İŞÇİ -> İşçi)."""
    low = s.replace("I", "ı").replace("İ", "i").lower()
    small = {"ve", "ile"}
    return " ".join(w if w in small else (("İ" if w[0] == "i" else w[0].upper()) + w[1:]) for w in low.split())


def build_guide_caption(gs, tags, label, day):
    parts = [f"📘 Günün Rehberi | {title_tr(label)} | {day.day} {MONTHS_TR[day.month - 1]} {day.year}", ""]
    for i, g in enumerate(gs, 1):
        parts += [f"{i}. {g['title']}", g["summary"], f"Kaynak: {g['url']}", ""]
    provs = sorted({g["photo"].get("provider", "Pexels") for g in gs if g.get("photo")})
    parts.append("Bilgiler GOV.UK'den derlenmiştir (Open Government Licence). Kişisel durumunuz için resmi sayfayı kontrol edin."
                 + (f" Görseller: {', '.join(provs)}." if provs else ""))
    parts += ["", " ".join(tags[:30])]
    return "\n".join(parts)[:CAPTION_LIMIT].strip()



ICONS = {"kim": "☺", "sart": "✓", "para": "£", "sure": "◷", "belge": "▤", "bilgi": "i", "basvuru": "✎",
         "ev": "⌂", "iletisim": "☎", "hukuk": "⚖", "saglik": "♥", "seyahat": "✈", "uyari": "!"}


def tint(color, a):
    """Rengi beyazla karıştırır (a=0.1 çok açık ton)."""
    return tuple(int(255 + (c - 255) * a) for c in color)


def icon_circle(d, cx, cy, r, key, color):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    g = ICONS.get(key, "i")
    f = font("bold", int(r * 1.05))
    bb = d.textbbox((0, 0), g, font=f)
    d.text((cx - (bb[0] + bb[2]) / 2, cy - (bb[1] + bb[3]) / 2), g, font=f, fill=WHITE)


def fit_lines(d, text, kind, max_w, max_lines, start, minimum):
    size = start
    while True:
        f = font(kind, size)
        lines = wrap(d, no_emoji(text), f, max_w)
        if len(lines) <= max_lines or size <= minimum:
            if len(lines) > max_lines:
                lines = lines[:max_lines]
                lines[-1] = lines[-1].rstrip(".,;: ") + "…"
            return f, lines, int(size * 1.28)
        size -= 2


def info_header(d, color, page, total, chip_text):
    top = 62
    d.text((MARGIN, top), f"{page:02d} / {total:02d}", font=font("bold", 32), fill=color)
    brand = upper_tr(BRAND_TITLE)
    fl = font("regular", 26)
    d.text((W - MARGIN - d.textlength(brand, font=fl), top + 5), brand, font=fl, fill=MUTED)
    chip(d, MARGIN, top + 70, chip_text, color, size=26)
    return top + 140


def render_info_overview(g, gi, label, color, page, total, path):
    img, d = base_canvas(color)
    y = info_header(d, color, page, total, f"REHBER {gi} · {label}")
    max_w = W - 2 * MARGIN
    f, lines, lh = fit_lines(d, g["title"], "bold", max_w, 2, 54, 38)
    y = draw_lines(d, MARGIN, y, lines, f, lh, INK) + 22

    kf = g.get("key_fact")
    if kf and kf.get("value"):
        h = 150
        d.rounded_rectangle([MARGIN, y, W - MARGIN, y + h], radius=28, fill=tint(color, 0.13))
        fv, vlines, _ = fit_lines(d, kf["value"], "bold", 360, 1, 76, 44)
        vw = d.textlength(vlines[0], font=fv)
        vb = d.textbbox((0, 0), vlines[0], font=fv)
        d.text((MARGIN + 36, y + (h - (vb[3] - vb[1])) / 2 - vb[1]), vlines[0], font=fv, fill=color)
        lx = MARGIN + 36 + vw + 32
        fl, llines, llh = fit_lines(d, kf.get("label", ""), "regular", W - MARGIN - 30 - lx, 3, 30, 22)
        draw_lines(d, lx, y + (h - llh * len(llines)) / 2, llines, fl, llh, BODY)
        y += h + 26

    # 2x2 bilgi kutuları
    gap = 24
    bottom = H - 140
    bw = (max_w - gap) // 2
    bh = (bottom - y - gap) // 2
    for i, sec in enumerate(g["sections"][:4]):
        bx = MARGIN + (i % 2) * (bw + gap)
        by = y + (i // 2) * (bh + gap)
        d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=26, fill=(252, 250, 246), outline=tint(color, 0.35), width=3)
        icon_circle(d, bx + 54, by + 56, 30, sec.get("icon", "bilgi"), color)
        fh, hl, hlh = fit_lines(d, sec.get("heading", ""), "bold", bw - 120, 2, 30, 22)
        draw_lines(d, bx + 98, by + 56 - hlh * len(hl) / 2, hl, fh, hlh, INK)
        py = by + 110
        avail = bh - 125
        pts = [p for p in sec.get("points", []) if p][:3]
        for size in range(31, 19, -1):
            fp = font("regular", size)
            plh = int(size * 1.3)
            wrapped = [wrap(d, no_emoji(p), fp, bw - 74) for p in pts]
            need = sum(len(w) * plh + 10 for w in wrapped)
            if need <= avail:
                break
        for wl in wrapped:
            if py + plh > by + bh - 12:
                break
            d.ellipse([bx + 30, py + plh / 2 - 6, bx + 42, py + plh / 2 + 6], fill=color)
            for ln in wl:
                if py + plh > by + bh - 12:
                    break
                d.text((bx + 56, py), ln, font=fp, fill=BODY)
                py += plh
            py += 10
    footer(d, "Kaynak: GOV.UK")
    img.save(path, "JPEG", quality=92, optimize=True)


def render_info_steps(g, gi, label, color, page, total, url, path):
    img, d = base_canvas(color)
    y = info_header(d, color, page, total, f"NASIL YAPILIR? · REHBER {gi}")
    max_w = W - 2 * MARGIN
    f, lines, lh = fit_lines(d, g["title"], "bold", max_w, 2, 46, 34)
    y = draw_lines(d, MARGIN, y, lines, f, lh, INK) + 30

    warn = g.get("warning")
    warn_h = 0
    if warn:
        fw, wl, wlh = fit_lines(d, warn, "regular", max_w - 130, 3, 28, 22)
        warn_h = max(110, wlh * len(wl) + 44)
    bottom = H - 140 - (warn_h + 26 if warn else 0)
    steps = [s_ for s_ in g["steps"] if s_.get("heading") or s_.get("text")][:5]
    slot = (bottom - y) / max(1, len(steps))
    r = 34
    cx = MARGIN + r
    # bağlantı çizgisi
    if len(steps) > 1:
        d.line([cx, y + r, cx, y + slot * (len(steps) - 1) + r], fill=tint(color, 0.4), width=6)
    for i, st in enumerate(steps, 1):
        sy = y + (i - 1) * slot
        d.ellipse([cx - r, sy, cx + r, sy + 2 * r], fill=color)
        fnum = font("bold", 34)
        nb = d.textbbox((0, 0), str(i), font=fnum)
        d.text((cx - (nb[0] + nb[2]) / 2, sy + r - (nb[1] + nb[3]) / 2), str(i), font=fnum, fill=WHITE)
        tx = MARGIN + 2 * r + 30
        fh, hl, hlh = fit_lines(d, st.get("heading", ""), "bold", W - MARGIN - tx, 1, 32, 24)
        ty = draw_lines(d, tx, sy + 6, hl, fh, hlh, INK) + 4
        room = max(1, int((slot - (ty - sy) - 14) // 34))
        fb, bl, blh = fit_lines(d, st.get("text", ""), "regular", W - MARGIN - tx, min(3, room), 27, 21)
        draw_lines(d, tx, ty, bl, fb, blh, BODY)
    if warn:
        wy = H - 140 - warn_h - 10
        d.rounded_rectangle([MARGIN, wy, W - MARGIN, wy + warn_h], radius=26, fill=tint(BREAKING, 0.12))
        icon_circle(d, MARGIN + 56, wy + warn_h / 2, 28, "uyari", BREAKING)
        draw_lines(d, MARGIN + 110, wy + (warn_h - wlh * len(wl)) / 2, wl, fw, wlh, INK)
    short = url.replace("https://www.", "")
    footer(d, f"Ayrıntılar: {short}"[:48])
    img.save(path, "JPEG", quality=92, optimize=True)


def cmd_guide(args):
    if EN:
        log("Rehber postları yalnızca Türkçe hesap için; İngilizce hesapta atlanıyor.")
        return
    day = today_london()
    out = OUT_ROOT / day.isoformat() / "rehber"
    if (out / "published.json").exists():
        log("Bugünün rehber postu zaten yayınlanmış.")
        return
    if guide_too_early():
        return
    slug, label, color_key, subs = guide_plan(day)
    color = CATEGORIES[color_key][1]
    log(f"Bugünün kategorisi: {slug} | alt başlıklar: {', '.join(s.get('title', '') for s in subs)}")
    sources = [fetch_guide(sub) for sub in subs]
    data = summarize_guides(sources, label)
    gs = []
    for src, g in zip(sources, data.get("guides", [])):
        secs = [{"icon": clean(x.get("icon", "bilgi")).lower(), "heading": clean(x.get("heading", ""))[:40],
                 "points": [clean(p)[:80] for p in (x.get("points") or []) if clean(p)][:3]}
                for x in (g.get("sections") or []) if x.get("heading")][:4]
        steps = [{"heading": clean(x.get("heading", ""))[:50], "text": clean(x.get("text", ""))[:140]}
                 for x in (g.get("steps") or [])][:5]
        kf = g.get("key_fact") if isinstance(g.get("key_fact"), dict) else None
        if kf:
            kf = {"value": clean(kf.get("value", ""))[:14], "label": clean(kf.get("label", ""))[:90]}
        item = {"title": clean(g.get("title", src["title"]))[:80], "summary": clean(g.get("summary", ""))[:400],
                "sections": secs, "steps": steps, "key_fact": kf,
                "warning": clean(g.get("warning") or "")[:160] or None, "url": src["url"]}
        alltext = " ".join([item["title"], item["summary"]] + [x["heading"] + " " + " ".join(x["points"]) for x in secs]
                           + [x["heading"] + " " + x["text"] for x in steps])
        if len(secs) < 2 or len(steps) < 2:
            log(f"[rehber] Eksik içerik, atlandı: {item['title']}")
            continue
        if is_sensitive(alltext) or not is_turkish(alltext):
            log(f"[rehber] Dil ya da içerik kontrolünden geçmedi, atlandı: {item['title']}")
            continue
        gs.append(item)
    if not gs:
        log("Rehber içeriği üretilemedi, bugün rehber postu yok.")
        return
    tags = [t if t.startswith("#") else f"#{t}" for t in (data.get("hashtags") or [])]
    tags = [re.sub(r"\s+", "", t) for t in tags if len(t) > 1][:20]

    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.jpg"):
        old.unlink()
    images = []
    p = out / "00.jpg"
    render_guide_cover(gs, label, color, day, p)
    images.append(p.as_posix())
    total = 1 + 2 * len(gs)
    page = 1
    for gi, g in enumerate(gs, 1):
        page += 1
        p = out / f"{page - 1:02d}.jpg"
        render_info_overview(g, gi, label, color, page, total, p)
        images.append(p.as_posix())
        page += 1
        p = out / f"{page - 1:02d}.jpg"
        render_info_steps(g, gi, label, color, page, total, g["url"], p)
        images.append(p.as_posix())
    post = {"date": day.isoformat(), "category": slug, "guides": gs, "images": images,
            "caption": build_guide_caption(gs, tags, label, day)}
    (out / "post.json").write_text(json.dumps(post, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"Rehber postu hazırlandı: {len(images)} görsel, {out}")


def cmd_guide_publish(args):
    out = OUT_ROOT / today_london().isoformat() / "rehber"
    post_file = out / "post.json"
    if not post_file.exists():
        log("Bugün için hazırlanmış rehber postu yok.")
        return
    if (out / "published.json").exists():
        log("Bugünün rehber postu zaten yayınlanmış.")
        return
    if guide_too_early():
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
    media_id = publish_carousel(urls, post["caption"])
    (out / "published.json").write_text(json.dumps(
        {"media_id": media_id, "published_at": dt.datetime.now(TZ).isoformat()}, indent=2))
    log(f"Rehber postu yayınlandı. Instagram medya ID: {media_id}")


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
    sub.add_parser("guide")
    sub.add_parser("guide-publish")
    args = ap.parse_args()
    {"prepare": cmd_prepare, "publish": cmd_publish, "breaking": cmd_breaking,
     "breaking-publish": cmd_breaking_publish, "whoami": cmd_whoami,
     "guide": cmd_guide, "guide-publish": cmd_guide_publish}[args.cmd](args)


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
