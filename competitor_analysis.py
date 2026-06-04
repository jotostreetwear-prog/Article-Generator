#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Анализ конкурентов Wildberries по артикулу.

Отдельный самостоятельный инструмент (не связан с генератором артикулов).
По одному артикулу (nmID) собирает похожие товары того же предмета из
ПУБЛИЧНОГО каталога Wildberries и строит сравнительную таблицу по брендам
со всеми характеристиками и фотографиями.

Результат:
  • competitor_analysis.html — наглядная таблица с фото и всеми характеристиками
  • competitor_analysis.csv  — та же таблица для Excel/Google Sheets (+ ссылки на фото)

ВАЖНО про сеть:
  Публичные хосты WB (card.wb.ru, search.wb.ru, basket-*.wbbasket.ru) пускают
  только российские IP и режут ботов. Поэтому запускать скрипт нужно с машины/
  сервера с российским IP (например там же, где работает основной проект).
  С зарубежного адреса WB вернёт 403.

Использование:
  python competitor_analysis.py 949618026
  python competitor_analysis.py 949618026 --limit 20 --out my_report
  python competitor_analysis.py 949618026 --query "куртка оверсайз" --limit 15

Зависимости: httpx (есть в requirements.txt). Если httpx нет — используется
стандартная библиотека urllib, ставить ничего не нужно.
"""

import argparse
import csv
import html
import json
import sys
import time

# --- HTTP-слой: httpx, если доступен, иначе urllib из стандартной библиотеки ---
try:
    import httpx  # type: ignore

    def _http_get(url, timeout=30):
        r = httpx.get(url, headers=_HEADERS, timeout=timeout)
        return r.status_code, (r.text or "")
except Exception:  # httpx не установлен — работаем на голой стандартной библиотеке
    import urllib.request
    import urllib.error

    def _http_get(url, timeout=30):
        req = urllib.request.Request(url, headers=_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, ""
        except Exception:
            return 0, ""


# Браузерные заголовки — WB иначе быстрее отдаёт 403.
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Origin": "https://www.wildberries.ru",
    "Referer": "https://www.wildberries.ru/",
}

# dest=-1257786 — Москва. Регион влияет только на цену/наличие, не на состав данных.
DEST = "-1257786"


def get_json(url, retries=3):
    """GET с повторами; возвращает разобранный JSON либо None."""
    for attempt in range(retries):
        status, body = _http_get(url)
        if status == 200 and body:
            try:
                return json.loads(body)
            except Exception:
                return None
        if status == 403:
            print(f"  ! 403 Forbidden — WB блокирует запрос. Нужен российский IP. URL: {url[:80]}",
                  file=sys.stderr)
            return None
        time.sleep(2 * (attempt + 1))
    return None


def get_card_detail(nm):
    """Базовая карточка: бренд, название, предмет, цена, рейтинг, отзывы."""
    url = (f"https://card.wb.ru/cards/v2/detail?appType=1&curr=rub"
           f"&dest={DEST}&spp=30&nm={nm}")
    data = get_json(url)
    if not data:
        return None
    products = (data.get("data") or {}).get("products") or []
    return products[0] if products else None


def price_of(product):
    """Достаёт итоговую цену (в рублях) из разных форматов ответа WB."""
    sizes = product.get("sizes") or []
    for s in sizes:
        pr = s.get("price") or {}
        val = pr.get("product") or pr.get("total") or pr.get("basic")
        if val:
            return round(val / 100)
    for key in ("salePriceU", "priceU"):
        if product.get(key):
            return round(product[key] / 100)
    return None


def basket_paths(nm):
    """Числа vol/part по артикулу для адресов CDN-корзины."""
    return nm // 100000, nm // 1000


def find_basket_host(nm):
    """Перебирает basket-XX, пока card.json не ответит 200. Возвращает (host, vol, part)."""
    vol, part = basket_paths(nm)
    for n in range(1, 41):
        host = f"https://basket-{n:02d}.wbbasket.ru"
        url = f"{host}/vol{vol}/part{part}/{nm}/info/ru/card.json"
        status, _ = _http_get(url, timeout=15)
        if status == 200:
            return host, vol, part
        if status == 403:
            # Геоблок — дальше перебирать бессмысленно.
            return None, vol, part
    return None, vol, part


def get_characteristics(nm):
    """Полные характеристики товара из card.json + первая фотография."""
    host, vol, part = find_basket_host(nm)
    if not host:
        return {}, None
    cj = get_json(f"{host}/vol{vol}/part{part}/{nm}/info/ru/card.json")
    if not cj:
        return {}, None

    chars = {}
    # Новый формат: grouped_options -> options[{name,value}]
    for group in cj.get("grouped_options") or []:
        for opt in group.get("options") or []:
            name, value = opt.get("name"), opt.get("value")
            if name and value:
                chars[name.strip()] = str(value).strip()
    # Старый/плоский формат: options[{name,value}]
    for opt in cj.get("options") or []:
        name, value = opt.get("name"), opt.get("value")
        if name and value:
            chars.setdefault(name.strip(), str(value).strip())
    # Состав (compositions)
    comps = [c.get("name") for c in (cj.get("compositions") or []) if c.get("name")]
    if comps:
        chars.setdefault("Состав", ", ".join(comps))
    # Описание
    if cj.get("description"):
        chars.setdefault("Описание", cj["description"].strip())

    photo = f"{host}/vol{vol}/part{part}/{nm}/images/big/1.webp"
    return chars, photo


def search_competitors(query, exclude_nm, subject_id=None, limit=15):
    """Поиск конкурентов по ключевому запросу; при возможности фильтр по предмету."""
    url = (f"https://search.wb.ru/exactmatch/ru/common/v5/search?appType=1"
           f"&curr=rub&dest={DEST}&query={query}&resultset=catalog"
           f"&sort=popular&spp=30&suppressSpellcheck=false")
    data = get_json(url.replace(" ", "%20"))
    if not data:
        return []
    products = (data.get("data") or {}).get("products") or []
    result = []
    for p in products:
        nm = p.get("id")
        if not nm or nm == exclude_nm:
            continue
        if subject_id and p.get("subjectId") and p["subjectId"] != subject_id:
            continue
        result.append(nm)
        if len(result) >= limit:
            break
    return result


def collect(nm, query=None, limit=15):
    """Собирает целевой товар + конкурентов в единый список записей."""
    print(f"[1/3] Загружаю карточку артикула {nm}…", file=sys.stderr)
    base = get_card_detail(nm)
    if not base:
        print("Не удалось получить карточку. Скорее всего WB вернул 403 "
              "(нужен российский IP) либо артикул не существует.", file=sys.stderr)
        sys.exit(1)

    subject_id = base.get("subjectId")
    q = query or base.get("name") or base.get("entity") or ""
    print(f"      Бренд: {base.get('brand')} | Предмет: {base.get('entity') or subject_id}",
          file=sys.stderr)

    print(f"[2/3] Ищу конкурентов по запросу: «{q}»…", file=sys.stderr)
    competitor_nms = search_competitors(q, exclude_nm=nm, subject_id=subject_id, limit=limit)
    print(f"      Найдено конкурентов: {len(competitor_nms)}", file=sys.stderr)

    all_nms = [nm] + competitor_nms
    records = []
    print(f"[3/3] Собираю характеристики ({len(all_nms)} товаров)…", file=sys.stderr)
    for i, cur in enumerate(all_nms):
        product = base if cur == nm else (get_card_detail(cur) or {})
        chars, photo = get_characteristics(cur)
        records.append({
            "nm": cur,
            "is_target": cur == nm,
            "brand": product.get("brand") or "—",
            "name": product.get("name") or "—",
            "price": price_of(product),
            "rating": product.get("reviewRating") or product.get("rating"),
            "feedbacks": product.get("feedbacks") or product.get("nmFeedbacks"),
            "supplier": product.get("supplier") or "—",
            "url": f"https://www.wildberries.ru/catalog/{cur}/detail.aspx",
            "photo": photo,
            "chars": chars,
        })
        print(f"      [{i + 1}/{len(all_nms)}] {records[-1]['brand']} — {cur}", file=sys.stderr)
        time.sleep(0.3)  # вежливая пауза, чтобы не словить бан
    return records


# ----------------------------- Формирование таблицы -----------------------------

META_ROWS = [
    ("Цена, ₽", lambda r: ("" if r["price"] is None else str(r["price"]))),
    ("Рейтинг", lambda r: ("" if r["rating"] is None else str(r["rating"]))),
    ("Отзывов", lambda r: ("" if r["feedbacks"] is None else str(r["feedbacks"]))),
    ("Продавец", lambda r: r["supplier"]),
    ("Артикул", lambda r: str(r["nm"])),
]


def char_names_union(records):
    """Упорядоченное объединение всех названий характеристик по всем товарам."""
    seen = []
    for r in records:
        for name in r["chars"].keys():
            if name not in seen:
                seen.append(name)
    return seen


def write_csv(records, path):
    names = char_names_union(records)
    headers = ["Характеристика"] + [f"{r['brand']} ({r['nm']})" for r in records]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(headers)
        w.writerow(["Бренд"] + [r["brand"] for r in records])
        w.writerow(["Название"] + [r["name"] for r in records])
        for label, fn in META_ROWS:
            w.writerow([label] + [fn(r) for r in records])
        w.writerow(["Фото (URL)"] + [r["photo"] or "" for r in records])
        for name in names:
            w.writerow([name] + [r["chars"].get(name, "") for r in records])
    print(f"  → CSV:  {path}", file=sys.stderr)


def write_html(records, path, target_nm):
    names = char_names_union(records)
    esc = html.escape

    def col_header(r):
        star = " ⭐" if r["is_target"] else ""
        img = (f'<img src="{esc(r["photo"])}" alt="" loading="lazy">'
               if r["photo"] else '<div class="noimg">нет фото</div>')
        return (f'<th class="{ "target" if r["is_target"] else "" }">'
                f'{img}'
                f'<div class="brand">{esc(r["brand"])}{star}</div>'
                f'<div class="name" title="{esc(r["name"])}">{esc(r["name"])}</div>'
                f'<a class="art" href="{esc(r["url"])}" target="_blank">{r["nm"]}</a>'
                f'</th>')

    def cell(val, is_target):
        cls = "target" if is_target else ""
        return f'<td class="{cls}">{esc(val) if val else "—"}</td>'

    rows_html = []
    # Мета-строки
    for label, fn in META_ROWS:
        cells = "".join(cell(fn(r), r["is_target"]) for r in records)
        rows_html.append(f'<tr class="meta"><th class="rowname">{esc(label)}</th>{cells}</tr>')
    # Характеристики
    for name in names:
        cells = "".join(cell(r["chars"].get(name, ""), r["is_target"]) for r in records)
        rows_html.append(f'<tr><th class="rowname">{esc(name)}</th>{cells}</tr>')

    headers = "".join(col_header(r) for r in records)
    doc = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>Анализ конкурентов WB — {target_nm}</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 24px; color:#1c1c28; }}
  h1 {{ font-size: 20px; }}
  .sub {{ color:#6b6b7b; margin-bottom:16px; font-size:13px; }}
  table {{ border-collapse: collapse; font-size: 13px; }}
  th, td {{ border: 1px solid #e3e3ec; padding: 8px 10px; vertical-align: top; text-align: left; }}
  thead th {{ position: sticky; top: 0; background:#fafafd; width:180px; }}
  th.rowname {{ position: sticky; left: 0; background:#fafafd; font-weight:600;
               white-space: nowrap; z-index:1; }}
  thead th img {{ width: 110px; height: 140px; object-fit: cover; border-radius:6px; display:block; }}
  .noimg {{ width:110px; height:140px; background:#f1f1f6; border-radius:6px;
            display:flex; align-items:center; justify-content:center; color:#9a9aad; font-size:11px; }}
  .brand {{ font-weight:700; margin-top:6px; }}
  .name {{ color:#4a4a5a; max-width:170px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  .art {{ font-size:11px; color:#7a5cff; text-decoration:none; }}
  tr:nth-child(even) td {{ background:#fcfcfe; }}
  .meta td, .meta th.rowname {{ background:#fff7ed !important; font-weight:600; }}
  .target {{ background:#eef0ff !important; }}
  thead th.target {{ background:#eef0ff; }}
</style></head>
<body>
<h1>Анализ конкурентов Wildberries</h1>
<div class="sub">Целевой товар: артикул {target_nm} (отмечен ⭐). Товаров в сравнении: {len(records)}.
Колонки — товары по брендам, строки — характеристики.</div>
<table>
  <thead><tr><th class="rowname">Бренд / товар</th>{headers}</tr></thead>
  <tbody>{''.join(rows_html)}</tbody>
</table>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"  → HTML: {path}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="Анализ конкурентов WB по артикулу.")
    ap.add_argument("article", type=int, help="Артикул WB (nmID), напр. 949618026")
    ap.add_argument("--limit", type=int, default=15, help="Сколько конкурентов брать (по умолч. 15)")
    ap.add_argument("--query", default=None, help="Свой поисковый запрос вместо названия товара")
    ap.add_argument("--out", default="competitor_analysis", help="Префикс имён выходных файлов")
    args = ap.parse_args()

    records = collect(args.article, query=args.query, limit=args.limit)
    if len(records) <= 1:
        print("Конкуренты не найдены — попробуйте --query со своим ключевым словом.", file=sys.stderr)

    write_html(records, f"{args.out}.html", args.article)
    write_csv(records, f"{args.out}.csv")
    print("Готово.", file=sys.stderr)


if __name__ == "__main__":
    main()
