# CRM Tizimi

## Overview
Flask asosidagi kichik CRM tizimi - boss va xodim rollari bilan topshiriqlarni boshqarish.

## Vaqt zonasi
- O'zbekiston vaqti (UTC+5) bo'yicha ishlaydi
- 24 soatlik format (masalan: 14:30, 23:45)

## Features
- **2 ta rol**: Boss va Xodim
- **Boss imkoniyatlari**:
  - Xodim qo'shish/o'chirish
  - Topshiriq yaratish (o'ziga yoki xodimlarga)
  - Barcha topshiriqlarni ko'rish va filtr qilish
  - CSV export
- **Xodim imkoniyatlari**:
  - O'ziga berilgan topshiriqlarni ko'rish
  - Topshiriqni bajarildi deb belgilash (izoh bilan)
- **Telegram integratsiyasi**:
  - Yangi topshiriqda xodimga xabar
  - Bajarilganda bossga xabar
  - Muddat eslatmalari (2 soat, 30 daqiqa, 5 daqiqa oldin)

## Default Login
- **Boss**: `boss` / `magistr`
- Parolni o'zgartirish tavsiya etiladi!

## Tech Stack
- Python 3.12+
- Flask 3.0+
- SQLite (crm.db)
- python-telegram-bot

## Telegram Setup
Telegram xabarnomalarini yoqish uchun:
1. @BotFather orqali bot yarating
2. Bot tokenni `TELEGRAM_BOT_TOKEN` muhit o'zgaruvchisiga qo'ying
3. Boss chat ID'sini `BOSS_TELEGRAM_CHAT_ID` ga qo'ying
4. Har bir admin profilda telegram_chat_id ni to'ldiring

## Project Structure
```
/
├── app.py          # Asosiy dastur (barcha kod bitta faylda)
├── crm.db          # SQLite ma'lumotlar bazasi (avtomatik yaratiladi)
├── replit.md       # Loyiha hujjati
└── .gitignore      # Git ignore fayli
```

## Running
```bash
python app.py
```

Server 0.0.0.0:5000 portda ishga tushadi.
