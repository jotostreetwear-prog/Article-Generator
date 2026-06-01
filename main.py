import os
import json
import httpx
import schedule
import time
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import threading
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)

WB_API_TOKEN = os.environ.get("WB_API_TOKEN", "").strip()
B24_WEBHOOK = os.environ.get("B24_WEBHOOK", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
CTR_DIALOG_ID = "chat2024"

CATEGORIES = {
    "жилет": "01", "жилеты": "01",
    "куртка": "02", "куртки": "02",
    "водолазка": "03", "водолазки": "03",
    "джинсы": "04", "джинс": "04",
    "худи": "05",
    "свитер": "06", "свитера": "06",
    "лонгслив": "07", "лонгсливы": "07",
    "брюки": "09", "брюк": "09",
    "шорты": "10", "шорт": "10",
    "футболка": "11", "футболки": "11",
}

WB_CATEGORIES = {
    "01": "Жилеты", "02": "Куртки", "03": "Водолазки",
    "04": "Джинсы", "05": "Худи", "06": "Свитеры",
    "07": "Лонгсливы", "09": "Брюки", "10": "Шорты", "11": "Футболки",
}

user_states = {}
previous_ctr = {}


# ===== БАЗА ДАННЫХ =====

def get_db():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS model_counters (
                category_code VARCHAR(10) PRIMARY KEY,
                count INTEGER DEFAULT 0
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("БД инициализирована")
    except Exception as e:
        print(f"Ошибка БД: {e}")


def get_next_model_number(category_code: str) -> int:
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO model_counters (category_code, count)
            VALUES (%s, 1)
            ON CONFLICT (category_code)
            DO UPDATE SET count = model_counters.count + 1
            RETURNING count
        """, (category_code,))
        result = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        return result[0]
    except Exception as e:
        print(f"Ошибка получения номера модели: {e}")
        return 1


def get_current_counters() -> dict:
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM model_counters ORDER BY category_code")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return {row["category_code"]: row["count"] for row in rows}
    except Exception as e:
        print(f"Ошибка получения счётчиков: {e}")
        return {}


# ===== АРТИКУЛЫ =====

def generate_articul(category_code: str, model_num: int, color: str) -> str:
    return f"J{category_code}{str(model_num).zfill(3)}/{color.lower().strip()}"


def generate_description(title: str, category: str, color: str) -> str:
    prompt = (
        f"Напиши короткое продающее описание товара для карточки на Wildberries.\n"
        f"Товар: {title}\nКатегория: {category}\nЦвет: {color}\n"
        f"Требования: 2-3 предложения, на русском, упомяни бренд Joto. Только текст, без заголовков."
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    resp = httpx.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=30)
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


def create_wb_card(articul: str, title: str, description: str, category_code: str, color: str) -> dict:
    wb_category = WB_CATEGORIES.get(category_code, "Одежда")
    payload = {
        "subjectName": wb_category,
        "variants": [{
            "vendorCode": articul,
            "title": title,
            "description": description,
            "brand": "Joto",
            "dimensions": {"length": 30, "width": 20, "height": 5},
            "characteristics": [{"Цвет": [color]}, {"Бренд": ["Joto"]}]
        }]
    }
    headers = {"Authorization": WB_API_TOKEN, "Content-Type": "application/json"}
    response = httpx.post(
        "https://content-api.wildberries.ru/content/v2/cards/upload",
        json=[payload], headers=headers, timeout=30
    )
    return response.json()


def parse_message(text: str) -> dict | None:
    lines = text.lower().strip().split("\n")
    data = {}
    for line in lines:
        if ":" in line:
            key, _, val = line.partition(":")
            data[key.strip()] = val.strip()

    category_word = data.get("категория", "")
    category_code = CATEGORIES.get(category_word)
    if not category_code:
        return None

    color = data.get("цвет", "")
    title = data.get("название", "")
    if not color or not title:
        return None

    return {"category_code": category_code, "color": color, "title": title}


# ===== CTR МОНИТОРИНГ =====

def get_wb_ctr():
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")

    headers = {
        "Authorization": WB_API_TOKEN,
        "Content-Type": "application/json"
    }

    payload = {
        "brandNames": [],
        "objectIDs": [],
        "tagIDs": [],
        "nmIDs": [],
        "timezone": "Europe/Moscow",
        "selectedPeriod": {
            "start": yesterday,
            "end": today
        },
        "orderBy": {
            "field": "orderCount",
            "mode": "desc"
        },
        "page": 1
    }

    try:
        resp = httpx.post(
            "https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products",
            json=payload,
            headers=headers,
            timeout=30
        )
        print(f"WB CTR API: {resp.status_code}")
        return resp.json()
    except Exception as e:
        print(f"Ошибка WB CTR API: {e}")
        return None


def check_ctr():
    global previous_ctr
    print(f"Проверяю CTR... {datetime.now()}")

    data = get_wb_ctr()
    if not data:
        return

    products = (data.get("data", {}) or {}).get("products", [])
    if not products:
        products = data.get("products", [])
    if not products:
        print(f"Нет данных. Ответ: {json.dumps(data, ensure_ascii=False)[:300]}")
        return

    alerts = []

    for product in products:
        nm_id = str(product.get("nmID", product.get("nmId", "")))
        vendor_code = product.get("vendorCode", nm_id)
        name = product.get("name", product.get("imtName", vendor_code))

        statistic = product.get("statistic", {})
        selected = statistic.get("selected", statistic)

        open_card = selected.get("openCardCount", 0)
        view_count = selected.get("searchResultSuperpositionCount", selected.get("viewCount", 0))

        if view_count > 0:
            current_ctr = round((open_card / view_count) * 100, 2)
        else:
            current_ctr = 0

        if nm_id in previous_ctr:
            prev = previous_ctr[nm_id]
            drop = prev - current_ctr
            if drop >= 1.0:
                alerts.append(
                    f"Артикул: {vendor_code}\n"
                    f"Название: {name}\n"
                    f"CTR: {prev}% -> {current_ctr}% (снижение на {round(drop, 2)}%)"
                )

        previous_ctr[nm_id] = current_ctr

    if alerts:
        message = "Снижение CTR более чем на 1%\n\n" + "\n\n".join(alerts)
        send_b24_message(CTR_DIALOG_ID, message)
        print(f"Отправлено {len(alerts)} уведомлений")
    else:
        print("Снижений CTR >= 1% не найдено")


# ===== ОБЩИЕ ФУНКЦИИ =====

def send_b24_message(dialog_id: str, text: str):
    try:
        resp = httpx.post(
            f"{B24_WEBHOOK}/im.message.add.json",
            json={"DIALOG_ID": dialog_id, "MESSAGE": text},
            timeout=10
        )
        print(f"Битрикс ответ: {resp.status_code}")
    except Exception as e:
        print(f"Ошибка отправки: {e}")


def run_scheduler():
    schedule.every().day.at("06:00").do(check_ctr)
    print("Планировщик CTR запущен — проверка каждый день в 09:00 МСК")
    while True:
        schedule.run_pending()
        time.sleep(60)


# ===== WEBHOOK =====

@app.route("/", methods=["GET"])
def index():
    return "JOTO Bot работает"


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        if request.content_type and 'application/json' in request.content_type:
            data = request.json or {}
        else:
            data = request.form.to_dict()

        dialog_id = data.get("data[PARAMS][DIALOG_ID]", "").strip()
        from_user_id = data.get("data[PARAMS][FROM_USER_ID]", "").strip()
        text = data.get("data[PARAMS][MESSAGE]", "").strip()

        if not text or not dialog_id:
            return jsonify({"ok": True})

        state_key = from_user_id or dialog_id

        if text.lower() in ["помощь", "help", "/help", "start", "/start"]:
            counters = get_current_counters()
            counters_text = "\n".join([
                f"{WB_CATEGORIES.get(k, k)}: {v} моделей"
                for k, v in counters.items()
            ]) or "Моделей пока нет"
            send_b24_message(dialog_id,
                "Привет! Я создаю артикулы и карточки товаров на Wildberries.\n\n"
                "Напиши в таком формате:\n\n"
                "категория: худи\n"
                "цвет: black\n"
                "название: Худи оверсайз мужское\n\n"
                "Доступные категории:\n"
                "жилет, куртка, водолазка, джинсы, худи,\n"
                "свитер, лонгслив, брюки, шорты, футболка\n\n"
                f"Текущие счётчики моделей:\n{counters_text}"
            )
            return jsonify({"ok": True})

        if state_key in user_states:
            state = user_states[state_key]

            # Ожидаем подтверждение
            if text.lower() in ["да", "yes", "ок", "ok", "подтверждаю"]:
                category_code = state["category_code"]
                color = state["color"]
                title = state["title"]

                # Получаем следующий номер модели из БД
                model_num = get_next_model_number(category_code)
                articul = generate_articul(category_code, model_num, color)

                send_b24_message(dialog_id, f"Генерирую описание и создаю карточку...\nАртикул: {articul}")

                description = generate_description(title, WB_CATEGORIES.get(category_code, ""), color)
                result = create_wb_card(articul, title, description, category_code, color)

                del user_states[state_key]

                if result.get("error"):
                    send_b24_message(dialog_id,
                        f"Ошибка WB: {result.get('errorText', 'неизвестная ошибка')}\n"
                        f"Артикул {articul} сгенерирован, но карточка не создана."
                    )
                else:
                    send_b24_message(dialog_id,
                        f"Готово!\n\n"
                        f"Артикул: {articul}\n"
                        f"Название: {title}\n\n"
                        f"Описание:\n{description}\n\n"
                        f"Карточка создана на Wildberries!"
                    )
            elif text.lower() in ["нет", "no", "отмена", "отменить"]:
                del user_states[state_key]
                send_b24_message(dialog_id, "Отменено. Напиши новый запрос когда будешь готов.")
            else:
                send_b24_message(dialog_id, "Напиши 'да' чтобы подтвердить или 'нет' чтобы отменить.")
            return jsonify({"ok": True})

        parsed = parse_message(text)
        if not parsed:
            send_b24_message(dialog_id,
                "Не понял формат. Напиши 'помощь' чтобы увидеть пример."
            )
            return jsonify({"ok": True})

        # Показываем что будет создано и просим подтверждение
        category_code = parsed["category_code"]
        counters = get_current_counters()
        next_num = counters.get(category_code, 0) + 1
        preview_articul = generate_articul(category_code, next_num, parsed["color"])

        user_states[state_key] = parsed
        send_b24_message(dialog_id,
            f"Проверь данные:\n\n"
            f"Категория: {WB_CATEGORIES.get(category_code)}\n"
            f"Цвет: {parsed['color']}\n"
            f"Название: {parsed['title']}\n"
            f"Артикул будет: {preview_articul}\n\n"
            f"Подтверждаешь? (да/нет)"
        )

    except Exception as e:
        print(f"Ошибка: {e}")

    return jsonify({"ok": True})


@app.route("/check-ctr", methods=["GET"])
def check_ctr_now():
    threading.Thread(target=check_ctr, daemon=True).start()
    return jsonify({"ok": True, "message": "Проверка CTR запущена"})


@app.route("/counters", methods=["GET"])
def counters():
    return jsonify(get_current_counters())


if __name__ == "__main__":
    init_db()
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
