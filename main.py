import os
import httpx
import threading
import schedule
import time
import psycopg2
from flask import Flask, request, jsonify
from datetime import datetime, timedelta

app = Flask(__name__)

WB_API_TOKEN = os.environ.get("WB_API_TOKEN", "").strip()
B24_WEBHOOK = os.environ.get("B24_WEBHOOK", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

# Категории по инструкции JOTO
CATEGORIES = {
    "жилет": "01",
    "жилеты": "01",
    "куртка": "02",
    "куртки": "02",
    "водолазка": "03",
    "водолазки": "03",
    "джинсы": "04",
    "худи": "05",
    "свитер": "06",
    "свитера": "06",
    "лонгслив": "07",
    "лонгсливы": "07",
    "брюки": "09",
    "шорты": "10",
    "футболка": "11",
    "футболки": "11",
}

CATS_LIST = "жилет, куртка, водолазка, джинсы, худи, свитер, лонгслив, брюки, шорты, футболка"

# Состояния диалога
user_states = {}

# ===================== БД =====================

def get_db():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS model_counters (
                category_code VARCHAR(10) PRIMARY KEY,
                counter INTEGER DEFAULT 0
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("БД инициализирована")
    except Exception as e:
        print(f"Ошибка БД: {e}")

def get_next_model_number(category_code):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO model_counters (category_code, counter)
            VALUES (%s, 1)
            ON CONFLICT (category_code) DO UPDATE
            SET counter = model_counters.counter + 1
            RETURNING counter
        """, (category_code,))
        number = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        return str(number).zfill(3)
    except Exception as e:
        print(f"Ошибка счётчика: {e}")
        return "001"

def get_current_counter(category_code):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT counter FROM model_counters WHERE category_code=%s", (category_code,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row[0] if row else 0
    except Exception as e:
        print(f"Ошибка чтения счётчика: {e}")
        return 0

# ===================== БИТРИКС =====================

def send_b24_message(dialog_id, text):
    try:
        url = f"{B24_WEBHOOK}/im.message.add.json"
        resp = httpx.post(url, json={"DIALOG_ID": dialog_id, "MESSAGE": text}, timeout=10)
        print(f"Ответ Битрикс: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"Ошибка отправки: {e}")

# ===================== АРТИКУЛЫ =====================

def handle_message(user_id, text):
    text = text.strip()
    state = user_states.get(user_id, {})
    step = state.get("step", "start")

    print(f"handle_message: user_id={user_id}, step={step}, text={text}")

    # Помощь / старт
    if any(word in text.lower() for word in ["помощь", "help", "начать", "старт", "привет", "/start"]):
        user_states[user_id] = {"step": "start"}
        send_b24_message(user_id,
            "👋 Привет! Я бот JOTO для создания артикулов.\n\n"
            "Напиши *артикул* чтобы создать новый артикул.\n\n"
            f"Доступные категории:\n{CATS_LIST}"
        )
        return

    # Начать создание артикула
    if text.lower() in ["артикул", "создать", "новый"]:
        user_states[user_id] = {"step": "wait_category"}
        send_b24_message(user_id,
            f"📦 *Создание артикула*\n\n"
            f"Шаг 1/3: Введите категорию товара:\n{CATS_LIST}"
        )
        return

    # Шаг 1: Категория
    if step == "wait_category":
        category = text.lower()
        if category not in CATEGORIES:
            send_b24_message(user_id, f"❌ Категория не найдена.\n\nВведите одну из:\n{CATS_LIST}")
            return
        category_code = CATEGORIES[category]
        current = get_current_counter(category_code)
        next_num = str(current + 1).zfill(3)
        user_states[user_id] = {"step": "wait_color", "category": category, "category_code": category_code}
        send_b24_message(user_id,
            f"✅ Категория: {category.capitalize()} (J{category_code})\n"
            f"Следующий номер модели будет: *{next_num}*\n\n"
            f"Шаг 2/3: Введите цвет (например: black, white, grey, navy):"
        )
        return

    # Шаг 2: Цвет
    if step == "wait_color":
        color = text.lower().replace(" ", "")
        user_states[user_id]["color"] = color
        user_states[user_id]["step"] = "wait_name"
        send_b24_message(user_id, f"✅ Цвет: {color}\n\nШаг 3/3: Введите название товара:")
        return

    # Шаг 3: Название
    if step == "wait_name":
        category = state["category"]
        category_code = state["category_code"]
        color = state["color"]
        name = text

        model_number = get_next_model_number(category_code)
        article = f"J{category_code}{model_number}/{color}"

        user_states[user_id] = {"step": "start"}
        send_b24_message(user_id,
            f"✅ *Артикул создан!*\n\n"
            f"🏷 Артикул: *{article}*\n"
            f"📁 Категория: {category.capitalize()}\n"
            f"🎨 Цвет: {color}\n"
            f"📝 Название: {name}\n"
            f"🔢 Модель №{model_number}\n\n"
            f"Для создания ещё одного напиши *артикул*"
        )
        return

    # Не распознано
    send_b24_message(user_id, "Напиши *артикул* чтобы создать новый артикул, или *помощь* для справки.")

# ===================== CTR МОНИТОРИНГ =====================

previous_ctr = {}

def get_wb_ctr():
    try:
        today = datetime.now().date()
        date_from = (today - timedelta(days=2)).strftime("%Y-%m-%d")
        date_to = today.strftime("%Y-%m-%d")

        url = "https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products"
        headers = {"Authorization": WB_API_TOKEN}
        payload = {
            "dateFrom": date_from,
            "dateTo": date_to,
            "limit": 100,
            "offset": 0,
            "orderBy": {"field": "addToCartCount", "mode": "desc"},
            "selectedPeriod": {"begin": date_from, "end": date_to}
        }

        resp = httpx.post(url, headers=headers, json=payload, timeout=30)
        print(f"WB API статус: {resp.status_code}")
        if resp.status_code != 200:
            print(f"WB API ошибка: {resp.text[:300]}")
            return {}

        data = resp.json()
        items = data.get("data", {}).get("products", []) or data.get("products", []) or []

        result = {}
        for item in items:
            nm_id = item.get("nmID") or item.get("nmId")
            name = item.get("vendorCode", str(nm_id))
            views = item.get("openCardCount", 0) or 0
            clicks = item.get("addToCartCount", 0) or 0
            if nm_id and views > 0:
                result[nm_id] = {"ctr": round(clicks / views * 100, 2), "name": name}

        print(f"Получено артикулов с данными: {len(result)}")
        return result

    except Exception as e:
        print(f"Ошибка WB API: {e}")
        return {}

def check_ctr():
    global previous_ctr
    print(f"Проверка CTR: {datetime.now()}")

    current = get_wb_ctr()
    if not current:
        print("Нет данных CTR")
        return

    alerts = []
    for nm_id, data in current.items():
        ctr = data["ctr"]
        name = data["name"]
        if nm_id in previous_ctr:
            prev_ctr = previous_ctr[nm_id]["ctr"]
            if prev_ctr > 0 and (prev_ctr - ctr) >= 1.0:
                alerts.append(f"⚠️ {name}: CTR снизился с {prev_ctr}% до {ctr}% (−{round(prev_ctr-ctr,2)}%)")

    previous_ctr = current

    if alerts:
        msg = "📉 *Снижение CTR на Wildberries:*\n\n" + "\n".join(alerts)
        send_b24_message("chat2024", msg)
        print(f"Отправлено {len(alerts)} уведомлений")
    else:
        print("Снижений CTR >= 1% не найдено")

# ===================== FLASK =====================

@app.route("/", methods=["GET"])
def index():
    return "JOTO Bot работает ✓"

@app.route("/check-now", methods=["GET"])
def check_now():
    threading.Thread(target=check_ctr).start()
    return jsonify({"ok": True})

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        if request.content_type and "application/json" in request.content_type:
            data = request.json or {}
        else:
            data = request.form.to_dict()

        from_user_id = data.get("data[PARAMS][FROM_USER_ID]", "").strip()
        dialog_id = data.get("data[PARAMS][DIALOG_ID]", "").strip()
        text = data.get("data[PARAMS][MESSAGE]", "").strip()

        user_id = from_user_id or dialog_id
        print(f"user_id={user_id}, text={text}")

        if user_id and text:
            threading.Thread(target=handle_message, args=(user_id, text)).start()

        return jsonify({"ok": True})

    except Exception as e:
        print(f"Ошибка webhook: {e}")
        return jsonify({"ok": False})

# ===================== ЗАПУСК =====================

def run_scheduler():
    schedule.every().day.at("06:00").do(check_ctr)
    print("Планировщик запущен — проверка каждый день в 09:00 МСК")
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    init_db()
    threading.Thread(target=run_scheduler, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
