
import os
import json
import httpx
from flask import Flask, request, jsonify
import anthropic

app = Flask(__name__)

# === НАСТРОЙКИ ===
WB_API_TOKEN = os.environ.get("WB_API_TOKEN", "")
B24_WEBHOOK = os.environ.get("B24_WEBHOOK", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Категории JOTO
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

# Хранилище состояний пользователей (в памяти)
user_states = {}


def generate_articul(category_code: str, model_num: int, color: str) -> str:
    return f"J{category_code}{str(model_num).zfill(3)}/{color.lower().strip()}"


def generate_description(title: str, category: str, color: str) -> str:
    """Генерирует описание товара через Claude"""
    prompt = (
        f"Напиши короткое продающее описание товара для карточки на Wildberries.\n"
        f"Товар: {title}\n"
        f"Категория: {category}\n"
        f"Цвет: {color}\n"
        f"Требования: 2-3 предложения, без лишних слов, на русском языке. "
        f"Упомяни бренд Joto. Только текст описания, без заголовков."
    )
    message = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text.strip()


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
            "characteristics": [
                {"Цвет": [color]},
                {"Бренд": ["Joto"]},
            ]
        }]
    }
    headers = {"Authorization": WB_API_TOKEN, "Content-Type": "application/json"}
    response = httpx.post(
        "https://content-api.wildberries.ru/content/v2/cards/upload",
        json=[payload],
        headers=headers,
        timeout=30
    )
    return response.json()


def send_b24_message(user_id: str, text: str):
    httpx.post(
        f"{B24_WEBHOOK}/im.message.add.json",
        json={"DIALOG_ID": user_id, "MESSAGE": text},
        timeout=10
    )


def parse_first_message(text: str) -> dict | None:
    """Парсит первое сообщение: категория, цвет, название"""
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


@app.route("/", methods=["GET"])
def index():
    return "JOTO Bot работает ✓"


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.json or request.form.to_dict()
        user_id = data.get("data[USER][ID]") or data.get("USER_ID", "")
        text = data.get("data[MESSAGE]") or data.get("MESSAGE", "")

        if not text:
            return jsonify({"ok": True})

        text = text.strip()

        # Команда помощи
        if text.lower() in ["помощь", "help", "/help", "start", "/start"]:
            send_b24_message(user_id,
                "👋 Привет! Я создаю артикулы и карточки товаров на Wildberries.\n\n"
                "Напиши мне в таком формате:\n\n"
                "категория: худи\n"
                "цвет: black\n"
                "название: Худи оверсайз мужское\n\n"
                "Доступные категории:\n"
                "жилет, куртка, водолазка, джинсы, худи,\n"
                "свитер, лонгслив, брюки, шорты, футболка"
            )
            return jsonify({"ok": True})

        # Пользователь отвечает на вопрос о номере модели
        if user_id in user_states:
            state = user_states[user_id]
            try:
                model_num = int(text)
                if model_num < 1:
                    raise ValueError
            except ValueError:
                send_b24_message(user_id, "❌ Введи просто число, например: 3")
                return jsonify({"ok": True})

            # Всё есть — генерируем
            category_code = state["category_code"]
            color = state["color"]
            title = state["title"]

            articul = generate_articul(category_code, model_num, color)
            send_b24_message(user_id, f"⏳ Генерирую описание и создаю карточку...\nАртикул: {articul}")

            description = generate_description(title, WB_CATEGORIES.get(category_code, ""), color)
            result = create_wb_card(articul, title, description, category_code, color)

            del user_states[user_id]

            if result.get("error"):
                send_b24_message(user_id,
                    f"❌ Ошибка WB: {result.get('errorText', 'неизвестная ошибка')}\n"
                    f"Артикул {articul} сгенерирован, но карточка не создана."
                )
            else:
                send_b24_message(user_id,
                    f"✅ Готово!\n\n"
                    f"Артикул: {articul}\n"
                    f"Название: {title}\n\n"
                    f"Описание:\n{description}\n\n"
                    f"Карточка создана на Wildberries!"
                )
            return jsonify({"ok": True})

        # Первое сообщение — парсим категорию/цвет/название
        parsed = parse_first_message(text)
        if not parsed:
            send_b24_message(user_id,
                "❌ Не понял формат. Напиши 'помощь' чтобы увидеть пример."
            )
            return jsonify({"ok": True})

        # Сохраняем состояние и спрашиваем номер модели
        user_states[user_id] = parsed
        send_b24_message(user_id,
            f"📦 Категория: {WB_CATEGORIES.get(parsed['category_code'])}\n"
            f"Цвет: {parsed['color']}\n"
            f"Название: {parsed['title']}\n\n"
            f"Это какая модель по счёту? (введи число, например: 3)"
        )

    except Exception as e:
        print(f"Ошибка: {e}")

    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
