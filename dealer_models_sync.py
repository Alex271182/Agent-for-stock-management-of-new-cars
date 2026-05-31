"""
DEALER MODELS SYNC (ТТС → wordstat_models)
==========================================
Актуализирует список моделей для WordStat-парсера из каталога ТрансТехСервис (tts.ru).
Запуск: раз в 2 недели (GitHub Actions) или вручную.

ПОЧЕМУ ТТС, А НЕ MAJOR (решение сессии 31.05.2026):
  Major-auto банит IP после ~9 запросов к /models/ (HTTP 429/403), окно бана длинное
  (>10 мин), пауза не помогает. ТТС держит 14+ запросов подряд без бана (проверено).
  Структура ТТС чище: модели нормализованы (нет NOVIY/GWM/обрезков), как у Major.

ЛОГИКА:
  1. По каждому бренду из BRANDS идём на tts.ru/auto/<slug>/
  2. Из HTML достаём модели — ссылки вида /auto/<slug>/<model_slug>/
  3. Извлечение модели ГИБКОЕ (слаги ТТС непостоянны):
       'jetour-dashing' -> отрезаем префикс бренда -> 'dashing'
       's50'            -> префикса нет -> 's50'
       'x50plus'        -> 'x50plus'
  4. Нормализуем (collapse_model): сворачиваем маркетинговые токены С ГОЛОВЫ и С ХВОСТА:
       NOVIY/NEW/GWM/PLUS/PRO/FL/MAX/+/годы. НЕ трогаем значащие части.
       Проверено на Belgee: s50->S50, x50plus->X50, x70-fl->X70.
  5. keyword (латиница) = "<brand> <base_model>".
  6. Кириллицу скрипт НЕ генерирует (решение 10: без платного LLM).
     Новая модель -> keyword_cyr=NULL, keyword_cyr_source="manual", предупреждение в журнал.
  7. ГЛОБАЛЬНЫЙ ДЕДУП перед записью: если два слага свернулись в один (brand, model_name) —
     оставляем один. Иначе HTTP 409 duplicate key (баг прошлой сессии на Belgee S50).
  8. UPSERT в wordstat_models. ВСЕ объекты имеют ОДИНАКОВЫЙ набор ключей (иначе PGRST102).
     Существующую кириллицу НЕ затираем.
  9. Модель, пропавшую из каталога, помечаем active=false (не удаляем).
 10. Бренд дал 0 моделей -> флаг в журнал (не падаем). Так Jeland (пока нет на ТТС)
     корректно пропускается и подхватится сам, когда появится.

JELAND: правопреемник Jaecoo. Пока отсутствует на ТТС. Оставлен в BRANDS со слагом
  'jeland' — при 0 моделей просто пишется в журнал, код не трогать, подхватится автоматом.

ENV / GitHub Secrets:
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY

ЗАВИСИМОСТИ:
  pip install requests beautifulsoup4
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
# CONFIG
# ================================================================
TTS_BASE = "https://www.tts.ru/auto/"

# Бренды: "наш бренд" -> слаг ТТС. Слаги-исключения подтверждены по HTML каталога ТТС.
# Uni НЕ парсим (входит в Changan). Jeland — на вырост (пока нет на ТТС).
BRANDS = {
    "Belgee":    "belgee",
    "Changan":   "changan",
    "Geely":     "geely",
    "Haval":     "haval",
    "Haval Pro": "haval-pro",
    "Jetour":    "jetour",
    "GAC":       "gac",
    "Hongqi":    "hongqi",
    "KGM":       "kgm",
    "Voyah":     "voyah",
    "Deepal":    "deepal",
    "Omoda":     "omoda",
    "Jaecoo":    "jaecoo",
    "Exeed":     "exeed",
    "LADA":      "lada",
    "Tank":      "tank",
    "Tenet":     "tenet",
    "Moskvich":  "moskvich",
    "ROX":       "rox",
    "Jeland":    "jeland",   # появится позже — пока 0 моделей, не падаем
}

# Маркетинговые токены, сворачиваемые к базовой модели (регистронезависимо).
# Режутся и С ГОЛОВЫ, и С ХВОСТА имени.
COLLAPSE_TOKENS = {
    "plus", "pro", "fl", "new", "max", "mca",
    "noviy", "novyy", "новый", "новая", "новое",
    "gwm",
}
# Бренд-слова, прилипающие к имени (ТТС: 'h5-hongqi' -> H5). Срезаем с головы и хвоста.
BRAND_WORDS = {"hongqi", "gwm"}
# Полные слова-суффиксы, которые могут быть слитно с предыдущим токеном (x50plus -> x50, cs95new -> cs95).
COLLAPSE_GLUED = ["plus", "pro", "fl", "max", "new"]
# Год (2020–2099) — отдельный токен, сворачиваем.
YEAR_RE = re.compile(r"^20\d{2}$")

SB_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

HEADERS_BROWSER = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept-Language": "ru-RU,ru;q=0.9",
}

PAUSE_BETWEEN_BRANDS = 2.0   # ТТС бана нет, но вежливость не повредит
RETRY_BACKOFF = [5, 15]      # на всякий — лёгкий ретрай при сетевом сбое/429


# ─── SUPABASE ───────────────────────────────────────────────────────────────
def sb_req(method, path, **kwargs):
    headers = kwargs.pop("headers", {})
    headers.update({"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY,
                    "Content-Type": "application/json"})
    return requests.request(method, SB_URL + path, headers=headers, timeout=60, **kwargs)


def load_existing():
    """Текущие модели: {(brand, model_name): {keyword, keyword_cyr, active}}."""
    r = sb_req("GET", "/rest/v1/wordstat_models"
                      "?select=brand,model_name,keyword,keyword_cyr,active")
    r.raise_for_status()
    out = {}
    for row in r.json():
        out[(row["brand"], row["model_name"])] = row
    return out


def _full_row(brand, model_name, keyword, keyword_cyr, keyword_cyr_source, now_iso):
    """Единый набор ключей для ВСЕХ объектов upsert (иначе PGRST102)."""
    return {
        "brand": brand,
        "model_name": model_name,
        "keyword": keyword,
        "keyword_cyr": keyword_cyr,
        "keyword_cyr_source": keyword_cyr_source,
        "active": True,
        "source": "tts",
        "updated_at": now_iso,
    }


def upsert_models(rows):
    if not rows:
        return 0, None
    # on_conflict ОБЯЗАТЕЛЕН: без него resolution=merge-duplicates не знает, по какому
    # уникальному индексу мёржить, и запрос падает на дубле (HTTP 409). Индекс —
    # wordstat_models_brand_model_name_key = UNIQUE(brand, model_name).
    r = sb_req("POST", "/rest/v1/wordstat_models?on_conflict=brand,model_name", json=rows,
               headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
    if r.status_code in (200, 201, 204):
        return len(rows), None
    return 0, f"HTTP {r.status_code}: {r.text[:300]}"


def deactivate(brand, model_name):
    sb_req("PATCH",
           f"/rest/v1/wordstat_models?brand=eq.{brand}&model_name=eq.{model_name}",
           json={"active": False, "updated_at": datetime.now(timezone.utc).isoformat()},
           headers={"Prefer": "return=minimal"})


# ─── ПАРС ТТС ─────────────────────────────────────────────────────────────────
def fetch_brand_models(brand_slug):
    """
    Возвращает (set{model_slug}, err) для бренда с tts.ru/auto/<slug>/.
    Лёгкий ретрай при сетевом сбое / 429.
    """
    url = TTS_BASE + brand_slug + "/"
    attempts = len(RETRY_BACKOFF) + 1
    for i in range(attempts):
        try:
            resp = requests.get(url, headers=HEADERS_BROWSER, timeout=40)
        except Exception as e:
            if i < attempts - 1:
                time.sleep(RETRY_BACKOFF[i])
                continue
            return None, f"запрос упал: {type(e).__name__}: {e}"

        if resp.status_code == 200:
            return _parse_models_html(resp.text, brand_slug), None
        if resp.status_code == 429 and i < attempts - 1:
            time.sleep(RETRY_BACKOFF[i])
            continue
        return None, f"HTTP {resp.status_code}"
    return None, "не удалось после ретраев"


def _parse_models_html(html, brand_slug):
    """
    Ищем ссылки-модели /auto/<brand_slug>/<model_slug>/.
    Исключаем служебные хвосты (detail.php, пагинацию, query).
    """
    pattern = re.compile(
        rf"/auto/{re.escape(brand_slug)}/([^/\"?]+)/", re.IGNORECASE
    )
    found = set()
    for m in pattern.finditer(html):
        slug = m.group(1).lower()
        # detail.php и подобное отсекаем (там точка/параметры — не пройдут [^/"?]+ с точкой? точка пройдёт)
        if "." in slug or slug in ("", brand_slug):
            continue
        found.add(slug)
    return found


def strip_brand_prefix(model_slug, brand_slug):
    """
    Гибкое извлечение: 'jetour-dashing' -> 'dashing'; 's50' -> 's50'.
    Отрезаем префикс бренда ТОЛЬКО если он есть (слаги ТТС непостоянны).
    """
    prefix = brand_slug.lower() + "-"
    if model_slug.startswith(prefix):
        return model_slug[len(prefix):]
    return model_slug


def collapse_model(model_slug, brand, brand_slug):
    """
    Нормализует модель: отрезает префикс/суффикс бренда, маркетинговые токены, поколения,
    хвостовые числа-исполнения.
      'jetour-dashing'  -> 'DASHING'
      's50'             -> 'S50'
      'x50plus'         -> 'X50'        (plus/max сворачиваем всегда; поколения не различаем)
      'cs35plus-mca'    -> 'CS35'       (plus + mca срезаны)
      'cs95new'         -> 'CS95'
      'h5-hongqi'       -> 'H5'         (бренд-слово в хвосте)
      'novyy-atlas'     -> 'ATLAS'
      'dargo-x'         -> 'DARGO'      (модификация)
      'gs8-ii'          -> 'GS8'        (поколение)
      'uni-s-4-4'       -> 'UNI S'      (исполнение 2WD/4WD — хвостовые числа при >=2 токенах)
    НЕ трогает значащие имена: 'e-hs9' -> 'E HS9', 'emgrand-gs' -> 'EMGRAND GS',
    'uni-k' -> 'UNI K', '300' (Tank) -> '300' (одиночное число не режем).
    """
    raw = strip_brand_prefix(model_slug.lower(), brand_slug)
    base = raw.replace("-", "_")
    tokens = [t for t in base.split("_") if t]

    # маркер «срезаемый хвост»: маркетинг-токен / год / бренд-слово / римское поколение / одиночная буква-модификация
    def droppable_tail(tok, ntokens):
        if tok in COLLAPSE_TOKENS or tok in BRAND_WORDS:
            return True
        if YEAR_RE.match(tok):
            return True
        if tok in ("ii", "iii", "iv"):          # поколения: GS8 II -> GS8
            return True
        # хвостовое число-исполнение (UNI S 4 4) — только если в имени уже >=2 значащих токена
        if tok.isdigit() and ntokens > 2:
            return True
        return False

    def droppable_head(tok):
        return tok in COLLAPSE_TOKENS or tok in BRAND_WORDS or bool(YEAR_RE.match(tok))

    # 1) С ГОЛОВЫ
    while len(tokens) > 1 and droppable_head(tokens[0]):
        tokens.pop(0)
    # 2) С ХВОСТА
    while len(tokens) > 1 and droppable_tail(tokens[-1], len(tokens)):
        tokens.pop()
    # 3) суффикс слитно: x50plus -> x50, cs95new -> cs95
    if tokens:
        last = tokens[-1]
        for suf in COLLAPSE_GLUED:
            if last.endswith(suf) and len(last) > len(suf):
                tokens[-1] = last[:-len(suf)]
                break

    base_model = " ".join(tokens).strip() or base
    keyword_lat = f"{brand.lower()} {base_model}".strip()
    model_name = base_model.upper()
    return model_name, keyword_lat


# ─── ОСНОВНОЕ ────────────────────────────────────────────────────────────────
def main():
    if not (SB_URL and SB_KEY):
        print("ОШИБКА: не заданы SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")
        sys.exit(1)

    existing = load_existing()
    seen_now = set()
    # глобальный дедуп: (brand, model_name) -> row. Защита от HTTP 409.
    rows_by_key = {}
    warnings = []
    now_iso = datetime.now(timezone.utc).isoformat()

    print(f"=== ТТС-sync | брендов: {len(BRANDS)} ===\n")

    for brand, slug in BRANDS.items():
        print(f"[{brand}] {TTS_BASE}{slug}/ ...", flush=True)
        model_slugs, err = fetch_brand_models(slug)
        if err or not model_slugs:
            msg = f"{brand}: 0 моделей ({err or 'пусто'}) — проверь слаг"
            print("   ⚠", msg)
            warnings.append(msg)
            time.sleep(PAUSE_BETWEEN_BRANDS)
            continue

        # сворачиваем; дубли по model_name схлопываются прямо здесь
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
                rows_by_key[key] = _full_row(brand, model_name, kw_lat, prev_cyr, "manual", now_iso)
            else:
                warnings.append(f"{brand} {model_name}: новая, keyword_cyr=NULL — вписать вручную")
                rows_by_key[key] = _full_row(brand, model_name, kw_lat, None, "manual", now_iso)
        time.sleep(PAUSE_BETWEEN_BRANDS)

    # глобально дедуплицированный список (баг 409 закрыт)
    upsert_rows = list(rows_by_key.values())

    # модели, пропавшие из каталога -> деактивируем
    for (brand, model_name), row in existing.items():
        if row.get("active") and (brand, model_name) not in seen_now and brand in BRANDS:
            print(f"   - пропала из каталога: {brand} {model_name} -> active=false")
            deactivate(brand, model_name)

    inserted, err = upsert_models(upsert_rows)
    if err:
        print(f"\n⚠ Ошибка записи: {err}")
        sys.exit(1)

    print(f"\n✓ Готово: обновлено {inserted} моделей.")
    if warnings:
        print(f"⚠ Предупреждений: {len(warnings)}")
        for w in warnings:
            print("   -", w)


if __name__ == "__main__":
    main()
