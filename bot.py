import asyncio
import logging
import io
import sqlite3
import os
from contextlib import contextmanager
from collections import Counter
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice, PreCheckoutQuery
from aiogram.fsm.storage.memory import MemoryStorage

# ============================================
# ===== НАСТРОЙКИ =====
# ============================================

BOT_TOKEN = "8957792151:AAGox2cCtmSaylijWd5IGHQzOyoX9lhNymA"
ADMIN_ID = 7921694564# Твой Telegram ID
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 123456789))
# ============================================
# ===== БАЗА ДАННЫХ (SQLite) =====
# ============================================

DB_PATH = "bot_database.db"

@contextmanager
def get_db():
    """Контекстный менеджер для работы с БД"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    """Создаём таблицы при первом запуске"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Таблица пользователей
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                tokens INTEGER DEFAULT 3,
                first_visit TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_visit TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Таблица рефералов
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inviter_id INTEGER,
                invited_id INTEGER UNIQUE,
                bonus_given INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (inviter_id) REFERENCES users (user_id),
                FOREIGN KEY (invited_id) REFERENCES users (user_id)
            )
        ''')
        
        # Таблица покупок
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                tokens INTEGER,
                stars INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')
        
        # Таблица для статистики использования (кол-во обработанных фото)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS usage_stats (
                user_id INTEGER PRIMARY KEY,
                photos_processed INTEGER DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')
        
        conn.commit()

# ===== ФУНКЦИИ РАБОТЫ С БД =====

def get_user_tokens(user_id: int) -> int:
    """Получить баланс пользователя"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT tokens FROM users WHERE user_id = ?",
            (user_id,)
        )
        row = cursor.fetchone()
        if row:
            return row["tokens"]
        else:
            # Новый пользователь — создаём с 3 токенами
            cursor.execute(
                "INSERT INTO users (user_id, tokens) VALUES (?, ?)",
                (user_id, 3)
            )
            cursor.execute(
                "INSERT INTO usage_stats (user_id, photos_processed) VALUES (?, ?)",
                (user_id, 0)
            )
            conn.commit()
            return 3

def update_user_tokens(user_id: int, change: int):
    """Изменить баланс пользователя (+ или -)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET tokens = tokens + ?, last_visit = CURRENT_TIMESTAMP WHERE user_id = ?",
            (change, user_id)
        )
        conn.commit()

def increment_photos_processed(user_id: int):
    """Увеличить счетчик обработанных фото"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE usage_stats SET photos_processed = photos_processed + 1 WHERE user_id = ?",
            (user_id,)
        )
        conn.commit()

def get_photos_processed(user_id: int) -> int:
    """Получить количество обработанных фото"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT photos_processed FROM usage_stats WHERE user_id = ?",
            (user_id,)
        )
        row = cursor.fetchone()
        return row["photos_processed"] if row else 0

def get_all_users_count() -> int:
    """Количество пользователей"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as count FROM users")
        return cursor.fetchone()["count"]

def get_total_tokens() -> int:
    """Всего токенов в системе"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT SUM(tokens) as total FROM users")
        result = cursor.fetchone()
        return result["total"] if result["total"] else 0

def get_total_photos_processed() -> int:
    """Всего обработанных фото"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT SUM(photos_processed) as total FROM usage_stats")
        result = cursor.fetchone()
        return result["total"] if result["total"] else 0

def add_referral(inviter_id: int, invited_id: int, bonus: int):
    """Записать реферальный переход"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO referrals (inviter_id, invited_id, bonus_given) VALUES (?, ?, ?)",
            (inviter_id, invited_id, bonus)
        )
        conn.commit()

def get_referral_count(user_id: int) -> int:
    """Сколько людей пригласил пользователь"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) as count FROM referrals WHERE inviter_id = ?",
            (user_id,)
        )
        return cursor.fetchone()["count"]

def get_referral_bonus(user_id: int) -> int:
    """Сколько бонусов получил пригласивший"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT SUM(bonus_given) as total FROM referrals WHERE inviter_id = ?",
            (user_id,)
        )
        result = cursor.fetchone()
        return result["total"] if result["total"] else 0

def is_referral_used(user_id: int) -> bool:
    """Проверка: пользователь уже использовал чужую ссылку"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM referrals WHERE invited_id = ?",
            (user_id,)
        )
        return cursor.fetchone() is not None

def add_purchase(user_id: int, tokens: int, stars: int):
    """Записать покупку"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO purchases (user_id, tokens, stars) VALUES (?, ?, ?)",
            (user_id, tokens, stars)
        )
        conn.commit()

def get_top_referrals(limit: int = 10) -> list:
    """Топ приглашающих"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT 
                u.user_id,
                COUNT(r.id) as invite_count,
                SUM(r.bonus_given) as total_bonus
            FROM users u
            LEFT JOIN referrals r ON u.user_id = r.inviter_id
            GROUP BY u.user_id
            ORDER BY invite_count DESC
            LIMIT ?
        ''', (limit,))
        return cursor.fetchall()

def get_recent_purchases(limit: int = 10) -> list:
    """Последние покупки"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT user_id, tokens, stars, created_at 
            FROM purchases 
            ORDER BY created_at DESC 
            LIMIT ?
        ''', (limit,))
        return cursor.fetchall()

def get_total_revenue() -> int:
    """Общая выручка в звездах"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT SUM(stars) as total FROM purchases")
        result = cursor.fetchone()
        return result["total"] if result["total"] else 0

# ============================================
# ===== ЦЕНЫ =====
# ============================================

STAR_PACKAGES = {
    "10": {"stars": 85, "tokens": 10, "label": "85 ⭐ → 10 токенов", "old_price": 100},
    "25": {"stars": 212, "tokens": 25, "label": "212 ⭐ → 25 токенов", "old_price": 250},
    "50": {"stars": 425, "tokens": 50, "label": "425 ⭐ → 50 токенов", "old_price": 500},
    "100": {"stars": 850, "tokens": 100, "label": "850 ⭐ → 100 токенов", "old_price": 1000},
}

# ============================================
# ===== ИНИЦИАЛИЗАЦИЯ БОТА =====
# ============================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Инициализируем БД
init_db()


# ============================================
# ===== ОБРАБОТКА ИЗОБРАЖЕНИЙ =====
# ============================================

def group_similar_colors(colors_with_counts, threshold=25):
    if not colors_with_counts:
        return []
    
    grouped = []
    used = set()
    sorted_colors = sorted(colors_with_counts.items(), key=lambda x: x[1], reverse=True)
    
    for color1, count1 in sorted_colors:
        if color1 in used:
            continue
        
        group_colors = [color1]
        group_count = count1
        r1, g1, b1 = hex_to_rgb(color1)
        
        for color2, count2 in sorted_colors:
            if color2 in used or color2 == color1:
                continue
            
            r2, g2, b2 = hex_to_rgb(color2)
            distance = ((r1 - r2) ** 2 + (g1 - g2) ** 2 + (b1 - b2) ** 2) ** 0.5
            
            if distance < threshold:
                group_colors.append(color2)
                group_count += count2
                used.add(color2)
        
        used.add(color1)
        
        avg_r = sum(hex_to_rgb(c)[0] for c in group_colors) // len(group_colors)
        avg_g = sum(hex_to_rgb(c)[1] for c in group_colors) // len(group_colors)
        avg_b = sum(hex_to_rgb(c)[2] for c in group_colors) // len(group_colors)
        
        avg_hex = f"#{avg_r:02x}{avg_g:02x}{avg_b:02x}"
        grouped.append((avg_hex, group_count))
    
    return grouped


def extract_dominant_colors(image_bytes: bytes, max_colors=12):
    img = Image.open(io.BytesIO(image_bytes))
    img.thumbnail((300, 300))
    
    if img.mode != 'RGB':
        img = img.convert('RGB')
    
    pixels = list(img.getdata())
    color_counts = Counter()
    
    for r, g, b in pixels:
        brightness = (r * 299 + g * 587 + b * 114) / 1000
        if brightness < 20 or brightness > 235:
            continue
        hex_color = f"#{r:02x}{g:02x}{b:02x}"
        color_counts[hex_color] += 1
    
    if not color_counts:
        for r, g, b in pixels:
            hex_color = f"#{r:02x}{g:02x}{b:02x}"
            color_counts[hex_color] += 1
    
    grouped = group_similar_colors(color_counts, threshold=30)
    
    result = []
    for color, count in grouped[:max_colors]:
        if count / len(pixels) < 0.01:
            continue
        result.append(color)
    
    if len(result) < 5:
        for color, count in grouped[max_colors:]:
            if len(result) >= max_colors:
                break
            if color not in result:
                result.append(color)
    
    return result


def create_palette_image(colors: list):
    if not colors:
        return None
    
    rect_width = 1200
    rect_height = 200
    font_size = 56
    
    img_width = rect_width
    total_height = len(colors) * rect_height
    
    img = Image.new('RGB', (img_width, total_height), '#000000')
    draw = ImageDraw.Draw(img)
    
    try:
        font = ImageFont.truetype("arial.ttf", font_size)
    except:
        try:
            font = ImageFont.truetype("arialbd.ttf", font_size)
        except:
            font = ImageFont.load_default()
    
    for idx, hex_color in enumerate(colors):
        y = idx * rect_height
        x = 0
        
        try:
            color_rgb = hex_to_rgb(hex_color)
        except:
            continue
        
        draw.rectangle(
            [x, y, x + rect_width, y + rect_height],
            fill=color_rgb
        )
        
        text_color = 'white'
        text = f"{idx + 1}. {hex_color.upper()}"
        
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]
            text_x = (rect_width - text_width) // 2
            text_y = y + (rect_height - text_height) // 2
        except:
            text_x = rect_width // 2 - 50
            text_y = y + 50
        
        try:
            draw.text((text_x + 3, text_y + 3), text, fill='black', font=font)
        except:
            pass
        
        draw.text((text_x, text_y), text, fill=text_color, font=font)
        
        if idx < len(colors) - 1:
            line_y = y + rect_height - 1
            draw.line(
                [(0, line_y), (rect_width, line_y)],
                fill=(255, 255, 255, 30),
                width=2
            )
    
    img_bytes = io.BytesIO()
    img.save(img_bytes, format='PNG', quality=100, optimize=False)
    img_bytes.seek(0)
    
    return img_bytes.getvalue()


def hex_to_rgb(hex_color):
    hex_color = hex_color.lstrip('#')
    return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))


# ============================================
# ===== РЕФЕРАЛЬНАЯ СИСТЕМА =====
# ============================================

def generate_ref_link(user_id: int) -> str:
    """Генерирует реферальную ссылку для пользователя"""
    bot_username = bot.username or "YourBot"
    return f"https://t.me/{bot_username}?start=ref_{user_id}"

def process_referral(new_user_id: int, inviter_id: int) -> tuple:
    """
    Обрабатывает реферальный переход.
    Возвращает (success, message)
    """
    # Проверка: нельзя пригласить самого себя
    if new_user_id == inviter_id:
        return False, "❌ Нельзя пригласить самого себя!"
    
    # Проверка: пользователь уже использовал чужую ссылку
    if is_referral_used(new_user_id):
        return False, "❌ Ты уже использовал реферальную ссылку!"
    
    # Проверяем, не приглашал ли уже этого человека
    existing = get_referral_count(inviter_id)
    # Уникальность гарантируется БД (UNIQUE constraint на invited_id)
    
    # Бонус новому пользователю (+1 токен)
    update_user_tokens(new_user_id, 1)
    
    # Бонус пригласившему: 2 за первого, 1 за остальных
    invited_count = get_referral_count(inviter_id)
    bonus = 2 if invited_count == 0 else 1
    update_user_tokens(inviter_id, bonus)
    
    # Записываем рефералку
    add_referral(inviter_id, new_user_id, bonus)
    
    return True, f"✅ Ты получил +{bonus} токен{'а' if bonus > 1 else ''} за приглашение!"


# ============================================
# ===== КОМАНДЫ =====
# ============================================

# 1. /start - обрабатывает реферальные ссылки
@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject = None):
    user_id = message.from_user.id
    
    # Проверяем, есть ли реферальный параметр
    if command and command.args and command.args.startswith("ref_"):
        try:
            inviter_id = int(command.args.replace("ref_", ""))
            
            # Получаем токены пользователя (создаём запись в БД)
            get_user_tokens(user_id)
            
            # Обрабатываем реферальный переход
            success, msg = process_referral(user_id, inviter_id)
            await message.answer(msg)
        except:
            pass
    
    # Получаем баланс
    tokens = get_user_tokens(user_id)
    
    # Основное меню
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 Отправить фото", callback_data="send_photo")],
        [InlineKeyboardButton(text="💰 Баланс", callback_data="balance"),
         InlineKeyboardButton(text="⭐ Купить токены", callback_data="buy")],
        [InlineKeyboardButton(text="👥 Рефералка", callback_data="referral"),
         InlineKeyboardButton(text="❓ Помощь", callback_data="help")]
    ])
    
    # 🔥 ВСТАВЬ ССЫЛКУ НА КАРТИНКУ СЮДА:
    photo_url = "https://i.ibb.co/Q3x4fGKc/image.png"
    
    await message.answer_photo(
        photo=photo_url,
        caption=(
            f"🎨 <b>Бот-палитра PRO</b>\n\n"
            f"Привет! Я нахожу <b>главные цвета</b> на твоих фото.\n\n"
            f"💰 У тебя <b>{tokens}</b> токенов.\n"
            f"⚡ 1 токен = 1 фото\n\n"
            f"👥 <b>Приглашай друзей и получай бонусы!</b>\n"
            f"• За первого друга → <b>+2 токена</b>\n"
            f"• За каждого следующего → <b>+1 токен</b>\n"
            f"• Твой друг тоже получит <b>+1 токен</b>\n\n"
            f"📸 Отправь фото или нажми на кнопку ниже!"
        ),
        parse_mode='HTML',
        reply_markup=keyboard
    )

# 2. /help
@dp.message(Command("help"))
async def cmd_help(message: Message):
    help_text = (
        f"❓ <b>Помощь по боту</b>\n\n"
        f"📸 <b>Как пользоваться:</b>\n"
        f"1. Отправь мне любое фото\n"
        f"2. Я найду <b>главные цвета</b> на нём\n"
        f"3. Отдам палитру в виде цветных блоков\n\n"
        f"💰 <b>Токены:</b>\n"
        f"• Новым пользователям даётся <b>3</b> бесплатных токена\n"
        f"• 1 токен = 1 фото\n"
        f"• Команда /balance — проверить баланс\n"
        f"• Команда /buy — купить токены за Stars\n\n"
        f"👥 <b>Реферальная система:</b>\n"
        f"• Пригласи друга → получи <b>бонусные токены</b>\n"
        f"• За первого друга → <b>+2 токена</b>\n"
        f"• За каждого следующего → <b>+1 токен</b>\n"
        f"• Твой друг получит <b>+1 токен</b> за переход\n"
        f"• Команда /referral — получить ссылку\n\n"
        f"🔥 <b>Цены со скидкой 15%:</b>\n"
        f"• 85 ⭐ → 10 токенов (было 100 ⭐)\n"
        f"• 212 ⭐ → 25 токенов (было 250 ⭐)\n"
        f"• 425 ⭐ → 50 токенов (было 500 ⭐)\n"
        f"• 850 ⭐ → 100 токенов (было 1000 ⭐)"
    )
    
    await message.answer(help_text, parse_mode='HTML')


# 3. /balance
@dp.message(Command("balance"))
async def cmd_balance(message: Message):
    user_id = message.from_user.id
    tokens = get_user_tokens(user_id)
    photos = get_photos_processed(user_id)
    referrals_count = get_referral_count(user_id)
    referral_bonus = get_referral_bonus(user_id)
    
    filled = min(tokens, 10)
    empty = 10 - filled
    bar = "█" * filled + "░" * empty
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Купить токены", callback_data="buy")],
        [InlineKeyboardButton(text="👥 Пригласить друга", callback_data="referral")]
    ])
    
    await message.answer(
        f"💰 <b>Твой баланс</b>\n\n"
        f"{bar}\n"
        f"<b>{tokens}</b> токенов\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"📸 Обработано фото: <b>{photos}</b>\n"
        f"👥 Приглашено друзей: <b>{referrals_count}</b>\n"
        f"🎁 Получено бонусов: <b>{referral_bonus}</b>\n\n"
        f"💡 <i>1 токен = 1 анализ фото</i>",
        parse_mode='HTML',
        reply_markup=keyboard
    )


# 4. /referral
@dp.message(Command("referral"))
async def cmd_referral(message: Message):
    user_id = message.from_user.id
    
    # Генерируем ссылку
    ref_link = generate_ref_link(user_id)
    
    # Статистика приглашений
    invited_count = get_referral_count(user_id)
    bonus_total = get_referral_bonus(user_id)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Поделиться", switch_inline_query=f"🎨 Попробуй бот-палитру! {ref_link}")]
    ])
    
    await message.answer(
        f"👥 <b>Твоя реферальная ссылка</b>\n\n"
        f"🔗 <code>{ref_link}</code>\n\n"
        f"📊 Приглашено друзей: <b>{invited_count}</b>\n"
        f"🎁 Получено бонусов: <b>{bonus_total}</b> токенов\n\n"
        f"🎁 <b>Бонусы за приглашения:</b>\n"
        f"• За первого друга → <b>+2 токена</b>\n"
        f"• За каждого следующего → <b>+1 токен</b>\n"
        f"• Твой друг получит <b>+1 токен</b>\n\n"
        f"💡 <i>Отправь ссылку другу, и когда он запустит бота, вы оба получите бонусы!</i>",
        parse_mode='HTML',
        reply_markup=keyboard
    )


# 5. /buy
@dp.message(Command("buy"))
async def cmd_buy(message: Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 85 ⭐ → 10 токенов (-15%)", callback_data="buy_10")],
        [InlineKeyboardButton(text="🔥 212 ⭐ → 25 токенов (-15%)", callback_data="buy_25")],
        [InlineKeyboardButton(text="🔥 425 ⭐ → 50 токенов (-15%)", callback_data="buy_50")],
        [InlineKeyboardButton(text="🔥 850 ⭐ → 100 токенов (-15%)", callback_data="buy_100")],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="help")]
    ])
    
    await message.answer(
        f"💳 <b>Покупка токенов за Telegram Stars</b>\n\n"
        f"⭐ <b>Курс:</b> 8.5 ⭐ = 1 токен\n\n"
        f"🔥 <b>СКИДКА 15% НА ВСЕ ПАКЕТЫ!</b>\n\n"
        f"📦 <b>Доступные пакеты:</b>\n"
        f"• 85 ⭐ → 10 токенов (было 100 ⭐)\n"
        f"• 212 ⭐ → 25 токенов (было 250 ⭐)\n"
        f"• 425 ⭐ → 50 токенов (было 500 ⭐)\n"
        f"• 850 ⭐ → 100 токенов (было 1000 ⭐)\n\n"
        f"⬇️ <i>Нажми на пакет ниже для оплаты</i>",
        parse_mode='HTML',
        reply_markup=keyboard
    )


# 6. /info
@dp.message(Command("info"))
async def cmd_info(message: Message):
    total_users = get_all_users_count()
    total_tokens = get_total_tokens()
    total_photos = get_total_photos_processed()
    total_revenue = get_total_revenue()
    
    await message.answer(
        f"ℹ️ <b>О боте</b>\n\n"
        f"🎨 <b>Бот-палитра PRO</b> v1.0\n\n"
        f"📌 <b>Что умеет:</b>\n"
        f"• Находит <b>основные цвета</b> на фото\n"
        f"• Сортирует от <b>самого популярного цвета</b> к редкому\n"
        f"• Группирует <b>похожие оттенки</b>\n\n"
        f"👥 <b>Реферальная система:</b>\n"
        f"• Приглашай друзей и получай токены\n"
        f"• Команда /referral для ссылки\n\n"
        f"📊 <b>Статистика бота:</b>\n"
        f"👥 Пользователей: <b>{total_users}</b>\n"
        f"💰 Всего токенов: <b>{total_tokens}</b>\n"
        f"📸 Обработано фото: <b>{total_photos}</b>\n"
        f"⭐ Выручка: <b>{total_revenue}</b> Stars\n\n"
        f"🔥 <b>Скидка 15% на все пакеты!</b>\n\n"
        f"👨‍💻 <b>Разработчик:</b> @shoipov314",
        parse_mode='HTML'
    )


# 7. /feedback
@dp.message(Command("feedback"))
async def cmd_feedback(message: Message):
    await message.answer(
        f"📝 <b>Обратная связь</b>\n\n"
        f"Напиши своё сообщение сюда, и я передам его разработчику.\n\n"
        f"💡 <i>Можешь предложить новую фичу, сообщить о баге или просто сказать спасибо 😊</i>",
        parse_mode='HTML'
    )


# 8. Обработка текстовых сообщений (feedback)
@dp.message(lambda msg: msg.text and not msg.text.startswith('/'))
async def handle_feedback(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username or "без username"
    text = message.text
    
    try:
        await bot.send_message(
            ADMIN_ID,
            f"📩 <b>Новое сообщение от пользователя</b>\n\n"
            f"👤 ID: <code>{user_id}</code>\n"
            f"👤 Username: @{username}\n"
            f"📝 Сообщение:\n{text}",
            parse_mode='HTML'
        )
        await message.answer("✅ Спасибо! Твоё сообщение отправлено разработчику.")
    except:
        await message.answer("⚠️ Не удалось отправить сообщение. Попробуй позже.")


# ============================================
# ===== АДМИНСКИЕ КОМАНДЫ =====
# ============================================

@dp.message(Command("add_tokens"))
async def cmd_add_tokens(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ У тебя нет прав для этой команды.")
        return
    
    args = message.text.split()
    if len(args) != 3:
        await message.answer("❌ Формат: /add_tokens USER_ID КОЛИЧЕСТВО")
        return
    
    try:
        target_user = int(args[1])
        amount = int(args[2])
        update_user_tokens(target_user, amount)
        new_balance = get_user_tokens(target_user)
        await message.answer(
            f"✅ Пользователю <code>{target_user}</code> добавлено <b>{amount}</b> токенов.\n"
            f"📊 Новый баланс: <b>{new_balance}</b>",
            parse_mode='HTML'
        )
    except ValueError:
        await message.answer("❌ ID и количество должны быть числами.")


@dp.message(Command("reset_tokens"))
async def cmd_reset_tokens(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ У тебя нет прав для этой команды.")
        return
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET tokens = 3")
        cursor.execute("DELETE FROM referrals")
        cursor.execute("DELETE FROM purchases")
        conn.commit()
    
    await message.answer("✅ Все данные сброшены. У всех по 3 токена.")


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ У тебя нет прав для этой команды.")
        return
    
    total_users = get_all_users_count()
    total_tokens = get_total_tokens()
    total_photos = get_total_photos_processed()
    total_revenue = get_total_revenue()
    total_purchases = len(get_recent_purchases(9999))
    
    await message.answer(
        f"📊 <b>Статистика бота</b>\n\n"
        f"👥 Пользователей: <b>{total_users}</b>\n"
        f"💰 Всего токенов: <b>{total_tokens}</b>\n"
        f"📸 Обработано фото: <b>{total_photos}</b>\n"
        f"⭐ Выручка: <b>{total_revenue}</b> Stars\n"
        f"🛒 Покупок: <b>{total_purchases}</b>\n"
        f"📊 Средний баланс: <b>{total_tokens // total_users if total_users > 0 else 0}</b>",
        parse_mode='HTML'
    )


@dp.message(Command("top"))
async def cmd_top(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только для админа.")
        return
    
    top = get_top_referrals(10)
    
    if not top:
        await message.answer("📊 Пока нет приглашений.")
        return
    
    text = "🏆 <b>Топ приглашающих</b>\n\n"
    for i, row in enumerate(top, 1):
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        medal = medals.get(i, f"{i}.")
        text += f"{medal} <code>{row['user_id']}</code> → {row['invite_count']} чел. (+{row['total_bonus']} токенов)\n"
    
    await message.answer(text, parse_mode='HTML')


@dp.message(Command("recent_purchases"))
async def cmd_recent_purchases(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только для админа.")
        return
    
    purchases = get_recent_purchases(10)
    
    if not purchases:
        await message.answer("📊 Пока нет покупок.")
        return
    
    text = "🛒 <b>Последние покупки</b>\n\n"
    for row in purchases:
        text += f"👤 <code>{row['user_id']}</code> → +{row['tokens']} токенов за {row['stars']} ⭐\n"
        text += f"   📅 {row['created_at']}\n\n"
    
    await message.answer(text, parse_mode='HTML')


# ============================================
# ===== ОПЛАТА ЧЕРЕЗ TELEGRAM STARS =====
# ============================================

@dp.callback_query(lambda c: c.data and c.data.startswith("buy_"))
async def process_buy(callback: types.CallbackQuery):
    package_key = callback.data.replace("buy_", "")
    
    if package_key not in STAR_PACKAGES:
        await callback.answer("❌ Неверный пакет")
        return
    
    package = STAR_PACKAGES[package_key]
    stars = package["stars"]
    tokens = package["tokens"]
    old_price = package["old_price"]
    
    user_id = callback.from_user.id
    
    try:
        await bot.send_invoice(
            chat_id=user_id,
            title=f"🔥 {stars} ⭐ → {tokens} токенов (скидка 15%)",
            description=f"Пополнение баланса на {tokens} токенов.\n"
                        f"Было: {old_price} ⭐ | Сейчас: {stars} ⭐\n"
                        f"Экономия: {old_price - stars} ⭐",
            payload=f"tokens_{tokens}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=f"{stars} Stars", amount=stars)],
            start_parameter=f"buy_{package_key}",
            photo_url="https://ibb.co/C3SfzFHt",
            photo_width=512,
            photo_height=512
        )
        
        await callback.answer()
        
    except Exception as e:
        await callback.message.answer(f"⚠️ Ошибка при создании платежа: {e}")
        await callback.answer("❌ Ошибка", show_alert=True)


@dp.pre_checkout_query()
async def pre_checkout_query(pre_checkout: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout.id, ok=True)


@dp.message(lambda msg: msg.successful_payment is not None)
async def successful_payment(message: Message):
    user_id = message.from_user.id
    payment = message.successful_payment
    
    payload = payment.invoice_payload
    tokens_count = int(payload.split("_")[1])
    
    # Начисляем токены
    update_user_tokens(user_id, tokens_count)
    current_balance = get_user_tokens(user_id)
    
    stars = payment.total_amount
    
    # Записываем покупку
    add_purchase(user_id, tokens_count, stars)
    
    await message.answer(
        f"✅ <b>Оплата прошла успешно!</b>\n\n"
        f"⭐ Оплачено: <b>{stars} Stars</b>\n"
        f"💰 Добавлено: <b>{tokens_count}</b> токенов\n"
        f"📊 Твой баланс: <b>{current_balance}</b> токенов\n\n"
        f"📸 Можешь отправить новое фото для анализа!",
        parse_mode='HTML'
    )
    
    username = message.from_user.username or "без username"
    try:
        await bot.send_message(
            ADMIN_ID,
            f"💰 <b>Новая покупка!</b>\n\n"
            f"👤 Пользователь: @{username} (ID: <code>{user_id}</code>)\n"
            f"⭐ Звезд: <b>{stars}</b>\n"
            f"📦 Токенов: <b>{tokens_count}</b>\n"
            f"📊 Новый баланс: <b>{current_balance}</b>",
            parse_mode='HTML'
        )
    except:
        pass


# ============================================
# ===== ОБРАБОТКА ФОТО =====
# ============================================

@dp.message(lambda msg: msg.photo is not None)
async def handle_photo(message: Message):
    user_id = message.from_user.id

    # Проверяем баланс
    tokens = get_user_tokens(user_id)
    
    if tokens <= 0:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⭐ Купить токены", callback_data="buy")],
            [InlineKeyboardButton(text="👥 Пригласить друга", callback_data="referral")]
        ])
        await message.answer(
            "❌ У тебя закончились токены.\n\n"
            "💡 <b>Способы пополнить баланс:</b>\n"
            "1. Купить токены за Stars — /buy\n"
            "2. Пригласить друга — /referral",
            parse_mode='HTML',
            reply_markup=keyboard
        )
        return

    # Списываем токен
    update_user_tokens(user_id, -1)
    remaining = get_user_tokens(user_id)

    status_msg = await message.answer("🔄 Анализирую фото и создаю палитру...")

    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        image_bytes = await bot.download_file(file.file_path)
        image_data = image_bytes.read()

        colors = extract_dominant_colors(image_data, max_colors=12)
        
        if not colors:
            await status_msg.delete()
            await message.answer("😕 Не удалось найти цвета на этом фото.")
            # Возвращаем токен
            update_user_tokens(user_id, 1)
            return

        palette_image = create_palette_image(colors)
        
        if not palette_image:
            await status_msg.delete()
            await message.answer("⚠️ Не удалось создать палитру.")
            update_user_tokens(user_id, 1)
            return

        # Увеличиваем счетчик обработанных фото
        increment_photos_processed(user_id)

        await status_msg.delete()
        
        input_file = BufferedInputFile(
            palette_image,
            filename=f"palette_{user_id}.png"
        )
        
        colors_list = []
        for i, c in enumerate(colors, 1):
            colors_list.append(f"{i}. {c.upper()}")
        
        colors_text = "\n".join(colors_list)
        
        caption = (
            f"🎨 <b>ГЛАВНЫЕ ЦВЕТА</b>\n"
            f"📦 Найдено: <b>{len(colors)}</b>\n"
            f"💰 Осталось: <b>{remaining}</b> токенов\n\n"
            f"📋 <b>От частого к редкому:</b>\n"
            f"{colors_text}"
        )
        
        await message.answer_photo(
            photo=input_file,
            caption=caption,
            parse_mode='HTML'
        )
        
    except Exception as e:
        await status_msg.delete()
        await message.answer(f"⚠️ Ошибка: {e}")
        # Возвращаем токен при ошибке
        update_user_tokens(user_id, 1)


# ============================================
# ===== ОБРАБОТКА CALLBACK =====
# ============================================

@dp.callback_query()
async def handle_callback(callback: types.CallbackQuery):
    if callback.data == "send_photo":
        await callback.message.answer("📸 Отправь мне любое фото, и я создам палитру!")
        await callback.answer()
    
    elif callback.data == "balance":
        user_id = callback.from_user.id
        tokens = get_user_tokens(user_id)
        photos = get_photos_processed(user_id)
        referrals_count = get_referral_count(user_id)
        referral_bonus = get_referral_bonus(user_id)
        
        filled = min(tokens, 10)
        empty = 10 - filled
        bar = "█" * filled + "░" * empty
        
        await callback.message.answer(
            f"💰 <b>Твой баланс</b>\n\n"
            f"{bar}\n"
            f"<b>{tokens}</b> токенов\n\n"
            f"📊 <b>Статистика:</b>\n"
            f"📸 Обработано фото: <b>{photos}</b>\n"
            f"👥 Приглашено друзей: <b>{referrals_count}</b>\n"
            f"🎁 Получено бонусов: <b>{referral_bonus}</b>",
            parse_mode='HTML'
        )
        await callback.answer()
    
    elif callback.data == "help":
        await cmd_help(callback.message)
        await callback.answer()
    
    elif callback.data == "buy":
        await cmd_buy(callback.message)
        await callback.answer()
    
    elif callback.data == "referral":
        user_id = callback.from_user.id
        ref_link = generate_ref_link(user_id)
        invited_count = get_referral_count(user_id)
        bonus_total = get_referral_bonus(user_id)
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Поделиться", switch_inline_query=f"🎨 Попробуй бот-палитру! {ref_link}")]
        ])
        
        await callback.message.answer(
            f"👥 <b>Твоя реферальная ссылка</b>\n\n"
            f"🔗 <code>{ref_link}</code>\n\n"
            f"📊 Приглашено друзей: <b>{invited_count}</b>\n"
            f"🎁 Получено бонусов: <b>{bonus_total}</b> токенов\n\n"
            f"🎁 <b>Бонусы за приглашения:</b>\n"
            f"• За первого друга → <b>+2 токена</b>\n"
            f"• За каждого следующего → <b>+1 токен</b>\n"
            f"• Твой друг получит <b>+1 токен</b>",
            parse_mode='HTML',
            reply_markup=keyboard
        )
        await callback.answer()


# ============================================
# ===== ЗАПУСК =====
# ============================================

async def main():
    logging.basicConfig(level=logging.INFO)
    
    # Получаем информацию о боте
    bot_info = await bot.get_me()
    bot.username = bot_info.username
    
    print("=" * 60)
    print("🚀 Бот-палитра PRO с SQLite БД")
    print("=" * 60)
    print(f"👤 Админ ID: {ADMIN_ID}")
    print(f"🤖 Username: @{bot_info.username}")
    print(f"📁 База данных: {DB_PATH}")
    print("\n👥 Реферальная система:")
    print("   • За первого друга → +2 токена")
    print("   • За каждого следующего → +1 токен")
    print("   • Приглашенный получает +1 токен")
    print("\n🔥 Цены со скидкой 15%:")
    print("   85 ⭐ → 10 токенов")
    print("   212 ⭐ → 25 токенов")
    print("   425 ⭐ → 50 токенов")
    print("   850 ⭐ → 100 токенов")
    print("\n📋 Команды:")
    print("   /start      - Главное меню")
    print("   /help       - Помощь")
    print("   /balance    - Баланс")
    print("   /buy        - Купить токены")
    print("   /referral   - Реферальная ссылка")
    print("   /info       - О боте")
    print("   /feedback   - Обратная связь")
    print("\n🔧 Админ-команды:")
    print("   /add_tokens     - Добавить токены")
    print("   /reset_tokens   - Сбросить всё")
    print("   /stats          - Статистика")
    print("   /top            - Топ приглашающих")
    print("   /recent_purchases - Последние покупки")
    print("=" * 60)
    
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())