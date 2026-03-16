"""Telegram-бот: поиск запчастей на AFDparts.ru по артикулу."""
import asyncio
import logging
import os
import re

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ErrorEvent

from parser import AFDPartsParser

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
AFDPARTS_LOGIN = os.getenv("AFDPARTS_LOGIN", "i.kiselev@auto-parts.moscow")
AFDPARTS_PASSWORD = os.getenv("AFDPARTS_PASSWORD", "AFDparts2026")
DEBUG_SAVE_HTML = os.getenv("DEBUG_SAVE_HTML", "0").strip().lower() in ("1", "true", "yes")
TELEGRAM_ORDER_CHAT_ID = os.getenv("TELEGRAM_ORDER_CHAT_ID", "232066339").strip()

if not BOT_TOKEN:
    raise SystemExit(
        "Заполните .env: BOT_TOKEN. Создайте бота через @BotFather и укажите токен."
    )

# Состояние: ожидание выбора товара при нескольких вариантах по одному артикулу
user_search_state: dict[int, dict] = {}
# Состояние заявки: после результата — шаг position / phone, shown_items, part_number, выбранная позиция
user_order_state: dict[int, dict] = {}


def _take_up_to_3_unique_prices(items: list) -> list:
    """До 3 позиций в секции, цены не повторяются, от меньшей к большей."""
    sorted_items = sorted(items, key=lambda x: (x.get("price") is None, x.get("price") or 0))
    seen_prices = set()
    out = []
    for it in sorted_items:
        if len(out) >= 3:
            break
        p = it.get("price")
        if p is not None and p in seen_prices:
            continue
        if p is not None:
            seen_prices.add(p)
        out.append(it)
    return out


def _is_return_info(text: str) -> bool:
    if not text or len(text) < 5:
        return False
    t = text.lower()
    if "возврат" in t:
        return True
    if t.strip() == "в наличии":
        return False
    without_spaces = t.replace(" ", "").replace(",", "").replace(";", "")
    if without_spaces.isdigit():
        return False
    return False


def _only_return_conditions(text: str) -> str:
    if not text:
        return ""
    idx = text.lower().find("возврат")
    if idx >= 0:
        return text[idx:].strip()
    return text.strip()


def _format_item(i: int, item: dict) -> list[str]:
    code = (item.get("code") or "").strip()
    brand = (item.get("brand") or "").strip()
    price_text = item.get("price_text", "")
    if item.get("price") is not None and not price_text:
        price_text = f"{item['price']:.2f} ₽"
    desc = (item.get("description") or "").strip()
    availability = (item.get("availability") or "").strip()
    return_info = _only_return_conditions((item.get("warehouse_info") or "").strip())
    first_line = f"{i}. {code}"
    if brand:
        first_line += f"\t{brand}"
    block = [first_line]
    if price_text:
        block.append(f"   💰 {price_text}")
    if desc:
        block.append(f"   📝 {desc}")
    if availability:
        block.append(f"   📦 Наличие: {availability}")
    if return_info and _is_return_info(return_info):
        block.append(f"   🔄 Возврат: {return_info}")
    return block


def _build_result_text(part_number: str, result: dict, requested: list, originals: list, analogs: list) -> str:
    """Собирает текст ответа по выбранным спискам позиций."""
    req_show = _take_up_to_3_unique_prices(requested)
    orig_show = _take_up_to_3_unique_prices(originals)
    anlg_show = _take_up_to_3_unique_prices(analogs)
    sep = "═" * 28
    num = 1
    brand = (result.get("brand") or "").strip()
    lines = [f"🔍 Артикул: {part_number}"]
    if brand:
        lines.append(f"🏷 Бренд: {brand}")
    lines.extend(["", sep, "📌 Запрашиваемый артикул", sep, ""])
    if req_show:
        for item in req_show:
            lines.extend(_format_item(num, item))
            lines.append("")
            num += 1
    else:
        lines.append("   — позиций нет")
        lines.append("")
    lines.extend([sep, "📌 Оригинальные замены", sep, ""])
    if orig_show:
        for item in orig_show:
            lines.extend(_format_item(num, item))
            lines.append("")
            num += 1
    else:
        lines.append("   — позиций нет")
        lines.append("")
    lines.extend([sep, "📌 Аналоги", sep, ""])
    if anlg_show:
        for item in anlg_show:
            lines.extend(_format_item(num, item))
            lines.append("")
            num += 1
    else:
        lines.append("   — позиций нет")
        lines.append("")
    min_price = result.get("min_price")
    if min_price is not None:
        lines.append("═" * 28)
        lines.append(f"💰 Минимальная цена: {min_price:.2f} ₽")
    return "\n".join(lines)


def _flat_shown_items(requested: list, originals: list, analogs: list) -> list[tuple[int, dict]]:
    """Список (номер_позиции, item) в том же порядке, что и в выводе результата."""
    out = []
    num = 1
    for item in _take_up_to_3_unique_prices(requested):
        out.append((num, item))
        num += 1
    for item in _take_up_to_3_unique_prices(originals):
        out.append((num, item))
        num += 1
    for item in _take_up_to_3_unique_prices(analogs):
        out.append((num, item))
        num += 1
    return out


ORDER_BTN = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📋 Заказать", callback_data="order_start")]])

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
parser = AFDPartsParser(
    username=AFDPARTS_LOGIN,
    password=AFDPARTS_PASSWORD,
    debug_save_html=DEBUG_SAVE_HTML,
)


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id if message.from_user else 0
    logging.info("Получен /start от user_id=%s", user_id)

    try:
        auth_msg = await message.answer("🔐 Авторизация на AFDparts.ru...")
        loop = asyncio.get_event_loop()
        auth_result = await loop.run_in_executor(None, parser.authorize)
        if auth_result:
            parser.is_authorized = True
            await auth_msg.edit_text(
                "✅ Авторизация успешна!\n\n"
                "Привет! 👋\n"
                "Это бот для поиска запчастей по артикулу.\n\n"
                "Как это работает:\n\n"
                "У тебя есть артикул детали (OEM-номер).\n\n"
                "Ты вводишь его сюда.\n\n"
                "Я показываю цену, наличие и аналоги.\n\n"
                "🔢 Пример артикула: 7701045033\n\n"
                "Введи артикул в поле ввода, чтобы начать."
            )
        else:
            hint = "Проверьте AFDPARTS_LOGIN и AFDPARTS_PASSWORD в .env."
            if DEBUG_SAVE_HTML:
                hint += " Ответ сохранён в debug_afdparts_login.html."
            await auth_msg.edit_text(
                f"❌ Ошибка авторизации на AFDparts.ru. {hint}\n\n"
                "Попробуйте /start позже."
            )
    except Exception as e:
        logging.exception("Ошибка в /start: %s", e)
        try:
            await message.answer(f"❌ Ошибка: {e}")
        except Exception:
            pass


@dp.message(F.text)
async def handle_message(message: types.Message):
    user_id = message.from_user.id if message.from_user else 0
    text = (message.text or "").strip()
    logging.info("Сообщение от user_id=%s: %r", user_id, text[:80] if text else "")

    order = user_order_state.get(user_id)
    if order and order.get("step") == "position":
        # Ввод номера позиции
        try:
            num = int(text)
        except ValueError:
            await message.answer("Введите число — порядковый номер позиции из списка (1, 2, 3...).")
            return
        shown = order.get("shown_items") or []
        if num < 1 or num > len(shown):
            await message.answer(f"Нет позиции с номером {num}. Введите число от 1 до {len(shown)}.")
            return
        _, selected_item = shown[num - 1]
        order["step"] = "phone"
        order["selected_item"] = selected_item
        order["selected_num"] = num
        await message.answer(
            "Введите контактный телефон для связи или отправьте «-» (минус), чтобы пропустить."
        )
        return
    if order and order.get("step") == "phone":
        phone = "" if text in ("-", "пропустить", "нет") else text
        part_number = order.get("part_number", "")
        selected = order.get("selected_item") or {}
        num = order.get("selected_num", 0)
        price = selected.get("price")
        price_str = f"{price:.2f} ₽" if price is not None else "—"
        user_name = (message.from_user.username and f"@{message.from_user.username}") or ""
        user_full = message.from_user.full_name or ""
        manager_text = (
            "📋 Заявка с бота AFDparts\n\n"
            f"Артикул: {part_number}\n"
            f"Позиция в списке: {num}\n"
            f"Описание: {selected.get('description', '—')}\n"
            f"Цена: {price_str}\n"
            f"Пользователь: {user_full} {user_name}\n"
            f"ID: {user_id}\n"
        )
        if phone:
            manager_text += f"Телефон: {phone}\n"
        try:
            await bot.send_message(TELEGRAM_ORDER_CHAT_ID, manager_text)
        except Exception as e:
            logging.exception("Не удалось отправить заявку менеджеру: %s", e)
            await message.answer("Не удалось отправить заявку. Попробуйте позже.")
        else:
            await message.answer("✅ Заявка принята. С вами свяжутся при необходимости.")
        user_order_state.pop(user_id, None)
        return

    if not text:
        await message.answer("❌ Введите артикул запчасти.")
        return
    # Спецответ по запросам про владельца группы
    _owner_queries = {"зязин", "зязин антон", "антон зязин", "антон михайлович", "ам"}
    if text.strip().lower() in _owner_queries:
        await message.answer(
            "Владелец группы ТГ Техничка L405 https://t.me/+HKJc_eb6K2U1N2Iy"
        )
        return
    # Любая команда (/, /stop и т.д.) — не ищем как артикул, не показываем страницу магазина
    if text.startswith("/"):
        logging.info("Игнор команды как артикула: %r", text)
        await message.answer("Введите артикул для поиска. Команды: /start")
        return

    if not parser.is_authorized:
        loop = asyncio.get_event_loop()
        reauth = await loop.run_in_executor(None, parser.authorize)
        if reauth:
            parser.is_authorized = True
        else:
            await message.answer("❌ Бот не авторизован. Отправьте /start")
            return

    status_msg = await message.answer(f"🔍 Ищу по артикулу {text}...")
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, parser.search, text)

    if result is None:
        await status_msg.edit_text(
            f"❌ Ошибка поиска или сайт недоступен. Проверьте артикул {text} и попробуйте позже."
        )
        return

    def _is_product_item(it: dict) -> bool:
        """Отсекаем мусор со страницы (почта, личный кабинет, демо, нал, «Код детали» — не товары)."""
        raw = f"{it.get('name') or ''} {it.get('description') or ''} {it.get('code') or ''}"
        if re.search(r"[\w.-]+@[\w.-]+\.\w+", raw):
            return False
        if "Личный кабинет" in raw or "Демо-доступ" in raw:
            return False
        if str(it.get("code") or "").strip() in ("0", ""):
            return False
        if (str(it.get("code") or "").strip() == "Код детали"):
            return False
        desc = (it.get("description") or "").strip().lower()
        if desc in ("нал", "нал.", "налич", "налич.") or desc.replace(".", "").strip() == "нал":
            return False
        return True

    requested = [it for it in (result.get("requested") or []) if _is_product_item(it)]
    originals = [it for it in (result.get("originals") or []) if _is_product_item(it)]
    analogs = [it for it in (result.get("analogs") or []) if _is_product_item(it)]
    part_number = result.get("part_number", text)
    total = len(requested) + len(originals) + len(analogs)

    if total == 0:
        await status_msg.edit_text(
            f"По артикулу {part_number} ничего не найдено."
        )
        return

    # Группируем по «товару»: один артикул может быть у разных товаров (бренд + описание)
    all_items = requested + originals + analogs
    def _product_key(item: dict) -> tuple:
        b = (item.get("brand") or "").strip()
        d = (item.get("description") or "").strip()[:200]
        return (b, d)
    groups_dict: dict[tuple, list] = {}
    for it in all_items:
        k = _product_key(it)
        groups_dict.setdefault(k, []).append(it)
    groups_list = list(groups_dict.values())

    # Группы-мусор (только «Код детали», «Нал.» и т.п.) — не показываем в выборе
    def _is_real_product_group(group: list) -> bool:
        if not group:
            return False
        first = group[0]
        code = (first.get("code") or "").strip()
        desc = (first.get("description") or "").strip().lower()
        if code == "Код детали":
            return False
        if desc in ("нал", "нал.", "налич", "налич.") or desc.replace(".", "").strip() == "нал":
            return False
        if not any(_is_product_item(it) for it in group):
            return False
        return True

    groups_list = [g for g in groups_list if _is_real_product_group(g)]
    if not groups_list:
        await status_msg.edit_text(f"По артикулу {part_number} ничего не найдено.")
        return

    # Несколько разных товаров с одним артикулом — просим выбрать
    if len(groups_list) > 1:
        user_search_state[user_id] = {
            "part_number": part_number,
            "result": result,
            "groups_list": groups_list,
            "status_msg_id": status_msg.message_id,
        }
        choice_lines = [f"По артикулу {part_number} найдено несколько разных товаров. Выберите нужный:", ""]
        buttons = []
        # Убираем «нал» из подписи кнопки (без учёта регистра)
        def _clean_button_text(s: str) -> str:
            out = (s or "").strip()
            for x in ("нал.", "нал ", " нал", "нал", "налич.", "налич ", " налич", "налич"):
                # без учёта регистра
                pat = re.compile(re.escape(x), re.I)
                out = pat.sub(" ", out).strip()
            return " ".join(out.split())

        for i, group in enumerate(groups_list):
            first = group[0]
            brand = _clean_button_text(first.get("brand") or "")
            desc = _clean_button_text(first.get("description") or "")
            short = (desc[:40] + "…") if len(desc) > 40 else desc
            label = f"{brand} — {short}" if brand else (short or f"Вариант {i+1}")
            label = _clean_button_text(label)
            if not label or label.lower().replace(".", "").strip() in ("", "нал", "налич"):
                label = f"Вариант {i+1}"
            if len(label) > 64:
                label = label[:61] + "…"
            choice_lines.append(f"{i+1}. {label}")
            buttons.append(InlineKeyboardButton(text=label, callback_data=f"part_choose_{i}"))
        await status_msg.edit_text(
            "\n".join(choice_lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[buttons]),
        )
        return

    # Один «товар» — разбиваем группу по типам и показываем результат
    requested = [it for it in groups_list[0] if it.get("type") == "requested"]
    originals = [it for it in groups_list[0] if it.get("type") == "original"]
    analogs = [it for it in groups_list[0] if it.get("type") == "analog"]

    answer = _build_result_text(part_number, result, requested, originals, analogs)
    flat_items = _flat_shown_items(requested, originals, analogs)
    user_order_state[user_id] = {"part_number": part_number, "shown_items": flat_items, "step": None}
    if len(answer) > 4096:
        parts = [answer[i : i + 4096] for i in range(0, len(answer), 4096)]
        for part in parts[:-1]:
            await message.answer(part)
        await status_msg.edit_text(parts[-1], reply_markup=ORDER_BTN)
    else:
        await status_msg.edit_text(answer, reply_markup=ORDER_BTN)


@dp.callback_query(F.data == "order_start")
async def cb_order_start(callback: CallbackQuery):
    """Пользователь нажал «Заказать» — запрашиваем номер позиции."""
    user_id = callback.from_user.id if callback.from_user else 0
    order = user_order_state.get(user_id)
    if not order or not order.get("shown_items"):
        await callback.answer("Сначала выполните поиск и дождитесь результата.", show_alert=True)
        return
    order["step"] = "position"
    await callback.message.answer(
        "Введите порядковый номер позиции из списка (1, 2, 3...), которую хотите заказать."
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("part_choose_"))
async def cb_part_choose(callback: CallbackQuery):
    """Пользователь выбрал один из нескольких товаров с одним артикулом."""
    user_id = callback.from_user.id if callback.from_user else 0
    state = user_search_state.get(user_id)
    if not state:
        await callback.answer("Сессия истекла. Выполните поиск заново.", show_alert=True)
        return
    try:
        idx = int(callback.data.replace("part_choose_", ""))
    except ValueError:
        await callback.answer("Ошибка выбора.", show_alert=True)
        return
    groups_list = state.get("groups_list") or []
    if idx < 0 or idx >= len(groups_list):
        await callback.answer("Неверный вариант.", show_alert=True)
        return
    user_search_state.pop(user_id, None)
    group = groups_list[idx]
    part_number = state.get("part_number", "")
    requested = [it for it in group if it.get("type") == "requested"]
    originals = [it for it in group if it.get("type") == "original"]
    analogs = [it for it in group if it.get("type") == "analog"]
    first = group[0] if group else {}
    prices = [it["price"] for it in group if it.get("price") is not None]
    mini_result = {
        "part_number": part_number,
        "brand": first.get("brand", ""),
        "min_price": min(prices) if prices else None,
    }
    answer = _build_result_text(part_number, mini_result, requested, originals, analogs)
    flat_items = _flat_shown_items(requested, originals, analogs)
    user_order_state[user_id] = {"part_number": part_number, "shown_items": flat_items, "step": None}
    try:
        await callback.message.edit_text(answer, reply_markup=ORDER_BTN)
    except Exception:
        await callback.message.answer(answer, reply_markup=ORDER_BTN)
    await callback.answer()


@dp.error()
async def error_handler(event: ErrorEvent):
    """Ловим любые необработанные исключения в хендлерах, чтобы бот не падал."""
    logging.exception("Необработанная ошибка в обработчике: %s", event.exception)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    logging.info("AFDparts бот запущен. Команды (/) не ищутся как артикул.")
    while True:
        try:
            await dp.start_polling(bot)
        except Exception:
            logging.exception("Падение polling, перезапуск через 60 с")
            await asyncio.sleep(60)
        else:
            break


if __name__ == "__main__":
    asyncio.run(main())
