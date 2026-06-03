"""
Validator — проверяет качество данных после ежедневного парсинга.

Запускается через 1 час после старта парсеров. Если что-то не так — шлёт
алерт в Telegram.

Проверки (адаптированы под новую архитектуру stock_cars, где одна строка =
одна машина, INSERT только для новых car_id, UPDATE для уже существующих):
  1. Парсер запустился сегодня (есть свежая запись в parsing_runs).
  2. Парсер завершился успешно (status = 'success').
  3. ИНВАРИАНТ: активный сток в БД должен ТОЧНО совпадать с rows_total
     последнего прогона. Это главная проверка корректности логики
     apply_stock_snapshot.
  4. Размер сегодняшнего среза vs МЕДИАНА последних 7 дней —
     падение не больше 30%, рост не больше 50%. Медиана устойчива к
     одиночным аномальным дням (в отличие от сравнения с одним вчера).
  5. Доля записей с пустыми ключевыми полями (model / price_base /
     dealer_name) — не больше 5%.

Бренды: jetour, changan, uni, haval+haval_pro, geely, belgee.

Переменные окружения:
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID

Выход:
  exit 0 — все проверки прошли (алерт мог уйти, но критических ошибок нет).
  exit 1 — критическая ошибка валидатора.
"""

import os
import sys
import statistics
from datetime import date, timedelta, datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from supabase import create_client, Client

from telegram_alert import send_alert


# ─── НАСТРОЙКИ ────────────────────────────────────────────────────────────────

# Логическое имя → список brand-алиасов в БД.
# Один парсер может писать несколько брендов (haval_combo → haval + haval_pro).
EXPECTED_BRANDS = {
    "Jetour":   ["jetour"],
    "Changan":  ["changan"],
    "Uni":      ["uni"],
    "Haval":    ["haval", "haval_pro"],
    "Geely":    ["geely"],
    "Belgee":   ["belgee"],
}

# Пороги
STOCK_INVARIANT_TOLERANCE = 0   # инвариант — расхождение ровно 0 машин
DAY_OVER_MEDIAN_DROP_PCT = 30.0  # падение к медиане за 7 дней
DAY_OVER_MEDIAN_RISE_PCT = 50.0  # рост к медиане за 7 дней
EMPTY_FIELDS_PCT = 5.0           # доля записей с пустыми ключевыми полями
MEDIAN_WINDOW_DAYS = 7           # размер окна для медианы

# Ключевые поля, которые не должны быть пустыми
KEY_FIELDS = ["model", "price_base", "dealer_name"]


# ─── УТИЛИТЫ ──────────────────────────────────────────────────────────────────

def get_client() -> Client:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    return create_client(url, key)


def pct_diff(a: float, b: float) -> float:
    """Процент изменения от b к a. b=0 → 0 если a=0, иначе inf (вернём 999)."""
    if b == 0:
        return 0.0 if a == 0 else 999.0
    return (a - b) / b * 100.0


def fmt(x: float) -> str:
    """Округление числа для вывода в алерте."""
    return f"{x:.0f}" if x == int(x) else f"{x:.1f}"


# ─── ПРОВЕРКИ ─────────────────────────────────────────────────────────────────

def check_parsing_runs(client: Client, today: date) -> tuple[list[str], dict[str, dict]]:
    """
    Проверки 1, 2: журнал запусков за сегодня.
    Возвращает (список проблем, latest_by_brand для дальнейших проверок).
    """
    problems: list[str] = []

    today_start = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)

    resp = (client.table("parsing_runs")
            .select("brand, status, rows_inserted, rows_total, error_message, started_at")
            .gte("started_at", today_start.isoformat())
            .execute())
    runs = resp.data or []

    # Последний запуск по каждому brand
    latest_by_brand: dict[str, dict] = {}
    for run in runs:
        b = run["brand"]
        if b not in latest_by_brand or run["started_at"] > latest_by_brand[b]["started_at"]:
            latest_by_brand[b] = run

    # Проверка 1: парсер запустился сегодня
    for logical_name, brand_aliases in EXPECTED_BRANDS.items():
        found = any(alias in latest_by_brand for alias in brand_aliases)
        if not found:
            problems.append(f"❌ <b>{logical_name}</b>: парсер сегодня не запускался")

    # Проверка 2: статус
    for brand, run in latest_by_brand.items():
        status = run.get("status")
        if status not in ("success", "ok"):
            err = (run.get("error_message") or "").strip()[:200]
            problems.append(
                f"❌ <b>{brand}</b>: статус = <code>{status}</code>" +
                (f"\n   <i>{err}</i>" if err else "")
            )

    return problems, latest_by_brand


def check_stock_invariant(client: Client, latest_by_brand: dict[str, dict]) -> list[str]:
    """
    Проверка 3 (НОВАЯ — главный инвариант):
    Активный сток в БД должен ТОЧНО совпадать с rows_total последнего прогона.

    Если расходится — значит, функция apply_stock_snapshot отработала
    некорректно (либо не отработала вообще). Это критический сбой логики.
    """
    problems: list[str] = []

    for logical_name, aliases in EXPECTED_BRANDS.items():
        for alias in aliases:
            run = latest_by_brand.get(alias)
            if not run or run.get("status") not in ("success", "ok"):
                continue  # ошибки старта уже поймала check_parsing_runs

            rows_total = run.get("rows_total") or 0

            # Считаем активный сток через VIEW
            resp = (client.table("v_stock_active")
                    .select("car_id", count="exact", head=True)
                    .eq("brand", alias)
                    .execute())
            active_in_db = resp.count or 0

            diff = abs(active_in_db - rows_total)
            if diff > STOCK_INVARIANT_TOLERANCE:
                problems.append(
                    f"🚨 <b>{alias}</b>: ИНВАРИАНТ НАРУШЕН — "
                    f"активный сток в БД = {active_in_db}, "
                    f"парсер увидел = {rows_total} (расхождение {diff}). "
                    f"Проверь, отработала ли функция apply_stock_snapshot."
                )

    return problems


def check_day_over_median(client: Client, today: date) -> list[str]:
    """
    Проверка 4: сравнение размера сегодняшнего среза с МЕДИАНОЙ последних 7 дней.

    Почему медиана, а не вчера:
    - Один аномальный день (как Haval 02.06 со сбоем) не искажает базу.
    - Возвращение к норме после аномалии не воспринимается как «рост».
    - Сравнение более устойчивое и предсказуемое.

    За каждый день берём ПОСЛЕДНИЙ прогон (если их было несколько).
    """
    problems: list[str] = []

    def latest_runs_for_day(d: date) -> dict[str, int]:
        """Сумма rows_total последних прогонов за день, сгруппированная по логическому бренду."""
        day_start = datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc)
        day_end = datetime.combine(d + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        resp = (client.table("parsing_runs")
                .select("brand, rows_total, started_at, status")
                .gte("started_at", day_start.isoformat())
                .lt("started_at", day_end.isoformat())
                .execute())
        runs = resp.data or []

        latest: dict[str, dict] = {}
        for run in runs:
            b = run["brand"]
            if b not in latest or run["started_at"] > latest[b]["started_at"]:
                latest[b] = run

        bucket: dict[str, int] = {}
        for logical_name, aliases in EXPECTED_BRANDS.items():
            total = 0
            for alias in aliases:
                if alias in latest:
                    total += latest[alias].get("rows_total") or 0
            bucket[logical_name] = total
        return bucket

    today_counts = latest_runs_for_day(today)

    # Собираем медиану по последним 7 дням (не включая сегодня)
    history_counts: dict[str, list[int]] = {name: [] for name in EXPECTED_BRANDS}
    for offset in range(1, MEDIAN_WINDOW_DAYS + 1):
        d = today - timedelta(days=offset)
        day_counts = latest_runs_for_day(d)
        for name, val in day_counts.items():
            # Пропускаем нулевые дни (парсер не запускался в этот день)
            if val > 0:
                history_counts[name].append(val)

    for name in EXPECTED_BRANDS:
        t = today_counts[name]
        history = history_counts[name]
        if not history or t == 0:
            continue  # нет с чем сравнивать, либо сегодня парсер не отработал
        median_val = statistics.median(history)
        delta = pct_diff(t, median_val)
        if delta < -DAY_OVER_MEDIAN_DROP_PCT:
            problems.append(
                f"⚠️ <b>{name}</b>: падение к медиане за {MEDIAN_WINDOW_DAYS} дней "
                f"{delta:+.1f}% (сегодня {t}, медиана {fmt(median_val)})"
            )
        elif delta > DAY_OVER_MEDIAN_RISE_PCT:
            problems.append(
                f"⚠️ <b>{name}</b>: рост к медиане за {MEDIAN_WINDOW_DAYS} дней "
                f"{delta:+.1f}% (сегодня {t}, медиана {fmt(median_val)})"
            )

    return problems


def check_empty_fields(client: Client) -> list[str]:
    """
    Проверка 5: доля записей с пустыми ключевыми полями.

    Источник: v_stock_active (активные машины, через VIEW).
    Логика и порог (>5%) сохранены как раньше.
    """
    problems: list[str] = []

    for logical_name, aliases in EXPECTED_BRANDS.items():
        total_today = 0
        empty_count = 0
        for alias in aliases:
            total_resp = (client.table("v_stock_active")
                          .select("car_id", count="exact", head=True)
                          .eq("brand", alias)
                          .execute())
            total_today += total_resp.count or 0

            for field in KEY_FIELDS:
                empty_resp = (client.table("v_stock_active")
                              .select("car_id", count="exact", head=True)
                              .eq("brand", alias)
                              .is_(field, "null")
                              .execute())
                empty_count = max(empty_count, empty_resp.count or 0)

        if total_today == 0:
            continue

        empty_pct = empty_count / total_today * 100
        if empty_pct > EMPTY_FIELDS_PCT:
            problems.append(
                f"⚠️ <b>{logical_name}</b>: {empty_pct:.1f}% записей "
                f"с пустыми ключевыми полями ({empty_count} из {total_today})"
            )

    return problems


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main() -> int:
    try:
        client = get_client()
    except Exception as e:
        send_alert(f"🚨 <b>Validator упал</b>: не удалось подключиться к Supabase.\n<code>{e}</code>")
        return 1

    today = date.today()
    all_problems: list[str] = []

    try:
        problems_runs, latest_by_brand = check_parsing_runs(client, today)
        all_problems += problems_runs
        all_problems += check_stock_invariant(client, latest_by_brand)
        all_problems += check_day_over_median(client, today)
        all_problems += check_empty_fields(client)
    except Exception as e:
        send_alert(f"🚨 <b>Validator упал на проверках</b>:\n<code>{e}</code>")
        return 1

    if not all_problems:
        print(f"[{today}] ✅ Все проверки пройдены")
        return 0

    header = f"🔔 <b>Проверка стоков {today.strftime('%d.%m.%Y')}</b>\n"
    body = "\n".join(all_problems)
    send_alert(header + "\n" + body)
    print(f"[{today}] Отправлен алерт с {len(all_problems)} проблемами")
    return 0


if __name__ == "__main__":
    sys.exit(main())
