"""
PERXIS Stock Extractor через Playwright (Haval / Haval Pro / Geely / Belgee)
============================================================================
Запуск: python perxis_playwright_extractor.py

Что делает:
1. Через Playwright (headless-Chrome) открывает сайты Haval, Geely, Belgee
2. На каждом сайте выполняет JS-код, который использует уже загруженный
   на странице Perxis-клиент (window.instockWidget.perxisService._itemsClient)
   для запроса всех машин по всей России (без фильтра по городу)
3. Получает JSON, нормализует, льёт в stock_staging и вызывает apply_stock_snapshot (stock_cars)

Преимущество перед сырым gRPC-Web на Python:
- НЕ нужно реверсить protobuf-схему
- НЕ нужно угадывать поля limit/offset
- Использует ту же библиотеку, что и сам сайт → всё работает

Требования:
  pip install playwright requests python-dotenv
  playwright install chromium
"""

import csv
import json
import os
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

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ─── CONFIG ──────────────────────────────────────────────────────────────────
CONFIG = {
    # Бренды, которые парсим за один запуск.
    "brands_to_run": ["haval_combo", "geely", "belgee", "tank", "wey"],

    "brands": {
        "haval_combo": {
            "url": "https://haval.ru/online-stock/",
            "space_id": "cof2bt8beucc73e9g9ng",
            "split": {
                "haval":     {"M6", "JOLION", "DARGO", "DARGO X", "F7", "F7X", "POER"},
                "haval_pro": {"H3", "H5", "H7", "H9"},
            },
        },
        "geely": {
            "url": "https://www.geely-motors.com/cars-stock/",
            "space_id": "cmodn3anifss73b1p3ug",
            "brand_key": "geely",
            "alt_space_id": "d0rahr8beucc73aj6v3g",  # новый space с 24.06.2026
            "alt_env_id":   "d0rahrobeucc73aj6v40",
        },
        "belgee": {
            "url": "https://belgee.ru/cars-stock/",
            "space_id": "com9pcgbeucc7385megg",
            "brand_key": "belgee",
        },
        "tank": {
            "url": "https://tank.ru/cars/",
            "space_id": "d604ft8beucc73c5uv7g",
            "brand_key": "tank",
        },
        "wey": {
            "url": "https://gwm-wey.ru/online-stock/",
            "space_id": "d606848beucc73c6qm40",
            "brand_key": "wey",
        },
    },

    "page_load_timeout_ms":   60000,
    "widget_wait_timeout_ms": 60000,
    "js_exec_timeout_ms":     900000,  # 15 минут — для возможно больших коллекций

    "output_dir": ".",
    "headless": True,
}


# ─── JS-парсер, который Playwright выполнит на странице ─────────────────────
JS_EXTRACTOR = r"""
async ([spaceId, apiKey, altSpaceId, altEnvId]) => {
  const ENV_ID = 'master';
  const API_KEY = apiKey || 'yOhXS74DhPd5L2fEdUVmUPDRimporter';
  const COLLECTION = 'vehicles_vehicles';
  const PAGE = 1000;          // размер страницы
  const MAX_PAGES = 200;      // защита от бесконечного цикла (200 * 1000 = 200 000 машин max)

  const start = Date.now();
  while (!window.instockWidget?.perxisService?._itemsClient) {
    if (Date.now() - start > 60000) {
      return { error: 'instockWidget не появился за 60 секунд' };
    }
    await new Promise(r => setTimeout(r, 500));
  }

  const client = window.instockWidget.perxisService._itemsClient;
  const meta = { 'x-api-key': API_KEY };

  // БЕЗ фильтра — берём ВСЕ машины коллекции
  const allCars = [];
  let total = null;
  let offset = 0;

  for (let page = 1; page <= MAX_PAGES; page++) {
    let resp;
    try {
      resp = await client.findPublished({
        spaceId,
        envId: ENV_ID,
        collectionId: COLLECTION,
        options: { options: { limit: PAGE, offset, sort: ['price'] } },
      }, meta);
    } catch (e) {
      return { error: 'Запрос page ' + page + ': ' + (e.message || e), total, got: allCars.length };
    }

    const items = resp.items || [];
    if (total === null) total = resp.total;
    console.log('page ' + page + ': offset=' + offset + ' got=' + items.length + ' total=' + total);

    if (items.length === 0) break;
    allCars.push(...items);

    // Останавливаемся только когда API явно сказал что больше нечего отдавать
    if (items.length < PAGE) break;
    if (total && allCars.length >= total) break;
    offset += items.length;
  }

  if (allCars.length === 0) {
    return { error: 'Пустой ответ', total };
  }

  // Дедупликация по id (на случай если страницы пересеклись)
  const seen = new Set();
  const cars = [];
  for (const c of allCars) {
    if (c.id && !seen.has(c.id)) {
      seen.add(c.id);
      cars.push(c);
    }
  }

  // Собираем уникальные ID для справочников
  const ids = {
    model: new Set(), engine: new Set(), drivetrain: new Set(),
    gearbox: new Set(), exterior: new Set(), version: new Set(),
    dealership: new Set(), city: new Set(), benefit: new Set(),
  };

  for (const car of cars) {
    const d = car.data;
    if (d.model?.id) ids.model.add(d.model.id);
    if (d.engine?.id) ids.engine.add(d.engine.id);
    if (d.drivetrain?.id) ids.drivetrain.add(d.drivetrain.id);
    if (d.gearbox?.id) ids.gearbox.add(d.gearbox.id);
    if (d.exterior?.id) ids.exterior.add(d.exterior.id);
    if (d.version?.id) ids.version.add(d.version.id);
    for (const loc of (d.locations || [])) {
      if (loc.collection_id === 'dealers_dealerships') ids.dealership.add(loc.id);
      if (loc.collection_id === 'dealers_cities') ids.city.add(loc.id);
    }
    // maxBenefit — массив ссылок на максимальную выгоду
    if (Array.isArray(d.maxBenefit)) {
      for (const b of d.maxBenefit) {
        if (b?.id) ids.benefit.add(b.id);
      }
    } else if (d.maxBenefit?.id) {
      ids.benefit.add(d.maxBenefit.id);
    }
    // benefits — массив ВСЕХ применимых программ выгод
    if (Array.isArray(d.benefits)) {
      for (const b of d.benefits) {
        if (b?.id) ids.benefit.add(b.id);
      }
    }
  }

  // Грузим справочники чанками по 100 ID
  async function fetchRefChunked(collectionId, idSet) {
    if (!idSet.size) return {};
    const allIds = [...idSet];
    const chunks = [];
    for (let i = 0; i < allIds.length; i += 100) {
      chunks.push(allIds.slice(i, i + 100));
    }
    const map = {};
    for (const chunk of chunks) {
      const idList = chunk.map(id => "'" + id + "'").join(',');
      try {
        const resp = await client.findPublished({
          spaceId, envId: ENV_ID, collectionId,
          filter: { q: ['id in [' + idList + ']'] },
          options: { options: { limit: 200 } },
        }, meta);
        for (const item of (resp.items || [])) map[item.id] = item.data;
      } catch (e) {
        console.error('Ошибка справочника ' + collectionId + ': ' + e.message);
      }
    }
    return map;
  }


  // fetchRefHybrid — 3-этапный поиск:
  // 1. findPublished в основном space
  // 2. client.get() для не найденных
  // 3. client.get() в altSpaceId (для Geely: новый space с 24.06.2026)
  async function fetchRefHybrid(collectionId, idSet) {
    if (!idSet.size) return {};
    const map = await fetchRefChunked(collectionId, idSet);
    const missing = [...idSet].filter(id => !(id in map));
    if (!missing.length) return map;
    const BATCH = 10;
    // Шаг 2: client.get() в основном space
    for (let i = 0; i < missing.length; i += BATCH) {
      const batch = missing.slice(i, i + BATCH);
      const results = await Promise.all(
        batch.map(id =>
          client.get({ spaceId, envId: ENV_ID, collectionId, itemId: id }, meta)
            .catch(() => null)
        )
      );
      for (const r of results) { if (r?.item) map[r.item.id] = r.item.data; }
    }
    // Шаг 3: alt space (Geely → новый SpaceID)
    if (altSpaceId) {
      const stillMissing = [...idSet].filter(id => !(id in map));
      for (let i = 0; i < stillMissing.length; i += BATCH) {
        const batch = stillMissing.slice(i, i + BATCH);
        const results = await Promise.all(
          batch.map(id =>
            client.get({ spaceId: altSpaceId, envId: altEnvId || ENV_ID, collectionId, itemId: id }, meta)
              .catch(() => null)
          )
        );
        for (const r of results) { if (r?.item) map[r.item.id] = r.item.data; }
      }
    }
    return map;
  }

  const [models, engines, drivetrains, gearboxes, exteriors, versions,
         dealerships, cities, benefits] = await Promise.all([
    fetchRefChunked('vehicles_models', ids.model),
    fetchRefChunked('vehicles_engines', ids.engine),
    fetchRefChunked('vehicles_drivetrains', ids.drivetrain),
    fetchRefChunked('vehicles_gearboxes', ids.gearbox),
    fetchRefChunked('vehicles_exteriors', ids.exterior),
    fetchRefHybrid('vehicles_versions', ids.version),   // + alt space для Geely
    fetchRefChunked('dealers_dealerships', ids.dealership),
    fetchRefChunked('dealers_cities', ids.city),
    fetchRefChunked('vehicles_benefits', ids.benefit),
  ]);

  const result = [];
  for (const car of cars) {
    const d = car.data;
    const dealerLoc = (d.locations || []).find(l => l.collection_id === 'dealers_dealerships');
    const cityLoc = (d.locations || []).find(l => l.collection_id === 'dealers_cities');
    const year = d.productionDate ? new Date(d.productionDate).getFullYear() : null;
    const dealerData = dealerLoc ? dealerships[dealerLoc.id] : null;

    // Максимальная выгода (одна, лучшая)
    let maxBenefitValue = null;
    const maxBenefitRefs = Array.isArray(d.maxBenefit) ? d.maxBenefit
                          : (d.maxBenefit?.id ? [d.maxBenefit] : []);
    for (const ref of maxBenefitRefs) {
      const b = benefits[ref.id];
      if (b && typeof b.value === 'number') {
        if (maxBenefitValue === null || b.value > maxBenefitValue) {
          maxBenefitValue = b.value;
        }
      }
    }

    // Сумма всех применимых выгод
    let benefitsTotal = null;
    if (Array.isArray(d.benefits) && d.benefits.length > 0) {
      benefitsTotal = 0;
      for (const ref of d.benefits) {
        const b = benefits[ref.id];
        if (b && typeof b.value === 'number') benefitsTotal += b.value;
      }
      if (benefitsTotal === 0) benefitsTotal = null;
    }

    result.push({
      car_id:         car.id,
      vin:            d.vin || null,
      sku:            d.sku || null,
      model:          models[d.model?.id]?.name || null,
      engine:         engines[d.engine?.id]?.name || null,
      gearbox:        gearboxes[d.gearbox?.id]?.alternateName || null,
      drivetrain:     drivetrains[d.drivetrain?.id]?.alternateName || null,
      exterior:       exteriors[d.exterior?.id]?.name || null,
      version:        versions[d.version?.id]?.alternateName || null,
      type:           d.type || null,
      condition:      d.condition || null,
      availability:   d.availability || null,
      production_date: d.productionDate ? d.productionDate.slice(0, 10) : null,
      year:           year,
      price:          d.price || null,
      min_price:      d.minPrice || null,
      max_benefit:    maxBenefitValue,
      benefits_total: benefitsTotal,
      dealer_name:    dealerData?.name || null,
      dealer_address: dealerData?.address || null,
      city:           cityLoc ? (cities[cityLoc.id]?.name || null) : null,
    });
  }

  return { ok: true, cars: result, total, pages_loaded: Math.ceil(allCars.length / PAGE) };
}
"""


# ─── ТРАНСЛИТЕРАЦИЯ ──────────────────────────────────────────────────────────
def transliterate_city(name):
    if not name:
        return None
    table = {
        "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e",
        "ж":"zh","з":"z","и":"i","й":"y","к":"k","л":"l","м":"m",
        "н":"n","о":"o","п":"p","р":"r","с":"s","т":"t","у":"u",
        "ф":"f","х":"kh","ц":"ts","ч":"ch","ш":"sh","щ":"shch",
        "ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya",
    }
    out = []
    for ch in name.lower():
        if ch in table:
            out.append(table[ch])
        elif ch.isalnum():
            out.append(ch)
        elif ch in " -":
            out.append("-")
    s = "".join(out)
    while "--" in s:
        s = s.replace("--", "-")
    return s.strip("-")


# ─── SUPABASE ROW ────────────────────────────────────────────────────────────
def car_to_supabase_row(car, brand_key):
    return {
        "brand":              brand_key,
        "car_id":             str(car.get("car_id") or ""),
        "vin":                car.get("vin"),
        "vin_full":           car.get("vin"),
        "year":               car.get("year"),
        "prod_date":          car.get("production_date"),
        "published_at":       None,
        "days_on_stock":      None,
        "status":             car.get("availability"),
        "model":              car.get("model") or "",
        "model_alias":        None,
        "complectation":      car.get("version"),
        "complectation_code": None,
        "engine_volume":      None,
        "engine_power":       None,
        "transmission_type":  car.get("gearbox"),
        "drive_type":         car.get("drivetrain"),
        "body_type":          None,
        "color":              car.get("exterior"),
        "price_base":         int(car["price"]) if car.get("price") else None,
        "price_special":      int(car["min_price"]) if car.get("min_price") else None,
        "price_tradein":      None,
        "discount_tradein":   None,
        "discount_credit":    None,
        "discount_equipment": None,
        "discount_color":     None,
        "discount_insurance": None,
        "discount_max":       int(car["benefits_total"]) if car.get("benefits_total") else None,
        "dealer_id":          None,
        "dealer_name":        car.get("dealer_name"),
        "dealer_address":     car.get("dealer_address"),
        "dealer_city":        car.get("city"),
        "dealer_city_alias":  transliterate_city(car.get("city")),
        "dealer_lat":         None,
        "dealer_lon":         None,
        "dealer_phone":       None,
        "parallel_import":    False,
        "mileage_km":         None,
        "is_used":            car.get("condition") != "excellent" if car.get("condition") else False,
        "engine":             car.get("engine") or None,
    }


# ─── SUPABASE HTTP ──────────────────────────────────────────────────────────
def supabase_req(method, url, key, **kwargs):
    headers = kwargs.pop("headers", {})
    headers.update({
        "apikey": key, "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
    })
    return requests.request(method, url, headers=headers, timeout=60, **kwargs)


def supabase_run_start(sb_url, key, brand_key):
    rid = str(uuid.uuid4())
    url = sb_url.rstrip("/") + "/rest/v1/parsing_runs"
    payload = {
        "run_id": rid, "brand": brand_key,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
    }
    try:
        r = supabase_req("POST", url, key, json=payload,
                         headers={"Prefer": "return=minimal"})
        if r.status_code in (200, 201, 204):
            return rid
    except Exception:
        pass
    return None


def supabase_run_finish(sb_url, key, rid, status, rows_inserted, rows_total,
                       duration_sec, error_message=None):
    if not rid:
        return
    url = sb_url.rstrip("/") + "/rest/v1/parsing_runs?run_id=eq." + rid
    payload = {
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "status": status, "rows_inserted": rows_inserted,
        "rows_total": rows_total, "duration_sec": duration_sec,
    }
    if error_message:
        payload["error_message"] = error_message[:2000]
    try:
        supabase_req("PATCH", url, key, json=payload,
                     headers={"Prefer": "return=minimal"})
    except Exception:
        pass


def upload_to_supabase(rows, sb_url, key, brand_key, batch_size=200):
    """Льёт срез бренда в stock_staging, затем вызывает apply_stock_snapshot(brand_key, date).
    rows — уже готовые строки (без snapshot_date). Возвращает (result_dict_or_None, err_or_None).
    """
    snapshot_date = datetime.now().strftime("%Y-%m-%d")
    staging_url = sb_url.rstrip("/") + "/rest/v1/stock_staging"

    for row in rows:
        row["snapshot_date"] = snapshot_date

    seen = {}
    for row in rows:
        seen[(row["brand"], row["car_id"])] = row
    dups = len(rows) - len(seen)
    if dups:
        print("   ⚠ Дублей по car_id: {} (удалено)".format(dups))
    rows = list(seen.values())

    total = len(rows)
    print("   [a] Заливаю {} строк в stock_staging батчами по {} ...".format(total, batch_size))

    try:
        supabase_req("DELETE", staging_url + "?brand=eq." + brand_key, key,
                     headers={"Prefer": "return=minimal"})
    except Exception as e:
        print("   ⚠ Не удалось очистить staging заранее: {}: {}".format(type(e).__name__, e))

    for i in range(0, total, batch_size):
        batch = rows[i:i+batch_size]
        try:
            r = supabase_req(
                "POST", staging_url, key, json=batch,
                headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
            if r.status_code in (200, 201, 204):
                print("      батч {:4d}-{:4d}: OK".format(i+1, i+len(batch)))
            else:
                return None, "staging HTTP {}: {}".format(r.status_code, r.text[:300])
        except Exception as e:
            return None, "{}: {}".format(type(e).__name__, e)

    print("   [b] Вызываю apply_stock_snapshot('{}', '{}') ...".format(brand_key, snapshot_date))
    rpc_url = sb_url.rstrip("/") + "/rest/v1/rpc/apply_stock_snapshot"
    try:
        r = supabase_req("POST", rpc_url, key, json={
            "p_brand": brand_key, "p_snapshot_date": snapshot_date})
        if r.status_code not in (200, 201, 204):
            return None, "rpc HTTP {}: {}".format(r.status_code, r.text[:300])
        return (r.json() if r.text else {}), None
    except Exception as e:
        return None, "rpc {}: {}".format(type(e).__name__, e)


# ─── ОСНОВНОЕ ───────────────────────────────────────────────────────────────
def extract_brand_via_browser(brand, browser):
    settings = CONFIG["brands"][brand]
    url = settings["url"]
    space_id = settings["space_id"]
    api_key = os.environ.get("PERXIS_API_KEY", "yOhXS74DhPd5L2fEdUVmUPDRimporter")

    print("\n" + "=" * 60)
    print("Бренд: {}".format(brand.upper()))
    print("URL:   {}".format(url))
    print("Space: {}".format(space_id))
    print("=" * 60)

    context = browser.new_context(
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        viewport={"width": 1280, "height": 800},
    )
    page = context.new_page()
    # Увеличиваем общий таймаут страницы под долгий JS
    page.set_default_timeout(CONFIG["js_exec_timeout_ms"])

    try:
        print("   → Открываю страницу ...")
        page.goto(url, timeout=CONFIG["page_load_timeout_ms"], wait_until="domcontentloaded")
        print("   → Жду инициализации Perxis виджета ...")
        page.wait_for_timeout(5000)

        print("   → Выполняю JS-парсер (может занять до 5 минут) ...")
        alt_space_id = settings.get("alt_space_id", "")
        alt_env_id   = settings.get("alt_env_id", "")
        result = page.evaluate(JS_EXTRACTOR, [space_id, api_key, alt_space_id, alt_env_id])

        if not result or result.get("error"):
            err = result.get("error") if result else "пустой результат"
            print("   ✗ Ошибка JS-парсера: {}".format(err))
            return None

        cars = result.get("cars", [])
        total = result.get("total", "?")
        pages = result.get("pages_loaded", "?")
        print("   ✓ Получено {} машин (total в API: {}, страниц: {})".format(
            len(cars), total, pages))
        return cars

    except PlaywrightTimeoutError as e:
        print("   ✗ Таймаут: {}".format(e))
        return None
    except Exception as e:
        print("   ✗ Ошибка: {}: {}".format(type(e).__name__, e))
        return None
    finally:
        try:
            context.close()
        except Exception:
            pass


def process_brand(brand, browser, sb_url, sb_key):
    started = time.time()
    settings = CONFIG["brands"][brand]

    cars = extract_brand_via_browser(brand, browser)
    if not cars:
        return {brand: False}

    if "split" in settings:
        buckets = {key: [] for key in settings["split"]}
        unassigned = []
        for c in cars:
            model = (c.get("model") or "").upper().strip()
            matched = False
            for bucket_name, model_set in settings["split"].items():
                if model in model_set:
                    buckets[bucket_name].append(c)
                    matched = True
                    break
            if not matched:
                unassigned.append(c)
        if unassigned:
            print("   ⚠ Не классифицировано: {} машин".format(len(unassigned)))
            print("      Модели: {}".format(set((c.get("model") or "?") for c in unassigned)))
        for bn, lst in buckets.items():
            print("   → {}: {} машин".format(bn, len(lst)))
    else:
        bk = settings["brand_key"]
        buckets = {bk: cars}

    today = datetime.now().strftime("%Y-%m-%d")
    results = {}

    for bucket_brand, bucket_cars in buckets.items():
        if not bucket_cars:
            results[bucket_brand] = True
            continue

        csv_path = Path(CONFIG["output_dir"]) / "{}_rf_stock_{}.csv".format(bucket_brand, today)
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            keys = list(bucket_cars[0].keys())
            w.writerow(keys)
            for c in bucket_cars:
                w.writerow([c.get(k, "") for k in keys])
        print("\n   ✓ CSV {}: {} ({} строк)".format(bucket_brand, csv_path, len(bucket_cars)))

        if sb_url and sb_key:
            run_id = supabase_run_start(sb_url, sb_key, bucket_brand)
            rows = [car_to_supabase_row(c, bucket_brand) for c in bucket_cars]
            try:
                result, err = upload_to_supabase(rows, sb_url, sb_key, bucket_brand)
                duration = int(time.time() - started)
                if err is None:
                    arrived = result.get("arrived", 0)
                    updated = result.get("updated", 0)
                    removed = result.get("removed", 0)
                    removal_done = result.get("removal_done", True)
                    note = result.get("note", "")
                    supabase_run_finish(sb_url, sb_key, run_id, "success",
                                       arrived + updated, len(bucket_cars), duration,
                                       note or None)
                    print("   ✓ {}: stock_cars — приход {}, обновлено {}, выбытие {}".format(
                        bucket_brand.upper(), arrived, updated, removed))
                    if not removal_done:
                        print("      ⚠ ВЫБЫТИЕ ПРОПУЩЕНО: {}".format(note))
                    results[bucket_brand] = True
                else:
                    supabase_run_finish(sb_url, sb_key, run_id, "failed",
                                       0, len(bucket_cars), duration, err)
                    print("   ⚠ {}: загрузка не удалась: {}".format(bucket_brand.upper(), err))
                    results[bucket_brand] = False
            except Exception as e:
                duration = int(time.time() - started)
                supabase_run_finish(sb_url, sb_key, run_id, "failed",
                                   0, len(bucket_cars), duration,
                                   "{}: {}".format(type(e).__name__, e))
                print("   ✗ {}: упало: {}".format(bucket_brand.upper(), e))
                results[bucket_brand] = False
        else:
            results[bucket_brand] = True

    return results


def main():
    sb_url = os.environ.get("SUPABASE_URL")
    sb_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

    if not (sb_url and sb_key):
        print("ℹ Supabase не настроен — будет CSV-only режим")

    brands_to_run = CONFIG["brands_to_run"]
    print("=" * 60)
    print("Playwright-парсер Perxis: {}".format(", ".join(brands_to_run)))
    print("=" * 60)

    all_results = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=CONFIG["headless"])
        try:
            for brand in brands_to_run:
                try:
                    res = process_brand(brand, browser, sb_url, sb_key)
                    all_results.update(res)
                except Exception as e:
                    print("\n✗ Критическая ошибка '{}': {}: {}".format(
                        brand, type(e).__name__, e))
                    all_results[brand] = False
        finally:
            browser.close()

    print("\n" + "=" * 60)
    print("ИТОГИ:")
    for brand, ok in all_results.items():
        marker = "✓" if ok else "✗"
        print("   {} {}".format(marker, brand.upper()))
    print("=" * 60)

    return 0 if all(all_results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
