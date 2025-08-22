from __future__ import annotations
import os, json, difflib, logging, asyncio, re
from pathlib import Path
from typing import Dict, List, Tuple

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardMarkup, InlineKeyboardButton,
    InlineQueryResultArticle, InputTextMessageContent,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    InlineQueryHandler, ContextTypes, filters,
)

# ========= конфіг =========
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("health_warfarin_bot")

ROOT = Path(__file__).parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
FOODS_PATH = DATA / "foods.json"
USER_FOODS_PATH = DATA / "user_foods.json"  # залишили на майбутнє (не показуємо «Додати»)
USERS_PATH = DATA / "users.json"            # збереження мови

EMOJI = {"increase": "⬆️", "decrease": "⬇️", "neutral": "•"}

# ========= файли/збереження =========
def _load_json_dict(path: Path) -> Dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Не вдалось прочитати %s", path)
        return {}

def _save_json_dict(path: Path, data: Dict):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def _ensure_user_files():
    if not USERS_PATH.exists():
        _save_json_dict(USERS_PATH, {})

def load_foods_merged() -> Dict[str, dict]:
    """
    merge: base foods.json + user_foods.json (якщо є; user перевизначає base)
    """
    base = _load_json_dict(FOODS_PATH)
    user = _load_json_dict(USER_FOODS_PATH) if USER_FOODS_PATH.exists() else {}
    merged = {**base, **user}
    if not merged:
        log.warning("foods.json порожній або відсутній — списки будуть пустими.")
    return merged

def load_users() -> Dict[str, dict]:
    _ensure_user_files()
    return _load_json_dict(USERS_PATH)

def save_users(d: Dict[str, dict]):
    _save_json_dict(USERS_PATH, d)

USERS = load_users()

def get_lang(user_id: int) -> str:
    return (USERS.get(str(user_id), {}).get("lang") or "uk").lower()

def set_lang(user_id: int, lang: str):
    USERS[str(user_id)] = {"lang": "en" if lang.lower().startswith("en") else "uk"}
    save_users(USERS)

# ========= індекси/пошук =========
def _norm(s: str) -> str:
    # нормалізація для пошуку (англ+укр), збережемо цифри/пробіли/дефіс/апострофи
    return re.sub(r"[^a-zа-яіїєґ0-9\s\-ʼ']", "", (s or "").lower().strip())

def rebuild_indexes() -> Tuple[Dict[str, dict], Dict[str, str]]:
    """
    FOODS: canonical dict, ключ — англ назва в нижньому регістрі
    ALIAS: нормалізована назва/синонім (en/uk) -> canonical key
    """
    raw = load_foods_merged()
    foods: Dict[str, dict] = {}
    alias: Dict[str, str] = {}

    for key, it in raw.items():
        name_en = (it.get("name_en") or it.get("en") or it.get("name") or key).strip()
        name_uk = (it.get("name_uk") or it.get("uk") or it.get("назва") or name_en).strip()
        eff     = (it.get("effect") or "neutral").strip().lower()
        if eff not in ("increase", "decrease", "neutral"):
            eff = "neutral"

        item = {
            "name_en": name_en,
            "name_uk": name_uk,
            "effect": eff,
            "vitamin_k": it.get("vitamin_k") or "",
            "advice_en": it.get("advice_en") or "",
            "advice_uk": it.get("advice_uk") or it.get("advice") or "",
            "image": it.get("image") or "",
            "synonyms_en": it.get("synonyms_en") or [],
            "synonyms_uk": it.get("synonyms_uk") or [],
            "sources": it.get("sources") or [],
            "nutrition": it.get("nutrition") or None,
        }
        canon = name_en.lower()
        foods[canon] = item

        # alias-ключі (en+uk+синоніми)
        for s in [name_en, name_uk, *item["synonyms_en"], *item["synonyms_uk"]]:
            ns = _norm(s)
            if ns:
                alias[ns] = canon

    return foods, alias

FOODS, ALIAS = rebuild_indexes()

def search_foods(q: str, n: int = 20) -> List[str]:
    qn = _norm(q)
    if not qn:
        return []
    # Пряме попадання
    if qn in ALIAS:
        return [ALIAS[qn]]
    # Схожі варіанти
    keys = list(ALIAS.keys())
    maybes = difflib.get_close_matches(qn, keys, n=max(n*3, 60), cutoff=0.45)
    seen, out = set(), []
    for m in maybes:
        canon = ALIAS[m]
        if canon not in seen:
            seen.add(canon)
            out.append(canon)
        if len(out) >= n:
            break
    return out

# ========= локалізація/меню =========
def main_kb(lang: str) -> ReplyKeyboardMarkup:
    # без кнопки «Додати»
    if lang == "en":
        rows = [
            ["🔎 Search", "📋 List"],
            ["⬆️ Increase", "⬇️ Decrease"],
            ["🇺🇦 / 🇬🇧"],
        ]
    else:
        rows = [
            ["🔎 Пошук", "📋 Список"],
            ["⬆️ Підсилюють", "⬇️ Послаблюють"],
            ["🇺🇦 / 🇬🇧"],
        ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def _effect_text(eff: str, lang: str) -> str:
    eff = (eff or "neutral").lower()
    if lang == "en":
        return ("Increases warfarin effect (↑ INR / bleeding risk)" if eff=="increase"
                else "Decreases warfarin effect (↓ INR)" if eff=="decrease"
                else "Neutral/uncertain effect")
    return ("ПІДСИЛЮЄ дію варфарину (↑ INR / ↑ ризик кровотеч)" if eff=="increase"
            else "ПОСЛАБЛЮЄ дію варфарину (↓ INR)" if eff=="decrease"
            else "Нейтральний/невідомий вплив")

def format_food_answer(canon_key: str, lang: str) -> str:
    it = FOODS[canon_key]
    name = it["name_en"] if lang=="en" else it["name_uk"]
    advice = it.get("advice_en") if lang=="en" else it.get("advice_uk")
    vk = it.get("vitamin_k")
    eff = it.get("effect","neutral")
    syns = it.get("synonyms_en" if lang=="en" else "synonyms_uk", [])
    lines = [f"🍎 *{name.title()}*"]
    if syns: lines.append("_" + (", ".join(syns)) + "_")
    lines.append(f"{EMOJI.get(eff,'•')} {_effect_text(eff, lang)}")
    if vk: lines.append(("• Vitamin K: " if lang=="en" else "• Вітамін K: ") + str(vk))
    if advice: lines.append(("• Tip: " if lang=="en" else "• Порада: ") + advice)
    srcs = it.get("sources") or []
    if srcs: lines.append(("• Sources: " if lang=="en" else "• Джерела: ") + "; ".join(srcs[:3]))
    lines.append("\n⚠️ Educational info. Not medical advice." if lang=="en"
                 else "\n⚠️ Освітня довідка. Не медична рекомендація.")
    return "\n".join(lines)

def inline_result(canon_key: str, lang: str) -> InlineQueryResultArticle:
    text = format_food_answer(canon_key, lang)
    title = FOODS[canon_key]["name_en"] if lang=="en" else FOODS[canon_key]["name_uk"]
    desc  = FOODS[canon_key].get("effect","—")
    return InlineQueryResultArticle(
        id=canon_key,
        title=title.title(),
        description=desc,
        input_message_content=InputTextMessageContent(text, parse_mode="Markdown"),
    )

def nutrition_text(canon_key: str, lang: str) -> str:
    it = FOODS[canon_key]
    n = it.get("nutrition")
    if not n:
        return "Дані відсутні." if lang=="uk" else "No data."
    if lang == "en":
        return (
            "🍽 *Nutrition per 100 g*\n"
            f"• Kcal: {n.get('Kcal','—')}\n"
            f"• Protein: {n.get('Protein','—')} g\n"
            f"• Fat: {n.get('Fat','—')} g\n"
            f"• Carbs: {n.get('Carbs','—')} g\n"
            f"• Vitamin K: {n.get('VitaminK','—')} µg"
        )
    return (
        "🍽 *Харчова цінність на 100 г*\n"
        f"• Ккал: {n.get('Kcal','—')}\n"
        f"• Білки: {n.get('Protein','—')} г\n"
        f"• Жири: {n.get('Fat','—')} г\n"
        f"• Вуглеводи: {n.get('Carbs','—')} г\n"
        f"• Вітамін K: {n.get('VitaminK','—')} мкг"
    )

# ========= списки/пагінація =========
def _names_sorted(lang: str, effect: str | None = None) -> List[tuple[str,str]]:
    items = []
    for k, it in FOODS.items():
        if effect and (it.get("effect") or "").lower() != effect:
            continue
        nm = it["name_en"] if lang=="en" else it["name_uk"]
        items.append((k, nm))
    return sorted(items, key=lambda x: x[1].lower())

def _alphabet_pages(pairs: List[tuple[str,str]], page: int, per_page: int=20):
    start = page*per_page
    chunk = pairs[start:start+per_page]
    kb = [[InlineKeyboardButton(text=nm, callback_data=f"show:{key}")] for key, nm in chunk]
    nav = []
    if page>0: nav.append(InlineKeyboardButton("«", callback_data=f"page:{page-1}"))
    if start+per_page < len(pairs): nav.append(InlineKeyboardButton("»", callback_data=f"page:{page+1}"))
    if nav: kb.append(nav)
    return InlineKeyboardMarkup(kb)

# ========= команди =========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_user.id)
    msg = ("👋 Вітаю у *Warfarin × Food* боті!\n\nМеню знизу допоможе швидко знайти продукт або змінити мову."
           if lang=="uk" else
           "👋 Welcome to *Warfarin × Food* bot!\n\nUse the bottom menu to search foods or change language.")
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_kb(lang))

async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = get_lang(uid)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(("✅ Українська" if lang=="uk" else "Українська"), callback_data="lang:uk"),
         InlineKeyboardButton(("✅ English" if lang=="en" else "English"), callback_data="lang:en")]
    ])
    await update.message.reply_text("Оберіть мову / Choose language:", reply_markup=kb)

async def cb_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if not data.startswith("lang:"):
        return
    lang = data.split(":",1)[1]
    set_lang(q.from_user.id, lang)
    txt = "Мову збережено ✅" if lang=="uk" else "Language saved ✅"
    await q.edit_message_text(txt)
    await q.message.reply_text("Меню оновлено." if lang=="uk" else "Menu updated.", reply_markup=main_kb(lang))

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_user.id)
    pairs = _names_sorted(lang)
    if not pairs:
        await update.message.reply_text("Немає записів." if lang=="uk" else "No items.", reply_markup=main_kb(lang))
        return
    context.user_data["pairs"] = pairs
    context.user_data["page"]  = 0
    await update.message.reply_text(
        ("📋 Продукти (тисни для деталей):" if lang=="uk" else "📋 Products (tap to open):"),
        reply_markup=_alphabet_pages(pairs, 0)
    )

async def cmd_list_increase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_user.id)
    pairs = _names_sorted(lang, effect="increase")
    if not pairs:
        await update.message.reply_text("Немає записів." if lang=="uk" else "No items.", reply_markup=main_kb(lang))
        return
    context.user_data["pairs"] = pairs
    context.user_data["page"]  = 0
    header = "⬆️ Підсилюють (тисни для деталей):" if lang=="uk" else "⬆️ Increase (tap to open):"
    await update.message.reply_text(header, reply_markup=_alphabet_pages(pairs, 0))

async def cmd_list_decrease(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_user.id)
    pairs = _names_sorted(lang, effect="decrease")
    if not pairs:
        await update.message.reply_text("Немає записів." if lang=="uk" else "No items.", reply_markup=main_kb(lang))
        return
    context.user_data["pairs"] = pairs
    context.user_data["page"]  = 0
    header = "⬇️ Послаблюють (тисни для деталей):" if lang=="uk" else "⬇️ Decrease (tap to open):"
    await update.message.reply_text(header, reply_markup=_alphabet_pages(pairs, 0))

async def cb_paging(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not q.data.startswith("page:"): return
    page = int(q.data.split(":")[1])
    pairs = context.user_data.get("pairs") or _names_sorted(get_lang(q.from_user.id))
    context.user_data["page"] = page
    await q.edit_message_reply_markup(reply_markup=_alphabet_pages(pairs, page))

async def _send_card_with_buttons(target, text: str, img_url: str | None, lang: str, key: str):
    buttons = [[InlineKeyboardButton("🍽 Харчова цінність" if lang=="uk" else "🍽 Nutrition",
                                     callback_data=f"nut:{key}")]]
    km = InlineKeyboardMarkup(buttons)
    if img_url:
        try:
            await target.reply_photo(photo=img_url, caption=text, parse_mode="Markdown", reply_markup=km)
            return
        except Exception as e:
            # якщо Telegram не може завантажити картинку — впадемо на текст
            log.warning("send_photo failed for %s: %s", img_url, e)
    await target.reply_text(text, parse_mode="Markdown", reply_markup=km)

async def cb_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not q.data.startswith("show:"): return
    key = q.data.split(":",1)[1]
    lang = get_lang(q.from_user.id)
    if key not in FOODS:
        await q.message.reply_text("Елемент не знайдено.", reply_markup=main_kb(lang))
        return
    text = format_food_answer(key, lang)
    img  = FOODS[key].get("image") or None
    await _send_card_with_buttons(q.message, text, img, lang, key)

async def cb_nutrition(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not q.data.startswith("nut:"): return
    key = q.data.split(":",1)[1]
    lang = get_lang(q.from_user.id)
    txt = nutrition_text(key, lang)
    await q.message.reply_text(txt, parse_mode="Markdown", reply_markup=main_kb(lang))

# --------- пошук/food ----------
async def cmd_food(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_user.id)
    if not context.args:
        await update.message.reply_text("Формат: /food назва" if lang=="uk" else "Usage: /food name",
                                        reply_markup=main_kb(lang))
        return
    query = " ".join(context.args)
    matches = search_foods(query, n=7)
    if not matches:
        # Підкажемо кілька найближчих із усіх назв
        all_names = [FOODS[k]['name_uk'] if lang=="uk" else FOODS[k]['name_en'] for k in FOODS.keys()]
        tip = "Нічого не знайдено. Спробуйте іншу назву." if lang=="uk" else "No results. Try another name."
        await update.message.reply_text(tip, reply_markup=main_kb(lang))
        return

    key = matches[0]
    # якщо не дуже схоже — покажемо варіанти
    ref_text = f"{FOODS[key]['name_uk']} {FOODS[key]['name_en']} " \
               f"{' '.join(FOODS[key].get('synonyms_uk', []))} {' '.join(FOODS[key].get('synonyms_en', []))}"
    if difflib.SequenceMatcher(None, _norm(query), _norm(ref_text)).ratio() < 0.75:
        kb = [[InlineKeyboardButton((FOODS[k]['name_uk'] if lang=="uk" else FOODS[k]['name_en']).title(),
                                    callback_data=f"show:{k}")]
              for k in matches]
        await update.message.reply_text("Можливо ви мали на увазі:" if lang=="uk" else "Did you mean:",
                                        reply_markup=InlineKeyboardMarkup(kb))
        return

    text = format_food_answer(key, lang)
    img  = FOODS[key].get("image") or None
    await _send_card_with_buttons(update.message, text, img, lang, key)

# текстові кнопки меню + «очікую запит»
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = get_lang(uid)
    t = (update.message.text or "").strip()

    # перемикач мов через кнопки-прапорці
    if t == "🇺🇦 / 🇬🇧":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(("✅ Українська" if lang=="uk" else "Українська"), callback_data="lang:uk"),
             InlineKeyboardButton(("✅ English" if lang=="en" else "English"), callback_data="lang:en")]
        ])
        await update.message.reply_text("Оберіть мову / Choose language:", reply_markup=kb)
        return

    if context.user_data.get("awaiting_query"):
        context.user_data["awaiting_query"] = False
        # Виклик «/food <текст>» логікою
        class FakeCtx: pass
        fake = FakeCtx(); fake.args = [t]
        await cmd_food(update, fake)
        # Повертаємо нижню клавіатуру
        await update.message.reply_text("Готово." if lang=="uk" else "Done.", reply_markup=main_kb(lang))
        return

    if lang == "en":
        if t == "🔎 Search":
            context.user_data["awaiting_query"] = True
            await update.message.reply_text("Enter product name:", reply_markup=main_kb(lang))
        elif t == "📋 List":
            await cmd_list(update, context)
        elif t == "⬆️ Increase":
            await cmd_list_increase(update, context)
        elif t == "⬇️ Decrease":
            await cmd_list_decrease(update, context)
        else:
            # будь-який інший текст — пробуємо як запит
            matches = search_foods(t, n=1)
            if matches:
                class FakeCtx2: pass
                fake2 = FakeCtx2(); fake2.args = [t]
                await cmd_food(update, fake2)
            else:
                await update.message.reply_text("No results. Try another name.", reply_markup=main_kb(lang))
    else:
        if t == "🔎 Пошук":
            context.user_data["awaiting_query"] = True
            await update.message.reply_text("Введіть назву продукту:", reply_markup=main_kb(lang))
        elif t == "📋 Список":
            await cmd_list(update, context)
        elif t == "⬆️ Підсилюють":
            await cmd_list_increase(update, context)
        elif t == "⬇️ Послаблюють":
            await cmd_list_decrease(update, context)
        else:
            matches = search_foods(t, n=1)
            if matches:
                class FakeCtx3: pass
                fake3 = FakeCtx3(); fake3.args = [t]
                await cmd_food(update, fake3)
            else:
                await update.message.reply_text("Нічого не знайдено. Спробуйте іншу назву.", reply_markup=main_kb(lang))

# інлайн-режим (опційно)
async def on_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.inline_query.from_user.id)
    q = (update.inline_query.query or "").strip()
    if not q:
        top = sorted(FOODS.keys())[:10]
        await update.inline_query.answer([inline_result(x, lang) for x in top], cache_time=60, is_personal=True)
        return
    matches = search_foods(q, n=25)
    await update.inline_query.answer([inline_result(x, lang) for x in matches], cache_time=0, is_personal=True)

# ========= запуск =========
async def main_async():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN env var is required (.env)")
    if not FOODS_PATH.exists():
        log.warning("data/foods.json не знайдено — база може бути порожня.")
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("lang",      cmd_lang))
    app.add_handler(CommandHandler("food",      cmd_food))
    app.add_handler(CommandHandler("list",      cmd_list))
    app.add_handler(CommandHandler("increase",  cmd_list_increase))
    app.add_handler(CommandHandler("decrease",  cmd_list_decrease))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(cb_lang,     pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(cb_paging,   pattern=r"^page:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_show,     pattern=r"^show:.+"))
    app.add_handler(CallbackQueryHandler(cb_nutrition,pattern=r"^nut:.+"))
    app.add_handler(InlineQueryHandler(on_inline_query))

    log.info("✅ Warfarin × Food bot started. Ctrl+C to stop.")
    await app.initialize(); await app.start()
    try:
        await app.updater.start_polling(drop_pending_updates=True)
        await asyncio.Event().wait()
    finally:
        await app.stop(); await app.shutdown()

def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        log.info("👋 Stopped by user")

if __name__ == "__main__":
    main()