# Instagram AI kontent menejeri

Telegram bot orqali Instagram akkauntni sun'iy intellekt (Claude) yordamida boshqarish: post joylash,
rejalashtirish, o'chirish, izohlarga javob berish va statistikani ko'rish — oddiy so'zlar bilan.

```
Siz:  [3 ta rasm] Karusel qilib, ertaga soat 9 da chiqar. Kuzgi kolleksiya, 20% chegirma.
Bot:  Karusel tayyor, ertaga 09:00 ga rejalashtirishni tasdiqlang 👇
      [rasmlar]  🗓 So'rov #12: postni rejalashtirish ... Caption: ...   [✅ Tasdiqlash] [❌ Bekor qilish]
Siz:  ✅
Bot:  ✅ Post 2026-10-05 09:00 ga rejalashtirildi (reja #3).
```

## Nimalar qila oladi

- **Joylash** — rasm/video yuborasiz, AI rasmga qarab caption va heshteglar yozadi; lenta posti, karusel
  (2–10 ta), Reels va Story. Lentadagi rasmlar Instagram talab qiladigan nisbatga (4:5 … 1.91:1)
  avtomatik moslanadi.
- **Rejalashtirish** — «ertaga 9:00 da», «juma kuni kechqurun»; vaqti kelganda bot o'zi joylaydi va xabar beradi.
- **O'chirish** — «kechagi postni o'chir»: AI qaysi post ekanini topib, tasdiq so'raydi.
- **Izohlar** — o'qish, javob yozish, yashirish, o'chirish; post uchun izohlarni yoqish/o'chirish.
- **Statistika** — post bo'yicha ko'rishlar, qamrov, layk, saqlashlar; akkaunt va kunlik limit holati.

Instagramda biror narsa o'zgarishidan oldin bot har doim ✅/❌ tugmalari bilan tasdiq so'raydi.

## Instagram API cheklovlari

Bot faqat Meta'ning rasmiy Instagram Graph API'sidan foydalanadi (login/parol bilan ishlaydigan norasmiy
kutubxonalar akkauntni bloklatib qo'yishi mumkin). Shuning uchun:

- Akkaunt **professional** (Business yoki Creator) bo'lishi va **Facebook sahifasiga ulangan** bo'lishi kerak.
  Postni o'chirish faqat Facebook Login orqali olingan token bilan ishlaydi.
- Joylangan postning **captionini API orqali tahrirlab bo'lmaydi** — faqat o'chirib, qayta joylash mumkin.
- API orqali **24 soatda ko'pi bilan 100 ta post** joylash mumkin.
- Story'ga caption qo'yilmaydi; bitta video doim Reels bo'lib chiqadi; karuselda ko'pi bilan 10 ta media.
- Instagram faylni **internetdagi ochiq HTTPS manzildan** yuklab oladi, shuning uchun bot media fayllarni o'zi
  tarqatadi (`PUBLIC_BASE_URL`).
- Telegram botlar 20 MB dan katta faylni yuklab ololmaydi.

## Qanday ishlaydi

```
Admin (Telegram) ──► aiogram bot ──► Claude agent ──tools──► Instagram Graph API
                        │  ✅/❌ tugmalar   │                       ▲
                        ▼                  ▼                       │ media URL
                 SQLite: media, suhbatlar, so'rovlar, rejalar   aiohttp media server (/media/…)
```

| Fayl | Vazifasi |
|---|---|
| `igbot/bot.py` | Telegram: xabarlar, rasm/video/albom qabul qilish, tasdiqlash tugmalari |
| `igbot/agent.py` | Claude bilan suhbat va tool chaqiruvlari sikli |
| `igbot/tools.py` | AI ishlata oladigan 15 ta vosita (postlar, izohlar, statistika, joylash…) |
| `igbot/actions.py` | Instagramni o'zgartiradigan amallar: tekshiruv, tasdiq kartasi, bajarish |
| `igbot/instagram.py` | Instagram Graph API klienti (container → status → publish) |
| `igbot/scheduler.py` | Rejalashtirilgan postlarni vaqtida joylash, eski fayllarni tozalash |
| `igbot/media.py` | Rasmni JPEG ga keltirish, nisbatga moslash, AI uchun preview |
| `igbot/web.py` | Instagram fayllarni yuklab oladigan ochiq HTTP endpoint |
| `igbot/prompts.py` | AI uchun ko'rsatma (system prompt) |

## O'rnatish

### 1. Instagram va Facebook sahifa

1. Instagram ilovasida: **Sozlamalar → Akkaunt turi va vositalar → Professional akkauntga o'tish**
   (Business yoki Creator).
2. Facebook sahifa yarating (yoki mavjudini oling) va Instagram akkauntni unga ulang
   (Meta Business Suite → Sozlamalar → Instagram akkauntlar).

### 2. Meta ilova va access token

1. [developers.facebook.com](https://developers.facebook.com) → **My Apps → Create App**, turi **Business**.
2. Ilovaga **Instagram** mahsulotini qo'shing (*API setup with Facebook login*).
3. Token quyidagi ruxsatlar bilan kerak:
   `instagram_basic`, `instagram_content_publish`, `instagram_manage_contents`, `instagram_manage_comments`,
   `instagram_manage_insights`, `pages_show_list`, `pages_read_engagement`
   (sahifa Business portfolio orqali boshqarilsa, `business_management` ham).
4. Token olishning ikki yo'li:
   - **Doimiy (tavsiya etiladi):** [business.facebook.com](https://business.facebook.com) → Business settings →
     Users → **System users** → yangi system user → *Assign assets*: Facebook sahifa va Instagram akkaunt →
     **Generate new token**: ilovangiz, muddati *Never*, yuqoridagi ruxsatlar.
   - **Tez sinash uchun:** Graph API Explorer → ilovangizni tanlang → User Token → ruxsatlarni belgilang →
     Generate Access Token → Access Token Debugger'da **Extend Access Token** (60 kun amal qiladi).
5. Ilova *Development* rejimida qolishi mumkin: o'zingizning akkauntingizni boshqarish uchun App Review shart
   emas (ilovada admin/developer rolingiz bo'lsa).

`IG_USER_ID` ni bo'sh qoldirsangiz, bot uni token orqali o'zi topadi (`python -m igbot check` ham ko'rsatadi).

### 3. Telegram bot

[@BotFather](https://t.me/BotFather) → `/newbot` → tokenni `TELEGRAM_BOT_TOKEN` ga yozing.
O'z Telegram ID ingizni bilish uchun botni ishga tushirib, unga yozing — admin bo'lmaganlarga bot ID ni
aytadi. Uni `ADMIN_IDS` ga qo'shing.

### 4. Claude API kaliti

[console.anthropic.com](https://console.anthropic.com) → API Keys → kalitni `ANTHROPIC_API_KEY` ga yozing.

### 5. Ochiq HTTPS manzil

Instagram rasm/videoni `PUBLIC_BASE_URL/media/...` dan yuklab oladi, shuning uchun bot internetdan ochiq
bo'lishi kerak:

- **Server:** domen + reverse proxy (Caddy/nginx) → `localhost:8080`; `PUBLIC_BASE_URL=https://bot.sizning-domen.uz`
- **Sinash uchun:** `cloudflared tunnel --url http://localhost:8080` bergan `https://…trycloudflare.com` manzil.

### 6. Ishga tushirish

Docker bilan:

```bash
cp .env.example .env        # qiymatlarni to'ldiring
docker compose up -d --build
docker compose exec bot python -m igbot check
docker compose logs -f
```

Docker'siz (Python 3.10+):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # qiymatlarni to'ldiring
python -m igbot check       # sozlamalarni tekshirish
python -m igbot             # botni ishga tushirish
```

`check` Telegram tokenini, Instagram token ruxsatlari va akkauntni, kunlik limitni, Claude kalitini va
`PUBLIC_BASE_URL` internetdan ochilishini tekshirib, nima yetishmayotganini aytadi.

## Foydalanish

Buyruqlar: `/start` — yordam, `/status` — akkaunt holati va rejalar, `/pending` — tasdiq kutayotgan
so'rovlarni qayta ko'rsatish, `/new` — yangi suhbat.
Qolgan hamma narsani oddiy so'zlar bilan yozasiz:

- (rasm bilan) «Shuni joyla, caption'ni o'zing yoz» · «Story qilib qo'y»
- (albom) «Karusel qilib, shanba 10:00 da chiqar»
- «Rejadagi postlarni ko'rsat» · «3-rejani bekor qil»
- «Oxirgi 5 ta post» · «Kechagi postni o'chir»
- «Oxirgi postdagi izohlarni ko'rsat, savollarga javob yoz» · «Haqoratli izohni yashir»
- «O'tgan haftadagi postlar natijasi qanday?»

AI bilan suhbat 12 soat jim tursa yoki juda uzun bo'lib ketsa, yangi suhbat avtomatik boshlanadi.

## Sozlamalar (`.env`)

| O'zgaruvchi | Standart | Izoh |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | @BotFather tokeni |
| `ADMIN_IDS` | — | Botdan foydalana oladigan Telegram ID lar (vergul bilan) |
| `IG_ACCESS_TOKEN` | — | Meta access token (Facebook Login) |
| `IG_USER_ID` | avtomatik | Instagram professional akkaunt ID si |
| `GRAPH_API_VERSION` | `v25.0` | Graph API versiyasi |
| `ANTHROPIC_API_KEY` | — | Claude API kaliti |
| `CLAUDE_MODEL` | `claude-opus-5-5` | Model (arzonrog'i: `claude-sonnet-5-5`) |
| `CLAUDE_EFFORT` | `medium` | `low` / `medium` / `high` / `xhigh` / `max` — qancha chuqur o'ylashi |
| `CLAUDE_FALLBACKS` | `true` | Xavfsizlik filtri so'rovni rad etsa, Anthropic uni boshqa modelda qayta bajaradi |
| `PUBLIC_BASE_URL` | — | Bot ochiq HTTPS manzili (Instagram fayllarni shu yerdan oladi) |
| `WEB_HOST` / `WEB_PORT` | `0.0.0.0` / `8080` | Media server (`PORT` ham qabul qilinadi) |
| `TIMEZONE` | `Asia/Tashkent` | Vaqtlar shu mintaqada tushuniladi |
| `REQUIRE_CONFIRMATION` | `true` | O'zgarishlardan oldin ✅/❌ so'rash |
| `BRAND_GUIDE` / `BRAND_GUIDE_FILE` | — | Brend haqida: ohang, auditoriya, doimiy heshteglar… |
| `CONVERSATION_IDLE_HOURS` | `12` | Shuncha jim turgan suhbat yangidan boshlanadi |
| `MEDIA_RETENTION_DAYS` | `14` | Yuklangan fayllar shuncha kundan so'ng o'chiriladi |
| `DATA_DIR` | `data` | SQLite baza va fayllar papkasi |

## Xavfsizlik

- Bot faqat `ADMIN_IDS` dagi foydalanuvchilarga javob beradi.
- AI Instagramni (va post rejalarini) o'zi o'zgartirmaydi: har bir amal admin ✅ bosgandan keyin
  bajariladi; so'rov 24 soatdan keyin eskiradi.
- Izohlardagi matn AI uchun buyruq emas, faqat ma'lumot sifatida qaraladi.
- Media server faqat bot yaratgan tasodifiy nomli fayllarni beradi; baza va boshqa fayllar ochiq emas.
- `.env` (tokenlar) hech qachon gitga qo'shilmasin.

## Narx

Instagram Graph API bepul. Claude API ishlatilgan tokenlar bo'yicha to'lanadi (`claude-opus-5-5`: 1M kirish
tokeni $4, 1M chiqish tokeni $20; takrorlanadigan qism keshdan arzonroq o'qiladi). Taxminan: oddiy so'rov bir
necha sent, ko'p qadamli so'rov 10–20 sent atrofida. Arzonlashtirish uchun `CLAUDE_EFFORT=low` yoki
`CLAUDE_MODEL=claude-sonnet-5-5`.

## Muammolar

| Xabar | Yechim |
|---|---|
| «Instagram faylni yuklab ola olmadi» | `PUBLIC_BASE_URL` internetdan HTTPS orqali ochilishini tekshiring (`check`) |
| «Tokenda bu amal uchun ruxsat yo'q» | Token ruxsatlariga qarang (2-bo'lim), yangi token oling |
| «access token yaroqsiz yoki muddati tugagan» | Yangi token (System user tokeni muddatsiz) |
| «akkaunt ID si aniqlanmagan» | Instagram Facebook sahifaga ulanganini tekshiring yoki `IG_USER_ID` ni yozing |
| Bot javob bermayapti | `ADMIN_IDS` ni va `docker compose logs` ni tekshiring |

## Ishlab chiqish

```bash
pip install -r requirements-dev.txt
pytest
```

Testlar Telegram, Instagram va Claude API'larini soxta (fake) obyektlar bilan almashtiradi — internet yoki
kalitlar kerak emas.
