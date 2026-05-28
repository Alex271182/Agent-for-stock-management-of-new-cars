"""
CHANGAN + UNI MOTORS RF Stock Extractor  (v1)
==============================================
Запуск:  python changan_uni_stock_extractor.py

Что делает:
1. Получает справочник всех моделей через /api/internal/filter/stock/dicts
2. Постранично собирает ВСЕ автомобили со ВСЕЙ РФ (без фильтра по городу)
3. Сохраняет CSV (для подстраховки и Excel)
4. Если есть .env с SUPABASE_URL и SUPABASE_SERVICE_ROLE_KEY —
   дополнительно загружает данные в таблицу stock_snapshots и логирует
   запуск в parsing_runs.

Поддерживаемые бренды (выбирается через CONFIG):
  - Changan       (https://changanauto.ru)
  - Uni Motors    (https://uni-motors.ru)

Сделано по образцу jetour_stock_extractor.py v4 и существующего JS-парсера
changan_uni_stock_extractor.js, который уже разведал API.

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

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ─── CONFIG ──────────────────────────────────────────────────────────────────
CONFIG = {
    # Список брендов, которые парсить в одном запуске.
    # Парсер пройдёт их по очереди. Если один упадёт — другие отработают.
    # Допустимые значения: "changan", "uni".
    "brands_to_run": ["changan", "uni"],

    # Брендовые настройки
    "brands": {
        "changan": {
            "base_url":  "https://changanauto.ru",
            "brand_key": "changan",
            "ref_page":  "https://changanauto.ru/cars",
        },
        "uni": {
            "base_url":  "https://uni-motors.ru",
            "brand_key": "uni",
            "ref_page":  "https://uni-motors.ru/cars",
        },
    },

    # Сеть
    "delay_sec":     0.3,
    "timeout":       30,
    "max_pages":     500,
    "max_retries":   5,
    "retry_backoff": 2.0,

    # Куда сохранять
    "output_dir": ".",
}


def get_brand_settings(brand):
    if brand not in CONFIG["brands"]:
        raise SystemExit("Неизвестный бренд '{}'. Допустимые: {}".format(
            brand, list(CONFIG["brands"].keys())))
    return CONFIG["brands"][brand]


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


# ─── ОБЩИЕ УТИЛИТЫ ───────────────────────────────────────────────────────────
TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}


def get_nested(obj, *keys, default=""):
    cur = obj
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur if cur is not None else default


def fetch_with_retry(session, url, params=None, max_retries=None):
    """GET с автоматическими повторами при транзиентных ошибках."""
    if max_retries is None:
        max_retries = CONFIG["max_retries"]
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            r = session.get(url, headers=HEADERS, params=params,
                            timeout=CONFIG["timeout"])
            if r.status_code in TRANSIENT_HTTP_CODES:
                last_exc = requests.HTTPError(
                    "{} {}".format(r.status_code, r.reason), response=r)
                print("      попытка {}/{} → HTTP {} → жду".format(
                    attempt, max_retries, r.status_code))
            else:
                r.raise_for_status()
                return r.json(), None
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            print("      попытка {}/{} → {} → жду".format(
                attempt, max_retries, type(e).__name__))
        except requests.HTTPError as e:
            # Не транзиентная — пробрасываем
            raise

        if attempt < max_retries:
            time.sleep(CONFIG["retry_backoff"] ** attempt)

    return None, last_exc


# ─── РАЗВЕДКА: СПРАВОЧНИК МОДЕЛЕЙ ────────────────────────────────────────────
def fetch_dicts(session, brand_settings):
    """Получает справочник моделей и городов с сайта дистрибьютера."""
    print("[1/3] Запрашиваю справочник моделей ...")
    url = brand_settings["base_url"] + "/api/internal/filter/stock/dicts"

    data, exc = fetch_with_retry(session, url)
    if data is None:
        raise RuntimeError("Не удалось получить dicts: {}".format(exc))

    filter_data = data.get("filter_data") or {}
    models = filter_data.get("models") or []
    cities = filter_data.get("cities") or []

    print("   ✓ Моделей в каталоге: {}".format(len(models)))
    print("   ✓ Городов в каталоге: {}".format(len(cities)))

    return models


# ─── ПОСТРАНИЧНЫЙ СБОР МАШИН ─────────────────────────────────────────────────
def _fetch_filtered_page(session, base_url, model_id, page, extra_params):
    """Один запрос dicts с фильтрами. Возвращает (items, has_more)."""
    params = {
        "model_id":  str(model_id),
        "with_cars": "1",
        "only_cars": "1",
        "page":      str(page),
    }
    params.update(extra_params)
    url = base_url + "/api/internal/filter/stock/dicts"
    data, exc = fetch_with_retry(session, url, params=params)
    if data is None:
        return None, False
    cars_data = data.get("cars_data") or {}
    items = cars_data.get("items") or []
    has_more = bool(cars_data.get("load_more_endpoint")) and len(items) > 0
    return items, has_more


def _fetch_all_filtered(session, base_url, model_id, extra_params):
    """Все машины модели под заданным фильтром (с пагинацией). Возвращает list items."""
    result = []
    page = 1
    while page <= CONFIG["max_pages"]:
        items, has_more = _fetch_filtered_page(session, base_url, model_id, page, extra_params)
        if items is None:
            break
        if not items:
            break
        result.extend(items)
        if not has_more:
            break
        page += 1
        time.sleep(CONFIG["delay_sec"])
    return result


def fetch_model_dicts(session, base_url, model_id):
    """Берёт справочники complectations[] и colors[] для конкретной модели."""
    url = base_url + "/api/internal/filter/stock/dicts"
    data, exc = fetch_with_retry(session, url, params={"model_id": str(model_id), "with_seo": "1"})
    if data is None:
        return [], []
    fd = data.get("filter_data") or {}
    return (fd.get("complectations") or []), (fd.get("colors") or [])


def fetch_cars_for_model(session, brand_settings, model):
    """Собирает весь сток модели С КОМПЛЕКТАЦИЕЙ И ЦВЕТОМ.

    Стратегия (без прямого источника compl/color в объекте машины):
      1. Берём справочники комплектаций и цветов модели.
      2. Перебор по комплектациям: union всех комплектаций = весь сток модели
         (проверено на CS35 MAX: каждая машина ровно в одной комплектации).
         Заодно сразу проставляем complectation. Это ЗАМЕНЯЕТ обычный полный обход.
      3. Перебор по цветам: собираем только id->цвет, проставляем color.
    Нагрузка ≈ 2 полных обхода модели (комплектации + цвета), не больше.
    """
    base_url = brand_settings["base_url"]
    model_id = model.get("id")
    model_name = model.get("name") or model.get("title") or "?"

    complectations, colors = fetch_model_dicts(session, base_url, model_id)

    # 1. Перебор по комплектациям — даёт все машины + комплектацию
    cars_by_id = {}        # car_id -> объект машины
    _compl_seen = {}       # car_id -> имя комплектации (для детекта пересечений)
    for c in complectations:
        cid = c.get("id")
        cname = c.get("name")
        items = _fetch_all_filtered(session, base_url, model_id, {"complectations[]": str(cid)})
        for car in items:
            car_id = car.get("id")
            if car_id is None:
                continue
            # Детект пересечения: если машина уже была в другой комплектации — предупреждаем.
            if car_id in _compl_seen and _compl_seen[car_id] != cname:
                print("   ⚠ {}: машина {} в двух комплектациях ('{}' и '{}')".format(
                    model_name, car_id, _compl_seen[car_id], cname))
            _compl_seen[car_id] = cname
            car["complectation"] = cname
            car["_model_name"] = model_name
            car["_model_id"]   = model_id
            cars_by_id[car_id] = car
        time.sleep(CONFIG["delay_sec"])

    # Фоллбэк: если у модели нет справочника комплектаций (вдруг) —
    # делаем обычный полный обход, чтобы не потерять машины.
    if not complectations:
        items = _fetch_all_filtered(session, base_url, model_id, {})
        for car in items:
            car_id = car.get("id")
            if car_id is None:
                continue
            car["_model_name"] = model_name
            car["_model_id"]   = model_id
            cars_by_id[car_id] = car

    # 2. Перебор по цветам — только маппинг id -> цвет
    for col in colors:
        col_id = col.get("id")
        col_name = col.get("name")
        items = _fetch_all_filtered(session, base_url, model_id, {"colors[]": str(col_id)})
        for car in items:
            car_id = car.get("id")
            if car_id in cars_by_id:
                cars_by_id[car_id]["color"] = col_name
        time.sleep(CONFIG["delay_sec"])

    return list(cars_by_id.values())


def fetch_all_cars(session, brand_settings, models):
    """Перебирает все модели и собирает весь сток."""
    print("[2/3] Загружаю каталог по моделям ...")
    all_cars = []

    for model in models:
        # Если в справочнике у модели cars_count = 0 (или его нет) — она пустая,
        # но мы всё равно пробуем (на случай рассинхрона справочника и фактов)
        cars_count_hint = model.get("cars_count", "?")
        model_name = model.get("name") or model.get("title") or "?"

        cars = fetch_cars_for_model(session, brand_settings, model)
        if cars:
            print("   {:30s} {:5d} а/м (справочник обещал {})".format(
                model_name, len(cars), cars_count_hint))
            all_cars.extend(cars)
        else:
            if cars_count_hint and cars_count_hint != 0:
                print("   {:30s} пусто (справочник обещал {})".format(
                    model_name, cars_count_hint))

    return all_cars


# ─── CSV ─────────────────────────────────────────────────────────────────────
CSV_HEADER = [
    # Идентификация
    "ID", "VIN", "Год", "Статус",

    # Модель
    "Модель", "Модель ID",

    # Технические
    "Объём л", "Мощность лс", "КПП", "Привод",

    # Цены
    "Цена", "Цена мин", "Цена мин с ТИ", "Макс. выгода",

    # Дилер
    "Дилер", "Адрес", "Город",
]


def car_to_csv_row(car):
    salon = car.get("salon") or {}
    prices = car.get("prices") or {}
    benefits = car.get("benefits") or {}
    benefit_max = benefits.get("max_price") if isinstance(benefits, dict) else ""

    addr = salon.get("address") or ""
    city_name = extract_city_from_address(addr) or ""

    return [
        car.get("id", ""),
        car.get("vin", ""),
        car.get("year", ""),
        car.get("status", ""),
        car.get("_model_name", ""),
        car.get("_model_id", ""),
        car.get("volume", ""),
        car.get("power", ""),
        car.get("kpp", ""),
        car.get("gear", ""),
        prices.get("current", ""),
        prices.get("min", ""),
        prices.get("min_with_trade_in", ""),
        benefit_max,
        salon.get("name", ""),
        addr,
        city_name,
    ]


def save_csv(cars, brand_key):
    today = datetime.now().strftime("%Y-%m-%d")
    out_path = Path(CONFIG["output_dir"]) / "{}_rf_stock_{}.csv".format(brand_key, today)
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        w.writerow(CSV_HEADER)
        for car in cars:
            w.writerow(car_to_csv_row(car))
    return out_path


# ─── СТАТИСТИКА ──────────────────────────────────────────────────────────────
def print_stats(cars):
    print("\n[3/3] Сводка по выгрузке:")
    print("   Всего а/м: {}".format(len(cars)))

    by_model, by_city, by_status = {}, {}, {}
    for c in cars:
        m = c.get("_model_name") or "?"
        salon = c.get("salon") or {}
        city = extract_city_from_address(salon.get("address")) or "?"
        status = c.get("status", "?")
        by_model[m] = by_model.get(m, 0) + 1
        by_city[city] = by_city.get(city, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1

    print("\n   По моделям:")
    for m, n in sorted(by_model.items(), key=lambda x: -x[1]):
        print("      {:25s} {:5d}".format(m, n))

    print("\n   По статусам:")
    for s, n in sorted(by_status.items(), key=lambda x: -x[1]):
        print("      {:25s} {:5d}".format(s, n))

    print("\n   Топ-10 городов:")
    for city, n in sorted(by_city.items(), key=lambda x: -x[1])[:10]:
        print("      {:25s} {:5d}".format(city, n))


# ─── ЗАГРУЗКА В SUPABASE ─────────────────────────────────────────────────────
def extract_city_from_address(addr):
    """Извлекает населённый пункт из адреса дилера.
    У Changan/Uni нет структурированного поля city — только текстовый address.
    Покрытие на реальных данных 2026-05-20: 100% (3757/3757).
    """
    if not addr:
        return None
    a = re.sub(r'\s+', ' ', addr.strip())

    # Префиксы населённых пунктов. После префикса должна идти ЗАГЛАВНАЯ буква,
    # это отсекает "д. 11Б" (= дом, не деревня).
    pattern = (
        r'(?:^|[,\s])'
        r'(?:[Гг]\.\s?|[Сс]\.\s?|[Дд]\.\s?|пгт\s+|пос\.\s?|посёлок\s+|поселок\s+'
        r'|[Хх]\.\s?|деревня\s+|станица\s+)'
        r'([А-ЯЁ][а-яёА-ЯЁ\-]+(?:\s+[А-ЯЁ][а-яёА-ЯЁ\-]+){0,2})'
    )
    matches = re.findall(pattern, a)
    if matches:
        # Последнее вхождение = самый "узкий" уровень = сам город
        # (например: "Московская обл., г. Балашиха" → "Балашиха")
        return matches[-1].strip()

    # Fallback: первое слово, если это не область/край/респ
    first_part = a.split(",")[0].strip()
    if first_part and first_part[0].isupper():
        lower = first_part.lower()
        skip_words = ["обл", "край", "респ", "ао", "г.о", "округ", "район", "м.р-н"]
        if not any(w in lower for w in skip_words):
            return first_part

    return None


def transliterate_city(city_name):
    """Транслитерация русского названия города в alias.
    'Санкт-Петербург' → 'sankt-peterburg', 'Москва' → 'moskva'.
    """
    if not city_name:
        return None
    table = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
    result = []
    for ch in city_name.lower():
        if ch in table:
            result.append(table[ch])
        elif ch.isalnum():
            result.append(ch)
        elif ch in " -":
            result.append("-")
    alias = "".join(result)
    while "--" in alias:
        alias = alias.replace("--", "-")
    return alias.strip("-")


def car_to_supabase_row(car, snapshot_date, brand_key):
    """Преобразует JSON-машину Changan/Uni в строку stock_snapshots."""
    salon = car.get("salon") or {}
    prices = car.get("prices") or {}

    # У Changan/Uni в JSON salon нет city_id — только строковый адрес.
    # Город извлекаем из адреса регуляркой (покрытие ~95%).
    addr = salon.get("address") or ""
    city_name = extract_city_from_address(addr)
    city_alias = transliterate_city(city_name) if city_name else None

    # benefits: суммарная "максимальная выгода" от программ (аналог discount_max)
    benefits = car.get("benefits") or {}
    benefit_max = benefits.get("max_price") if isinstance(benefits, dict) else None

    def _int(v):
        if v is None or v == "":
            return None
        try:
            return int(v)
        except (ValueError, TypeError):
            return None

    def _num(v):
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    return {
        # PK
        "snapshot_date":      snapshot_date,
        "brand":              brand_key,
        "car_id":             str(car.get("id", "")),

        # VIN/базовые
        "vin":                car.get("vin") or None,
        "vin_full":           car.get("vin") or None,  # у Changan VIN маскирован "*"
        "year":               _int(car.get("year")),
        "prod_date":          None,                     # отсутствует у Changan
        "published_at":       None,                     # отсутствует у Changan
        "days_on_stock":      None,                     # не вычисляется
        "status":             car.get("status") or None,

        # Модель / комплектация
        "model":              car.get("_model_name") or car.get("name") or None,
        "model_alias":        None,
        "complectation":      car.get("complectation") or car.get("trim") or None,
        "complectation_code": None,
        "engine_volume":      _num(car.get("volume")),
        "engine_power":       _int(car.get("power")),
        "transmission_type":  car.get("kpp") or None,
        "drive_type":         car.get("gear") or None,
        "body_type":          None,
        "color":              car.get("color") or None,

        # Цены / скидки
        "price_base":         _int(prices.get("current")),
        "price_special":      _int(prices.get("min")),
        "price_tradein":      _int(prices.get("min_with_trade_in")),
        "discount_tradein":   None,                     # Changan не разделяет
        "discount_credit":    None,
        "discount_equipment": None,
        "discount_color":     None,
        "discount_insurance": None,
        "discount_max":       _int(benefit_max),        # из benefits.max_price

        # Дилер
        "dealer_id":          str(salon.get("id", "")) or None,
        "dealer_name":        salon.get("name") or None,
        "dealer_address":     addr or None,
        "dealer_city":        city_name,
        "dealer_city_alias":  city_alias,
        "dealer_lat":         _num(salon.get("lat") or salon.get("latitude")),
        "dealer_lon":         _num(salon.get("lon") or salon.get("longitude")),
        "dealer_phone":       salon.get("phone") or None,

        # Прочее
        "parallel_import":    False,
        "mileage_km":         _int(car.get("mileage")),
        "is_used":            False,

        # Сырой JSON
        "raw_data":           car,
    }


def supabase_request(method, url, key, **kwargs):
    headers = kwargs.pop("headers", {})
    headers.update({
        "apikey":        key,
        "Authorization": "Bearer " + key,
        "Content-Type":  "application/json",
    })
    return requests.request(method, url, headers=headers, timeout=60, **kwargs)


def supabase_log_run_start(supabase_url, key, brand_key):
    run_id = str(uuid.uuid4())
    url = supabase_url.rstrip("/") + "/rest/v1/parsing_runs"
    payload = {
        "run_id":     run_id,
        "brand":      brand_key,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status":     "running",
    }
    try:
        r = supabase_request("POST", url, key, json=payload,
                             headers={"Prefer": "return=minimal"})
        if r.status_code in (200, 201, 204):
            return run_id
        print("   ⚠ Не удалось создать parsing_runs: HTTP {}: {}".format(
            r.status_code, r.text[:200]))
    except Exception as e:
        print("   ⚠ Ошибка создания parsing_runs: {}: {}".format(
            type(e).__name__, e))
    return None


def supabase_log_run_finish(supabase_url, key, run_id, status,
                            rows_inserted, rows_total, duration_sec,
                            error_message=None):
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


def upload_to_supabase(cars, supabase_url, key, brand_key,
                      batch_size=500):
    snapshot_date = datetime.now().strftime("%Y-%m-%d")
    url = supabase_url.rstrip("/") + "/rest/v1/stock_snapshots"

    rows = [car_to_supabase_row(c, snapshot_date, brand_key) for c in cars]

    # Дедупликация по PK
    seen = {}
    for row in rows:
        pk = (row["snapshot_date"], row["brand"], row["car_id"])
        seen[pk] = row
    deduped = len(rows) - len(seen)
    if deduped:
        print("   ⚠ Дублей по car_id: {} (удалено)".format(deduped))
    rows = list(seen.values())

    total = len(rows)
    inserted = 0
    print("   Загружаю {} строк батчами по {} ...".format(total, batch_size))

    for i in range(0, total, batch_size):
        batch = rows[i:i + batch_size]
        try:
            r = supabase_request(
                "POST", url, key, json=batch,
                headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            )
            if r.status_code in (200, 201, 204):
                inserted += len(batch)
                print("      батч {:4d}-{:4d}: OK".format(i + 1, i + len(batch)))
            else:
                err = "HTTP {}: {}".format(r.status_code, r.text[:300])
                print("      батч {:4d}-{:4d}: FAIL {}".format(
                    i + 1, i + len(batch), err))
                return inserted, err
        except Exception as e:
            err = "{}: {}".format(type(e).__name__, e)
            print("      батч {:4d}-{:4d}: ERROR {}".format(
                i + 1, i + len(batch), err))
            return inserted, err

    return inserted, None


# ─── MAIN ────────────────────────────────────────────────────────────────────
def process_brand(brand, supabase_url, supabase_key):
    """Полный цикл для одного бренда: парсинг → CSV → загрузка в Supabase.
    Возвращает True при успехе, False при сбое (но не падает).
    """
    started = time.time()
    brand_settings = get_brand_settings(brand)
    brand_key = brand_settings["brand_key"]

    print("\n" + "=" * 60)
    print("Бренд: {}".format(brand.upper()))
    print("Base URL: {}".format(brand_settings["base_url"]))
    print("=" * 60)

    session = requests.Session()

    try:
        models = fetch_dicts(session, brand_settings)
    except Exception as e:
        print("✗ Не удалось получить справочник: {}: {}".format(
            type(e).__name__, e))
        return False

    cars = fetch_all_cars(session, brand_settings, models)
    if not cars:
        print("✗ Не получено ни одного автомобиля.")
        return False

    print_stats(cars)

    out_path = save_csv(cars, brand_key)
    print("\n✓ CSV сохранён: {}".format(out_path.resolve()))

    # Опциональная загрузка в Supabase
    if not (supabase_url and supabase_key):
        print("\nℹ Supabase не настроен — пропускаю загрузку в БД.")
        return True

    print("\nЗагрузка в Supabase ...")
    run_id = supabase_log_run_start(supabase_url, supabase_key, brand_key)

    try:
        inserted, err = upload_to_supabase(
            cars, supabase_url, supabase_key, brand_key)
        duration = int(time.time() - started)

        if err is None:
            supabase_log_run_finish(
                supabase_url, supabase_key, run_id,
                status="success",
                rows_inserted=inserted,
                rows_total=len(cars),
                duration_sec=duration,
            )
            print("\n✓ {}: загружено {}/{} строк за {} сек".format(
                brand.upper(), inserted, len(cars), duration))
            return True
        else:
            supabase_log_run_finish(
                supabase_url, supabase_key, run_id,
                status="partial" if inserted > 0 else "failed",
                rows_inserted=inserted,
                rows_total=len(cars),
                duration_sec=duration,
                error_message=err,
            )
            print("\n⚠ {}: загрузка прервалась после {} строк: {}".format(
                brand.upper(), inserted, err))
            return False

    except Exception as e:
        duration = int(time.time() - started)
        supabase_log_run_finish(
            supabase_url, supabase_key, run_id,
            status="failed",
            rows_inserted=0,
            rows_total=len(cars),
            duration_sec=duration,
            error_message="{}: {}".format(type(e).__name__, e),
        )
        print("\n✗ {}: загрузка в Supabase упала: {}: {}".format(
            brand.upper(), type(e).__name__, e))
        return False


def main():
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

    brands_to_run = CONFIG["brands_to_run"]
    print("Будет обработано брендов: {} → {}".format(
        len(brands_to_run), ", ".join(brands_to_run)))

    results = {}
    for brand in brands_to_run:
        try:
            results[brand] = process_brand(brand, supabase_url, supabase_key)
        except Exception as e:
            print("\n✗ Критическая ошибка при обработке '{}': {}: {}".format(
                brand, type(e).__name__, e))
            results[brand] = False

    # Итоговый отчёт
    print("\n" + "=" * 60)
    print("ИТОГИ:")
    for brand, ok in results.items():
        marker = "✓" if ok else "✗"
        print("   {} {}".format(marker, brand.upper()))
    print("=" * 60)

    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
