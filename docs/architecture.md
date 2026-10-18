# Архитектура Agent Platform

## Компоненты

- **FastAPI** — REST API (JWT), WebSocket-стриминг логов, запуск оркестратора.
- **aiogram 3.x Bot** — приём задач, подтверждения опасных команд (human-in-the-loop).
- **Orchestrator** — планирование через LLM (Kimi), постановка шагов в очередь arq.
- **arq Workers** — выполнение шагов в изолированных Docker-песочницах, публикация событий.
- **Redis 7** — очередь задач (arq) и pub/sub канал событий `agent.events.task.*`.
- **PostgreSQL 16** — пользователи, задачи, шаги, аппрувы, события, артефакты.
- **Docker Sandbox** — контейнер на задачу: `network=none`, лимиты CPU/RAM/PIDs, read-only rootfs, примонтированный workspace.
- **Artifacts storage** — локальный volume (по умолчанию) или MinIO/S3.

## Диаграмма

```mermaid
flowchart LR
    subgraph Clients
        U1[Telegram User]
        U2[Web User]
    end

    subgraph Edge
        FE["Next.js 14 Dashboard<br/>WebSocket live logs"]
        BOT["aiogram Bot<br/>whitelist by Telegram ID"]
    end

    subgraph Backend
        REST["FastAPI REST<br/>JWT auth"]
        WS["WebSocket<br/>/api/ws/tasks/id"]
        ORCH["Orchestrator<br/>Planner + Scheduler"]
    end

    LLM["Kimi LLM<br/>OpenAI-compatible API"]

    subgraph Queue[Redis 7]
        ARQ[(arq job queue)]
        PUB[(pub/sub agent.events.task.*)]
    end

    subgraph Workers
        W[arq worker pool]
    end

    subgraph Sandbox[Isolated Docker sandbox - one per task]
        SC["Container<br/>network=none<br/>cpu/mem/pids limits<br/>read-only rootfs<br/>workspace volume"]
    end

    PG[(PostgreSQL 16)]
    FS[(Artifacts storage<br/>local volume / MinIO)]

    U1 --> BOT
    U2 --> FE
    FE --> REST
    FE --> WS
    BOT --> ORCH
    REST --> ORCH
    ORCH --> LLM
    ORCH --> PG
    ORCH --> ARQ
    ARQ --> W
    W --> SC
    W --> PG
    W --> FS
    W --> PUB
    PUB --> WS
    PUB --> BOT
```

## Поток выполнения задачи

1. Пользователь создаёт задачу (Telegram `/new_task` или `POST /api/tasks`).
2. Оркестратор вызывает LLM, получает JSON-план и сохраняет шаги в PostgreSQL.
3. Scheduler ставит job планирования / шаги в очередь arq (Redis).
4. Воркер поднимает песочницу задачи (или переиспользует), выполняет инструмент.
5. `run_shell` с опасной командой → создаётся approval, бот спрашивает подтверждение.
6. События публикуются в Redis pub/sub → WebSocket (Web) и бот (Telegram).
7. `finish_task` → задача завершена, артефакты собраны и отданы пользователю.

## Принятые решения

- **arq** вместо Celery: нативный asyncio-стек, меньше движущихся частей, те же Redis-очереди.
- **Один Docker-контейнер на задачу**, переиспользуется между шагами (shared workspace), удаляется по завершении/таймауту.
- **Mirrored path** для `/data/workspaces`: воркер и sandbox видят один и тот же абсолютный путь, поэтому bind-mount подкаталогов задачи работает через Docker socket.
- **События** пишутся и в PostgreSQL (история), и в Redis pub/sub (real-time).
- **Изоляция**: `network_mode=none`, `cap_drop=ALL`, `no-new-privileges`, лимиты памяти/CPU/PIDs; `http_request` выполняется воркером с whitelist хостов.
