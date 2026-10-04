"""Texts the bot shows to admins (Uzbek)."""

START = """\
Salom! Men Instagram akkauntingizni boshqaradigan AI yordamchiman.

Menga oddiy so'zlar bilan yozing yoki rasm/video yuboring, masalan:
• (rasm bilan) «Shuni chiroyli caption bilan joyla»
• (bir nechta rasm) «Karusel qilib, ertaga 9:00 da chiqar»
• «Oxirgi 5 ta postni ko'rsat»
• «Kechagi postni o'chir»
• «Oxirgi postdagi izohlarga javob yoz»
• «Bu haftadagi postlar natijasi qanday?»

Instagramda biror narsa o'zgarishidan oldin (joylash, o'chirish, izohga javob) sizdan ✅ tugmasi bilan tasdiq so'rayman.

Buyruqlar:
/status — akkaunt holati va rejalashtirilgan postlar
/new — yangi suhbat boshlash
/help — yordam"""

NOT_ADMIN = """\
⛔ Bu bot faqat adminlar uchun.
Sizning Telegram ID raqamingiz: {user_id}
Admin bo'lish uchun bu raqamni ADMIN_IDS sozlamasiga qo'shish kerak."""

NEW_CONVERSATION = "🆕 Yangi suhbat boshlandi."
UNSUPPORTED_FILE = "Faqat rasm (JPEG, PNG, WEBP) yoki video (MP4, MOV) yuboring."
UNSUPPORTED_MESSAGE = "Menga matn, rasm yoki video yuboring."
UNSUPPORTED_VIDEO = "Video MP4 yoki MOV formatda bo'lishi kerak."
FILE_TOO_BIG = "Fayl 20 MB dan katta — Telegram botlar bunday faylni yuklab ololmaydi. Kichikroq fayl yuboring."
SAVE_FAILED = "Faylni saqlab bo'lmadi, qayta yuborib ko'ring."
REFUSED = "Kechirasiz, bu so'rovni bajara olmayman."
AI_AUTH = "AI xizmatiga ulanib bo'lmadi: ANTHROPIC_API_KEY noto'g'ri yoki sozlanmagan."
AI_BUSY = "AI xizmati hozir band. Bir daqiqadan keyin qayta urinib ko'ring."
AI_UNREACHABLE = "AI xizmatiga ulanib bo'lmadi. Birozdan keyin qayta urinib ko'ring."
AI_ERROR = "AI xizmati xatosi: {error}"
UNEXPECTED = "Kutilmagan xato yuz berdi. Qayta urinib ko'ring."
CONFIRM = "✅ Tasdiqlash"
DECLINE = "❌ Bekor qilish"
# Shown under a confirmation card after a button press.
RUNNING = "⏳ Bajarilmoqda…"
CANCELLED = "❌ Bekor qilindi."
ALREADY_HANDLED = "Bu so'rov allaqachon ko'rib chiqilgan."
NOT_FOUND = "So'rov topilmadi."
INTERRUPTED_POST = "⚠️ Rejalashtirilgan post (reja #{id}) joylanayotganda bot to'xtab qoldi. Instagramda chiqqan-chiqmaganini tekshiring."
INTERRUPTED_ACTION = "⚠️ So'rov #{id} bajarilayotganda bot to'xtab qoldi. Natijasini Instagramda tekshiring."
