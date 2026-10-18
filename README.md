# Автономная агент-платформа

Платформа, в которой ИИ-агент (Kimi) планирует и выполняет задачи в изолированных
Docker-песочницах, а пользователь управляет ими через веб-дашборд и Telegram-бота:
подтверждает опасные команды, читает журнал событий в реальном времени и забирает
артефакты (файлы, созданные агентом).

## Возможности

- **Планирование задач через LLM** — задача разбивается на шаги с валидацией
  инструментов и путей (`planner.py`, `schemas/plan.py`).
- **Безопасное исполнение** — каждый шаг выполняется в одноразовом Docker-контейнере
  без сети, с read-only rootfs, лимитами CPU/RAM/PIDs и рабочей папкой задачи.
- **Подтверждение опасных команд** — шелл-команды, совпавшие с набором правил,
  приостанавливают задачу до решения пользователя (approve/reject) с TTL.
- **Артефакты** — файлы из рабочей папки собираются, хешируются (sha256) и доступны
  для скачивания из дашборда и отправки в Telegram.
- **Live-обновления** — события задачи публикуются в Redis pub/sub и транслируются
  в браузер по WebSocket; в Telegram приходят уведомления с кнопками подтверждения.
- **Роли и доступ** — JWT-авторизация в API, bootstrap-админ, whitelist Telegram-чатов.

## Архитектура

```mermaid
flowchart LR
  TG[Telegram bot] --> DB[(PostgreSQL)]
  WEB[Next.js dashboard] --> API[FastAPI]
  API --> DB
  API --> R[(Redis)]
  W[arq worker] --> R
  W --> DB
  W --> LLM[Kimi API]
  W --> SB[Docker sandboxes]
  SB --> W
  R -. events .-> API
  R -. events .-> TG
```

Подробное описание потоков, схемы БД и решений — в `docs/architecture.md`.

## Стек

- **Backend**: Python 3.11, FastAPI, SQLAlchemy 2.0 (async), Alembic, arq, aiogram 3,
  Docker SDK, openai (Kimi, OpenAI-совместимый API), PyJWT, bcrypt.
- **Frontend**: Next.js 14 (App Router), React 18, TypeScript, чистый CSS.
- **Инфраструктура**: PostgreSQL 16, Redis 7, Docker Compose.

## Структура репозитория

```
backend/
  app/
    api/          REST + WebSocket маршруты
    bot/          Telegram-бот: хендлеры, уведомления, точка входа
    core/         конфигурация, Redis, события, JWT/bcrypt
    db/           модели SQLAlchemy, async-сессии
    executor/     Docker-песочница, инструменты агента, сбор артефактов
    llm/          клиент Kimi (chat / structured JSON)
    orchestrator/ планировщик, очередь шагов, сервис задач, approvals
    schemas/      pydantic-схемы плана и API
    workers/      arq-воркеры (план, шаги, TTL approvals, очистка песочниц)
  alembic/        миграции
deploy/
  docker-compose.yml
  .env.example
docs/
  architecture.md
frontend/
  app/            страницы: логин, список задач, карточка задачи (WebSocket)
  lib/            API-клиент, WebSocket-клиент, типы, форматирование
```

## Быстрый старт

Требования: Docker 24+ с Compose v2.

1. Подготовьте окружение:

   ```bash
   cd deploy
   cp .env.example .env
   ```

2. Заполните в `deploy/.env` обязательные переменные:

   | Переменная | Назначение |
   |---|---|
   | `LLM_API_KEY` | ключ Kimi (Moonshot) |
   | `TELEGRAM_BOT_TOKEN` | токен бота от @BotFather (можно оставить пустым, если бот не нужен) |
   | `TELEGRAM_ALLOWED_USER_IDS` | id пользователей, которым разрешён бот |
   | `SECRET_KEY`, `JWT_SECRET_KEY` | длинные случайные строки |
   | `POSTGRES_PASSWORD` | пароль БД |
   | `BOOTSTRAP_ADMIN_EMAIL`, `BOOTSTRAP_ADMIN_PASSWORD` | учётка админа для входа в дашборд |

3. Соберите и запустите:

   ```bash
   docker compose up --build -d
   ```

4. Откройте `http://localhost:3000` и войдите под bootstrap-админом.
   Бот — опциональный сервис под профилем: `docker compose --profile telegram up -d`.
   Если `api.telegram.org` недоступен напрямую, задайте `TELEGRAM_PROXY`.

## Использование

**Веб-дашборд**

- Войдите по email/паролю из `BOOTSTRAP_ADMIN_*`.
- «Новая задача» — опишите задачу текстом; откроется карточка с планом.
- На карточке: статусы шагов, вывод инструментов, результат, артефакты,
  кнопки «Разрешить/Отклонить» для опасных команд и живой журнал событий.

**Telegram**

- `/new_task` — поставить задачу, `/tasks` — последние задачи,
  `/task <id>` — детали (id можно указать префиксом), `/cancel` — отмена ввода.
- Уведомления приходят на чат, из которого создана задача; подтверждения —
  inline-кнопками.
- Если провайдер блокирует `api.telegram.org`, укажите в `.env` прокси —
  `TELEGRAM_PROXY=socks5://user:pass@host:port` (или `http://...`), бот пойдёт
  через него. Сам сервис `bot` включается профилем `telegram`.

## Конфигурация

Базовый набор — в `deploy/.env.example`; ключевые группы (переменные с дефолтами в `backend/app/core/config.py`):

| Группа | Переменные |
|---|---|
| LLM | `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_TEMPERATURE`, `LLM_MAX_TOKENS`, `LLM_PLAN_MAX_ATTEMPTS` |
| Песочница | `SANDBOX_IMAGE`, `SANDBOX_NETWORK_MODE`, `SANDBOX_MEMORY_LIMIT_MB`, `SANDBOX_CPU_LIMIT`, `SANDBOX_TMPFS_SIZE_MB`, `SANDBOX_IDLE_TIMEOUT_SECONDS` |
| Инструменты | `SHELL_TIMEOUT_SECONDS`, `SHELL_APPROVAL_MODE` (`dangerous`/`all`/`never`), `HTTP_ALLOWED_HOSTS`, `MAX_STEPS_PER_TASK`, `STEP_MAX_ATTEMPTS` |
| Approvals | `APPROVAL_TTL_SECONDS`, `DANGEROUS_SHELL_PATTERNS` (regex-список) |
| Хранилище | `WORKSPACES_ROOT`, `ARTIFACTS_ROOT`, `ARTIFACT_MAX_BYTES` |
| Воркеры | `WORKER_MAX_JOBS`, `WORKER_JOB_TIMEOUT_SECONDS`, `WORKER_KEEP_RESULT_SECONDS` |
| Веб/боты | `WEB_BASE_URL`, `PUBLIC_API_URL`, `PUBLIC_WS_URL`, `TELEGRAM_POLL_TIMEOUT_SECONDS`, `TELEGRAM_PROXY` |

Для деплоя вне localhost поменяйте `PUBLIC_API_URL`/`PUBLIC_WS_URL` (адреса видны
из браузера) и `WEB_BASE_URL` (ссылки в Telegram).

## Безопасность

- Шаги выполняются в контейнерах `--network none`, без capabilities, с лимитами
  и read-only rootfs; рабочая папка — единственный writable mount.
- `run_shell`-команды по умолчанию проходят через правила `DANGEROUS_SHELL_PATTERNS`
  и требуют ручного подтверждения; `http_request` ограничен whitelist хостов.
- Доступ к API — JWT; бот — whitelist по Telegram id.
- **Внимание**: воркеру нужен доступ к `/var/run/docker.sock`, это root-эквивалент
  на хосте. В продакшене поставьте перед сокетом прокси
  (например, `tecnativa/docker-socket-proxy`) и пробросьте только нужные API.

## Разработка без Docker

Backend (нужны Python 3.11+, PostgreSQL, Redis; секреты — через `.env`):

```bash
cd backend
pip install -e ".[dev]"
alembic upgrade head            # или полагайтесь на create_all при старте
uvicorn app.main:app --reload   # API на :8000
arq app.workers.tasks.WorkerSettings
python -m app.bot.main
ruff check .
mypy app
```

Frontend:

```bash
cd frontend
npm install
npm run dev        # :3000
npm run typecheck
```

## Проверка после сборки

```bash
docker compose -f deploy/docker-compose.yml ps
curl http://localhost:8000/health
```

Схема БД создаётся автоматически при старте сервисов; для управляемых миграций
используйте Alembic (`backend/alembic`).
