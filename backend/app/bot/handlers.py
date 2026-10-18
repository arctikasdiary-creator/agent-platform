from __future__ import annotations

import html
import logging
import re
from typing import Any
from uuid import UUID

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
from sqlalchemy import String, cast, select

from app.core.config import get_settings
from app.db.models import Approval, ApprovalStatus, StepStatus, Task, TaskSource, TaskStatus
from app.db.session import SessionFactory
from app.orchestrator.approvals import ApprovalError, decide_approval
from app.orchestrator.service import create_task, get_or_create_telegram_user

logger = logging.getLogger(__name__)

router = Router(name="agent-platform-bot")

TASK_ID_PATTERN = re.compile(r"^[0-9a-fA-F-]{1,36}$")

TASK_STATUS_LABELS = {
    TaskStatus.PENDING: "в очереди",
    TaskStatus.PLANNING: "планируется",
    TaskStatus.RUNNING: "выполняется",
    TaskStatus.AWAITING_APPROVAL: "ждёт подтверждения",
    TaskStatus.COMPLETED: "завершена",
    TaskStatus.FAILED: "с ошибкой",
    TaskStatus.CANCELLED: "отменена",
}

STEP_STATUS_LABELS = {
    StepStatus.PENDING: "ожидает",
    StepStatus.RUNNING: "выполняется",
    StepStatus.AWAITING_APPROVAL: "ждёт подтверждения",
    StepStatus.SUCCEEDED: "выполнен",
    StepStatus.FAILED: "ошибка",
    StepStatus.SKIPPED: "пропущен",
}


class NewTaskForm(StatesGroup):
    waiting_for_prompt = State()


class AccessMiddleware(BaseMiddleware):
    def __init__(self, allowed_user_ids: set[int]) -> None:
        self._allowed = allowed_user_ids

    async def __call__(self, handler: Any, event: TelegramObject, data: dict[str, Any]) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return None
        if not self._allowed:
            logger.warning("telegram whitelist is empty, rejecting user %s", user.id)
            await _deny(
                event,
                "Бот не настроен: попросите администратора добавить ваш ID "
                "в TELEGRAM_ALLOWED_USER_IDS.",
            )
            return None
        if user.id not in self._allowed:
            logger.warning("rejected telegram user %s (not in whitelist)", user.id)
            await _deny(event, "Доступ запрещён.")
            return None
        return await handler(event, data)


async def _deny(event: TelegramObject, text: str) -> None:
    if isinstance(event, Message):
        await event.answer(text)
    elif isinstance(event, CallbackQuery):
        await event.answer(text, show_alert=True)


def _short_id(task_id: UUID) -> str:
    return str(task_id).split("-")[0]


def _status_label(status: TaskStatus) -> str:
    return TASK_STATUS_LABELS.get(status, status.value)


def _step_label(status: StepStatus) -> str:
    return STEP_STATUS_LABELS.get(status, status.value)


def _task_link(task_id: UUID) -> str:
    return f"{get_settings().web_base_url.rstrip('/')}/tasks/{task_id}"


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "<b>Автономная агент-платформа</b>\n\n"
        "Я планирую и выполняю задачи в изолированной песочнице.\n\n"
        "/new_task — поставить задачу\n"
        "/tasks — последние задачи\n"
        "/task &lt;id&gt; — детали задачи\n"
        "/cancel — отменить ввод"
    )


@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext) -> None:
    await cmd_start(message, state)


@router.message(Command("new_task"))
async def cmd_new_task(message: Message, state: FSMContext) -> None:
    await state.set_state(NewTaskForm.waiting_for_prompt)
    await message.answer("Опишите задачу одним сообщением. /cancel — отмена.")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer("Сейчас нечего отменять.")
        return
    await state.clear()
    await message.answer("Ввод отменён.")


@router.message(StateFilter(NewTaskForm.waiting_for_prompt), F.text, ~F.text.startswith("/"))
async def on_task_prompt(message: Message, state: FSMContext) -> None:
    if message.from_user is None or message.text is None:
        return
    prompt = message.text.strip()
    if len(prompt) < 3:
        await message.answer("Слишком короткое описание, попробуйте ещё раз.")
        return
    if len(prompt) > 8000:
        await message.answer("Слишком длинное описание (максимум 8000 символов).")
        return
    await state.clear()
    try:
        async with SessionFactory() as session:
            user = await get_or_create_telegram_user(
                session, message.from_user.id, message.from_user.full_name
            )
            task = await create_task(
                session,
                prompt=prompt,
                user_id=user.id,
                source=TaskSource.TELEGRAM,
                telegram_chat_id=message.chat.id,
            )
    except Exception:
        logger.exception("failed to create task from telegram message")
        await message.answer("Не удалось создать задачу, попробуйте позже.")
        return
    await message.answer(
        f"Задача принята: <code>{_short_id(task.id)}</code>\n"
        f"Начинаю планирование.\n{_task_link(task.id)}"
    )


@router.message(Command("tasks"))
async def cmd_tasks(message: Message) -> None:
    if message.from_user is None:
        return
    async with SessionFactory() as session:
        user = await get_or_create_telegram_user(
            session, message.from_user.id, message.from_user.full_name
        )
        result = await session.execute(
            select(Task)
            .where(Task.user_id == user.id)
            .order_by(Task.created_at.desc())
            .limit(10)
        )
        tasks = list(result.scalars())
    if not tasks:
        await message.answer("Задач пока нет. /new_task — создать.")
        return
    lines = ["<b>Последние задачи</b>"]
    for task in tasks:
        lines.append(
            f"• <code>{_short_id(task.id)}</code> {html.escape(task.title)} — "
            f"{_status_label(task.status)}"
        )
    lines.append("")
    lines.append("/task &lt;id&gt; — подробности")
    await message.answer("\n".join(lines))


@router.message(Command("task"))
async def cmd_task(message: Message) -> None:
    if message.from_user is None or message.text is None:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Использование: /task &lt;id или его начало&gt;")
        return
    prefix = parts[1].strip().lower()
    if not TASK_ID_PATTERN.match(prefix):
        await message.answer("Id задачи состоит из шестнадцатеричных символов и дефисов.")
        return
    async with SessionFactory() as session:
        user = await get_or_create_telegram_user(
            session, message.from_user.id, message.from_user.full_name
        )
        result = await session.execute(
            select(Task)
            .where(Task.user_id == user.id, cast(Task.id, String).like(f"{prefix}%"))
            .order_by(Task.created_at.desc())
            .limit(5)
        )
        matches = list(result.scalars())
    if not matches:
        await message.answer("Задача не найдена.")
        return
    if len(matches) > 1:
        lines = ["Найдено несколько задач, уточните id:"]
        for task in matches:
            lines.append(f"• <code>{_short_id(task.id)}</code> {html.escape(task.title)}")
        await message.answer("\n".join(lines))
        return
    await _send_task_details(message, matches[0])


async def _send_task_details(message: Message, task: Task) -> None:
    lines = [
        f"<b>{html.escape(task.title)}</b>",
        f"id: <code>{task.id}</code>",
        f"статус: {_status_label(task.status)}",
    ]
    ordered_steps = sorted(task.steps, key=lambda item: item.position)
    if ordered_steps:
        lines.append("")
        lines.append("<b>Шаги</b>")
        for step in ordered_steps:
            lines.append(
                f"{step.position + 1}. [{_step_label(step.status)}] {html.escape(step.title)}"
            )
    if task.error:
        lines.append("")
        lines.append(f"<b>Ошибка</b>: {html.escape(task.error[:800])}")
    elif task.result:
        lines.append("")
        lines.append(f"<b>Результат</b>: {html.escape(task.result[:1500])}")
    if task.artifacts:
        names = ", ".join(html.escape(item.filename) for item in task.artifacts[:10])
        lines.append("")
        lines.append(f"<b>Артефакты ({len(task.artifacts)})</b>: {names}")
    lines.append("")
    lines.append(f"Дашборд: {_task_link(task.id)}")

    rows: list[list[InlineKeyboardButton]] = []
    for step in ordered_steps:
        approval = step.approval
        if approval is not None and approval.status is ApprovalStatus.PENDING:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"Разрешить шаг {step.position + 1}",
                        callback_data=f"approve:{approval.id}",
                    ),
                    InlineKeyboardButton(
                        text=f"Отклонить шаг {step.position + 1}",
                        callback_data=f"reject:{approval.id}",
                    ),
                ]
            )
    markup = InlineKeyboardMarkup(inline_keyboard=rows[:3]) if rows else None
    await message.answer("\n".join(lines), reply_markup=markup)


@router.callback_query(F.data.startswith("approve:") | F.data.startswith("reject:"))
async def on_approval_callback(callback: CallbackQuery) -> None:
    if callback.data is None or callback.from_user is None:
        return
    action, _, raw_id = callback.data.partition(":")
    try:
        approval_id = UUID(raw_id)
    except ValueError:
        await callback.answer("Некорректный идентификатор подтверждения.", show_alert=True)
        return
    approved = action == "approve"

    async with SessionFactory() as session:
        approval = await session.get(Approval, approval_id)
        if approval is None:
            await callback.answer("Подтверждение не найдено.", show_alert=True)
            return
        task = await session.get(Task, approval.task_id)
        user = await get_or_create_telegram_user(
            session, callback.from_user.id, callback.from_user.full_name
        )
        if task is None or (task.user_id != user.id and not user.is_admin):
            await callback.answer("Нет доступа к этой задаче.", show_alert=True)
            return

    try:
        await decide_approval(
            approval_id,
            approved=approved,
            decided_by=user.id,
            reason=f"telegram:{callback.from_user.id}",
        )
    except ApprovalError as exc:
        await callback.answer(f"Не удалось: {exc}", show_alert=True)
        return

    verdict = "разрешено" if approved else "отклонено"
    await callback.answer(f"Решение принято: {verdict}")
    if callback.message is not None:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.debug("failed to remove approval keyboard", exc_info=True)
        author = html.escape(callback.from_user.full_name)
        try:
            await callback.message.reply(f"Решение: {verdict} ({author})")
        except Exception:
            logger.debug("failed to send approval verdict", exc_info=True)


@router.message(F.text)
async def on_unknown_text(message: Message) -> None:
    await message.answer("Не понял команду. /new_task — новая задача, /help — справка.")
