"""
Validator — проверяет качество данных после ежедневного парсинга.

Запускается в 08:00 МСК (через час после старта парсеров в 07:00).
Если что-то не так — шлёт алерт в Telegram.

Проверки (по согласованной матрице):
1. Парсер запустился сегодня (есть свежая запись в parsing_runs).
2. Парсер завершился успешно (status = 'success').
3. rows_inserted vs rows_total — расхождение не больше 2%.
4. Сегодня vs вчера по бренду — падение не больше 30%, рост не больше 50%.
5. Доля записей с пустыми ключевыми полями (model / price_base / dealer_name)
   — не больше 5%.

Бренды, которые ожидаем увидеть сегодня:
  jetour, changan, uni, haval, geely, belgee
  (haval_combo парсер раскладывается в БД на haval и haval_pro — оба
  допустимы; если нет ни одного из них — алерт).

Переменные окружения:
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID

Выход:
  exit 0 — все проверки прошли (алерт всё равно мог уйти, если были warnings,
           но критических ошибок нет).
  exit 1 — критическая ошибка валидатора (не смог подключиться к БД и т.п.).
"""

import os
import sys
from datetime import date, timedelta, datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from supabase import create_client, Client

from telegram_alert import send_alert


# ─── НАСТРОЙКИ ────────────────────────────────────────────────────────────────

# Какие бренды ожидаем в БД сегодня.
# Ключ — логическое имя для отчёта.
# Значение — список вариантов в поле brand (т.к. один парсер может писать
# несколько брендов: haval_combo → haval + haval_pro).
EXPECTED_BRANDS = {
    "Jetour":   ["jetour"],
    "Changan":  ["changan"],
    "Uni":      ["uni"],
    "Haval":    ["haval", "haval_pro"],
    "Geely":    ["geely"],
    "Belgee":   ["belgee"],
}

# Пороги для алертов.
ROW_INSERT_DELTA_PCT = 2.0      # rows_inserted vs rows_total
DAY_OVER_DAY_DROP_PCT = 30.0    # падение к вчера
DAY_OVER_DAY_RISE_PCT = 50.0    # рост к вчера
EMPTY_FIELDS_PCT = 5.0          # доля записей с пустыми ключевыми полями

# Ключевые поля, которые не должны быть пустыми
KEY_FIELDS = ["model", "price_base", "dealer_name"]


# ─── УТИЛИТЫ ──────────────────────────────────────────────────────────────────

def get_client() -> Client:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    return create_client(url, key)


def pct_diff(a: int, b: int) -> float:
    """Процент изменения от b к a. b=0 → 0 если a=0, иначе inf (вернём 999)."""
    if b == 0:
        return 0.0 if a == 0 else 999.0
    return (a - b) / b * 100.0


# ─── ПРОВЕРКИ ─────────────────────────────────────────────────────────────────

def check_parsing_runs(client: Client, today: date) -> list[str]:
    """
    Проверки 1, 2, 3: журнал запусков за сегодня.
    Возвращает список текстов проблем (пусто = всё ок).
    """
    problems = []

    # Берём все запуски, у которых started_at >= начало сегодняшнего дня UTC.
    # Парсер мог стартовать в 07:00 МСК = 04:00 UTC.
    today_start = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)

    resp = (client.table("parsing_runs")
            .select("brand, status, rows_inserted, rows_total, error_message, started_at")
            .gte("started_at", today_start.isoformat())
            .execute())
    runs = resp.data or []

    # Группируем последние запуски по brand
    latest_by_brand: dict[str, dict] = {}
    for run in runs:
        b = run["brand"]
        if b not in latest_by_brand or run["started_at"] > latest_by_brand[b]["started_at"]:
            latest_by_brand[b] = run

    # Проверка 1: парсер запустился сегодня для каждого ожидаемого бренда
    for logical_name, brand_aliases in EXPECTED_BRANDS.items():
        found = any(alias in latest_by_brand for alias in brand_aliases)
        if not found:
            problems.append(f"❌ <b>{logical_name}</b>: парсер сегодня не запускался")

    # Проверки 2, 3: статус и расхождение rows_inserted/rows_total
    for brand, run in latest_by_brand.items():
        status = run.get("status")
        if status not in ("success", "ok"):
            err = (run.get("error_message") or "").strip()[:200]
            problems.append(
                f"❌ <b>{brand}</b>: статус = <code>{status}</code>" +
                (f"\n   <i>{err}</i>" if err else "")
            )
            continue

        inserted = run.get("rows_inserted") or 0
        total = run.get("rows_total") or 0
        if total > 0:
            diff_pct = abs(inserted - total) / total * 100
            if diff_pct > ROW_INSERT_DELTA_PCT:
                problems.append(
                    f"⚠️ <b>{brand}</b>: расхождение rows_inserted={inserted} "
                    f"vs rows_total={total} ({diff_pct:.1f}%)"
                )

    return problems


def check_day_over_day(client: Client, today: date) -> list[str]:
    """
    Проверка 4: сравнение количества записей сегодня vs вчера.
    """
    problems = []
    yesterday = today - timedelta(days=1)

    # Считаем кол-во строк через RPC? Нет — у Supabase Python SDK для count
    # достаточно select с count='exact', head=True.
    today_counts: dict[str, int] = {}
    yest_counts: dict[str, int] = {}

    for d, bucket in [(today, today_counts), (yesterday, yest_counts)]:
        for logical_name, aliases in EXPECTED_BRANDS.items():
            total = 0
            for alias in aliases:
                resp = (client.table("stock_snapshots")
                        .select("car_id", count="exact", head=True)
                        .eq("snapshot_date", d.isoformat())
                        .eq("brand", alias)
                        .execute())
                total += resp.count or 0
            bucket[logical_name] = total

    for brand in EXPECTED_BRANDS:
        t = today_counts[brand]
        y = yest_counts[brand]
        delta = pct_diff(t, y)
        if delta < -DAY_OVER_DAY_DROP_PCT:
            problems.append(
                f"⚠️ <b>{brand}</b>: падение к вчера {delta:+.1f}% "
                f"(сегодня {t}, вчера {y})"
            )
        elif delta > DAY_OVER_DAY_RISE_PCT:
            problems.append(
                f"⚠️ <b>{brand}</b>: рост к вчера {delta:+.1f}% "
                f"(сегодня {t}, вчера {y})"
            )

    return problems


def check_empty_fields(client: Client, today: date) -> list[str]:
    """
    Проверка 5: доля записей с пустыми ключевыми полями.
    """
    problems = []

    for logical_name, aliases in EXPECTED_BRANDS.items():
        total_today = 0
        empty_count = 0
        for alias in aliases:
            # Общее число записей сегодня
            total_resp = (client.table("stock_snapshots")
                          .select("car_id", count="exact", head=True)
                          .eq("snapshot_date", today.isoformat())
                          .eq("brand", alias)
                          .execute())
            total_today += total_resp.count or 0

            # Записи с пустыми ключевыми полями (хотя бы одно поле NULL)
            for field in KEY_FIELDS:
                empty_resp = (client.table("stock_snapshots")
                              .select("car_id", count="exact", head=True)
                              .eq("snapshot_date", today.isoformat())
                              .eq("brand", alias)
                              .is_(field, "null")
                              .execute())
                empty_count = max(empty_count, empty_resp.count or 0)

        if total_today == 0:
            continue  # эту проблему уже поймает check_parsing_runs

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
        all_problems += check_parsing_runs(client, today)
        all_problems += check_day_over_day(client, today)
        all_problems += check_empty_fields(client, today)
    except Exception as e:
        send_alert(f"🚨 <b>Validator упал на проверках</b>:\n<code>{e}</code>")
        return 1

    if not all_problems:
        # Тихий успех — не спамим. Можно раз в неделю слать "все ок", но это позже.
        print(f"[{today}] ✅ Все проверки пройдены")
        return 0

    # Группируем алерт
    header = f"🔔 <b>Проверка стоков {today.strftime('%d.%m.%Y')}</b>\n"
    body = "\n".join(all_problems)
    send_alert(header + "\n" + body)
    print(f"[{today}] Отправлен алерт с {len(all_problems)} проблемами")
    return 0


if __name__ == "__main__":
    sys.exit(main())
