# ДепозитоПомогатор — Деплой на Render

## Шаг 1: Создай GitHub репозиторий

```bash
# Создай новый приватный репо на GitHub, потом:
cd dp-render
git init
git add .
git commit -m "init DepositoPomogator"
git remote add origin git@github.com:ТВОЙ_ЮЗЕР/depositopomogator.git
git push -u origin main
```

## Шаг 2: Создай Web Service на Render

1. Иди на https://dashboard.render.com
2. **New** → **Web Service**
3. Подключи GitHub → выбери репо `depositopomogator`
4. Настройки:

| Поле | Значение |
|------|----------|
| Name | `depositopomogator` |
| Region | `Frankfurt (EU Central)` ← ближе к Киеву |
| Branch | `main` |
| Runtime | `Python` |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `gunicorn -w 2 -b 0.0.0.0:$PORT server:app --timeout 120` |
| Instance Type | `Free` (для старта) или `Starter $7/mo` (для persistent disk) |

## Шаг 3: Environment Variables

На странице сервиса → **Environment** → добавь:

| Key | Value |
|-----|-------|
| `DP_API_TOKEN` | *(нажми Generate)* — запиши его, он нужен для дашборда |
| `DP_CORS_ORIGINS` | `*` *(потом ограничь до своих доменов)* |
| `DP_SESSIONS_DIR` | `./sessions` |
| `DP_RATE_LIMIT` | `120` |
| `PYTHON_VERSION` | `3.11.0` |

## Шаг 4: Деплой

Render начнёт билдить автоматически. Подожди 1-2 минуты.

После деплоя ты получишь URL типа:
```
https://depositopomogator.onrender.com
```

## Шаг 5: Проверь

```bash
# Health check
curl https://depositopomogator.onrender.com/api/health

# Ответ:
# {"auth_enabled":true,"sessions":0,"status":"ok","uptime":...}

# Проверь что tracker.js отдаётся
curl -I https://depositopomogator.onrender.com/tracker.js
# Должен быть 200 OK с Content-Type: application/javascript
```

## Шаг 6: Вставь трекер в ленды

В Keitaro → настройки лендинга → Scripts → Before `</body>`:

```html
<script src="https://depositopomogator.onrender.com/tracker.js"
        data-endpoint="https://depositopomogator.onrender.com/api/track"></script>
```

Или если используешь свой домен для скрипта (рекомендую — менее палевно):

```html
<script>window.DP_ENDPOINT="https://depositopomogator.onrender.com/api/track";</script>
<script src="https://depositopomogator.onrender.com/tracker.js"></script>
```

## Шаг 7: Подключи дашборд

В дашборде (React артифакт) просто открой и смотри демо данные.
Для live данных — нужно добавить fetch к API. Скажи когда будешь готов, добавлю.

---

## Custom домен (опционально)

Чтобы не палить onrender.com в скриптах лендов:

1. Render → Settings → Custom Domain → добавь `analytics.твой-домен.com`
2. В DNS добавь CNAME: `analytics.твой-домен.com → depositopomogator.onrender.com`
3. Render автоматически выпустит SSL
4. Обнови URL в скриптах лендов

---

## Важно: ограничения Free Tier

- **Диск не персистентный** — сессии сотрутся при каждом redeploy
- **Засыпает через 15 мин без трафика** — первый запрос после сна ~30с
- **750 часов/месяц** — хватает на 1 сервис 24/7

Для продакшна: Starter план ($7/mo) — не засыпает + persistent disk.

---

## Структура файлов

```
dp-render/
├── server.py          # Flask бэкенд
├── tracker.js         # JS трекер для лендов
├── requirements.txt   # Python зависимости
├── render.yaml        # Render blueprint (auto-config)
├── .gitignore
└── sessions/          # Данные сессий (создаётся автоматически)
    └── .gitkeep
```

## API endpoints

```
POST /api/track         — приём данных (без auth)
GET  /tracker.js        — отдача JS трекера (без auth)
GET  /api/health        — health check (без auth)
GET  /api/offers        — список офферов (auth)
GET  /api/sessions      — сессии (auth)
GET  /api/sessions/<id> — одна сессия (auth)
GET  /api/heatmap       — хитмапа (auth)
GET  /api/elements      — элементы (auth)
GET  /api/forms         — форм фаннел (auth)
GET  /api/stats         — общая статистика (auth)
POST /api/cleanup       — очистка старых (auth)
```

Auth: заголовок `Authorization: Bearer ТВОЙ_DP_API_TOKEN`

## Кастомные события на ленде

```javascript
// После загрузки трекера:
DepositoPomogator.track('video_play', { videoId: 'intro' });
DepositoPomogator.track('calculator_used', { amount: 2500 });
DepositoPomogator.track('form_step', { step: 2 });
```
