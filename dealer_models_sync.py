"""
DEALER MODELS SYNC (ТТС → wordstat_models)
==========================================
Актуализирует список моделей для WordStat-парсера из каталога ТрансТехСервис (tts.ru).
Запуск: раз в 2 недели (GitHub Actions) или вручную.

ПОЧЕМУ ТТС, А НЕ MAJOR (решение 31.05.2026):
  Major-auto банит IP после ~9 запросов (429/403). ТТС держит 14+ подряд. Структура чище.

ЛОГИКА:
  1. По каждому бренду из BRANDS идём на tts.ru/auto/<slug>/
  2. Из HTML достаём модели — ссылки /auto/<slug>/<model_slug>/
  3. Извлечение гибкое: 'jetour-dashing'->'dashing'; 's50'->'s50'; 'x50plus'->'x50plus'
  4. Нормализуем (collapse_model): сворачиваем маркетинг-токены с головы и хвоста.
  5. keyword (латиница) = "<brand> <base_model>".
  6. Кириллица (решение 10 + автоген 05.07.2026):
       - НОВАЯ модель, модельная часть С ЦИФРОЙ (H6, T4L, EX5) -> keyword_cyr авто =
         "<бренд-кир из существующих строк> <модель латиницей как есть>", source='auto'.
       - Модель-слово без цифр (Coolray) ИЛИ нет бренд-кириллицы -> NULL + флаг, source='manual'.
       - Платный LLM НЕ используется.
  7. Глобальный дедуп перед записью (защита от 409).
  8. UPSERT: ВСЕ объекты с ОДИНАКОВЫМ набором ключей (иначе PGRST102). Кириллицу не затираем.
  9. МЯГКАЯ ДЕАКТИВАЦИЯ (решение 05.08.2026):
       - модель НАЙДЕНА в каталоге -> miss_count=0, active=true (авто-возврат после сбоев).
       - модель НЕ найдена -> miss_count+=1; active=false ТОЛЬКО если miss_count>=4.
       Защита от ложного выключения рабочей модели при разовом сбое выдачи ТТС
       (случай Tank 400, Jetour Dashing — выключались после 1 промаха).
 10. Бренд дал 0 моделей -> ВСЕ его модели считаем "не найденными" этот прогон
       (miss_count+=1), но это тоже под порогом 4. Флаг в журнал.

ENV / GitHub Secrets: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
ЗАВИСИМОСТИ: pip install requests beautifulsoup4
"""

import os
import re
import sys
import time
import requests
from datetime import datetime, timezone

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("ОШИБКА: нет beautifulsoup4. Выполни: pip install beautifulsoup4")
    sys.exit(1)

# ================================================================
MISS_THRESHOLD = 4   # деактивируем модель после N промахов ПОДРЯД (решение 05.08.2026)

TTS_BASE = "https://www.tts.ru/auto/"

BRANDS = {
    "Belgee": "belgee", "Changan": "changan", "Geely": "geely", "Haval": "haval",
    "Haval Pro": "haval-pro", "Jetour": "jetour", "GAC": "gac", "Hongqi": "hongqi",
    "KGM": "kgm", "Voyah": "voyah", "Deepal": "deepal", "Omoda": "omoda",
    "Jaecoo": "jaecoo", "Exeed": "exeed", "LADA": "lada", "Tank": "tank",
    "Tenet": "tenet", "Moskvich": "moskvich", "ROX": "rox", "Jeland": "jeland",
}

COLLAPSE_TOKENS = {
    "plus", "pro", "fl", "new", "max", "mca",
    "noviy", "novyy", "новый", "новая", "новое", "gwm",
}
BRAND_WORDS = {"hongqi", "gwm"}
COLLAPSE_GLUED = ["plus", "pro", "fl", "max", "new"]
YEAR_RE = re.compile(r"^20\d{2}$")

SB_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

HEADERS_BROWSER = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept-Language": "ru-RU,ru;q=0.9",
}
PAUSE_BETWEEN_BRANDS = 2.0
RETRY_BACKOFF = [5, 15]


# ─── SUPABASE ───────────────────────────────────────────────────────────────
def sb_req(method, path, **kwargs):
    headers = kwargs.pop("headers", {})
    headers.update({"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY,
                    "Content-Type": "application/json"})
    return requests.request(method, SB_URL + path, headers=headers, timeout=60, **kwargs)


def load_existing():
    """{(brand, model_name): {keyword, keyword_cyr, active, miss_count}}."""
    r = sb_req("GET", "/rest/v1/wordstat_models"
                      "?select=brand,model_name,keyword,keyword_cyr,active,miss_count")
    r.raise_for_status()
    out = {}
    for row in r.json():
        out[(row["brand"], row["model_name"])] = row
    return out


def _full_row(brand, model_name, keyword, keyword_cyr, keyword_cyr_source,
              active, miss_count, now_iso):
    """Единый набор ключей для ВСЕХ объектов upsert (иначе PGRST102)."""
    return {
        "brand": brand, "model_name": model_name, "keyword": keyword,
        "keyword_cyr": keyword_cyr, "keyword_cyr_source": keyword_cyr_source,
        "active": active, "miss_count": miss_count,
        "source": "tts", "updated_at": now_iso,
    }


def upsert_models(rows):
    if not rows:
        return 0, None
    r = sb_req("POST", "/rest/v1/wordstat_models?on_conflict=brand,model_name", json=rows,
               headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
    if r.status_code in (200, 201, 204):
        return len(rows), None
    return 0, f"HTTP {r.status_code}: {r.text[:300]}"


# ─── ПАРС ТТС ─────────────────────────────────────────────────────────────────
def fetch_brand_models(brand_slug):
    url = TTS_BASE + brand_slug + "/"
    attempts = len(RETRY_BACKOFF) + 1
    for i in range(attempts):
        try:
            resp = requests.get(url, headers=HEADERS_BROWSER, timeout=40)
        except Exception as e:
            if i < attempts - 1:
                time.sleep(RETRY_BACKOFF[i]); continue
            return None, f"запрос упал: {type(e).__name__}: {e}"
        if resp.status_code == 200:
            return _parse_models_html(resp.text, brand_slug), None
        if resp.status_code == 429 and i < attempts - 1:
            time.sleep(RETRY_BACKOFF[i]); continue
        return None, f"HTTP {resp.status_code}"
    return None, "не удалось после ретраев"


def _parse_models_html(html, brand_slug):
    pattern = re.compile(rf"/auto/{re.escape(brand_slug)}/([^/\"?]+)/", re.IGNORECASE)
    found = set()
    for m in pattern.finditer(html):
        slug = m.group(1).lower()
        if "." in slug or slug in ("", brand_slug):
            continue
        found.add(slug)
    return found


def strip_brand_prefix(model_slug, brand_slug):
    prefix = brand_slug.lower() + "-"
    return model_slug[len(prefix):] if model_slug.startswith(prefix) else model_slug


def collapse_model(model_slug, brand, brand_slug):
    raw = strip_brand_prefix(model_slug.lower(), brand_slug)
    tokens = [t for t in raw.replace("-", "_").split("_") if t]

    def droppable_tail(tok, n):
        if tok in COLLAPSE_TOKENS or tok in BRAND_WORDS: return True
        if YEAR_RE.match(tok): return True
        if tok in ("ii", "iii", "iv"): return True
        if tok.isdigit() and n > 2: return True
        return False

    def droppable_head(tok):
        return tok in COLLAPSE_TOKENS or tok in BRAND_WORDS or bool(YEAR_RE.match(tok))

    while len(tokens) > 1 and droppable_head(tokens[0]):
        tokens.pop(0)
    while len(tokens) > 1 and droppable_tail(tokens[-1], len(tokens)):
        tokens.pop()
    if tokens:
        last = tokens[-1]
        for suf in COLLAPSE_GLUED:
            if last.endswith(suf) and len(last) > len(suf):
                tokens[-1] = last[:-len(suf)]; break

    base_model = " ".join(tokens).strip() or raw.replace("-", " ")
    return base_model.upper(), f"{brand.lower()} {base_model}".strip()


# ─── АВТОКИРИЛЛИЦА ───────────────────────────────────────────────────────────
def build_brand_cyr_map(existing):
    from collections import Counter, defaultdict
    per_brand = defaultdict(Counter)
    for (brand, _), row in existing.items():
        cyr = (row.get("keyword_cyr") or "").strip()
        if cyr:
            first = cyr.split()[0]
            if re.search(r"[а-яё]", first):
                per_brand[brand][first] += 1
    return {b: c.most_common(1)[0][0] for b, c in per_brand.items() if c}


def auto_cyrillic(brand, model_name, brand_cyr_map):
    brand_cyr = brand_cyr_map.get(brand)
    if brand_cyr and re.search(r"\d", model_name):
        return f"{brand_cyr} {model_name.lower().strip()}", "auto"
    return None, "manual"


# ─── ОСНОВНОЕ ────────────────────────────────────────────────────────────────
def main():
    if not (SB_URL and SB_KEY):
        print("ОШИБКА: не заданы SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")
        sys.exit(1)

    existing = load_existing()
    brand_cyr_map = build_brand_cyr_map(existing)
    rows_by_key = {}
    warnings = []
    seen_now = set()
    deactivated = []
    now_iso = datetime.now(timezone.utc).isoformat()

    print(f"=== ТТС-sync | брендов: {len(BRANDS)} | бренд-кириллиц: {len(brand_cyr_map)} "
          f"| порог деактивации: {MISS_THRESHOLD} ===\n")

    for brand, slug in BRANDS.items():
        print(f"[{brand}] {TTS_BASE}{slug}/ ...", flush=True)
        model_slugs, err = fetch_brand_models(slug)
        if err or not model_slugs:
            warnings.append(f"{brand}: 0 моделей ({err or 'пусто'}) — модели этого бренда +1 промах")
            print("   ⚠ 0 моделей —", err or "пусто")
            time.sleep(PAUSE_BETWEEN_BRANDS)
            continue  # модели этого бренда обработаются как "не найденные" ниже

        collapsed = {}
        for ms in model_slugs:
            model_name, kw_lat = collapse_model(ms, brand, slug)
            collapsed[model_name] = kw_lat
        print(f"   модели: {', '.join(sorted(collapsed))}")

        for model_name, kw_lat in collapsed.items():
            key = (brand, model_name)
            seen_now.add(key)
            if key in existing:
                prev_cyr = existing[key].get("keyword_cyr")
                # НАЙДЕНА -> сброс промахов, активна
                rows_by_key[key] = _full_row(brand, model_name, kw_lat, prev_cyr,
                                             "manual", True, 0, now_iso)
            else:
                cyr, src = auto_cyrillic(brand, model_name, brand_cyr_map)
                if cyr:
                    print(f"   + новая: {model_name} -> кириллица авто: '{cyr}'")
                else:
                    warnings.append(f"{brand} {model_name}: новая, keyword_cyr=NULL — вписать вручную")
                rows_by_key[key] = _full_row(brand, model_name, kw_lat, cyr, src,
                                             True, 0, now_iso)
        time.sleep(PAUSE_BETWEEN_BRANDS)

    # МЯГКАЯ ДЕАКТИВАЦИЯ: модели в БД, которых НЕ увидели в этом прогоне
    for (brand, model_name), row in existing.items():
        if brand not in BRANDS:
            continue
        if (brand, model_name) in seen_now:
            continue  # найдена — уже обработана выше
        # НЕ найдена в этом прогоне -> +1 промах
        new_miss = int(row.get("miss_count") or 0) + 1
        still_active = new_miss < MISS_THRESHOLD
        if not still_active:
            deactivated.append(f"{brand} {model_name} (miss={new_miss})")
        rows_by_key[(brand, model_name)] = _full_row(
            brand, model_name, row.get("keyword"), row.get("keyword_cyr"),
            "manual", still_active, new_miss, now_iso)

    upsert_rows = list(rows_by_key.values())
    inserted, err = upsert_models(upsert_rows)
    if err:
        print(f"\n⚠ Ошибка записи: {err}")
        sys.exit(1)

    print(f"\n✓ Готово: обновлено {inserted} моделей.")
    if deactivated:
        print(f"⛔ Деактивировано (>= {MISS_THRESHOLD} промахов подряд): {len(deactivated)}")
        for d in deactivated:
            print("   -", d)
    if warnings:
        print(f"⚠ Предупреждений: {len(warnings)}")
        for w in warnings:
            print("   -", w)


if __name__ == "__main__":
    main()
