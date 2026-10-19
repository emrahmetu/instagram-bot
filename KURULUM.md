# Birleşik Krallık Gündemi: Instagram Otomatik Paylaşım Botu

## Bot ne yapıyor?

Depoda üç otomatik iş var:

| İş | Ne zaman | Ne yapar |
|---|---|---|
| **Instagram Günlük Paylaşım** | Her sabah 08:00 (Londra) | Son 24 saatin en önemli 8 haberini kapak + 8 kartlık bir carousel olarak paylaşır |
| **Instagram Son Dakika Kontrolü** | 08:00–22:00 arası saat başı | Son 2 saatin yeni başlıklarına bakar. Gerçekten son dakika niteliğinde bir haber varsa (yeni vize kuralı, istifa, faiz kararı, ulusal aksama vb.) tek kartlık ara paylaşım yapar. Günde en fazla 2. |
| **Instagram Hesap Kontrolü** | Sadece sen çalıştırınca | Botun hangi Instagram hesabına bağlı olduğunu gösterir. Hiçbir şey paylaşmaz. |

Haber kaynakları: BBC, Sky News, Guardian, The Independent, Evening Standard, Metro ve Nation.Cymru. Göç ve vize haberleri için ayrıca GOV.UK (İçişleri Bakanlığı ile Vize ve Göç birimi) ve göç hukuku sitesi Free Movement takip ediliyor.

Claude haberleri seçip Türkçe özetliyor. Kurallar: uydurma yok, göç haberleri öncelikli, hassas içerik elenir, "Chancellor" maliye bakanı olarak çevrilir. Bir kez paylaşılan haber tekrar paylaşılmaz.

---

## Maliyet

| Kalem | Ücret |
|---|---|
| GitHub Actions (public depo) | Ücretsiz |
| Görsellerin barındırılması (GitHub) | Ücretsiz |
| Instagram API | Ücretsiz |
| Claude (Haiku modeli) | Kabaca ayda birkaç dolar veya altı |

Son dakika kontrolü yalnızca yeni başlık varsa Claude'a gidiyor ve aynı başlığı ikinci kez göndermiyor, bu yüzden maliyeti düşük kalıyor. Anthropic hesabına genelde en az 5 dolar kredi yüklemek gerekiyor; konsoldan aylık harcama limiti de koyabilirsin.

---

## Bot hangi Instagram hesabına paylaşacağını nereden biliyor?

Bota Instagram şifreni vermiyorsun. Bağlantı Meta'nın resmi sistemiyle şöyle kuruluyor:

**Instagram hesabın → bağlı olduğu Facebook Sayfası → Meta işletme hesabındaki "sistem kullanıcısı" → bu kullanıcının ürettiği token**

Bot bu token ve hesabın kimlik numarası (`IG_USER_ID`) ile paylaşım yapıyor. Token yalnızca senin izin verdiğin hesaba erişebiliyor. Kurulumun sonunda "Hesap Kontrolü" işini çalıştırıp botun gerçekten doğru hesabı (ör. @emresterling) gördüğünü teyit edeceksin. Ayrıca `EXPECTED_IG_USERNAME` ayarıyla bot her paylaşımdan önce hesap adını kontrol ediyor; farklı bir hesap görürse paylaşmadan duruyor.

---

## Kurulum (bir kez, yaklaşık 30–45 dakika)

Meta'nın ekranları zaman zaman değişiyor. Menü adları birebir aynı olmayabilir, ama adımların sırası aynı kalır.

### Adım 1: Instagram hesabını profesyonel hesaba çevir

Telefonda, paylaşım yapılacak Instagram hesabıyla:

1. Profiline git, sağ üstteki ☰ menüye dokun.
2. **Hesap türü ve araçlar → Profesyonel hesaba geç**'e dokun.
3. Bir kategori seç (ör. "Haber ve medya web sitesi") ve **İçerik Üreticisi** ya da **İşletme**'yi seç. İkisi de olur.

Bu ücretsiz. Takipçilerin ve gönderilerin aynı kalır.

### Adım 2: Hesabı bir Facebook Sayfasına bağla

1. Facebook Sayfan yoksa bilgisayardan https://www.facebook.com/pages/create adresinden ücretsiz bir sayfa aç (ör. "Birleşik Krallık Gündemi").
2. Sayfayı aç, **Ayarlar → Bağlı hesaplar → Instagram → Hesabı bağla** de ve Instagram hesabınla giriş yap.

### Adım 3: Meta işletme hesabını hazırla

1. https://business.facebook.com adresine gir. İşletme hesabın (Business Portfolio) yoksa oluştur, ücretsiz.
2. **Ayarlar (Business settings) → Hesaplar → Sayfalar → Ekle** ile Facebook Sayfanı ekle.
3. **Hesaplar → Instagram hesapları → Ekle** ile Instagram hesabını ekle.

### Adım 4: Meta geliştirici uygulaması oluştur

1. https://developers.facebook.com adresine gir, **My Apps → Create App** de.
2. Uygulama türü olarak **Business**'ı seç ve uygulamayı Adım 3'teki işletme hesabına bağla.
3. Uygulama panelinde **Instagram** ürününü ekle (Facebook girişli "Instagram API / Graph API" seçeneği).

Uygulamayı sadece kendi hesabın için kullandığından genelde Meta'nın "App Review" incelemesine gerek kalmaz.

### Adım 5: Süresi dolmayan token oluştur

İnsan müdahalesi olmaması için en önemli adım bu. Normal token'lar 60 günde bitiyor, sistem kullanıcısı token'ı ise bitmiyor.

1. https://business.facebook.com → **Ayarlar → Kullanıcılar → Sistem kullanıcıları → Ekle**. Ad: "instagram-bot", rol: **Admin**.
2. Bu kullanıcıyı seç, **Varlık ata** de ve şunları ekle (tam kontrol yetkisiyle):
   - Facebook Sayfan
   - Instagram hesabın
   - Adım 4'te oluşturduğun uygulama
3. **Yeni token oluştur** de, uygulamanı seç, süre olarak **Hiçbir zaman (Never)** seç ve şu izinleri işaretle:
   - `instagram_basic`
   - `instagram_content_publish`
   - `pages_show_list`
   - `pages_read_engagement`
   - `business_management`
4. Çıkan uzun metni kopyala ve güvenli bir yere not et. Bu bir şifre gibidir, kimseyle paylaşma ve hiçbir yere (X, mesaj vb.) yapıştırma.

### Adım 6: Claude API anahtarı al

1. https://console.anthropic.com adresinde hesap aç.
2. **Billing** bölümünden kredi yükle (genelde en az 5 dolar). İstersen aylık harcama limiti koy.
3. **API Keys → Create Key** de ve anahtarı kopyala.

### Adım 7: GitHub deposunu kur

1. https://github.com adresinde ücretsiz hesap aç, **New repository** de.
   - Ad: ör. `instagram-bot`
   - **Public** seç. Instagram görselleri herkese açık adresten çekiyor. Token ve anahtarlar gizli kalır, depoda görünmez.
2. Zip'teki dosyaları depoya yükle: `bot.py`, `requirements.txt`, `KURULUM.md`, `ornek_haberler.json`, `ornek_son_dakika.json`.
   - **Add file → Upload files** ile sürükle-bırak yapabilirsin.
   - `.github` klasörü gizli olduğu için sürükleyince gitmeyebilir. En garantisi şu: her iş dosyası için **Add file → Create new file** de, dosya adına tam yolu yaz ve içeriğini yapıştır:
     - `.github/workflows/instagram.yml`
     - `.github/workflows/son-dakika.yml`
     - `.github/workflows/hesap-kontrol.yml`
3. **Settings → Actions → General → Workflow permissions** bölümünde **Read and write permissions**'ı seç, kaydet.
4. **Settings → Secrets and variables → Actions → Secrets** sekmesinde **New repository secret** ile iki gizli değer ekle:
   - `ANTHROPIC_API_KEY`: Adım 6'daki anahtar
   - `IG_ACCESS_TOKEN`: Adım 5'teki token

### Adım 8: Hesabı doğrula ve IG_USER_ID'yi al

1. Depoda **Actions** sekmesine gir. Uyarı çıkarsa "I understand… enable" de.
2. Soldan **Instagram Hesap Kontrolü**'nü seç, **Run workflow** de.
3. İş bitince içine tıkla, "Bağlı hesapları listele" adımını aç. Şöyle bir satır görmelisin:

   ```
   Facebook Sayfası: Birleşik Krallık Gündemi  →  Instagram: @emresterling  |  IG_USER_ID = 17841400000000000
   ```

4. Kullanıcı adı doğruysa:
   - **Secrets** sekmesine `IG_USER_ID` adıyla bu numarayı ekle.
   - **Variables** sekmesine `EXPECTED_IG_USERNAME` adıyla kullanıcı adını `@` olmadan ekle (ör. `emresterling`). Bot her paylaşımdan önce bunu kontrol eder.

"bağlı Instagram profesyonel hesabı YOK" ya da "erişebildiği Facebook Sayfası yok" yazıyorsa Adım 2, 3 veya 5'te bir bağlantı eksik demektir.

### Adım 9: Test et ve başlat

1. **Actions → Instagram Günlük Paylaşım → Run workflow** de. `dry_run` işaretli kalsın.
2. İş bitince depoda `output/TARİH/` klasöründeki görsellere ve `post.json`'daki açıklamaya bak.
3. Her şey iyiyse bir kez daha **Run workflow** de, bu sefer `dry_run` işaretini kaldır. Post Instagram'a düşmeli.
4. **Instagram Son Dakika Kontrolü** için de aynı şekilde önce `dry_run` ile dene. O an son dakika niteliğinde haber yoksa "son dakika niteliğinde haber yok" yazması normal.

Bundan sonra her şey kendiliğinden çalışır. Elle çalıştırdığında saat kontrolleri atlanır, yani testi günün her saatinde yapabilirsin. GitHub zamanlanmış işleri 5–30 dakika gecikmeyle başlatabildiği için sabah postu 08:00–08:30 arasında düşebilir.

İlk hafta paylaşımlara arada bir göz atmanı öneririm. Bot yalnızca haber başlıklarını ve kısa özetleri yeniden yazıyor; uydurma yapmaması için sıkı kurallar var, yine de hata yapabilir.

---

## Ayarlar (isteğe bağlı)

İlgili iş dosyasında (`.github/workflows/...yml`) "Haberleri hazırla" ya da "Yeni başlıkları kontrol et" adımının `env:` bölümüne eklenir:

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `NEWS_COUNT` | `8` | Sabah bültenindeki haber sayısı (1–9) |
| `POST_HOUR_LONDON` | `8` | Sabah bülteni saati. Değiştirirsen `instagram.yml`'deki iki `cron` satırını da güncelle: biri yaz saati için (saat − 1, UTC), biri kış saati için (saat, UTC). |
| `COVER_LABEL` | `SABAH BÜLTENİ` | Kapaktaki kırmızı etiket |
| `BREAKING_MAX_PER_DAY` | `2` | Günlük en fazla son dakika paylaşımı |
| `BREAKING_START_HOUR` / `BREAKING_END_HOUR` | `8` / `22` | Son dakika paylaşımı yapılabilecek saat aralığı (Londra) |
| `BREAKING_WINDOW_HOURS` | `2` | Son dakika için kaç saat geriye bakılır |
| `BRAND_TITLE` | `Birleşik Krallık Gündemi` | Kapak başlığı |
| `BRAND_HANDLE` | `@emresterling` | Kartların altındaki hesap adı |
| `CLAUDE_MODEL` | `claude-haiku-4-5-20251001` | Daha güçlü bir model daha iyi özet çıkarır ama maliyet artar |

Kaynak listesini değiştirmek için `bot.py` içindeki `FEEDS` listesini düzenle. Çalışmayan bir kaynak olursa bot onu atlar ve kayda uyarı yazar.

---

## Sık karşılaşılan sorunlar

- **"Hesap uyuşmuyor … Paylaşım iptal edildi"**: Token farklı bir Instagram hesabına bağlı. Adım 8'i tekrar çalıştır.
- **"Görsel herkese açık değil"**: Depo Public değil. Settings → General → Danger Zone → Change visibility.
- **Instagram API hatası (190 / OAuthException)**: Token geçersiz ya da izinler eksik. Adım 5'i tekrar yap, `IG_ACCESS_TOKEN`'ı güncelle.
- **Instagram API hatası (permission / 10 / 200)**: Sistem kullanıcısına sayfa, Instagram hesabı ya da uygulama varlık olarak atanmamış (Adım 5.2).
- **İşler hiç çalışmadı**: GitHub zamanlanmış işleri bazen geç başlatır. Uzun süre hareketsiz kalan depolarda zamanlanmış işleri durdurabilir; bot her gün dosya eklediği için bu normalde olmaz, olursa GitHub e-posta atar ve tek tıkla yeniden açarsın.
- **Graph API sürümü**: Meta eski sürümleri birkaç yılda bir kapatır. Hata alırsan `GRAPH_API_VERSION` değişkenine güncel sürümü yaz (ör. `v24.0`).

## Neden NewsNow'dan çekmiyor?

NewsNow'un robots.txt dosyası, Anthropic (Claude) ve OpenAI'ın yapay zeka botlarının siteye erişimini açıkça yasaklıyor ve NewsNow'un herkese açık bir RSS akışı da yok. Bot bu yüzden NewsNow'u taramıyor. Onun yerine NewsNow'un derlediği haber sitelerinin kendi RSS akışlarını doğrudan kullanıyor. Sonuç büyük ölçüde aynı haberler, üstüne GOV.UK'nin resmi göç duyuruları da var.

## Neden X'ten okumuyor?

X'teki postları otomatik okumak X API'nin ücretli planını gerektiriyor. Bot aynı kaynaklardan kendisi derlediği için ücretsiz.
