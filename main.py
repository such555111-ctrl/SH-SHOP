import os
import json
import uuid
import logging
import threading
import functools
from datetime import datetime, timezone

from flask import Flask, request, send_from_directory, abort, jsonify
import telebot
from telebot import types

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sh_shop")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Переменная окружения BOT_TOKEN не задана")

# Render автоматически прокидывает RENDER_EXTERNAL_URL для web-сервисов.
# Можно переопределить вручную через WEBHOOK_URL, если нужно.
BASE_URL = (os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")

# Telegram-аккаунт менеджера (без @) — на него ведёт кнопка «Написать менеджеру».
SHOP_CONTACT = (os.environ.get("SHOP_CONTACT") or "SHvape_manager").lstrip("@").strip()

# Пароль для входа в админ-панель (/admin). Задаётся в переменных окружения.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

# Telegram ID администратора(ов). Только этим пользователям бот присылает
# ссылку на админ-панель по команде /start. Можно указать несколько ID
# через запятую: "111111,222222".
ADMIN_CHAT_IDS = {
    int(x.strip())
    for x in os.environ.get("ADMIN_CHAT_ID", "").split(",")
    if x.strip().lstrip("-").isdigit()
}

APP_DIR = os.path.dirname(os.path.abspath(__file__))
PRODUCTS_FILE = os.path.join(APP_DIR, "products.json")
ORDERS_FILE = os.path.join(APP_DIR, "orders.json")
QUESTIONS_FILE = os.path.join(APP_DIR, "questions.json")
PROMOS_FILE = os.path.join(APP_DIR, "promos.json")
UPLOAD_DIR = os.path.join(APP_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Доставка и самовывоз
try:
    DELIVERY_FEE = int(os.environ.get("DELIVERY_FEE", "200"))
except ValueError:
    DELIVERY_FEE = 200
PICKUP_POINTS = [
    p.strip() for p in (os.environ.get("PICKUP_POINTS") or "Пролетарская,Девяткино,Мурино").split(",") if p.strip()
]

ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
ALLOWED_BADGES = {"hit", "trend", "sale", "instock"}
ALLOWED_PROMO_TYPES = {"percent", "fixed"}

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 МБ на файл


# --- Вспомогательные функции для товаров и авторизации админа -----------

# Данные лежат в json-файлах, а gunicorn работает в нескольких потоках —
# блокировка не даёт двум запросам одновременно списать/перезаписать остатки.
DATA_LOCK = threading.RLock()


def locked(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with DATA_LOCK:
            return fn(*args, **kwargs)
    return wrapper


def require_admin():
    if not ADMIN_PASSWORD:
        abort(403, description="ADMIN_PASSWORD не задан на сервере")
    supplied = request.headers.get("X-Admin-Password", "")
    if supplied != ADMIN_PASSWORD:
        abort(401, description="Неверный пароль")


def load_products():
    if not os.path.exists(PRODUCTS_FILE):
        return []
    with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_products(items):
    with open(PRODUCTS_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def load_orders():
    if not os.path.exists(ORDERS_FILE):
        return []
    with open(ORDERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_orders(items):
    with open(ORDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def load_questions():
    if not os.path.exists(QUESTIONS_FILE):
        return []
    with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_questions(items):
    with open(QUESTIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def load_promos():
    if not os.path.exists(PROMOS_FILE):
        return []
    with open(PROMOS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_promos(items):
    with open(PROMOS_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def fmt_price(n):
    return f"{int(n):,}".replace(",", " ") + " ₽"


def sanitize_images(raw):
    if isinstance(raw, list):
        return [str(u).strip() for u in raw if isinstance(u, (str,)) and str(u).strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    return []


def sanitize_badge(raw):
    b = str(raw or "").strip().lower()
    return b if b in ALLOWED_BADGES else None


def sanitize_stock(raw):
    """Остаток на складе: целое >= 0 либо None (None = учёт не ведётся, товар без ограничений)."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return None
    return max(0, min(v, 1_000_000))


def qty_by_product(items):
    """Суммарное количество по id товара (один товар может быть в корзине с разными вкусами)."""
    totals = {}
    for i in items:
        try:
            pid = int(i.get("id"))
            qty = int(i.get("qty") or 1)
        except (TypeError, ValueError, AttributeError):
            continue
        totals[pid] = totals.get(pid, 0) + qty
    return totals


def reserve_stock(items):
    """Проверяет остатки и списывает их. Возвращает текст ошибки либо None. Вызывать под DATA_LOCK."""
    totals = qty_by_product(items)
    products_list = load_products()
    by_id = {p.get("id"): p for p in products_list}
    for pid, qty in totals.items():
        p = by_id.get(pid)
        if not p or p.get("stock") is None:
            continue
        have = int(p["stock"])
        if have <= 0:
            return f"«{p.get('name')}» — нет в наличии"
        if qty > have:
            return f"«{p.get('name')}» — доступно только {have} шт."
    changed = False
    for pid, qty in totals.items():
        p = by_id.get(pid)
        if p and p.get("stock") is not None:
            p["stock"] = max(0, int(p["stock"]) - qty)
            changed = True
    if changed:
        save_products(products_list)
    return None


def shift_stock(items, sign):
    """Возвращает (sign=+1) или снова списывает (sign=-1, не ниже нуля) остатки по позициям заказа."""
    totals = qty_by_product(items)
    products_list = load_products()
    changed = False
    for p in products_list:
        qty = totals.get(p.get("id"))
        if qty and p.get("stock") is not None:
            p["stock"] = max(0, int(p["stock"]) + sign * qty)
            changed = True
    if changed:
        save_products(products_list)


def sanitize_old_price(raw):
    if raw in (None, ""):
        return None
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


# --- Промокоды -----------------------------------------------------------

def sanitize_positive_int(raw):
    """Возвращает положительное целое либо None, если значение не задано/некорректно."""
    if raw in (None, ""):
        return None
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def find_promo_by_code(code):
    code_norm = (code or "").strip().upper()
    if not code_norm:
        return None
    promos = load_promos()
    return next((p for p in promos if p.get("code") == code_norm), None)


def validate_promo_for_use(promo, subtotal, customer_chat_id=None):
    """Проверяет, что промокод можно применить к заказу на сумму subtotal.
    Возвращает текст ошибки либо None, если всё в порядке."""
    if not promo:
        return "Промокод не найден"
    if not promo.get("active", True):
        return "Промокод больше не активен"
    max_uses = promo.get("max_uses")
    if max_uses is not None and int(promo.get("used_count") or 0) >= int(max_uses):
        return "Лимит использований промокода исчерпан"
    min_total = promo.get("min_total")
    if min_total is not None and subtotal < int(min_total):
        return f"Промокод действует от суммы заказа {fmt_price(min_total)}"
    if customer_chat_id and has_customer_used_promo(promo.get("code"), customer_chat_id):
        return "Вы уже использовали этот промокод"
    return None


def has_customer_used_promo(promo_code, customer_chat_id):
    """Проверяет, применял ли этот покупатель данный промокод ранее
    (отменённые заказы не считаются использованием)."""
    if not promo_code or not customer_chat_id:
        return False
    orders = load_orders()
    return any(
        o.get("promo_code") == promo_code
        and o.get("customer_chat_id") == customer_chat_id
        and o.get("status") != "cancelled"
        for o in orders
    )


def compute_promo_discount(promo, subtotal):
    if promo.get("type") == "percent":
        discount = subtotal * float(promo.get("value") or 0) / 100.0
    else:
        discount = float(promo.get("value") or 0)
    discount = max(0.0, min(discount, subtotal))
    return int(round(discount))


# --- Telegram handlers -------------------------------------------------

@bot.message_handler(commands=["start"])
def cmd_start(message: types.Message):
    if not BASE_URL:
        bot.send_message(
            message.chat.id,
            "SH SHOP: мини-апп ещё не сконфигурирован (не задан WEBHOOK_URL)."
        )
        return

    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(
        types.InlineKeyboardButton(
            text="🚀 Открыть SH SHOP",
            web_app=types.WebAppInfo(url=BASE_URL + "/")
        )
    )

    if SHOP_CONTACT:
        keyboard.add(
            types.InlineKeyboardButton(
                text="💬 Написать менеджеру",
                url=f"https://t.me/{SHOP_CONTACT}"
            )
        )

    is_admin = bool(message.from_user) and message.from_user.id in ADMIN_CHAT_IDS
    if is_admin:
        keyboard.add(
            types.InlineKeyboardButton(
                text="Панель администратора",
                web_app=types.WebAppInfo(url=BASE_URL + "/admin")
            )
        )

    bot.send_message(
        message.chat.id,
        "🚀 *SH SHOP — всё для пара*\n\n"
        "*Лучшие цены. Всё оригинал.*\n"
        "Одноразки, жидкости, поды, расходники и шайбы — выбирайте в каталоге.\n"
        "📍 Самовывоз: Пролетарская, Девяткино, Мурино\n"
        "🚚 Доставка — 200 ₽\n\n"
        "Нажмите кнопку ниже, чтобы открыть каталог и оформить заказ.\n\n"
        "🔞 _Продукция содержит никотин, который вызывает зависимость. Только для лиц старше 18 лет._",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )



@bot.message_handler(func=lambda m: True, content_types=["text"])
def fallback(message: types.Message):
    cmd_start(message)


# --- Flask routes --------------------------------------------------------

@app.route("/", methods=["GET"])
def index():
    return send_from_directory(APP_DIR, "index.html")


@app.route("/api/config", methods=["GET"])
def public_config():
    return jsonify({"contact": SHOP_CONTACT, "delivery_fee": DELIVERY_FEE, "pickup_points": PICKUP_POINTS})


@app.route("/products.json", methods=["GET"])
def products():
    # короткий кэш + ETag: повторные открытия не качают файл заново
    resp = send_from_directory(APP_DIR, "products.json", max_age=30)
    return resp


def parse_delivery(raw):
    """Проверяет блок доставки из заказа и возвращает (delivery, error)."""
    raw = raw if isinstance(raw, dict) else {}
    mode = raw.get("mode")
    if mode not in ("pickup", "delivery"):
        return None, "Выберите самовывоз или доставку"

    d = {"mode": mode, "point": None, "address": None, "asap": True, "date": None, "time": None, "fee": 0}
    if mode == "pickup":
        point = (raw.get("point") or "").strip()
        if point not in PICKUP_POINTS:
            return None, "Выберите пункт самовывоза"
        d["point"] = point
    else:
        address = (raw.get("address") or "").strip()
        if len(address) < 5:
            return None, "Укажите адрес доставки"
        d["address"] = address[:300]
        d["fee"] = DELIVERY_FEE

    if not raw.get("asap", True):
        date_s = (raw.get("date") or "").strip()
        time_s = (raw.get("time") or "").strip()
        try:
            datetime.strptime(date_s, "%Y-%m-%d")
            datetime.strptime(time_s, "%H:%M")
        except ValueError:
            return None, "Укажите дату и время"
        d["asap"] = False
        d["date"] = date_s
        d["time"] = time_s
    return d, None


def delivery_text(d):
    if not isinstance(d, dict) or not d:
        return ""
    if d.get("mode") == "delivery":
        line = f"🚚 Доставка ({fmt_price(d.get('fee') or 0)}): {d.get('address') or '—'}"
    else:
        line = f"📍 Самовывоз: {d.get('point') or '—'}"
    if d.get("asap", True):
        when = "⚡ Как можно скорее"
    else:
        try:
            day = datetime.strptime(d.get("date", ""), "%Y-%m-%d").strftime("%d.%m")
        except ValueError:
            day = d.get("date") or ""
        when = f"🕒 {day} в {d.get('time') or ''}"
    return line + "\n" + when


@app.route("/api/order", methods=["POST"])
@locked
def create_order():
    data = request.get_json(force=True, silent=True) or {}

    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    comment = (data.get("comment") or "").strip()
    items = data.get("items") or []

    if not name or not phone:
        abort(400, description="Укажите имя и телефон")
    if not isinstance(items, list) or not items:
        abort(400, description="Корзина пуста")
    for it in items:
        try:
            if not isinstance(it, dict) or int(it.get("qty") or 1) < 1:
                raise ValueError
        except (TypeError, ValueError):
            abort(400, description="Некорректное количество товара")

    stock_err = reserve_stock(items)
    if stock_err:
        abort(400, description=stock_err)

    delivery, delivery_err = parse_delivery(data.get("delivery"))
    if delivery_err:
        abort(400, description=delivery_err)

    subtotal = sum(int(i.get("price") or 0) * int(i.get("qty") or 1) for i in items)

    tg_user = data.get("tg_user") or {}
    try:
        customer_chat_id = int(tg_user.get("id")) if tg_user.get("id") else None
    except (TypeError, ValueError):
        customer_chat_id = None
    customer_username = (tg_user.get("username") or "").strip() or None

    promo_code_raw = (data.get("promo_code") or "").strip()
    promo = None
    discount = 0
    if promo_code_raw:
        promo = find_promo_by_code(promo_code_raw)
        promo_err = validate_promo_for_use(promo, subtotal, customer_chat_id)
        if promo_err:
            abort(400, description=promo_err)
        discount = compute_promo_discount(promo, subtotal)

    total = subtotal - discount + delivery["fee"]

    orders = load_orders()
    new_id = (max([o.get("id", 0) for o in orders], default=0) + 1)
    order = {
        "id": new_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "name": name,
        "phone": phone,
        "comment": comment,
        "items": items,
        "subtotal": subtotal,
        "discount": discount,
        "promo_code": promo.get("code") if promo else None,
        "delivery": delivery,
        "total": total,
        "customer_chat_id": customer_chat_id,
        "customer_username": customer_username,
        "status": "new",
        "stock_deducted": True,
        "messages": [],
    }
    orders.append(order)
    save_orders(orders)

    if promo:
        promos = load_promos()
        for p in promos:
            if p.get("id") == promo.get("id"):
                p["used_count"] = int(p.get("used_count") or 0) + 1
        save_promos(promos)

    items_lines = "\n".join(
        f"· {i.get('name','—')} ({i.get('size') or '—'}) × {i.get('qty', 1)}"
        for i in items
    )

    delivery_block = delivery_text(delivery)
    fee_line = f"Доставка: {fmt_price(delivery['fee'])}\n" if delivery["fee"] else ""

    discount_line = ""
    if discount > 0:
        discount_line = (
            f"Сумма товаров: {fmt_price(subtotal)}\n"
            f"Промокод: {promo.get('code')} (−{fmt_price(discount)})\n"
        )

    if customer_chat_id:
        try:
            bot.send_message(
                customer_chat_id,
                "✅ Заказ оформлен!\n\n"
                f"Номер заказа: SH-{new_id}\n"
                f"{items_lines}\n\n"
                f"{delivery_block}\n\n"
                f"{discount_line}"
                f"{fee_line}"
                f"Итого: {fmt_price(total)}\n\n"
                "Мы свяжемся с вами в этом чате, чтобы подтвердить заказ и время."
            )
        except Exception as e:
            log.warning("Не удалось отправить подтверждение покупателю: %s", e)

    admin_text = (
        f"🛒 Новый заказ SH-{new_id}\n\n"
        f"Клиент: {name}\n"
        f"Телефон: {phone}\n"
        + (f"Telegram: @{customer_username}\n" if customer_username else "")
        + f"\n{items_lines}\n\n"
        f"{delivery_block}\n\n"
        f"{discount_line}"
        f"{fee_line}"
        f"Итого: {fmt_price(total)}\n"
        + (f"\nКомментарий: {comment}\n" if comment else "")
        + "\nОткройте панель администратора (вкладка «Заказы»), чтобы "
          "написать клиенту и подтвердить время."
    )
    for admin_id in ADMIN_CHAT_IDS:
        try:
            bot.send_message(admin_id, admin_text)
        except Exception as e:
            log.warning("Не удалось уведомить администратора %s: %s", admin_id, e)

    return jsonify({"ok": True, "order_id": new_id}), 201


@app.route("/api/promo/check", methods=["POST"])
def check_promo():
    """Проверка промокода из корзины (до оформления заказа)."""
    data = request.get_json(force=True, silent=True) or {}
    code = (data.get("code") or "").strip()
    try:
        subtotal = int(float(data.get("subtotal") or 0))
    except (TypeError, ValueError):
        subtotal = 0
    tg_user = data.get("tg_user") or {}
    try:
        customer_chat_id = int(tg_user.get("id")) if tg_user.get("id") else None
    except (TypeError, ValueError):
        customer_chat_id = None
    if not code:
        abort(400, description="Введите промокод")

    promo = find_promo_by_code(code)
    err = validate_promo_for_use(promo, subtotal, customer_chat_id)
    if err:
        abort(400, description=err)

    discount = compute_promo_discount(promo, subtotal)
    return jsonify({
        "ok": True,
        "code": promo.get("code"),
        "type": promo.get("type"),
        "value": promo.get("value"),
        "discount": discount,
        "new_total": subtotal - discount,
    })


@app.route("/api/orders", methods=["GET"])
def customer_orders():
    """История заказов покупателя (по его Telegram ID)."""
    try:
        tg_id = int(request.args.get("tg_id", ""))
    except (TypeError, ValueError):
        abort(400, description="Некорректный tg_id")
    orders = load_orders()
    mine = [o for o in orders if o.get("customer_chat_id") == tg_id]
    mine.sort(key=lambda o: o.get("id", 0), reverse=True)
    return jsonify(mine)


@app.route("/api/orders/<int:oid>/cancel", methods=["POST"])
@locked
def customer_cancel_order(oid):
    """Покупатель отменяет свой заказ, если он ещё не взят в обработку."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        tg_id = int(data.get("tg_id"))
    except (TypeError, ValueError):
        abort(400, description="Некорректный tg_id")

    orders = load_orders()
    order = next((o for o in orders if o.get("id") == oid), None)
    if not order:
        abort(404, description="Заказ не найден")
    if order.get("customer_chat_id") != tg_id:
        abort(403, description="Это не ваш заказ")
    if order.get("status") != "new":
        abort(400, description="Заказ уже в обработке — отмена недоступна")

    order["status"] = "cancelled"
    if order.get("stock_deducted"):
        shift_stock(order.get("items") or [], +1)   # отменённый заказ возвращает товар на склад
        order["stock_deducted"] = False
    save_orders(orders)

    for admin_id in ADMIN_CHAT_IDS:
        try:
            bot.send_message(admin_id, f"❌ Клиент отменил заказ SH-{oid}")
        except Exception as e:
            log.warning("Не удалось уведомить администратора %s: %s", admin_id, e)

    return jsonify(order)


@app.route("/api/question", methods=["POST"])
def create_question():
    data = request.get_json(force=True, silent=True) or {}

    question = (data.get("question") or "").strip()
    product_name = (data.get("product_name") or "").strip()
    try:
        product_id = int(data.get("product_id"))
    except (TypeError, ValueError):
        product_id = None

    if not question:
        abort(400, description="Введите вопрос")

    tg_user = data.get("tg_user") or {}
    try:
        customer_chat_id = int(tg_user.get("id")) if tg_user.get("id") else None
    except (TypeError, ValueError):
        customer_chat_id = None
    customer_username = (tg_user.get("username") or "").strip() or None

    questions = load_questions()
    new_id = (max([q.get("id", 0) for q in questions], default=0) + 1)
    entry = {
        "id": new_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "product_id": product_id,
        "product_name": product_name,
        "question": question,
        "customer_chat_id": customer_chat_id,
        "customer_username": customer_username,
        "status": "new",
        "messages": [],
    }
    questions.append(entry)
    save_questions(questions)

    if customer_chat_id:
        try:
            bot.send_message(
                customer_chat_id,
                "✅ Ваш вопрос отправлен администратору.\n\n"
                f"Товар: {product_name}\n"
                f"Вопрос: {question}\n\n"
                "Мы ответим вам в этом чате."
            )
        except Exception as e:
            log.warning("Не удалось отправить подтверждение по вопросу: %s", e)

    admin_text = (
        f"❓ Новый вопрос по товару «{product_name}»\n\n"
        + (f"От: @{customer_username}\n\n" if customer_username else "\n")
        + f"{question}\n\n"
        "Откройте панель администратора (вкладка «Вопросы»), чтобы ответить клиенту."
    )
    for admin_id in ADMIN_CHAT_IDS:
        try:
            bot.send_message(admin_id, admin_text)
        except Exception as e:
            log.warning("Не удалось уведомить администратора %s: %s", admin_id, e)

    return jsonify({"ok": True, "question_id": new_id}), 201


@app.route("/uploads/<path:filename>", methods=["GET"])
def uploaded_file(filename):
    # имена файлов — uuid, содержимое не меняется, можно кэшировать надолго
    return send_from_directory(UPLOAD_DIR, filename, max_age=60 * 60 * 24 * 30)


@app.route("/admin", methods=["GET"])
def admin_page():
    return send_from_directory(APP_DIR, "admin.html")


@app.route("/healthz", methods=["GET"])
def healthz():
    return {"status": "ok"}


# --- Admin API -------------------------------------------------------

@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    require_admin()
    return jsonify({"ok": True})


@app.route("/api/admin/products", methods=["GET"])
def admin_get_products():
    require_admin()
    return jsonify(load_products())


@app.route("/api/admin/products", methods=["POST"])
@locked
def admin_create_product():
    require_admin()
    data = request.get_json(force=True, silent=True) or {}
    items = load_products()
    new_id = (max([p.get("id", 0) for p in items], default=0) + 1)
    sizes_raw = data.get("sizes", "")
    sizes = [s.strip() for s in sizes_raw.split(",") if s.strip()] if isinstance(sizes_raw, str) else (sizes_raw or [])
    try:
        price = int(float(data.get("price") or 0))
    except (TypeError, ValueError):
        price = 0
    images = sanitize_images(data.get("images") if "images" in data else data.get("image"))
    product = {
        "id": new_id,
        "name": (data.get("name") or "").strip(),
        "category": (data.get("category") or "").strip(),
        "subcategory": (data.get("subcategory") or "").strip(),
        "price": price,
        "old_price": sanitize_old_price(data.get("old_price")),
        "badge": sanitize_badge(data.get("badge")),
        "sizes": sizes or ["Стандарт"],
        "swatch": int(data.get("swatch") or 0) % 6,
        "desc": (data.get("desc") or "").strip(),
        "images": images,
        "image": images[0] if images else None,
        "stock": sanitize_stock(data.get("stock")),
    }
    items.append(product)
    save_products(items)
    return jsonify(product), 201


@app.route("/api/admin/products/<int:pid>", methods=["PUT"])
@locked
def admin_update_product(pid):
    require_admin()
    data = request.get_json(force=True, silent=True) or {}
    items = load_products()
    for p in items:
        if p.get("id") == pid:
            if "name" in data:
                p["name"] = (data.get("name") or "").strip()
            if "category" in data:
                p["category"] = (data.get("category") or "").strip()
            if "subcategory" in data:
                p["subcategory"] = (data.get("subcategory") or "").strip()
            if "price" in data:
                try:
                    p["price"] = int(float(data.get("price") or 0))
                except (TypeError, ValueError):
                    pass
            if "old_price" in data:
                p["old_price"] = sanitize_old_price(data.get("old_price"))
            if "badge" in data:
                p["badge"] = sanitize_badge(data.get("badge"))
            if "sizes" in data:
                sizes_raw = data.get("sizes", "")
                p["sizes"] = [s.strip() for s in sizes_raw.split(",") if s.strip()] if isinstance(sizes_raw, str) else (sizes_raw or p["sizes"])
            if "stock" in data:
                p["stock"] = sanitize_stock(data.get("stock"))
            if "desc" in data:
                p["desc"] = (data.get("desc") or "").strip()
            if "swatch" in data:
                p["swatch"] = int(data.get("swatch") or 0) % 6
            if "images" in data:
                images = sanitize_images(data.get("images"))
                p["images"] = images
                p["image"] = images[0] if images else None
            elif "image" in data:
                images = sanitize_images(data.get("image"))
                p["images"] = images
                p["image"] = images[0] if images else None
            save_products(items)
            return jsonify(p)
    abort(404, description="Товар не найден")


@app.route("/api/admin/products/<int:pid>", methods=["DELETE"])
@locked
def admin_delete_product(pid):
    require_admin()
    items = load_products()
    new_items = [p for p in items if p.get("id") != pid]
    if len(new_items) == len(items):
        abort(404, description="Товар не найден")
    save_products(new_items)
    return jsonify({"ok": True})


@app.route("/api/admin/upload", methods=["POST"])
def admin_upload():
    require_admin()
    file = request.files.get("file")
    if not file or not file.filename:
        abort(400, description="Файл не передан")
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_IMAGE_EXT:
        abort(400, description="Недопустимый формат файла")
    filename = f"{uuid.uuid4().hex}{ext}"
    file.save(os.path.join(UPLOAD_DIR, filename))
    return jsonify({"url": f"/uploads/{filename}"}), 201


@app.route("/api/admin/promos", methods=["GET"])
def admin_get_promos():
    require_admin()
    promos = load_promos()
    promos.sort(key=lambda p: p.get("id", 0), reverse=True)
    return jsonify(promos)


@app.route("/api/admin/promos", methods=["POST"])
def admin_create_promo():
    require_admin()
    data = request.get_json(force=True, silent=True) or {}

    code = (data.get("code") or "").strip().upper()
    if not code:
        abort(400, description="Укажите код промокода")

    promo_type = (data.get("type") or "").strip().lower()
    if promo_type not in ALLOWED_PROMO_TYPES:
        abort(400, description="Некорректный тип скидки")

    try:
        value = float(data.get("value"))
    except (TypeError, ValueError):
        abort(400, description="Укажите значение скидки")
    if value <= 0:
        abort(400, description="Значение скидки должно быть больше нуля")
    if promo_type == "percent" and value > 100:
        abort(400, description="Скидка в процентах не может быть больше 100")

    promos = load_promos()
    if any(p.get("code") == code for p in promos):
        abort(400, description="Такой промокод уже существует")

    new_id = (max([p.get("id", 0) for p in promos], default=0) + 1)
    promo = {
        "id": new_id,
        "code": code,
        "type": promo_type,
        "value": value,
        "active": bool(data.get("active", True)),
        "max_uses": sanitize_positive_int(data.get("max_uses")),
        "used_count": 0,
        "min_total": sanitize_positive_int(data.get("min_total")),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    promos.append(promo)
    save_promos(promos)
    return jsonify(promo), 201


@app.route("/api/admin/promos/<int:pid>", methods=["PUT"])
def admin_update_promo(pid):
    require_admin()
    data = request.get_json(force=True, silent=True) or {}
    promos = load_promos()
    for p in promos:
        if p.get("id") == pid:
            if "code" in data:
                code = (data.get("code") or "").strip().upper()
                if not code:
                    abort(400, description="Укажите код промокода")
                if any(o.get("code") == code and o.get("id") != pid for o in promos):
                    abort(400, description="Такой промокод уже существует")
                p["code"] = code
            if "type" in data:
                promo_type = (data.get("type") or "").strip().lower()
                if promo_type not in ALLOWED_PROMO_TYPES:
                    abort(400, description="Некорректный тип скидки")
                p["type"] = promo_type
            if "value" in data:
                try:
                    value = float(data.get("value"))
                except (TypeError, ValueError):
                    abort(400, description="Укажите значение скидки")
                if value <= 0:
                    abort(400, description="Значение скидки должно быть больше нуля")
                if p.get("type") == "percent" and value > 100:
                    abort(400, description="Скидка в процентах не может быть больше 100")
                p["value"] = value
            if "active" in data:
                p["active"] = bool(data.get("active"))
            if "max_uses" in data:
                p["max_uses"] = sanitize_positive_int(data.get("max_uses"))
            if "min_total" in data:
                p["min_total"] = sanitize_positive_int(data.get("min_total"))
            save_promos(promos)
            return jsonify(p)
    abort(404, description="Промокод не найден")


@app.route("/api/admin/promos/<int:pid>", methods=["DELETE"])
def admin_delete_promo(pid):
    require_admin()
    promos = load_promos()
    new_promos = [p for p in promos if p.get("id") != pid]
    if len(new_promos) == len(promos):
        abort(404, description="Промокод не найден")
    save_promos(new_promos)
    return jsonify({"ok": True})


@app.route("/api/admin/orders", methods=["GET"])
def admin_get_orders():
    require_admin()
    orders = load_orders()
    orders.sort(key=lambda o: o.get("id", 0), reverse=True)
    return jsonify(orders)


@app.route("/api/admin/orders/<int:oid>/status", methods=["PUT"])
@locked
def admin_update_order_status(oid):
    require_admin()
    data = request.get_json(force=True, silent=True) or {}
    status = (data.get("status") or "").strip()
    if status not in {"new", "contacted", "done", "cancelled"}:
        abort(400, description="Некорректный статус")
    orders = load_orders()
    for o in orders:
        if o.get("id") == oid:
            prev = o.get("status")
            o["status"] = status
            # остатки следуют за статусом: отмена возвращает товар, «оживление» отменённого заказа списывает снова
            if status == "cancelled" and prev != "cancelled" and o.get("stock_deducted"):
                shift_stock(o.get("items") or [], +1)
                o["stock_deducted"] = False
            elif prev == "cancelled" and status != "cancelled" and o.get("stock_deducted") is False:
                shift_stock(o.get("items") or [], -1)
                o["stock_deducted"] = True
            save_orders(orders)
            return jsonify(o)
    abort(404, description="Заказ не найден")


@app.route("/api/admin/orders/<int:oid>/message", methods=["POST"])
def admin_message_customer(oid):
    require_admin()
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        abort(400, description="Введите текст сообщения")

    orders = load_orders()
    order = next((o for o in orders if o.get("id") == oid), None)
    if not order:
        abort(404, description="Заказ не найден")

    chat_id = order.get("customer_chat_id")
    if not chat_id:
        abort(400, description="У этого заказа нет чата с покупателем в Telegram")

    try:
        bot.send_message(
            chat_id,
            f"💬 Сообщение от SH SHOP по заказу SH-{oid}:\n\n{text}"
        )
    except Exception as e:
        abort(400, description=f"Не удалось отправить сообщение: {e}")

    order.setdefault("messages", []).append({
        "text": text,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    if order.get("status") == "new":
        order["status"] = "contacted"
    save_orders(orders)
    return jsonify(order)


@app.route("/api/admin/questions", methods=["GET"])
def admin_get_questions():
    require_admin()
    questions = load_questions()
    questions.sort(key=lambda q: q.get("id", 0), reverse=True)
    return jsonify(questions)


@app.route("/api/admin/questions/<int:qid>/message", methods=["POST"])
def admin_message_question(qid):
    require_admin()
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        abort(400, description="Введите текст ответа")

    questions = load_questions()
    q = next((x for x in questions if x.get("id") == qid), None)
    if not q:
        abort(404, description="Вопрос не найден")

    chat_id = q.get("customer_chat_id")
    if not chat_id:
        abort(400, description="У этого вопроса нет чата с покупателем в Telegram")

    try:
        bot.send_message(
            chat_id,
            f"💬 Ответ от SH SHOP по товару «{q.get('product_name','')}»:\n\n{text}"
        )
    except Exception as e:
        abort(400, description=f"Не удалось отправить ответ: {e}")

    q.setdefault("messages", []).append({
        "text": text,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    q["status"] = "answered"
    save_questions(questions)
    return jsonify(q)


@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    if request.headers.get("content-type") != "application/json":
        abort(403)
    update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
    bot.process_new_updates([update])
    return "OK", 200


@app.errorhandler(400)
@app.errorhandler(401)
@app.errorhandler(403)
@app.errorhandler(404)
def handle_api_errors(err):
    if request.path.startswith("/api/"):
        return jsonify({"error": getattr(err, "description", str(err))}), err.code
    return err


def setup_webhook():
    if not BASE_URL:
        log.warning("WEBHOOK_URL / RENDER_EXTERNAL_URL не заданы — вебхук не установлен.")
        return
    url = f"{BASE_URL}/webhook/{BOT_TOKEN}"
    bot.remove_webhook()
    bot.set_webhook(url=url)
    log.info("Webhook установлен: %s", url)


setup_webhook()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
