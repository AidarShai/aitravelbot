"""
✈️ Travel Planner Bot — Айдар & Лиза
AI подбирает жильё, места, рестораны одним пакетом.
"""

import logging
import sqlite3
import os
import json
import re
from datetime import datetime
import anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ConversationHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ── конфиг ──────────────────────────────────────────────────
BOT_TOKEN      = os.environ["BOT_TOKEN"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"]
# Telegram ID всех участников — видят планы друг друга
FAMILY = {
    46474536,   # твой ID (посмотри в /start)
    455674930,   # ID Лизы
}
DB_PATH        = "trips.db"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ai = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ── шаги диалога ────────────────────────────────────────────
ASK_CITY, ASK_DATES, ASK_BUDGET, ASK_VIBE = range(4)


# ── база данных ─────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS trips (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            city        TEXT,
            dates       TEXT,
            budget      TEXT,
            vibe        TEXT,
            plan_json   TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS saved (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id  INTEGER,
            trip_id  INTEGER,
            category TEXT,
            item     TEXT
        );
    """)
    con.commit()
    con.close()


def db():
    return sqlite3.connect(DB_PATH)


# ── AI планировщик ──────────────────────────────────────────
PLAN_PROMPT = """Ты — экспертный тревел-планировщик. Пользователь едет в {city}.
Даты: {dates}. Бюджет: {budget}. Настроение поездки: {vibe}.

Составь детальный план поездки в формате JSON. Отвечай ТОЛЬКО валидным JSON без markdown.

Структура:
{{
  "summary": "2-3 предложения о поездке и почему это отличный выбор",
  "hotels": [
    {{
      "name": "название отеля или апартаментов",
      "type": "отель/апартаменты/хостел",
      "price_night": "примерная цена за ночь в EUR",
      "area": "район города",
      "why": "почему подходит под бюджет и настроение",
      "maps_query": "название + город для поиска в Google Maps",
      "booking_hint": "подсказка где искать (Booking.com / Airbnb / Hotels.com)"
    }}
  ],
  "sights": [
    {{
      "name": "название места",
      "type": "музей/парк/район/смотровая/рынок",
      "description": "1-2 предложения что это и почему стоит посетить",
      "tip": "практический совет (лучшее время, цена, как добраться)",
      "maps_query": "название + город"
    }}
  ],
  "restaurants": [
    {{
      "name": "название",
      "cuisine": "тип кухни",
      "price_range": "€/€€/€€€",
      "must_try": "что обязательно попробовать",
      "maps_query": "название + город"
    }}
  ],
  "day_tip": "один неочевидный совет для этого города который знают только местные"
}}

Требования:
- hotels: ровно 3 варианта под указанный бюджет
- sights: ровно 5 мест, разных типов
- restaurants: ровно 3 заведения, разного ценового диапазона
- Все названия — реально существующие места
- maps_query составляй так чтобы первый результат в Google Maps был правильным местом
- Отвечай на русском языке
"""


async def generate_plan(city: str, dates: str, budget: str, vibe: str) -> dict:
    prompt = PLAN_PROMPT.format(city=city, dates=dates, budget=budget, vibe=vibe)
    msg = ai.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = msg.content[0].text.strip()
    # убираем возможные markdown-обёртки
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def maps_link(query: str) -> str:
    q = query.replace(" ", "+")
    return f"https://maps.google.com/?q={q}"


# ── форматирование плана ─────────────────────────────────────
def format_hotels(hotels: list) -> str:
    lines = ["🏨 *Жильё*\n"]
    for i, h in enumerate(hotels, 1):
        link = maps_link(h["maps_query"])
        lines.append(
            f"{i}. *{h['name']}* — {h['type']}\n"
            f"   💶 {h['price_night']}/ночь · 📍 {h['area']}\n"
            f"   _{h['why']}_\n"
            f"   [Найти на карте]({link}) · {h['booking_hint']}"
        )
    return "\n\n".join(lines)


def format_sights(sights: list) -> str:
    lines = ["🗺 *Что посмотреть*\n"]
    for i, s in enumerate(sights, 1):
        link = maps_link(s["maps_query"])
        lines.append(
            f"{i}. *{s['name']}* — {s['type']}\n"
            f"   {s['description']}\n"
            f"   💡 {s['tip']}\n"
            f"   [Открыть на карте]({link})"
        )
    return "\n\n".join(lines)


def format_restaurants(restaurants: list) -> str:
    lines = ["🍽 *Где поесть*\n"]
    for i, r in enumerate(restaurants, 1):
        link = maps_link(r["maps_query"])
        lines.append(
            f"{i}. *{r['name']}* — {r['cuisine']} {r['price_range']}\n"
            f"   Попробуй: _{r['must_try']}_\n"
            f"   [Найти на карте]({link})"
        )
    return "\n\n".join(lines)


# ── клавиатуры ──────────────────────────────────────────────
def plan_keyboard(trip_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🏨 Жильё", callback_data=f"show_hotels:{trip_id}"),
            InlineKeyboardButton("🗺 Места", callback_data=f"show_sights:{trip_id}"),
            InlineKeyboardButton("🍽 Рестораны", callback_data=f"show_restaurants:{trip_id}"),
        ],
        [
            InlineKeyboardButton("💾 Сохранить всё", callback_data=f"save_all:{trip_id}"),
            InlineKeyboardButton("🔄 Новый план", callback_data=f"new_plan"),
        ]
    ])


def vibe_keyboard() -> InlineKeyboardMarkup:
    vibes = [
        ("🏖 Пляж и релакс", "пляж и отдых"),
        ("🏛 Культура и история", "культура и история"),
        ("🍷 Гастро и вино", "гастрономия и вино"),
        ("🥾 Природа и хайкинг", "природа и активный отдых"),
        ("🎉 Тусовки и ночная жизнь", "ночная жизнь и развлечения"),
        ("🤔 Сюрприз!", "что-нибудь неожиданное"),
    ]
    rows = []
    for i in range(0, len(vibes), 2):
        row = [InlineKeyboardButton(v[0], callback_data=f"vibe:{v[1]}") for v in vibes[i:i+2]]
        rows.append(row)
    return InlineKeyboardMarkup(rows)


# ── /start ──────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name
    await update.message.reply_text(
        f"Привет, {name}! ✈️\n\n"
        "Я помогу вам с Лизой спланировать идеальное путешествие.\n"
        "Скажу где остановиться, что посмотреть и где поесть — всё под ваш бюджет.\n\n"
        "Команды:\n"
        "/plan — спланировать поездку\n"
        "/saved — сохранённые места\n"
        "/trips — история поездок\n"
        "/help — справка"
    )


# ── /plan — диалог ───────────────────────────────────────────
async def cmd_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🌍 *Куда едем?*\n\nНапиши город или страну:",
        parse_mode="Markdown"
    )
    return ASK_CITY


async def got_city(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["city"] = update.message.text.strip()
    await update.message.reply_text(
        "📅 *Когда?*\n\nНапиши примерные даты или период:\n"
        "_(например: «10-17 июля», «конец августа, неделя»)_",
        parse_mode="Markdown"
    )
    return ASK_DATES


async def got_dates(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["dates"] = update.message.text.strip()
    await update.message.reply_text(
        "💶 *Бюджет на двоих?*\n\nТолько жильё или всё вместе — напиши как удобно:\n"
        "_(например: «600 EUR на всё», «200 EUR/ночь на жильё»)_",
        parse_mode="Markdown"
    )
    return ASK_BUDGET


async def got_budget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["budget"] = update.message.text.strip()
    await update.message.reply_text(
        "🎭 *Какое настроение поездки?*",
        parse_mode="Markdown",
        reply_markup=vibe_keyboard()
    )
    return ASK_VIBE


async def got_vibe_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    vibe = query.data.replace("vibe:", "")
    ctx.user_data["vibe"] = vibe

    city   = ctx.user_data["city"]
    dates  = ctx.user_data["dates"]
    budget = ctx.user_data["budget"]

    await query.edit_message_text(
        f"⏳ Подбираю план для *{city}*...\n\n"
        f"📅 {dates} · 💶 {budget} · 🎭 {vibe}\n\n"
        "_Обычно занимает 10-15 секунд_",
        parse_mode="Markdown"
    )

    try:
        plan = await generate_plan(city, dates, budget, vibe)
    except Exception as e:
        logger.error(f"AI error: {e}")
        await query.edit_message_text(
            "😔 Что-то пошло не так при генерации плана. Попробуй ещё раз через /plan"
        )
        return ConversationHandler.END

    # сохраняем в БД
    user_id = update.effective_user.id
    with db() as con:
        cur = con.execute(
            "INSERT INTO trips(user_id,city,dates,budget,vibe,plan_json) VALUES(?,?,?,?,?,?)",
            (user_id, city, dates, budget, vibe, json.dumps(plan, ensure_ascii=False))
        )
        trip_id = cur.lastrowid

    summary = plan.get("summary", "")
    day_tip = plan.get("day_tip", "")

    await query.edit_message_text(
        f"✅ *План готов!*\n\n"
        f"📍 *{city}* · {dates}\n\n"
        f"{summary}\n\n"
        f"💡 *Совет от местных:* {day_tip}\n\n"
        f"Выбери что смотреть:",
        parse_mode="Markdown",
        reply_markup=plan_keyboard(trip_id),
        disable_web_page_preview=True
    )
    ctx.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("Отменено. /plan — начать заново.")
    return ConversationHandler.END


# ── callbacks кнопок плана ──────────────────────────────────
async def cb_show_hotels(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    trip_id = int(query.data.split(":")[1])
    with db() as con:
        row = con.execute("SELECT city, plan_json FROM trips WHERE id=?", (trip_id,)).fetchone()
    if not row:
        await query.answer("Поездка не найдена", show_alert=True)
        return
    plan = json.loads(row[1])
    text = format_hotels(plan["hotels"])
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("❤️ Сохранить жильё", callback_data=f"save_hotels:{trip_id}"),
        InlineKeyboardButton("◀️ Назад", callback_data=f"back:{trip_id}")
    ]])
    await query.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=kb, disable_web_page_preview=False)


async def cb_show_sights(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    trip_id = int(query.data.split(":")[1])
    with db() as con:
        row = con.execute("SELECT city, plan_json FROM trips WHERE id=?", (trip_id,)).fetchone()
    if not row:
        await query.answer("Поездка не найдена", show_alert=True)
        return
    plan = json.loads(row[1])
    text = format_sights(plan["sights"])
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("❤️ Сохранить места", callback_data=f"save_sights:{trip_id}"),
        InlineKeyboardButton("◀️ Назад", callback_data=f"back:{trip_id}")
    ]])
    await query.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=kb, disable_web_page_preview=False)


async def cb_show_restaurants(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    trip_id = int(query.data.split(":")[1])
    with db() as con:
        row = con.execute("SELECT city, plan_json FROM trips WHERE id=?", (trip_id,)).fetchone()
    if not row:
        await query.answer("Поездка не найдена", show_alert=True)
        return
    plan = json.loads(row[1])
    text = format_restaurants(plan["restaurants"])
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("❤️ Сохранить рестораны", callback_data=f"save_restaurants:{trip_id}"),
        InlineKeyboardButton("◀️ Назад", callback_data=f"back:{trip_id}")
    ]])
    await query.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=kb, disable_web_page_preview=False)


async def cb_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    trip_id = int(query.data.split(":")[1])
    with db() as con:
        row = con.execute(
            "SELECT city, dates, budget, vibe, plan_json FROM trips WHERE id=?", (trip_id,)
        ).fetchone()
    if not row:
        return
    city, dates, budget, vibe, plan_json = row
    plan = json.loads(plan_json)
    summary = plan.get("summary", "")
    day_tip = plan.get("day_tip", "")
    await query.edit_message_text(
        f"✅ *План готов!*\n\n"
        f"📍 *{city}* · {dates}\n\n"
        f"{summary}\n\n"
        f"💡 *Совет от местных:* {day_tip}\n\n"
        f"Выбери что смотреть:",
        parse_mode="Markdown",
        reply_markup=plan_keyboard(trip_id),
        disable_web_page_preview=True
    )


async def cb_save_section(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, trip_id_str = query.data.split(":")
    trip_id = int(trip_id_str)
    user_id = update.effective_user.id

    category_map = {
        "save_hotels": ("hotels", "жильё"),
        "save_sights": ("sights", "места"),
        "save_restaurants": ("restaurants", "рестораны"),
        "save_all": (None, "всё"),
    }
    key, label = category_map.get(action, (None, ""))

    with db() as con:
        row = con.execute("SELECT plan_json FROM trips WHERE id=?", (trip_id,)).fetchone()
        if not row:
            return
        plan = json.loads(row[0])

        # удаляем старые сохранения этой категории для этой поездки
        if key:
            con.execute("DELETE FROM saved WHERE user_id=? AND trip_id=? AND category=?",
                        (user_id, trip_id, key))
            for item in plan[key]:
                con.execute(
                    "INSERT INTO saved(user_id,trip_id,category,item) VALUES(?,?,?,?)",
                    (user_id, trip_id, key, json.dumps(item, ensure_ascii=False))
                )
        else:
            con.execute("DELETE FROM saved WHERE user_id=? AND trip_id=?", (user_id, trip_id))
            for cat in ["hotels", "sights", "restaurants"]:
                for item in plan.get(cat, []):
                    con.execute(
                        "INSERT INTO saved(user_id,trip_id,category,item) VALUES(?,?,?,?)",
                        (user_id, trip_id, cat, json.dumps(item, ensure_ascii=False))
                    )

    await query.answer(f"❤️ Сохранено: {label}", show_alert=False)


async def cb_new_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "🌍 *Куда едем?*\n\nНапиши город или страну:",
        parse_mode="Markdown"
    )
    # сброс диалога через новое сообщение — пользователь начнёт /plan заново


# ── /saved ──────────────────────────────────────────────────
async def cmd_saved(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    with db() as con:
        rows = con.execute(
            "SELECT s.category, s.item, t.city "
            "FROM saved s JOIN trips t ON s.trip_id=t.id "
            f"WHERE user_id IN ({','.join('?'*len(FAMILY))}) ORDER BY id DESC LIMIT 10",
list(FAMILY)
        ).fetchall()

    if not rows:
        await update.message.reply_text(
            "Сохранённых мест пока нет.\n"
            "Используй ❤️ кнопки в плане поездки."
        )
        return

    city_sections = {}
    for cat, item_json, city in rows:
        key = city
        if key not in city_sections:
            city_sections[key] = {"hotels": [], "sights": [], "restaurants": []}
        city_sections[key][cat].append(json.loads(item_json))

    cat_emoji = {"hotels": "🏨", "sights": "🗺", "restaurants": "🍽"}
    cat_label = {"hotels": "Жильё", "sights": "Места", "restaurants": "Рестораны"}

    parts = []
    for city, cats in city_sections.items():
        section = [f"📍 *{city}*"]
        for cat, items in cats.items():
            if items:
                section.append(f"\n{cat_emoji[cat]} *{cat_label[cat]}*")
                for item in items:
                    name = item.get("name", "")
                    mq = item.get("maps_query", name)
                    link = maps_link(mq)
                    section.append(f"• [{name}]({link})")
        parts.append("\n".join(section))

    await update.message.reply_text(
        "\n\n─────────────────\n\n".join(parts),
        parse_mode="Markdown",
        disable_web_page_preview=True
    )


# ── /trips ──────────────────────────────────────────────────
async def cmd_trips(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    with db() as con:
        rows = con.execute(
            "SELECT id, city, dates, budget, vibe, created_at FROM trips "
            f"WHERE user_id IN ({','.join('?'*len(FAMILY))}) ORDER BY id DESC LIMIT 10",
list(FAMILY)
        ).fetchall()

    if not rows:
        await update.message.reply_text("Планов пока нет. Начни с /plan 🌍")
        return

    lines = ["*Твои поездки:*\n"]
    for tid, city, dates, budget, vibe, created in rows:
        lines.append(
            f"*#{tid} {city}*\n"
            f"📅 {dates} · 💶 {budget}\n"
            f"🎭 {vibe}"
        )
    await update.message.reply_text(
        "\n\n".join(lines),
        parse_mode="Markdown"
    )


# ── /help ────────────────────────────────────────────────────
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✈️ *Travel Planner — справка*\n\n"
        "/plan — создать новый план поездки\n"
        "/saved — сохранённые жильё, места и рестораны\n"
        "/trips — история всех планов\n\n"
        "*Как работает:*\n"
        "1. Введи город → даты → бюджет → настроение\n"
        "2. AI подбирает 3 варианта жилья, 5 мест, 3 ресторана\n"
        "3. Каждое место — с ссылкой на Google Maps\n"
        "4. Понравилось — жми ❤️ и оно сохранится в /saved",
        parse_mode="Markdown"
    )


# ── main ─────────────────────────────────────────────────────
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    plan_conv = ConversationHandler(
        entry_points=[CommandHandler("plan", cmd_plan)],
        states={
            ASK_CITY:   [MessageHandler(filters.TEXT & ~filters.COMMAND, got_city)],
            ASK_DATES:  [MessageHandler(filters.TEXT & ~filters.COMMAND, got_dates)],
            ASK_BUDGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_budget)],
            ASK_VIBE:   [CallbackQueryHandler(got_vibe_button, pattern=r"^vibe:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("saved", cmd_saved))
    app.add_handler(CommandHandler("trips", cmd_trips))
    app.add_handler(plan_conv)

    app.add_handler(CallbackQueryHandler(cb_show_hotels,      pattern=r"^show_hotels:"))
    app.add_handler(CallbackQueryHandler(cb_show_sights,      pattern=r"^show_sights:"))
    app.add_handler(CallbackQueryHandler(cb_show_restaurants, pattern=r"^show_restaurants:"))
    app.add_handler(CallbackQueryHandler(cb_back,             pattern=r"^back:"))
    app.add_handler(CallbackQueryHandler(cb_save_section,     pattern=r"^save_"))
    app.add_handler(CallbackQueryHandler(cb_new_plan,         pattern=r"^new_plan"))

    logger.info("✈️ Travel bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
