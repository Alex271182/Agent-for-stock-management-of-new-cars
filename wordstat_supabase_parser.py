"""
WORDSTAT SUPABASE PARSER v2 (Python + Playwright)
=================================================
Парсит дневную динамику запросов из Яндекс.Вордстат по РФ и грузит в Supabase.

ЧТО МЕРЯЕМ (решение сессии 30.05.2026, intent восстановлен 01.06.2026):
  По каждой модели берём ТРИ типа запроса, каждый на ДВУХ языках (лат keyword + кир keyword_cyr):
    - broad   (suffix='')        — широкий интерес, напр. "geely monjaro"
    - купить  (suffix='купить')  — горячий интерес, напр. "geely monjaro купить"
    - цена    (suffix='цена')    — горячий интерес, напр. "geely monjaro цена"
  Все запросы в broad-режиме WordStat (фраза с любыми хвостами).
  WordStat НЕ схлопывает языки (проверено замером: лат 3 732 vs кир 15 756).
  Итоговый интерес = count_lat + count_cyr (по каждому типу отдельной строкой с suffix).
  Оба числа + сами написания сохраняются раздельно (ручной контроль, п.3 сессии).
  intent ("горячий" спрос) = строки suffix IN ('купить','цена').

ИСТОЧНИК МОДЕЛЕЙ:
  Таблица wordstat_models (active=true). Латиница — из Major (dealer_models_sync.py).
  Кириллица — генерится LLM один раз при появлении модели, кэшируется в keyword_cyr.
  Если keyword_cyr пуст — парсим только латиницу + пишем предупреждение в журнал.

ЗАПУСК:
  python wordstat_supabase_parser.py --login    # один раз локально: получить cookies
  python wordstat_supabase_parser.py --headed   # видимый браузер (отладка/капча)
  python wordstat_supabase_parser.py            # headless (GitHub Actions)

ENV / GitHub Secrets:
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, YANDEX_SESSION_JSON

ЗАВИСИМОСТИ:
  pip install playwright requests
  python -m playwright install chromium
"""

import os
import sys
import json
import uuid
import asyncio
import re
from pathlib import Path
from urllib.parse import quote
from datetime import date, datetime, timezone, timedelta

import requests

try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
except ImportError:
    print("ОШИБКА: Playwright не установлен. Выполни:")
    print("    pip install playwright requests")
    print("    python -m playwright install chromium")
    sys.exit(1)

# ================================================================
# CONFIG
# ================================================================
REGION_ID      = 225    # РФ. Федеральный уровень (методология Корр. 4.4).
WINDOW_DAYS    = 21     # глубина парсинга за прогон (методология требует мин. 18)
RETENTION_DAYS = 30     # хранить N дней, старше — удалять

SCRIPT_DIR   = Path(__file__).parent
SESSION_FILE = SCRIPT_DIR / "yandex_session.json"

SB_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

MONTHS_RU = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4, "май": 5, "июнь": 6,
    "июль": 7, "август": 8, "сентябрь": 9, "октябрь": 10, "ноябрь": 11, "декабрь": 12,
}


# ─── SUPABASE HTTP ──────────────────────────────────────────────────────────
def sb_req(method, path, **kwargs):
    headers = kwargs.pop("headers", {})
    headers.update({
        "apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY,
        "Content-Type": "application/json",
    })
    return requests.request(method, SB_URL + path, headers=headers, timeout=60, **kwargs)


def load_models():
    """Активные модели из wordstat_models (латиница + кириллица)."""
    r = sb_req("GET", "/rest/v1/wordstat_models?active=eq.true"
                      "&select=brand,model_name,keyword,keyword_cyr&order=brand,model_name")
    r.raise_for_status()
    return r.json()


def run_start():
    rid = str(uuid.uuid4())
    payload = {"run_id": rid, "started_at": datetime.now(timezone.utc).isoformat(), "status": "running"}
    try:
        sb_req("POST", "/rest/v1/wordstat_runs", json=payload, headers={"Prefer": "return=minimal"})
    except Exception:
        pass
    return rid


def run_finish(rid, status, rows_inserted, rows_total, duration_sec, error_message=None):
    payload = {
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "status": status, "rows_inserted": rows_inserted,
        "rows_total": rows_total, "duration_sec": duration_sec,
    }
    if error_message:
        payload["error_message"] = error_message[:2000]
    try:
        sb_req("PATCH", f"/rest/v1/wordstat_runs?run_id=eq.{rid}", json=payload,
               headers={"Prefer": "return=minimal"})
    except Exception:
        pass


def upsert_daily(rows, batch_size=500):
    inserted = 0
    # Дедуп по ключу UNIQUE: Postgres не даёт дважды затронуть одну строку
    # в одной UPSERT-команде. Оставляем последнее вхождение.
    seen = {}
    for row in rows:
        seen[(row["brand"], row["model_name"], row["suffix"], row["search_date"])] = row
    rows = list(seen.values())
    # on_conflict обязателен: у таблицы два уникальных ограничения (PK id и
    # UNIQUE brand+model_name+suffix+search_date). Без явного on_conflict
    # PostgREST не активирует merge-duplicates по нужному ключу и падает с 409
    # на повторных днях (WordStat отдаёт историю за 19 дней, часть уже в БД).
    url = "/rest/v1/wordstat_daily?on_conflict=brand,model_name,suffix,search_date"
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        r = sb_req("POST", url, json=batch,
                   headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
        if r.status_code in (200, 201, 204):
            inserted += len(batch)
        else:
            return inserted, f"HTTP {r.status_code}: {r.text[:300]}"
    return inserted, None


def purge_old():
    cutoff = (date.today() - timedelta(days=RETENTION_DAYS)).isoformat()
    try:
        r = sb_req("DELETE", f"/rest/v1/wordstat_daily?search_date=lt.{cutoff}",
                   headers={"Prefer": "return=minimal"})
        return r.status_code in (200, 204)
    except Exception:
        return False


# ─── ДАТЫ ───────────────────────────────────────────────────────────────────
def parse_period_to_iso(s: str):
    s = s.strip().lower()
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$", s)
    if m:
        d, mo, y = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    m = re.match(r"^(\d{1,2})\.(\d{1,2})$", s)
    if m:
        d, mo = m.groups()
        return _resolve_year(int(d), int(mo))
    m = re.match(r"^(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?$", s)
    if m:
        d_str, mo_word, y_str = m.groups()
        mo = MONTHS_RU.get(mo_word)
        if not mo:
            return None
        if y_str:
            return f"{y_str}-{mo:02d}-{int(d_str):02d}"
        return _resolve_year(int(d_str), mo)
    return None


def _resolve_year(day: int, month: int) -> str:
    today = date.today()
    candidate = date(today.year, month, day)
    year = today.year if candidate <= today else today.year - 1
    return f"{year}-{month:02d}-{day:02d}"


def build_url(words: str) -> str:
    return f"https://wordstat.yandex.ru/?region={REGION_ID}&view=graph&words={quote(words)}"


# ─── PLAYWRIGHT ──────────────────────────────────────────────────────────────
def get_storage_state():
    env_json = os.environ.get("YANDEX_SESSION_JSON")
    if env_json:
        return json.loads(env_json)
    if SESSION_FILE.exists():
        return json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    return None


async def detect_captcha(page) -> bool:
    try:
        html = (await page.content()).lower()
    except Exception:
        return False
    return any(m in html for m in ["smartcaptcha", "showcaptcha", "captcha-required"])


async def switch_to_daily(page) -> bool:
    try:
        clicked = False
        for current in ["По месяцам", "По неделям"]:
            loc = page.get_by_text(current, exact=False).first
            if await loc.count() > 0:
                await loc.click(timeout=5_000)
                clicked = True
                break
        if not clicked:
            return False
        await page.wait_for_timeout(500)
        await page.get_by_text("По дням", exact=False).first.click(timeout=5_000)
        await page.wait_for_timeout(2_500)
        return True
    except Exception:
        return False


async def parse_daily_table(page) -> dict:
    try:
        await page.wait_for_selector("table", timeout=15_000)
    except PWTimeout:
        return {}
    raw = await page.evaluate(r"""
        () => {
            const tables = [...document.querySelectorAll('table')];
            for (const t of tables) {
                const rows = [...t.querySelectorAll('tr')].slice(1);
                if (rows.length < 5) continue;
                const firstCell = rows[0].querySelector('td');
                if (!firstCell) continue;
                if (!/^\d{1,2}[.\s]/.test(firstCell.innerText.trim())) continue;
                const result = {};
                for (const row of rows) {
                    const cells = [...row.querySelectorAll('td')];
                    if (cells.length < 2) continue;
                    const period = cells[0].innerText.trim();
                    const num = parseInt(cells[1].innerText.trim().replace(/\s/g, ''), 10);
                    if (period && !isNaN(num)) result[period] = num;
                }
                return result;
            }
            return {};
        }
    """)
    cutoff = date.today() - timedelta(days=WINDOW_DAYS)
    out = {}
    for period, count in raw.items():
        iso = parse_period_to_iso(period)
        if iso and date.fromisoformat(iso) >= cutoff:
            out[iso] = count
    return out


async def fetch_one(page, words, is_warmup, headless, context):
    """Парсит дневную таблицу для одной фразы. Возвращает (dict, captcha_bool)."""
    captcha_hit = False
    daily = {}
    for attempt in range(2):
        await page.goto(build_url(words), wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(3_000 if (is_warmup and attempt == 0) else 1_800)

        if await detect_captcha(page):
            captcha_hit = True
            print("\n⚠ КАПЧА.", end=" ")
            if headless:
                return None, True
            input("Пройди капчу в окне и нажми Enter...")
            await context.storage_state(path=str(SESSION_FILE))

        if not await switch_to_daily(page):
            if attempt == 0:
                await page.wait_for_timeout(1_500)
                continue
            return {}, captcha_hit
        daily = await parse_daily_table(page)
        if daily:
            break
        if attempt == 0:
            await page.wait_for_timeout(1_500)
    return daily, captcha_hit


async def scrape(headless: bool):
    if not (SB_URL and SB_KEY):
        print("ОШИБКА: не заданы SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")
        sys.exit(1)

    storage = get_storage_state()
    if storage is None:
        print("ОШИБКА: нет cookies. Запусти --login локально или задай YANDEX_SESSION_JSON.")
        sys.exit(1)

    models = load_models()
    if not models:
        print("ОШИБКА: в wordstat_models нет активных моделей.")
        sys.exit(1)

    print(f"\n=== WordStat → Supabase v2 | регион {REGION_ID} | окно {WINDOW_DAYS} дн ===")
    print(f"Моделей: {len(models)} | по 2 языка (лат+кир) | режим: {'headless' if headless else 'видимый'}\n")

    rid = run_start()
    started = datetime.now(timezone.utc)
    rows = []
    captcha_hit = False
    warnings = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(storage_state=storage)
        page = await context.new_page()
        try:
            print("Прогрев WordStat (~5 сек)...", flush=True)
            await page.goto("https://wordstat.yandex.ru/", wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(5_000)

            for idx, model in enumerate(models, 1):
                brand  = model["brand"]
                name   = model["model_name"]
                kw_lat = (model.get("keyword") or "").strip()
                kw_cyr = (model.get("keyword_cyr") or "").strip()
                print(f"[{idx}/{len(models)}] {brand} {name}", flush=True)

                # Три типа запроса на модель: broad (''), intent 'купить', intent 'цена'.
                # Каждый — на двух языках (лат keyword + кир keyword_cyr), в broad-режиме
                # WordStat (фраза с любыми хвостами). Пишется отдельной строкой с suffix.
                SUFFIXES = ["", "купить", "цена"]
                for suf in SUFFIXES:
                    q_lat = f"{kw_lat} {suf}".strip() if kw_lat else ""
                    q_cyr = f"{kw_cyr} {suf}".strip() if kw_cyr else ""
                    suf_label = suf or "broad"

                    # латиница
                    lat_data = {}
                    if q_lat:
                        print(f"    [{suf_label}] лат '{q_lat}' ...", end=" ", flush=True)
                        lat_data, cap = await fetch_one(page, q_lat, idx == 1 and suf == "", headless, context)
                        if cap and headless:
                            run_finish(rid, "failed", 0, 0,
                                       int((datetime.now(timezone.utc) - started).total_seconds()),
                                       "captcha on headless")
                            await browser.close(); sys.exit(2)
                        print(f"OK {len(lat_data)} дн" if lat_data else "пусто")

                    # кириллица
                    cyr_data = {}
                    if q_cyr:
                        print(f"    [{suf_label}] кир '{q_cyr}' ...", end=" ", flush=True)
                        cyr_data, cap = await fetch_one(page, q_cyr, False, headless, context)
                        if cap and headless:
                            run_finish(rid, "failed", 0, 0,
                                       int((datetime.now(timezone.utc) - started).total_seconds()),
                                       "captcha on headless")
                            await browser.close(); sys.exit(2)
                        print(f"OK {len(cyr_data)} дн" if cyr_data else "пусто")
                    elif suf == "":
                        warnings.append(f"{brand} {name}: нет keyword_cyr (только латиница)")

                    # объединение: count_lat + count_cyr = query_count
                    all_dates = set(lat_data) | set(cyr_data)
                    if not all_dates:
                        if suf == "":
                            warnings.append(f"{brand} {name}: пусто по обоим языкам")
                        continue
                    for d in all_dates:
                        cl = lat_data.get(d)
                        cc = cyr_data.get(d)
                        rows.append({
                            "brand":       brand,
                            "model_name":  name,
                            "keyword":     kw_lat,
                            "keyword_cyr": kw_cyr or None,
                            "suffix":      suf,
                            "region_id":   REGION_ID,
                            "search_date": d,
                            "count_lat":   cl,
                            "count_cyr":   cc,
                            "query_count": (cl or 0) + (cc or 0),
                            # явно проставляем время прогона: DEFAULT now() срабатывает
                            # только при INSERT, а при UPSERT-обновлении метка замерзала.
                            # Теперь parsed_at = реальное время прогона и при INSERT, и при UPDATE.
                            "parsed_at":   started.isoformat(),
                        })
                    await page.wait_for_timeout(600)
        finally:
            await browser.close()

    duration = int((datetime.now(timezone.utc) - started).total_seconds())

    if not rows:
        run_finish(rid, "failed", 0, 0, duration, "no rows parsed; " + "; ".join(warnings[:10]))
        print("\n⚠ Данных нет.")
        sys.exit(1)

    print(f"\nЗагружаю {len(rows)} строк в Supabase...")
    inserted, err = upsert_daily(rows)
    if err:
        run_finish(rid, "partial" if inserted else "failed", inserted, len(rows), duration, err)
        print(f"⚠ Загрузка прервалась: {err}")
        sys.exit(1)

    purged = purge_old()
    status = "success" if not (captcha_hit or warnings) else "partial"
    run_finish(rid, status, inserted, len(rows), duration,
               "; ".join(warnings[:20]) if warnings else None)
    print(f"✓ Готово: {inserted} строк, статус {status}, очистка 30+ дн: {'ok' if purged else 'fail'}")
    if warnings:
        print(f"⚠ Предупреждений: {len(warnings)} (см. журнал wordstat_runs)")
        for w in warnings[:10]:
            print("   -", w)


async def login_flow():
    print("\n=== РЕЖИМ АВТОРИЗАЦИИ ===")
    print("Откроется браузер. Залогинься в нужном Яндекс-аккаунте и закрой окно.\n")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto("https://passport.yandex.ru/auth")
        try:
            await page.wait_for_event("close", timeout=600_000)
        except PWTimeout:
            print("Время вышло. Запусти --login ещё раз.")
            await browser.close()
            return
        await context.storage_state(path=str(SESSION_FILE))
        await browser.close()
    print(f"\nCookies сохранены: {SESSION_FILE}")
    print("Для Actions: содержимое файла -> Secret YANDEX_SESSION_JSON (одной строкой).")


def main():
    args = sys.argv[1:]
    if "--login" in args:
        asyncio.run(login_flow())
    else:
        asyncio.run(scrape(headless="--headed" not in args))


if __name__ == "__main__":
    main()
