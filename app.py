#!/usr/bin/env python3
"""
CRM Tizimi - Flask asosidagi kichik CRM
========================================
Boss va Xodim rollari, topshiriqlar boshqaruvi, Telegram integratsiyasi

Default login:
- Boss: boss / magistr (parolni o'zgartirish tavsiya etiladi!)

Ishga tushirish:
    python app.py

Telegram bot token'ni muhit o'zgaruvchisiga qo'ying:
    export TELEGRAM_BOT_TOKEN="your_bot_token_here"
"""

import os
import csv
import io
import sqlite3
import hashlib
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, render_template_string, request, redirect, url_for, session, flash, Response
from dotenv import load_dotenv

# .env faylini o'qish
load_dotenv()

# Telegram uchun
import requests as http_requests
TELEGRAM_AVAILABLE = True

# ============== KONFIGURATSIYA ==============
app = Flask(__name__)
app.secret_key = os.environ.get('SESSION_SECRET', 'dev-secret-key-change-in-production')
# Use an absolute path for the SQLite database so Gunicorn and other
# process working directories don't change where the DB is created.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, 'crm.db')

# O'zbekiston vaqt zonasi (UTC+5)
UZB_TIMEZONE = timezone(timedelta(hours=5))

def get_uzb_now():
    """O'zbekiston vaqtini olish"""
    return datetime.now(UZB_TIMEZONE)

def format_datetime(dt):
    """Datetime'ni 24 soatlik formatda ko'rsatish"""
    if dt is None:
        return '-'
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UZB_TIMEZONE)
    return dt.strftime('%d.%m.%Y %H:%M')

def is_overdue(deadline):
    """Muddat o'tganligini tekshirish (timezone-safe)"""
    if deadline is None:
        return False
    now = get_uzb_now()
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UZB_TIMEZONE)
    return deadline < now

# Telegram sozlamalari
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
BOSS_TELEGRAM_CHAT_ID = os.environ.get('BOSS_TELEGRAM_CHAT_ID', '')

# SQLite datetime adapterlari (Python 3.12+ uchun)
sqlite3.register_adapter(datetime, lambda d: d.isoformat())
sqlite3.register_converter("DATETIME", lambda s: datetime.fromisoformat(s.decode()) if s else None)

# ============== BAZANI SOZLASH ==============
def get_db():
    """Ma'lumotlar bazasiga ulanish"""
    conn = sqlite3.connect(DATABASE, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Bazani yaratish va boshlang'ich ma'lumotlarni qo'shish"""
    conn = get_db()
    cursor = conn.cursor()
    
    # Users jadvali
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('boss', 'xodim')),
            full_name TEXT,
            telegram_chat_id TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Tasks jadvali
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            assigned_to INTEGER,
            deadline DATETIME,
            status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'completed')),
            completion_note TEXT,
            completed_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            reminder_2h_sent INTEGER DEFAULT 0,
            reminder_30m_sent INTEGER DEFAULT 0,
            reminder_5m_sent INTEGER DEFAULT 0,
            FOREIGN KEY (assigned_to) REFERENCES users(id)
        )
    ''')
    
    # Default boss foydalanuvchisini qo'shish
    hashed_password = hashlib.sha256('magistr'.encode()).hexdigest()
    cursor.execute('''
        INSERT OR IGNORE INTO users (username, password, role, full_name)
        VALUES (?, ?, ?, ?)
    ''', ('boss', hashed_password, 'boss', 'Bosh Direktor'))
    
    conn.commit()
    conn.close()

# Ensure the database exists when the module is imported (e.g. when
# running under Gunicorn). This creates tables if the DB file is
# missing. It's safe to call multiple times because CREATE TABLE IF
# NOT EXISTS is used.
if not os.path.exists(DATABASE):
    try:
        init_db()
    except Exception as e:
        print(f"Failed to initialize DB on import: {e}")

# ============== TELEGRAM FUNKSIYALARI ==============
def send_telegram_message(chat_id, message):
    """Telegram orqali xabar yuborish"""
    if not TELEGRAM_BOT_TOKEN or not chat_id or not TELEGRAM_AVAILABLE:
        return False
    
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {
            'chat_id': chat_id,
            'text': message,
            'parse_mode': 'HTML'
        }
        response = http_requests.post(url, data=data, timeout=10)
        return response.status_code == 200
    except Exception as e:
        print(f"Telegram xato: {e}")
        return False

def notify_user_new_task(user_id, task_title, deadline=None):
    """Yangi topshiriq haqida xodimga xabar"""
    conn = get_db()
    user = conn.execute('SELECT telegram_chat_id, full_name FROM users WHERE id = ?', (user_id,)).fetchone()
    conn.close()
    
    if user and user['telegram_chat_id']:
        deadline_text = f"\n📅 Muddat: {deadline.strftime('%d.%m.%Y %H:%M')}" if deadline else ""
        message = f"📋 <b>Yangi topshiriq!</b>\n\n{task_title}{deadline_text}"
        send_telegram_message(user['telegram_chat_id'], message)

def notify_boss_task_completed(task_id):
    """Topshiriq bajarilganda bossga xabar"""
    if not BOSS_TELEGRAM_CHAT_ID:
        return
    
    conn = get_db()
    task = conn.execute('''
        SELECT t.*, u.full_name as xodim_name 
        FROM tasks t 
        LEFT JOIN users u ON t.assigned_to = u.id 
        WHERE t.id = ?
    ''', (task_id,)).fetchone()
    conn.close()
    
    if task:
        message = f"✅ <b>Topshiriq bajarildi!</b>\n\n"
        message += f"📋 {task['title']}\n"
        message += f"👤 Bajardi: {task['xodim_name'] or 'Noma\'lum'}\n"
        if task['completion_note']:
            message += f"💬 Izoh: {task['completion_note']}"
        send_telegram_message(BOSS_TELEGRAM_CHAT_ID, message)

# ============== REMINDER TIZIMI ==============
reminder_thread_started = False
reminder_lock = threading.Lock()

def check_reminders():
    """Muddat yaqinlashgan topshiriqlarni tekshirish"""
    while True:
        try:
            conn = get_db()
            now = get_uzb_now()
            
            # Bajarilmagan va muddati bor topshiriqlarni olish
            tasks = conn.execute('''
                SELECT t.*, u.telegram_chat_id, u.full_name
                FROM tasks t
                LEFT JOIN users u ON t.assigned_to = u.id
                WHERE t.status = 'pending' AND t.deadline IS NOT NULL
            ''').fetchall()
            
            for task in tasks:
                if not task['deadline'] or not task['telegram_chat_id']:
                    continue
                
                # Deadline'ni O'zbekiston vaqt zonasiga o'tkazish
                deadline = task['deadline']
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=UZB_TIMEZONE)
                
                time_left = deadline - now
                minutes_left = time_left.total_seconds() / 60
                
                # 2 soat oldin (120 daqiqa)
                if 115 <= minutes_left <= 125 and not task['reminder_2h_sent']:
                    message = f"⏰ <b>Eslatma!</b>\n\n📋 {task['title']}\n⏳ Muddat: 2 soatdan kam qoldi!"
                    if send_telegram_message(task['telegram_chat_id'], message):
                        conn.execute('UPDATE tasks SET reminder_2h_sent = 1 WHERE id = ?', (task['id'],))
                
                # 30 daqiqa oldin
                elif 25 <= minutes_left <= 35 and not task['reminder_30m_sent']:
                    message = f"⚠️ <b>Shoshiling!</b>\n\n📋 {task['title']}\n⏳ Muddat: 30 daqiqadan kam qoldi!"
                    if send_telegram_message(task['telegram_chat_id'], message):
                        conn.execute('UPDATE tasks SET reminder_30m_sent = 1 WHERE id = ?', (task['id'],))
                
                # 5 daqiqa oldin
                elif 3 <= minutes_left <= 7 and not task['reminder_5m_sent']:
                    message = f"🚨 <b>DIQQAT!</b>\n\n📋 {task['title']}\n⏳ Muddat: 5 daqiqadan kam qoldi!"
                    if send_telegram_message(task['telegram_chat_id'], message):
                        conn.execute('UPDATE tasks SET reminder_5m_sent = 1 WHERE id = ?', (task['id'],))
            
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Reminder xato: {e}")
        
        time.sleep(60)  # Har daqiqada tekshirish

def start_reminder_thread():
    """Reminder threadni boshlash (bir marta)"""
    global reminder_thread_started
    with reminder_lock:
        if not reminder_thread_started:
            thread = threading.Thread(target=check_reminders, daemon=True)
            thread.start()
            reminder_thread_started = True

# ============== AUTENTIFIKATSIYA ==============
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Iltimos, tizimga kiring', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def boss_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Iltimos, tizimga kiring', 'warning')
            return redirect(url_for('login'))
        if session.get('role') != 'boss':
            flash('Bu sahifa faqat boss uchun!', 'error')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function

# ============== CSS STILLARI ==============
CSS_STYLES = '''
<style>
    * {
        margin: 0;
        padding: 0;
        box-sizing: border-box;
    }
    
    body {
        font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        min-height: 100vh;
        padding: 20px;
    }
    
    .container {
        max-width: 1200px;
        margin: 0 auto;
    }
    
    .card {
        background: white;
        border-radius: 16px;
        box-shadow: 0 10px 40px rgba(0,0,0,0.2);
        padding: 30px;
        margin-bottom: 20px;
    }
    
    .card-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 25px;
        padding-bottom: 15px;
        border-bottom: 2px solid #f0f0f0;
    }
    
    h1, h2, h3 {
        color: #333;
    }
    
    h1 {
        font-size: 28px;
        background: linear-gradient(135deg, #667eea, #764ba2);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        background-clip: text;
    }
    
    .btn {
        padding: 12px 24px;
        border: none;
        border-radius: 8px;
        cursor: pointer;
        font-size: 14px;
        font-weight: 600;
        text-decoration: none;
        display: inline-block;
        transition: all 0.3s ease;
    }
    
    .btn-primary {
        background: linear-gradient(135deg, #667eea, #764ba2);
        color: white;
    }
    
    .btn-primary:hover {
        transform: translateY(-2px);
        box-shadow: 0 5px 20px rgba(102, 126, 234, 0.4);
    }
    
    .btn-success {
        background: linear-gradient(135deg, #11998e, #38ef7d);
        color: white;
    }
    
    .btn-success:hover {
        transform: translateY(-2px);
        box-shadow: 0 5px 20px rgba(17, 153, 142, 0.4);
    }
    
    .btn-danger {
        background: linear-gradient(135deg, #eb3349, #f45c43);
        color: white;
    }
    
    .btn-danger:hover {
        transform: translateY(-2px);
        box-shadow: 0 5px 20px rgba(235, 51, 73, 0.4);
    }
    
    .btn-secondary {
        background: #6c757d;
        color: white;
    }
    
    .btn-sm {
        padding: 8px 16px;
        font-size: 12px;
    }
    
    .form-group {
        margin-bottom: 20px;
    }
    
    .form-group label {
        display: block;
        margin-bottom: 8px;
        font-weight: 600;
        color: #555;
    }
    
    .form-control {
        width: 100%;
        padding: 12px 16px;
        border: 2px solid #e0e0e0;
        border-radius: 8px;
        font-size: 14px;
        transition: border-color 0.3s ease;
    }
    
    .form-control:focus {
        outline: none;
        border-color: #667eea;
    }
    
    select.form-control {
        appearance: none;
        background: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23333' d='M6 8L1 3h10z'/%3E%3C/svg%3E") no-repeat right 12px center;
        background-color: white;
    }
    
    textarea.form-control {
        resize: vertical;
        min-height: 100px;
    }
    
    .table-container {
        overflow-x: auto;
    }
    
    table {
        width: 100%;
        border-collapse: collapse;
    }
    
    th, td {
        padding: 15px;
        text-align: left;
        border-bottom: 1px solid #f0f0f0;
    }
    
    th {
        background: linear-gradient(135deg, #667eea, #764ba2);
        color: white;
        font-weight: 600;
    }
    
    tr:hover {
        background: #f8f9fa;
    }
    
    .badge {
        padding: 6px 12px;
        border-radius: 20px;
        font-size: 12px;
        font-weight: 600;
    }
    
    .badge-pending {
        background: #fff3cd;
        color: #856404;
    }
    
    .badge-completed {
        background: #d4edda;
        color: #155724;
    }
    
    .badge-overdue {
        background: #f8d7da;
        color: #721c24;
    }
    
    .badge-boss {
        background: linear-gradient(135deg, #667eea, #764ba2);
        color: white;
    }
    
    .badge-xodim {
        background: linear-gradient(135deg, #11998e, #38ef7d);
        color: white;
    }
    
    .alert {
        padding: 15px 20px;
        border-radius: 8px;
        margin-bottom: 20px;
        font-weight: 500;
    }
    
    .alert-success {
        background: #d4edda;
        color: #155724;
        border-left: 4px solid #28a745;
    }
    
    .alert-warning {
        background: #fff3cd;
        color: #856404;
        border-left: 4px solid #ffc107;
    }
    
    .alert-error {
        background: #f8d7da;
        color: #721c24;
        border-left: 4px solid #dc3545;
    }
    
    .alert-info {
        background: #cce5ff;
        color: #004085;
        border-left: 4px solid #007bff;
    }
    
    .nav {
        display: flex;
        gap: 15px;
        align-items: center;
        flex-wrap: wrap;
    }
    
    .nav a {
        color: #667eea;
        text-decoration: none;
        font-weight: 500;
        padding: 8px 16px;
        border-radius: 8px;
        transition: all 0.3s ease;
    }
    
    .nav a:hover {
        background: #f0f0f0;
    }
    
    .user-info {
        display: flex;
        align-items: center;
        gap: 10px;
    }
    
    .user-avatar {
        width: 40px;
        height: 40px;
        border-radius: 50%;
        background: linear-gradient(135deg, #667eea, #764ba2);
        display: flex;
        align-items: center;
        justify-content: center;
        color: white;
        font-weight: bold;
    }
    
    .login-container {
        max-width: 400px;
        margin: 50px auto;
    }
    
    .login-logo {
        text-align: center;
        margin-bottom: 30px;
    }
    
    .login-logo h1 {
        font-size: 36px;
        margin-bottom: 10px;
    }
    
    .login-logo p {
        color: #666;
    }
    
    .grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
        gap: 20px;
    }
    
    .stat-card {
        background: linear-gradient(135deg, #667eea, #764ba2);
        color: white;
        padding: 25px;
        border-radius: 12px;
        text-align: center;
    }
    
    .stat-card h3 {
        font-size: 36px;
        color: white;
        margin-bottom: 10px;
    }
    
    .stat-card p {
        opacity: 0.9;
    }
    
    .filter-form {
        display: flex;
        gap: 15px;
        flex-wrap: wrap;
        align-items: flex-end;
        margin-bottom: 20px;
    }
    
    .filter-form .form-group {
        margin-bottom: 0;
        flex: 1;
        min-width: 150px;
    }
    
    .modal-backdrop {
        display: none;
        position: fixed;
        top: 0;
        left: 0;
        width: 100%;
        height: 100%;
        background: rgba(0,0,0,0.5);
        z-index: 1000;
        align-items: center;
        justify-content: center;
    }
    
    .modal-backdrop.active {
        display: flex;
    }
    
    .modal {
        background: white;
        border-radius: 16px;
        padding: 30px;
        max-width: 500px;
        width: 90%;
        max-height: 90vh;
        overflow-y: auto;
    }
    
    .actions {
        display: flex;
        gap: 10px;
    }
    
    @media (max-width: 768px) {
        .card-header {
            flex-direction: column;
            gap: 15px;
        }
        
        .nav {
            flex-direction: column;
            width: 100%;
        }
        
        .filter-form {
            flex-direction: column;
        }
        
        .filter-form .form-group {
            width: 100%;
        }
    }
</style>
'''

# ============== HTML SHABLONLARI ==============
BASE_TEMPLATE = '''
<!DOCTYPE html>
<html lang="uz">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ title }} - CRM Tizimi</title>
    ''' + CSS_STYLES + '''
</head>
<body>
    <div class="container">
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="alert alert-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        {{ content|safe }}
    </div>
</body>
</html>
'''

LOGIN_TEMPLATE = '''
<div class="login-container">
    <div class="card">
        <div class="login-logo">
            <h1>📊 CRM</h1>
            <p>Tizimga kirish</p>
        </div>
        <form method="POST">
            <div class="form-group">
                <label>Foydalanuvchi nomi</label>
                <input type="text" name="username" class="form-control" required placeholder="Login">
            </div>
            <div class="form-group">
                <label>Parol</label>
                <input type="password" name="password" class="form-control" required placeholder="Parol">
            </div>
            <button type="submit" class="btn btn-primary" style="width: 100%;">Kirish</button>
        </form>
    </div>
</div>
'''

DASHBOARD_TEMPLATE = '''
<div class="card">
    <div class="card-header">
        <h1>📊 CRM Tizimi</h1>
        <div class="user-info">
            <div class="user-avatar">{{ session.full_name[0] if session.full_name else 'U' }}</div>
            <div>
                <strong>{{ session.full_name or session.username }}</strong>
                <span class="badge badge-{{ session.role }}">{{ session.role|upper }}</span>
            </div>
        </div>
    </div>
    
    <div class="nav">
        <a href="{{ url_for('dashboard') }}">🏠 Bosh sahifa</a>
        {% if session.role == 'boss' %}
            <a href="{{ url_for('xodimlar') }}">👥 Xodimlar</a>
            <a href="{{ url_for('add_task') }}">➕ Topshiriq qo'shish</a>
            <a href="{{ url_for('all_tasks') }}">📋 Barcha topshiriqlar</a>
            <a href="{{ url_for('export_csv') }}">📥 CSV Export</a>
            <a href="{{ url_for('change_profile') }}">⚙️ Profil o'zgartirish</a>
        {% endif %}
        <a href="{{ url_for('my_tasks') }}">📝 Mening topshiriqlarim</a>
        <a href="{{ url_for('logout') }}" class="btn btn-danger btn-sm">Chiqish</a>
    </div>
</div>

{% if session.role == 'boss' and session.username == 'boss' %}
<div class="alert alert-warning">
    ⚠️ <strong>Xavfsizlik ogohlantirishi:</strong> Siz default parol bilan kirgansiz. Iltimos, parolni o'zgartiring!
</div>
{% endif %}

<div class="grid">
    <div class="stat-card">
        <h3>{{ stats.total }}</h3>
        <p>Jami topshiriqlar</p>
    </div>
    <div class="stat-card" style="background: linear-gradient(135deg, #11998e, #38ef7d);">
        <h3>{{ stats.completed }}</h3>
        <p>Bajarilgan</p>
    </div>
    <div class="stat-card" style="background: linear-gradient(135deg, #eb3349, #f45c43);">
        <h3>{{ stats.pending }}</h3>
        <p>Kutilmoqda</p>
    </div>
    <div class="stat-card" style="background: linear-gradient(135deg, #f093fb, #f5576c);">
        <h3>{{ stats.overdue }}</h3>
        <p>Muddati o'tgan</p>
    </div>
</div>
'''

CHANGE_PROFILE_TEMPLATE = '''
<div class="card">
    <div class="card-header">
        <h1>⚙️ Profil o'zgartirish</h1>
        <a href="{{ url_for('dashboard') }}" class="btn btn-secondary btn-sm">⬅ Orqaga</a>
    </div>
    
    <form method="POST">
        <div class="form-group">
            <label>Yangi foydalanuvchi nomi (ixtiyoriy)</label>
            <input type="text" name="new_username" class="form-control" placeholder="Hozirgi: {{ session.username }}">
            <small>Bo'sh qoldirsangiz, o'zgarmaydi</small>
        </div>
        <div class="form-group">
            <label>Yangi parol (ixtiyoriy)</label>
            <input type="password" name="new_password" class="form-control" placeholder="Yangi parol kiritish">
            <small>Bo'sh qoldirsangiz, o'zgarmaydi</small>
        </div>
        <div class="form-group">
            <label>Parolni tasdiqlang</label>
            <input type="password" name="confirm_password" class="form-control" placeholder="Parolni qayta kiritish">
        </div>
        <div class="form-group">
            <label>Hozirgi parol *</label>
            <input type="password" name="current_password" class="form-control" required placeholder="Tasdiqlash uchun hozirgi parol">
        </div>
        <button type="submit" class="btn btn-primary">💾 Saqlash</button>
    </form>
</div>
'''

XODIMLAR_TEMPLATE = '''
<div class="card">
    <div class="card-header">
        <h1>👥 Xodimlar boshqaruvi</h1>
        <a href="{{ url_for('dashboard') }}" class="btn btn-secondary btn-sm">⬅ Orqaga</a>
    </div>
    
    <h3 style="margin-bottom: 20px;">Yangi xodim qo'shish</h3>
    <form method="POST" style="margin-bottom: 30px;">
        <div class="grid">
            <div class="form-group">
                <label>Foydalanuvchi nomi</label>
                <input type="text" name="username" class="form-control" required>
            </div>
            <div class="form-group">
                <label>Parol</label>
                <input type="password" name="password" class="form-control" required>
            </div>
            <div class="form-group">
                <label>To'liq ism</label>
                <input type="text" name="full_name" class="form-control">
            </div>
            <div class="form-group">
                <label>Telegram Chat ID</label>
                <input type="text" name="telegram_chat_id" class="form-control" placeholder="Ixtiyoriy">
            </div>
        </div>
        <button type="submit" class="btn btn-success">➕ Xodim qo'shish</button>
    </form>
    
    <h3 style="margin-bottom: 20px;">Mavjud xodimlar</h3>
    <div class="table-container">
        <table>
            <thead>
                <tr>
                    <th>ID</th>
                    <th>Login</th>
                    <th>To'liq ism</th>
                    <th>Telegram</th>
                    <th>Qo'shilgan</th>
                    <th>Amallar</th>
                </tr>
            </thead>
            <tbody>
                {% for xodim in xodimlar %}
                <tr>
                    <td>{{ xodim.id }}</td>
                    <td>{{ xodim.username }}</td>
                    <td>{{ xodim.full_name or '-' }}</td>
                    <td>{{ xodim.telegram_chat_id or '-' }}</td>
                    <td>{{ xodim.created_at.strftime('%d.%m.%Y') if xodim.created_at else '-' }}</td>
                    <td>
                                        <form method="POST" action="{{ url_for('delete_xodim', id=xodim.id) }}" style="display: inline;">
                                            <button type="submit" class="btn btn-danger btn-sm" onclick="return confirm('Rostdan o\\'chirmoqchimisiz?')">🗑 O'chirish</button>
                                        </form>
                                        <a href="{{ url_for('edit_xodim', id=xodim.id) }}" class="btn btn-primary btn-sm">✏️ O'zgartirish</a>
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
</div>
'''

EDIT_XODIM_TEMPLATE = '''
<div class="card">
    <div class="card-header">
        <h1>✏️ Xodimni o'zgartirish</h1>
        <a href="{{ url_for('xodimlar') }}" class="btn btn-secondary btn-sm">⬅ Orqaga</a>
    </div>

    <form method="POST">
        <div class="form-group">
            <label>Foydalanuvchi nomi *</label>
            <input type="text" name="username" class="form-control" required value="{{ user.username }}">
        </div>
        <div class="form-group">
            <label>Yangi parol (ixtiyoriy)</label>
            <input type="password" name="password" class="form-control" placeholder="Agar parolni o'zgartirmoqchi bo'lsangiz kiriting">
        </div>
        <div class="form-group">
            <label>To'liq ism</label>
            <input type="text" name="full_name" class="form-control" value="{{ user.full_name or '' }}">
        </div>
        <div class="form-group">
            <label>Telegram Chat ID</label>
            <input type="text" name="telegram_chat_id" class="form-control" value="{{ user.telegram_chat_id or '' }}">
        </div>
        <button type="submit" class="btn btn-primary">💾 Saqlash</button>
    </form>
</div>
'''

EDIT_TASK_TEMPLATE = '''
<div class="card">
    <div class="card-header">
        <h1>✏️ Topshiriqni o'zgartirish</h1>
        <a href="{{ url_for('all_tasks') }}" class="btn btn-secondary btn-sm">⬅ Orqaga</a>
    </div>

    <form method="POST">
        <div class="form-group">
            <label>Topshiriq nomi *</label>
            <input type="text" name="title" class="form-control" required value="{{ task.title }}">
        </div>
        <div class="form-group">
            <label>Batafsil tavsif</label>
            <textarea name="description" class="form-control">{{ task.description or '' }}</textarea>
        </div>
        <div class="grid">
            <div class="form-group">
                <label>Kimga topshiriq *</label>
                <select name="assigned_to" class="form-control" required>
                    <option value="">-- Tanlang --</option>
                    {% for user in users %}
                        <option value="{{ user.id }}" {{ 'selected' if user.id == task.assigned_to }}>{{ user.full_name or user.username }}</option>
                    {% endfor %}
                </select>
            </div>
            <div class="form-group">
                <label>Muddat sanasi (ixtiyoriy)</label>
                <input type="date" name="deadline_date" class="form-control" value="{{ deadline_date }}">
            </div>
            <div class="form-group">
                <label>Muddat vaqti (24 soatlik)</label>
                <input type="time" name="deadline_time" class="form-control" step="60" value="{{ deadline_time }}">
            </div>
        </div>
        <button type="submit" class="btn btn-primary">💾 Saqlash</button>
    </form>
</div>
'''

ADD_TASK_TEMPLATE = '''
<div class="card">
    <div class="card-header">
        <h1>➕ Yangi topshiriq</h1>
        <a href="{{ url_for('dashboard') }}" class="btn btn-secondary btn-sm">⬅ Orqaga</a>
    </div>
    
    <form method="POST">
        <div class="form-group">
            <label>Topshiriq nomi *</label>
            <input type="text" name="title" class="form-control" required placeholder="Topshiriq sarlavhasi">
        </div>
        <div class="form-group">
            <label>Batafsil tavsif</label>
            <textarea name="description" class="form-control" placeholder="Qo'shimcha ma'lumotlar..."></textarea>
        </div>
        <div class="grid">
            <div class="form-group">
                <label>Kimga topshiriq *</label>
                <select name="assigned_to" class="form-control" required>
                    <option value="">-- Tanlang --</option>
                    {% for user in users %}
                        <option value="{{ user.id }}">{{ user.full_name or user.username }}</option>
                    {% endfor %}
                </select>
            </div>
            <div class="form-group">
                <label>Muddat sanasi (ixtiyoriy)</label>
                <input type="date" name="deadline_date" class="form-control">
            </div>
            <div class="form-group">
                <label>Muddat vaqti (24 soatlik)</label>
                <input type="time" name="deadline_time" class="form-control" step="60">
            </div>
        </div>
        <button type="submit" class="btn btn-primary">💾 Saqlash</button>
    </form>
</div>
'''

ALL_TASKS_TEMPLATE = '''
<div class="card">
    <div class="card-header">
        <h1>📋 Barcha topshiriqlar</h1>
        <a href="{{ url_for('dashboard') }}" class="btn btn-secondary btn-sm">⬅ Orqaga</a>
    </div>
    
    <form class="filter-form" method="GET">
        <div class="form-group">
            <label>Holati</label>
            <select name="status" class="form-control">
                <option value="">Barchasi</option>
                <option value="pending" {{ 'selected' if request.args.get('status') == 'pending' }}>Kutilmoqda</option>
                <option value="completed" {{ 'selected' if request.args.get('status') == 'completed' }}>Bajarilgan</option>
            </select>
        </div>
        <div class="form-group">
            <label>Xodim</label>
            <select name="xodim" class="form-control">
                <option value="">Barchasi</option>
                {% for user in users %}
                    <option value="{{ user.id }}" {{ 'selected' if request.args.get('xodim')|int == user.id }}>{{ user.full_name or user.username }}</option>
                {% endfor %}
            </select>
        </div>
        <button type="submit" class="btn btn-primary">🔍 Filtr</button>
    </form>
    
    <div class="table-container">
        <table>
            <thead>
                <tr>
                    <th>ID</th>
                    <th>Topshiriq</th>
                    <th>Xodim</th>
                    <th>Muddat</th>
                    <th>Holat</th>
                    <th>Yaratilgan</th>
                    <th>Amallar</th>
                </tr>
            </thead>
            <tbody>
                {% for task in tasks %}
                <tr>
                    <td>{{ task.id }}</td>
                    <td>
                        <strong>{{ task.title }}</strong>
                        {% if task.description %}
                            <br><small style="color: #666;">{{ task.description[:100] }}...</small>
                        {% endif %}
                        {% if task.completion_note %}
                            <br><small style="color: #28a745;">💬 {{ task.completion_note }}</small>
                        {% endif %}
                    </td>
                    <td>{{ task.xodim_name or '-' }}</td>
                    <td>
                        {% if task.deadline %}
                            {{ task.deadline.strftime('%d.%m.%Y %H:%M') }}
                            {% if task.status == 'pending' and is_overdue(task.deadline) %}
                                <span class="badge badge-overdue">Muddati o'tgan!</span>
                            {% endif %}
                        {% else %}
                            -
                        {% endif %}
                    </td>
                    <td>
                        {% if task.status == 'completed' %}
                            <span class="badge badge-completed">✅ Bajarilgan</span>
                        {% else %}
                            <span class="badge badge-pending">⏳ Kutilmoqda</span>
                        {% endif %}
                    </td>
                    <td>{{ task.created_at.strftime('%d.%m.%Y') if task.created_at else '-' }}</td>
                    <td>
                        <form method="POST" action="{{ url_for('delete_task', id=task.id) }}" style="display:inline; margin-right:6px;">
                            <button type="submit" class="btn btn-danger btn-sm" onclick="return confirm('Rostdan o\\'chirmoqchimisiz?')">🗑 O'chirish</button>
                        </form>
                        <a href="{{ url_for('edit_task', id=task.id) }}" class="btn btn-primary btn-sm">✏️ O'zgartirish</a>
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
</div>
'''

MY_TASKS_TEMPLATE = '''
<div class="card">
    <div class="card-header">
        <h1>📝 Mening topshiriqlarim</h1>
        <a href="{{ url_for('dashboard') }}" class="btn btn-secondary btn-sm">⬅ Orqaga</a>
    </div>
    
    <div class="table-container">
        <table>
            <thead>
                <tr>
                    <th>ID</th>
                    <th>Topshiriq</th>
                    <th>Muddat</th>
                    <th>Holat</th>
                    <th>Amallar</th>
                </tr>
            </thead>
            <tbody>
                {% for task in tasks %}
                <tr>
                    <td>{{ task.id }}</td>
                    <td>
                        <strong>{{ task.title }}</strong>
                        {% if task.description %}
                            <br><small style="color: #666;">{{ task.description }}</small>
                        {% endif %}
                        {% if task.completion_note %}
                            <br><small style="color: #28a745;">💬 {{ task.completion_note }}</small>
                        {% endif %}
                    </td>
                    <td>
                        {% if task.deadline %}
                            {{ task.deadline.strftime('%d.%m.%Y %H:%M') }}
                            {% if task.status == 'pending' and is_overdue(task.deadline) %}
                                <span class="badge badge-overdue">Muddati o'tgan!</span>
                            {% endif %}
                        {% else %}
                            -
                        {% endif %}
                    </td>
                    <td>
                        {% if task.status == 'completed' %}
                            <span class="badge badge-completed">✅ Bajarilgan</span>
                            {% if task.completed_at %}
                                <br><small>{{ task.completed_at.strftime('%d.%m.%Y %H:%M') }}</small>
                            {% endif %}
                        {% else %}
                            <span class="badge badge-pending">⏳ Kutilmoqda</span>
                        {% endif %}
                    </td>
                    <td>
                        {% if task.status == 'pending' %}
                            <button onclick="showCompleteModal({{ task.id }})" class="btn btn-success btn-sm">✅ Bajarildi</button>
                        {% else %}
                            -
                        {% endif %}
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
</div>

<!-- Modal -->
<div id="completeModal" class="modal-backdrop">
    <div class="modal">
        <h2 style="margin-bottom: 20px;">Topshiriqni yakunlash</h2>
        <form id="completeForm" method="POST">
            <div class="form-group">
                <label>Izoh (ixtiyoriy)</label>
                <textarea name="note" class="form-control" placeholder="Bajarilgan ish haqida izoh..."></textarea>
            </div>
            <div class="actions">
                <button type="submit" class="btn btn-success">✅ Tasdiqlash</button>
                <button type="button" onclick="hideCompleteModal()" class="btn btn-secondary">Bekor qilish</button>
            </div>
        </form>
    </div>
</div>

<script>
function showCompleteModal(taskId) {
    document.getElementById('completeForm').action = '/complete_task/' + taskId;
    document.getElementById('completeModal').classList.add('active');
}

function hideCompleteModal() {
    document.getElementById('completeModal').classList.remove('active');
}

document.getElementById('completeModal').addEventListener('click', function(e) {
    if (e.target === this) hideCompleteModal();
});
</script>
'''

# ============== ROUTELAR ==============
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        if not username or not password:
            flash('Login va parol kiritilishi shart!', 'error')
            return redirect(url_for('login'))
        
        hashed = hashlib.sha256(password.encode()).hexdigest()
        
        conn = get_db()
        user = conn.execute(
            'SELECT * FROM users WHERE username = ? AND password = ?',
            (username, hashed)
        ).fetchone()
        conn.close()
        
        if user:
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']
            session['full_name'] = user['full_name']
            flash(f'Xush kelibsiz, {user["full_name"] or user["username"]}!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Noto\'g\'ri login yoki parol!', 'error')
    
    return render_template_string(BASE_TEMPLATE, title='Kirish', content=LOGIN_TEMPLATE)

@app.route('/logout')
def logout():
    session.clear()
    flash('Tizimdan chiqdingiz', 'info')
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    conn = get_db()
    now = get_uzb_now()
    
    # Statistika
    if session['role'] == 'boss':
        total = conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
        completed = conn.execute('SELECT COUNT(*) FROM tasks WHERE status = "completed"').fetchone()[0]
        pending = conn.execute('SELECT COUNT(*) FROM tasks WHERE status = "pending"').fetchone()[0]
        overdue = conn.execute(
            'SELECT COUNT(*) FROM tasks WHERE status = "pending" AND deadline < ?',
            (now,)
        ).fetchone()[0]
    else:
        total = conn.execute('SELECT COUNT(*) FROM tasks WHERE assigned_to = ?', (session['user_id'],)).fetchone()[0]
        completed = conn.execute('SELECT COUNT(*) FROM tasks WHERE assigned_to = ? AND status = "completed"', (session['user_id'],)).fetchone()[0]
        pending = conn.execute('SELECT COUNT(*) FROM tasks WHERE assigned_to = ? AND status = "pending"', (session['user_id'],)).fetchone()[0]
        overdue = conn.execute(
            'SELECT COUNT(*) FROM tasks WHERE assigned_to = ? AND status = "pending" AND deadline < ?',
            (session['user_id'], now)
        ).fetchone()[0]
    
    conn.close()
    
    stats = {'total': total, 'completed': completed, 'pending': pending, 'overdue': overdue}
    
    return render_template_string(BASE_TEMPLATE, title='Dashboard', content=render_template_string(DASHBOARD_TEMPLATE, stats=stats, session=session))

@app.route('/xodimlar', methods=['GET', 'POST'])
@boss_required
def xodimlar():
    conn = get_db()
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        full_name = request.form.get('full_name', '').strip()
        telegram_chat_id = request.form.get('telegram_chat_id', '').strip()
        
        if not username or not password:
            flash('Login va parol kiritilishi shart!', 'error')
        else:
            hashed = hashlib.sha256(password.encode()).hexdigest()
            try:
                conn.execute(
                    'INSERT INTO users (username, password, role, full_name, telegram_chat_id) VALUES (?, ?, ?, ?, ?)',
                    (username, hashed, 'xodim', full_name or None, telegram_chat_id or None)
                )
                conn.commit()
                flash(f'Xodim "{username}" qo\'shildi!', 'success')
            except sqlite3.IntegrityError:
                flash('Bu login allaqachon mavjud!', 'error')
    
    xodimlar = conn.execute('SELECT * FROM users WHERE role = "xodim" ORDER BY id DESC').fetchall()
    conn.close()
    
    return render_template_string(BASE_TEMPLATE, title='Xodimlar', content=render_template_string(XODIMLAR_TEMPLATE, xodimlar=xodimlar))

@app.route('/delete_xodim/<int:id>', methods=['POST'])
@boss_required
def delete_xodim(id):
    conn = get_db()
    conn.execute('DELETE FROM users WHERE id = ? AND role = "xodim"', (id,))
    conn.commit()
    conn.close()
    flash('Xodim o\'chirildi!', 'success')
    return redirect(url_for('xodimlar'))


@app.route('/delete_task/<int:id>', methods=['POST'])
@boss_required
def delete_task(id):
    conn = get_db()
    conn.execute('DELETE FROM tasks WHERE id = ?', (id,))
    conn.commit()
    conn.close()
    flash('Topshiriq o\'chirildi!', 'success')
    return redirect(url_for('all_tasks'))


@app.route('/edit_xodim/<int:id>', methods=['GET', 'POST'])
@boss_required
def edit_xodim(id):
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id = ? AND role = "xodim"', (id,)).fetchone()
    if not user:
        conn.close()
        flash('Xodim topilmadi!', 'error')
        return redirect(url_for('xodimlar'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        full_name = request.form.get('full_name', '').strip()
        telegram_chat_id = request.form.get('telegram_chat_id', '').strip()

        if not username:
            flash('Username bo\'sh bo\'lmasligi kerak!', 'error')
            conn.close()
            return redirect(url_for('edit_xodim', id=id))

        # Username uniqueness
        if username != user['username']:
            existing = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
            if existing:
                flash('Bu username allaqachon mavjud!', 'error')
                conn.close()
                return redirect(url_for('edit_xodim', id=id))

        try:
            updates = []
            params = []
            updates.append('username = ?')
            params.append(username)

            if password:
                updates.append('password = ?')
                params.append(hashlib.sha256(password.encode()).hexdigest())

            updates.append('full_name = ?')
            params.append(full_name or None)
            updates.append('telegram_chat_id = ?')
            params.append(telegram_chat_id or None)

            params.append(id)
            query = f"UPDATE users SET {', '.join(updates)} WHERE id = ?"
            conn.execute(query, params)
            conn.commit()
            flash('Xodim muvaffaqiyatli yangilandi!', 'success')
        except Exception as e:
            flash(f'Xatolik: {str(e)}', 'error')
        finally:
            conn.close()

        return redirect(url_for('xodimlar'))

    conn.close()
    return render_template_string(BASE_TEMPLATE, title='Xodimni o\'zgartirish', content=render_template_string(EDIT_XODIM_TEMPLATE, user=user))

@app.route('/add_task', methods=['GET', 'POST'])
@boss_required
def add_task():
    conn = get_db()
    
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        assigned_to = request.form.get('assigned_to')
        deadline_date = request.form.get('deadline_date', '').strip()
        deadline_time = request.form.get('deadline_time', '').strip()
        
        # Deadline xavfsiz parsing (alohida sana va vaqt)
        deadline = None
        if deadline_date:
            try:
                if deadline_time:
                    deadline = datetime.strptime(f"{deadline_date} {deadline_time}", '%Y-%m-%d %H:%M')
                else:
                    deadline = datetime.strptime(f"{deadline_date} 23:59", '%Y-%m-%d %H:%M')
            except ValueError:
                pass  # Noto'g'ri format - None qoladi
        
        if not title or not assigned_to:
            flash('Topshiriq nomi va xodim tanlanishi shart!', 'error')
        else:
            try:
                cursor = conn.execute(
                    'INSERT INTO tasks (title, description, assigned_to, deadline) VALUES (?, ?, ?, ?)',
                    (title, description or None, int(assigned_to), deadline)
                )
                conn.commit()
                
                # Telegram xabar yuborish
                notify_user_new_task(int(assigned_to), title, deadline)
                
                flash('Topshiriq qo\'shildi!', 'success')
                return redirect(url_for('all_tasks'))
            except Exception as e:
                flash(f'Xatolik: {str(e)}', 'error')
    
    # Barcha foydalanuvchilar (boss ham, xodim ham)
    users = conn.execute('SELECT * FROM users ORDER BY role DESC, full_name').fetchall()
    conn.close()
    
    return render_template_string(BASE_TEMPLATE, title='Topshiriq qo\'shish', content=render_template_string(ADD_TASK_TEMPLATE, users=users))


@app.route('/edit_task/<int:id>', methods=['GET', 'POST'])
@boss_required
def edit_task(id):
    conn = get_db()
    task = conn.execute('SELECT * FROM tasks WHERE id = ?', (id,)).fetchone()
    if not task:
        conn.close()
        flash('Topshiriq topilmadi!', 'error')
        return redirect(url_for('all_tasks'))

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        assigned_to = request.form.get('assigned_to')
        deadline_date = request.form.get('deadline_date', '').strip()
        deadline_time = request.form.get('deadline_time', '').strip()

        deadline = None
        if deadline_date:
            try:
                if deadline_time:
                    deadline = datetime.strptime(f"{deadline_date} {deadline_time}", '%Y-%m-%d %H:%M')
                else:
                    deadline = datetime.strptime(f"{deadline_date} 23:59", '%Y-%m-%d %H:%M')
            except ValueError:
                deadline = None

        if not title or not assigned_to:
            flash('Topshiriq nomi va xodim tanlanishi shart!', 'error')
            conn.close()
            return redirect(url_for('edit_task', id=id))

        try:
            conn.execute(
                'UPDATE tasks SET title = ?, description = ?, assigned_to = ?, deadline = ? WHERE id = ?',
                (title, description or None, int(assigned_to), deadline, id)
            )
            conn.commit()
            flash('Topshiriq muvaffaqiyatli yangilandi!', 'success')
        except Exception as e:
            flash(f'Xatolik: {str(e)}', 'error')
        finally:
            conn.close()

        return redirect(url_for('all_tasks'))

    # Prepare deadline date/time values for form
    deadline_date = ''
    deadline_time = ''
    if task['deadline']:
        try:
            deadline_date = task['deadline'].strftime('%Y-%m-%d')
            deadline_time = task['deadline'].strftime('%H:%M')
        except Exception:
            deadline_date = ''
            deadline_time = ''

    conn2 = get_db()
    users = conn2.execute('SELECT * FROM users ORDER BY role DESC, full_name').fetchall()
    conn2.close()

    return render_template_string(BASE_TEMPLATE, title='Topshiriqni o\'zgartirish', content=render_template_string(EDIT_TASK_TEMPLATE, task=task, users=users, deadline_date=deadline_date, deadline_time=deadline_time))

@app.route('/all_tasks')
@boss_required
def all_tasks():
    conn = get_db()
    
    query = '''
        SELECT t.*, u.full_name as xodim_name 
        FROM tasks t 
        LEFT JOIN users u ON t.assigned_to = u.id 
        WHERE 1=1
    '''
    params = []
    
    status = request.args.get('status')
    if status:
        query += ' AND t.status = ?'
        params.append(status)
    
    xodim_id = request.args.get('xodim')
    if xodim_id:
        query += ' AND t.assigned_to = ?'
        params.append(int(xodim_id))
    
    query += ' ORDER BY t.id DESC'
    
    tasks = conn.execute(query, params).fetchall()
    users = conn.execute('SELECT * FROM users ORDER BY role DESC, full_name').fetchall()
    conn.close()
    
    return render_template_string(BASE_TEMPLATE, title='Barcha topshiriqlar', content=render_template_string(ALL_TASKS_TEMPLATE, tasks=tasks, users=users, is_overdue=is_overdue, request=request))

@app.route('/my_tasks')
@login_required
def my_tasks():
    conn = get_db()
    tasks = conn.execute(
        'SELECT * FROM tasks WHERE assigned_to = ? ORDER BY id DESC',
        (session['user_id'],)
    ).fetchall()
    conn.close()
    
    return render_template_string(BASE_TEMPLATE, title='Mening topshiriqlarim', content=render_template_string(MY_TASKS_TEMPLATE, tasks=tasks, is_overdue=is_overdue))

@app.route('/complete_task/<int:id>', methods=['POST'])
@login_required
def complete_task(id):
    note = request.form.get('note', '').strip()
    now = get_uzb_now()
    
    conn = get_db()
    
    # Faqat o'ziga berilgan topshiriqni yakunlay oladi
    task = conn.execute('SELECT * FROM tasks WHERE id = ? AND assigned_to = ?', (id, session['user_id'])).fetchone()
    
    if not task:
        flash('Topshiriq topilmadi yoki sizga tegishli emas!', 'error')
    else:
        conn.execute(
            'UPDATE tasks SET status = ?, completion_note = ?, completed_at = ? WHERE id = ?',
            ('completed', note or None, now, id)
        )
        conn.commit()
        
        # Bossga xabar yuborish
        notify_boss_task_completed(id)
        
        flash('Topshiriq bajarildi deb belgilandi!', 'success')
    
    conn.close()
    return redirect(url_for('my_tasks'))

@app.route('/change_profile', methods=['GET', 'POST'])
@boss_required
def change_profile():
    if request.method == 'POST':
        new_username = request.form.get('new_username', '').strip()
        new_password = request.form.get('new_password', '').strip()
        confirm_password = request.form.get('confirm_password', '').strip()
        current_password = request.form.get('current_password', '').strip()
        
        conn = get_db()
        user = conn.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        
        # Hozirgi parolni tekshirish
        hashed_current = hashlib.sha256(current_password.encode()).hexdigest()
        if hashed_current != user['password']:
            flash('Hozirgi parol noto\'g\'ri!', 'error')
            conn.close()
            return redirect(url_for('change_profile'))
        
        # Yangi username tekshirish
        if new_username and new_username != session['username']:
            existing = conn.execute('SELECT * FROM users WHERE username = ?', (new_username,)).fetchone()
            if existing:
                flash('Bu username allaqachon mavjud!', 'error')
                conn.close()
                return redirect(url_for('change_profile'))
        
        # Yangi parolni tekshirish
        if new_password:
            if new_password != confirm_password:
                flash('Parollar mos kelmadi!', 'error')
                conn.close()
                return redirect(url_for('change_profile'))
            if len(new_password) < 4:
                flash('Parol kamida 4 ta belgidan iborat bo\'lishi kerak!', 'error')
                conn.close()
                return redirect(url_for('change_profile'))
        
        # O'zgartirishlarni saqlash
        try:
            updates = []
            params = []
            
            if new_username and new_username != session['username']:
                updates.append('username = ?')
                params.append(new_username)
                session['username'] = new_username
            
            if new_password:
                updates.append('password = ?')
                params.append(hashlib.sha256(new_password.encode()).hexdigest())
            
            if updates:
                params.append(session['user_id'])
                query = f"UPDATE users SET {', '.join(updates)} WHERE id = ?"
                conn.execute(query, params)
                conn.commit()
                flash('Profil muvaffaqiyatli o\'zgartirildi!', 'success')
            else:
                flash('Hech narsa o\'zgartirilmadi', 'info')
        except Exception as e:
            flash(f'Xatolik: {str(e)}', 'error')
        finally:
            conn.close()
        
        return redirect(url_for('dashboard'))
    
    return render_template_string(BASE_TEMPLATE, title='Profil o\'zgartirish', content=render_template_string(CHANGE_PROFILE_TEMPLATE))

@app.route('/export_csv')
@boss_required
def export_csv():
    conn = get_db()
    tasks = conn.execute('''
        SELECT t.id, t.title, t.description, u.full_name as xodim_name, 
               t.deadline, t.status, t.completion_note, t.completed_at, t.created_at
        FROM tasks t 
        LEFT JOIN users u ON t.assigned_to = u.id 
        ORDER BY t.id DESC
    ''').fetchall()
    conn.close()
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Sarlavhalar
    writer.writerow(['ID', 'Topshiriq', 'Tavsif', 'Xodim', 'Muddat', 'Holat', 'Izoh', 'Bajarilgan sana', 'Yaratilgan sana'])
    
    for task in tasks:
        writer.writerow([
            task['id'],
            task['title'],
            task['description'] or '',
            task['xodim_name'] or '',
            task['deadline'].strftime('%d.%m.%Y %H:%M') if task['deadline'] else '',
            'Bajarilgan' if task['status'] == 'completed' else 'Kutilmoqda',
            task['completion_note'] or '',
            task['completed_at'].strftime('%d.%m.%Y %H:%M') if task['completed_at'] else '',
            task['created_at'].strftime('%d.%m.%Y %H:%M') if task['created_at'] else ''
        ])
    
    output.seek(0)
    
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=topshiriqlar.csv'}
    )

# ============== ISHGA TUSHIRISH ==============
if __name__ == '__main__':
    # Bazani yaratish
    init_db()
    
    # Reminder threadni boshlash
    start_reminder_thread()
    
    print("=" * 50)
    print("CRM Tizimi ishga tushdi!")
    print("=" * 50)
    print("Default login: boss / magistr")
    print("Parolni o'zgartirishni unutmang!")
    print("=" * 50)
    print("\nTelegram integratsiyasi uchun:")
    print("export TELEGRAM_BOT_TOKEN='your_token'")
    print("export BOSS_TELEGRAM_CHAT_ID='your_chat_id'")
    print("=" * 50)
    
    # Production va development uchun portni sozlash
    port = int(os.environ.get('PORT', 5000))
    
    # Flask serverni ishga tushirish
    app.run(host='0.0.0.0', port=port, debug=False)
