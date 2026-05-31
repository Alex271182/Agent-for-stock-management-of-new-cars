"""
JETOUR RF Stock Extractor  (v4)
================================
Запуск:  python jetour_stock_extractor.py

Что делает:
1. Собирает ВСЕ автомобили Jetour по всей РФ через API TradeDealer.
2. Сохраняет CSV с расширенным набором полей (для подстраховки/Excel).
3. Если рядом со скриптом есть .env с SUPABASE_URL и SUPABASE_SERVICE_ROLE_KEY,
   дополнительно заливает срез в stock_staging и вызывает серверную функцию
   apply_stock_snapshot (мёрж в stock_cars: приход/обновление/выбытие) и логирует
   запуск в parsing_runs. Если .env нет — работает как раньше (CSV-only).

Изменения v4:
- Добавлена опциональная загрузка в Supabase (без слома существующего CSV).
- Журнал запусков в parsing_runs (для контроля свежести данных 2-м агентом).
- UPSERT-стратегия: один и тот же запуск дважды за день не задвоит записи.

Изменения v3:
- Retry-логика на сетевые сбои (RemoteDisconnected, 5xx, 429).
- Увеличена пауза между запросами с 0.3 до 0.5 сек.

Изменения v2:
- Токены _token и _tokenProduct захардкожены в CONFIG.
- Fallback: попытка выудить _tokenProduct из HTML (на случай смены).

Зависимости:  requests, python-dotenv
Установка:    pip install requests python-dotenv
"""

import csv
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

# python-dotenv опционален: без него CSV работает, Supabase — нет
try:
    from dotenv import load_dotenv
    load_dotenv()  # подгружает .env из текущей папки
except ImportError:
    pass


# ─── CONFIG ──────────────────────────────────────────────────────────────────
CONFIG = {
    # Базовые URL
    "site_url":   "https://jetour-ru.com/cars",
    "api_url":    "https://locator-backend.tradedealer.ru/filter",

    # ТОКЕНЫ платформы TradeDealer (постоянные для проекта Jetour).
    # Получены из реального запроса браузера 16.05.2026.
    # Если перестанут работать — обнови, открыв сайт jetour-ru.com/cars, DevTools→
    # Network→filter?brands[]=jetour и скопировав значения _token / _tokenProduct
    # из URL.
    "token":          "zkxRRQVkoqalQeOL",
    "token_product":  "F1kJOEWOdv0Efwpr",

    # Параметры запроса к API
    "brand":      "jetour",
    "car_type":   "new",
    "page_limit": 24,
    "order":      "photo",
    "gens":       1,

    # Сеть
    "delay_sec":     0.5,    # Базовая пауза между успешными запросами
    "timeout":       30,
    "max_pages":     500,    # 3427/24 ≈ 143 страницы, 500 — с большим запасом
    "max_retries":   5,      # Сколько раз перезапросить страницу при сетевой ошибке
    "retry_backoff": 2.0,    # Множитель экспоненциальной паузы (2 → 4 → 8 → 16 → 32 сек)

    # Куда сохранять
    "output_dir": ".",
}

# Заголовки запросов (под обычный Chrome, с Referer/Origin как у реального браузера)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Referer": "https://jetour-ru.com/",
    "Origin":  "https://jetour-ru.com",
}


# ─── ПОДГОТОВКА СЕССИИ (опционально — посетить /cars для куки) ───────────────
def warmup_session(session):
    """Получает HTML страницы /cars, чтобы:
    1. Заполнить session.cookies (некоторые WAF/CDN могут проверять).
    2. Попытаться выудить актуальный _tokenProduct (на случай его смены в будущем).

    Возвращает обновлённый словарь токенов.
    """
    tokens = {
        "_token":        CONFIG["token"],
        "_tokenProduct": CONFIG["token_product"],
    }

    print("[1/3] Прогрев сессии: открываю {} ...".format(CONFIG["site_url"]))
    try:
        r = session.get(CONFIG["site_url"], headers=HEADERS, timeout=CONFIG["timeout"])
        r.raise_for_status()
        html = r.text
    except Exception as e:
        print("   ⚠ Не удалось получить страницу /cars: {}: {}".format(type(e).__name__, e))
        print("   ⚠ Продолжаю с захардкоженными токенами из CONFIG.")
        return tokens

    # Пытаемся обновить _tokenProduct (он стабильно лежит в HTML, см. анализ).
    m = re.search(r"tokenProduct\s*=\s*['\"]([A-Za-z0-9_-]+)['\"]", html)
    if m:
        token_product_fresh = m.group(1)
        if token_product_fresh != tokens["_tokenProduct"]:
            print("   ⚠ _tokenProduct в HTML отличается от CONFIG. Использую новый: {}"
                  .format(token_product_fresh))
            tokens["_tokenProduct"] = token_product_fresh
        else:
            print("   ✓ _tokenProduct подтверждён из HTML: {}".format(token_product_fresh))
    else:
        print("   ⚠ _tokenProduct в HTML не найден; использую CONFIG: {}"
              .format(tokens["_tokenProduct"]))

    print("   ✓ _token из CONFIG: {}".format(tokens["_token"]))
    print("   ✓ Cookie получены, сессия готова.")
    return tokens


# ─── ОСНОВНОЙ ЗАПРОС К API ───────────────────────────────────────────────────
# Какие ошибки считаем "транзиентными" — стоит ли их повторять.
# RemoteDisconnected, ConnectionError, Timeout — типичные сбои rate-limit/сети.
# HTTP 429/500/502/503/504 — на стороне сервера, тоже имеет смысл повторить.
TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}


def _fetch_page_once(session, page, tokens):
    """Один запрос страницы, без retry."""
    params = {
        "brands[]":      CONFIG["brand"],
        "order":         CONFIG["order"],
        "reversed":      "false",
        "page":          page,
        "carType":       CONFIG["car_type"],
        "gens":          CONFIG["gens"],
        "limit":         CONFIG["page_limit"],
        "_token":        tokens["_token"],
        "_tokenProduct": tokens["_tokenProduct"],
        "_version":      "desktop",
    }
    r = session.get(CONFIG["api_url"], headers=HEADERS, params=params,
                    timeout=CONFIG["timeout"])
    r.raise_for_status()
    return r.json()


def fetch_page(session, page, tokens):
    """Запрашивает страницу с автоматическими повторами при транзиентных ошибках.

    Возвращает (data, None) при успехе и (None, last_exception) при провале
    всех попыток. Не транзиентные ошибки (например, 401) пробрасываются сразу.
    """
    last_exc = None
    for attempt in range(1, CONFIG["max_retries"] + 1):
        try:
            return _fetch_page_once(session, page, tokens), None

        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status not in TRANSIENT_HTTP_CODES:
                # 401/403/404 — повторять бесполезно, отдаём вверх
                raise
            last_exc = e
            print("      попытка {}/{} → HTTP {} → жду и повторяю".format(
                attempt, CONFIG["max_retries"], status))

        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            print("      попытка {}/{} → {} → жду и повторяю".format(
                attempt, CONFIG["max_retries"], type(e).__name__))

        # Экспоненциальная пауза: 2, 4, 8, 16, 32 сек
        if attempt < CONFIG["max_retries"]:
            backoff = CONFIG["retry_backoff"] ** attempt
            time.sleep(backoff)

    return None, last_exc


def fetch_all_cars(session, tokens):
    """Постранично собирает все автомобили."""
    all_cars = []
    page = 1
    debug_saved = False

    print("[2/3] Загружаю каталог постранично (limit={}/стр) ...".format(CONFIG["page_limit"]))

    while page <= CONFIG["max_pages"]:
        try:
            data, retry_exc = fetch_page(session, page, tokens)
        except requests.HTTPError as e:
            # Не транзиентная ошибка — записываем дебаг и прерываемся
            print("   ✗ Ошибка HTTP на странице {}: {}".format(page, e))
            if not debug_saved and e.response is not None:
                try:
                    dbg = Path(CONFIG["output_dir"]) / "jetour_debug_response.txt"
                    dbg.write_text(
                        "URL: {}\nStatus: {}\n\n{}".format(
                            e.response.url, e.response.status_code, e.response.text),
                        encoding="utf-8")
                    print("   ⚠ Полный ответ сохранён в {}".format(dbg))
                    debug_saved = True
                except Exception:
                    pass
            break
        except Exception as e:
            print("   ✗ Ошибка на странице {}: {}: {}".format(
                page, type(e).__name__, e))
            break

        if data is None:
            # Все retry исчерпаны на транзиентной ошибке
            print("   ✗ Страница {} не получена после {} попыток: {}".format(
                page, CONFIG["max_retries"], retry_exc))
            print("   ⚠ Сохраняю то, что успели скачать.")
            break

        items = data.get("list", [])
        total = data.get("total", 0)
        can_more = data.get("canShowMore", False)

        if not items:
            if page == 1:
                print("   ✗ API вернул пустой список. Ответ: {}".format(
                    json.dumps(data, ensure_ascii=False)[:500]))
            break

        all_cars.extend(items)
        print("   стр {:3d}: получено {:3d} а/м, всего {:4d}/{}".format(
            page, len(items), len(all_cars), total))

        if not can_more:
            break

        page += 1
        time.sleep(CONFIG["delay_sec"])

    return all_cars


# ─── ФОРМИРОВАНИЕ CSV ────────────────────────────────────────────────────────
CSV_HEADER = [
    # Идентификация
    "ID", "VIN", "VIN полный", "Год",
    "Дата производства", "Дата публикации", "Дата обновления", "Статус",

    # Модель / комплектация
    "Модель", "Модель alias", "Поколение", "Комплектация", "Mcode комплектации",
    "Объём л", "Мощность лс", "КПП тип", "КПП название", "Привод", "Кузов",

    # Цвет
    "Цвет", "Базовый цвет код", "Доплата за цвет",

    # Цены / скидки
    "Цена базовая", "Цена спец", "Цена с ТИ",
    "Скидка ТИ", "Скидка кредит", "Скидка комплектация", "Скидка цвет",
    "Скидка доп. оборудование", "Скидка страхование", "Скидка рассрочка",
    "Скидка лизинг", "Максимальная скидка",

    # Дилер
    "Дилер ID", "Дилер название", "Дилер alias", "Дилер адрес",
    "Дилер город", "Дилер город alias", "Дилер lon", "Дилер lat",
    "Дилер OEM ID", "Дилер телефон",

    # Прочее
    "Параллельный импорт", "Пробег", "Б/У",
    "Фото реальных", "Фото каталог", "Доп. оборудование шт",
]


def get_nested(obj, *keys, default=""):
    """Безопасное извлечение вложенного поля."""
    cur = obj
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur if cur is not None else default


def car_to_row(car):
    """Преобразует один объект машины из JSON в строку CSV."""
    company = car.get("company") or {}
    city = company.get("city") or {}
    location = company.get("location") or {}
    phone_list = company.get("phoneList") or {}

    main_phone = ""
    if isinstance(phone_list, dict):
        main = phone_list.get("main") or phone_list.get("newCars") or []
        if isinstance(main, list) and main:
            main_phone = main[0]

    color = car.get("color") or {}
    base_color = car.get("baseColor") or {}
    complect = car.get("complectation") or {}
    modif = car.get("modification") or {}
    transmission = modif.get("transmission") or {}
    model = car.get("model") or {}
    generation = car.get("generation") or {}

    return [
        # Идентификация
        car.get("id", ""), car.get("vin", ""), car.get("vin_full", ""),
        car.get("year", ""), car.get("prodDate", ""), car.get("publishedAt", ""),
        car.get("updatedAt", ""), car.get("status", ""),

        # Модель
        model.get("titleRus", ""), model.get("alias", ""),
        generation.get("titleRus", ""),
        complect.get("titleRus", ""), complect.get("mcode", ""),
        modif.get("volume", "") or modif.get("displacement", ""),
        modif.get("power", "") or modif.get("hp", ""),
        transmission.get("type", ""), transmission.get("title", ""),
        modif.get("wheel", "") or modif.get("drive", ""),
        modif.get("body", "") or get_nested(car, "body", "title"),

        # Цвет
        color.get("titleRus", "") or color.get("title", ""),
        base_color.get("code", ""),
        car.get("colorPrice", 0),

        # Цены / скидки
        car.get("price", ""), car.get("specialPrice", ""),
        get_nested(car, "specials", "tradein", default=""),
        car.get("tradeinDiscount", 0), car.get("creditDiscount", 0),
        car.get("equipmentDiscount", 0), car.get("colorPrice", 0),
        car.get("addingDiscount", 0), car.get("insuranceDiscount", 0),
        car.get("installmentDiscount", 0), car.get("leasingDiscount", 0),
        car.get("maxDiscounts", 0),

        # Дилер
        company.get("id", ""), company.get("commerceTitle", ""),
        company.get("alias", ""), company.get("address", ""),
        city.get("titleWithoutSpaces", "") or city.get("titleRus", ""),
        city.get("alias", ""),
        location.get("lon", ""), location.get("lat", ""),
        company.get("oemDealerId", ""), main_phone,

        # Прочее
        car.get("parallelImport", False), car.get("run", 0), car.get("used", False),
        car.get("realPhotosCount", 0), car.get("catalogPhotosCount", 0),
        car.get("addEquipmentCount", 0),
    ]


def save_csv(cars):
    """Сохраняет CSV в формате для русского Excel (UTF-8 BOM, разделитель ';')."""
    today = datetime.now().strftime("%Y-%m-%d")
    out_path = Path(CONFIG["output_dir"]) / "jetour_rf_stock_{}.csv".format(today)

    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        w.writerow(CSV_HEADER)
        for car in cars:
            w.writerow(car_to_row(car))

    return out_path


# ─── СТАТИСТИКА ──────────────────────────────────────────────────────────────
def print_stats(cars):
    print("[3/3] Сводка по выгрузке:")
    print("   Всего а/м: {}".format(len(cars)))

    by_model, by_city, by_status = {}, {}, {}
    for c in cars:
        m = get_nested(c, "model", "titleRus") or "?"
        city = get_nested(c, "company", "city", "titleWithoutSpaces") or "?"
        status = c.get("status", "?")
        by_model[m] = by_model.get(m, 0) + 1
        by_city[city] = by_city.get(city, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1

    print("\n   По моделям:")
    for m, n in sorted(by_model.items(), key=lambda x: -x[1]):
        print("      {:20s} {:5d}".format(m, n))

    print("\n   По статусам:")
    for s, n in sorted(by_status.items(), key=lambda x: -x[1]):
        print("      {:20s} {:5d}".format(s, n))

    print("\n   Топ-10 городов:")
    for city, n in sorted(by_city.items(), key=lambda x: -x[1])[:10]:
        print("      {:25s} {:5d}".format(city, n))


# ─── ЗАГРУЗКА В SUPABASE (опционально) ──────────────────────────────────────
# Брендовое имя для записи в stock_cars.brand / stock_staging.brand
BRAND_KEY = "jetour"


def car_to_supabase_row(car):
    """Преобразует JSON-объект машины в строку для stock_staging (без даты и raw_data).
    Дата среза проставляется при заливке в staging.
    """
    company = car.get("company") or {}
    city = company.get("city") or {}
    location = company.get("location") or {}
    phone_list = company.get("phoneList") or {}

    main_phone = ""
    if isinstance(phone_list, dict):
        main = phone_list.get("main") or phone_list.get("newCars") or []
        if isinstance(main, list) and main:
            main_phone = main[0]

    color = car.get("color") or {}
    complect = car.get("complectation") or {}
    modif = car.get("modification") or {}
    transmission = modif.get("transmission") or {}
    model = car.get("model") or {}

    # Считаем days_on_stock сами (сегодня - published_at)
    published_at_str = car.get("publishedAt") or ""
    published_date = ""
    days_on_stock = None
    if published_at_str:
        try:
            published_date = published_at_str[:10]  # ISO → YYYY-MM-DD
            d_pub = datetime.strptime(published_date, "%Y-%m-%d").date()
            d_snap = datetime.now().date()
            days_on_stock = (d_snap - d_pub).days
        except (ValueError, TypeError):
            published_date = ""
            days_on_stock = None

    prod_date_str = car.get("prodDate") or ""
    prod_date = prod_date_str[:10] if prod_date_str else None

    def _int_or_none(v):
        if v is None or v == "":
            return None
        try:
            return int(v)
        except (ValueError, TypeError):
            return None

    def _num_or_none(v):
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    return {
        "brand":              BRAND_KEY,
        "car_id":             str(car.get("id", "")),

        # VIN/базовые
        "vin":                car.get("vin") or None,
        "vin_full":           car.get("vin_full") or None,
        "year":               _int_or_none(car.get("year")),
        "prod_date":          prod_date,
        "published_at":       published_date or None,
        "days_on_stock":      days_on_stock,
        "status":             car.get("status") or None,

        # Модель / комплектация
        "model":              model.get("titleRus") or None,
        "model_alias":        model.get("alias") or None,
        "complectation":      complect.get("titleRus") or None,
        "complectation_code": complect.get("mcode") or None,
        "engine_volume":      _num_or_none(modif.get("volume") or modif.get("displacement")),
        "engine_power":       _int_or_none(modif.get("power") or modif.get("hp")),
        "transmission_type":  transmission.get("type") or None,
        "drive_type":         modif.get("wheel") or modif.get("drive") or None,
        "body_type":          modif.get("body") or get_nested(car, "body", "title") or None,
        "color":              color.get("titleRus") or color.get("title") or None,

        # Цены / скидки
        "price_base":         _int_or_none(car.get("price")),
        "price_special":      _int_or_none(car.get("specialPrice")),
        "price_tradein":      _int_or_none(get_nested(car, "specials", "tradein", default=None)),
        "discount_tradein":   _int_or_none(car.get("tradeinDiscount")),
        "discount_credit":    _int_or_none(car.get("creditDiscount")),
        "discount_equipment": _int_or_none(car.get("equipmentDiscount")),
        "discount_color":     _int_or_none(car.get("colorPrice")),
        "discount_insurance": _int_or_none(car.get("insuranceDiscount")),
        "discount_max":       _int_or_none(car.get("maxDiscounts")),

        # Дилер
        "dealer_id":          str(company.get("id", "")) or None,
        "dealer_name":        company.get("commerceTitle") or None,
        "dealer_address":     company.get("address") or None,
        "dealer_city":        city.get("titleWithoutSpaces") or city.get("titleRus") or None,
        "dealer_city_alias":  city.get("alias") or None,
        "dealer_lat":         _num_or_none(location.get("lat")),
        "dealer_lon":         _num_or_none(location.get("lon")),
        "dealer_phone":       main_phone or None,

        # Прочее
        "parallel_import":    bool(car.get("parallelImport", False)),
        "mileage_km":         _int_or_none(car.get("run")),
        "is_used":            bool(car.get("used", False)),
    }


def supabase_request(method, url, key, **kwargs):
    """Обёртка для запросов к Supabase REST API с авторизацией."""
    headers = kwargs.pop("headers", {})
    headers.update({
        "apikey":        key,
        "Authorization": "Bearer " + key,
        "Content-Type":  "application/json",
    })
    return requests.request(method, url, headers=headers, timeout=60, **kwargs)


def supabase_log_run_start(supabase_url, key):
    """Создаёт запись в parsing_runs со status='running'.
    Возвращает run_id или None при ошибке.
    """
    run_id = str(uuid.uuid4())
    url = supabase_url.rstrip("/") + "/rest/v1/parsing_runs"
    payload = {
        "run_id":     run_id,
        "brand":      BRAND_KEY,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status":     "running",
    }
    try:
        r = supabase_request("POST", url, key, json=payload,
                             headers={"Prefer": "return=minimal"})
        if r.status_code in (200, 201, 204):
            return run_id
        print("   ⚠ Не удалось создать запись parsing_runs: HTTP {}: {}".format(
            r.status_code, r.text[:200]))
        return None
    except Exception as e:
        print("   ⚠ Ошибка создания parsing_runs: {}: {}".format(type(e).__name__, e))
        return None


def supabase_log_run_finish(supabase_url, key, run_id, status,
                            rows_inserted, rows_total, duration_sec,
                            error_message=None):
    """Обновляет запись в parsing_runs итоговым статусом."""
    if not run_id:
        return
    url = (supabase_url.rstrip("/") + "/rest/v1/parsing_runs"
           "?run_id=eq." + run_id)
    payload = {
        "finished_at":   datetime.now(timezone.utc).isoformat(),
        "status":        status,
        "rows_inserted": rows_inserted,
        "rows_total":    rows_total,
        "duration_sec":  duration_sec,
    }
    if error_message:
        payload["error_message"] = error_message[:2000]
    try:
        r = supabase_request("PATCH", url, key, json=payload,
                             headers={"Prefer": "return=minimal"})
        if r.status_code not in (200, 204):
            print("   ⚠ Не удалось обновить parsing_runs: HTTP {}: {}".format(
                r.status_code, r.text[:200]))
    except Exception as e:
        print("   ⚠ Ошибка обновления parsing_runs: {}: {}".format(
            type(e).__name__, e))


def upload_to_supabase(cars, supabase_url, key, batch_size=200):
    """Загружает срез в stock_staging батчами, затем вызывает серверную функцию
    apply_stock_snapshot(brand, date), которая мёржит staging в stock_cars
    (приход/обновление/выбытие с защитами) и чистит staging.

    Возвращает (result_dict_or_None, error_message_or_None).
    result_dict — JSON-ответ функции (arrived/updated/removed/removal_done/note).

    batch_size=200: чтобы избежать statement_timeout Supabase на больших вставках.
    """
    snapshot_date = datetime.now().strftime("%Y-%m-%d")
    staging_url = supabase_url.rstrip("/") + "/rest/v1/stock_staging"

    rows = [car_to_supabase_row(c) for c in cars]
    for row in rows:
        row["snapshot_date"] = snapshot_date

    # Дедуп по (brand, car_id): API иногда отдаёт машину дважды на стыке страниц.
    seen = {}
    for row in rows:
        seen[(row["brand"], row["car_id"])] = row
    deduped = len(rows) - len(seen)
    if deduped:
        print("   ⚠ Дублей по car_id: {} (удалено)".format(deduped))
    rows = list(seen.values())

    total = len(rows)
    print("   [a] Заливаю {} строк в stock_staging батчами по {} ...".format(total, batch_size))

    # На всякий случай чистим staging этого бренда перед заливкой (если прошлый прогон упал)
    try:
        supabase_request(
            "DELETE", staging_url + "?brand=eq." + BRAND_KEY, key,
            headers={"Prefer": "return=minimal"})
    except Exception as e:
        print("   ⚠ Не удалось очистить staging заранее: {}: {}".format(type(e).__name__, e))

    staged = 0
    for i in range(0, total, batch_size):
        batch = rows[i:i + batch_size]
        try:
            r = supabase_request(
                "POST", staging_url, key, json=batch,
                headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
            if r.status_code in (200, 201, 204):
                staged += len(batch)
                print("      батч {:3d}-{:3d}: OK".format(i + 1, i + len(batch)))
            else:
                err = "staging HTTP {}: {}".format(r.status_code, r.text[:300])
                print("      батч {:3d}-{:3d}: FAIL {}".format(i + 1, i + len(batch), err))
                return None, err
        except Exception as e:
            err = "{}: {}".format(type(e).__name__, e)
            print("      батч {:3d}-{:3d}: ERROR {}".format(i + 1, i + len(batch), err))
            return None, err

    # [b] Вызываем серверную функцию мёржа staging -> stock_cars
    print("   [b] Вызываю apply_stock_snapshot('{}', '{}') ...".format(BRAND_KEY, snapshot_date))
    rpc_url = supabase_url.rstrip("/") + "/rest/v1/rpc/apply_stock_snapshot"
    try:
        r = supabase_request("POST", rpc_url, key, json={
            "p_brand":         BRAND_KEY,
            "p_snapshot_date": snapshot_date,
        })
        if r.status_code not in (200, 201, 204):
            return None, "rpc HTTP {}: {}".format(r.status_code, r.text[:300])
        result = r.json() if r.text else {}
        return result, None
    except Exception as e:
        return None, "rpc {}: {}".format(type(e).__name__, e)


# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    session = requests.Session()
    started = time.time()

    tokens = warmup_session(session)

    cars = fetch_all_cars(session, tokens)
    if not cars:
        print("✗ Не получено ни одного автомобиля. См. вывод выше.")
        return 1

    print_stats(cars)

    out_path = save_csv(cars)
    print("\n✓ CSV сохранён: {}".format(out_path.resolve()))
    print("  Для фильтрации по региону используй колонку 'Дилер город alias'")
    print("  Пример: sankt-peterburg, moskva, ekaterinburg")

    # Опциональная загрузка в Supabase
    supabase_url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

    if supabase_url and key:
        print("\n[4/4] Загрузка в Supabase ...")
        run_id = supabase_log_run_start(supabase_url, key)

        try:
            result, err = upload_to_supabase(cars, supabase_url, key)
            duration = int(time.time() - started)

            if err is None:
                arrived = result.get("arrived", 0)
                updated = result.get("updated", 0)
                removed = result.get("removed", 0)
                removal_done = result.get("removal_done", True)
                note = result.get("note", "")
                # rows_inserted в журнал = приход + обновление (сколько строк затронуто)
                supabase_log_run_finish(
                    supabase_url, key, run_id,
                    status="success",
                    rows_inserted=arrived + updated,
                    rows_total=len(cars),
                    duration_sec=duration,
                    error_message=note or None,
                )
                print("\n✓ stock_cars обновлён за {} сек:".format(duration))
                print("    приход (новые):     {}".format(arrived))
                print("    обновлено:          {}".format(updated))
                print("    выбытие:            {}".format(removed))
                if not removal_done:
                    print("    ⚠ ВЫБЫТИЕ ПРОПУЩЕНО: {}".format(note))
            else:
                supabase_log_run_finish(
                    supabase_url, key, run_id,
                    status="failed",
                    rows_inserted=0,
                    rows_total=len(cars),
                    duration_sec=duration,
                    error_message=err,
                )
                print("\n⚠ Загрузка не удалась. Причина: {}".format(err))

        except Exception as e:
            duration = int(time.time() - started)
            supabase_log_run_finish(
                supabase_url, key, run_id,
                status="failed",
                rows_inserted=0,
                rows_total=len(cars),
                duration_sec=duration,
                error_message="{}: {}".format(type(e).__name__, e),
            )
            print("\n✗ Загрузка в Supabase упала: {}: {}".format(
                type(e).__name__, e))
    else:
        print("\nℹ Supabase не настроен (нет SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY в .env).")
        print("  Парсер отработал в режиме CSV-only.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
