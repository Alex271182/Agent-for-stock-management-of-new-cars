# Stock Parsing Agent

Автоматический ежедневный сбор данных по складам конкурентов в Supabase + алерты в Telegram при отклонениях.

## Что делает

**07:00 МСК** — запускает 3 парсера:
- `jetour_stock_extractor.py` — Jetour (TradeDealer API)
- `changan_uni_stock_extractor.py` — Changan + Uni
- `perxis_playwright_extractor.py` — Haval + Haval Pro + Geely + Belgee

Все данные грузятся в Supabase: таблицы `stock_snapshots` + журнал в `parsing_runs`.

**08:00 МСК** — запускает валидатор `validator.py`, который проверяет:

1. Парсер вообще запустился сегодня (есть запись в `parsing_runs`).
2. Парсер завершился со `status = 'success'`.
3. `rows_inserted` vs `rows_total` — расхождение ≤ 2%.
4. Сегодня vs вчера по бренду — падение ≤ 30%, рост ≤ 50%.
5. Доля записей с пустыми ключевыми полями (`model` / `price_base` / `dealer_name`) ≤ 5%.

Если что-то не так — Telegram-алерт.

---

## Deploy на GitHub Actions

### 1. Создай приватный репозиторий

В GitHub → New repository → Private. Например: `dealership-stock-parsers`.

### 2. Залей файлы

Структура должна быть такая:
```
.
├── .github/
│   └── workflows/
│       └── daily-parse.yml
├── .gitignore
├── README.md
├── requirements.txt
├── telegram_alert.py
├── validator.py
├── jetour_stock_extractor.py
├── changan_uni_stock_extractor.py
└── perxis_playwright_extractor.py
```

Через веб-интерфейс GitHub: на странице репо → `Add file → Upload files` → drag-and-drop всех файлов. Файлы внутри `.github/workflows/` загружай отдельно (создав папку вручную).

### 3. Настрой secrets

В репозитории: **Settings → Secrets and variables → Actions → New repository secret**.

Добавь 4 секрета:

| Имя | Что это |
|---|---|
| `SUPABASE_URL` | URL твоего Supabase-проекта (например `https://qoggtkcnxfvqriwrhngj.supabase.co`) |
| `SUPABASE_SERVICE_ROLE_KEY` | Service Role ключ (Settings → API → service_role secret) |
| `TELEGRAM_BOT_TOKEN` | Токен от @BotFather (см. инструкцию ниже) |
| `TELEGRAM_CHAT_ID` | ID твоего чата (см. инструкцию ниже) |

### 4. Создай Telegram-бота

*(Инструкция в отдельном файле / в следующей сессии. Пока можешь добавить пустые значения — алерты работать не будут, но парсеры пойдут.)*

### 5. Проверь — запусти вручную

Перейди: **Actions → Daily Stock Parse → Run workflow → all → Run**.

Зелёная галочка = работает. Красный крест = смотри логи.

---

## Расписание

| Время МСК | Что | Cron UTC |
|---|---|---|
| 07:00 | Парсеры | `0 4 * * *` |
| 08:00 | Валидатор | `0 5 * * *` |

⚠️ **GitHub Actions может задерживать scheduled-задачи на 0–60 минут** при пиковой нагрузке. Это нормально. Валидатор стартует через час после парсеров — этого запаса хватает.

---

## Что НЕ автоматизировано

- Починка парсеров при поломке API дистрибьютера (ручной разбор).
- Включение RLS в Supabase (отдельная задача безопасности).
- WordStat-парсер (требует авторизации, GitHub Actions не подходит).

---

## Локальный запуск (если нужно отладить)

```bash
# Установка зависимостей
pip install -r requirements.txt
playwright install chromium

# Локальный .env (НЕ коммитить!)
cat > .env <<EOF
SUPABASE_URL=...
SUPABASE_SERVICE_ROLE_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
EOF

# Запуск
python jetour_stock_extractor.py
python validator.py
```
