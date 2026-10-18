"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";

import { ApiError, api, downloadArtifact, getToken, taskSocketUrl } from "@/lib/api";
import {
  formatBytes,
  formatDateTime,
  formatTime,
  shortId,
  stepStatusLabel,
  taskStatusLabel,
} from "@/lib/format";
import type { Approval, EventItem, Task } from "@/lib/types";
import { openTaskSocket } from "@/lib/ws";

const REFRESH_KINDS = new Set([
  "task.created",
  "task.planned",
  "task.completed",
  "task.failed",
  "step.started",
  "step.succeeded",
  "step.failed",
  "step.skipped",
  "approval.requested",
  "approval.granted",
  "approval.rejected",
]);

const SOCKET_LABELS: Record<string, string> = {
  connecting: "подключение...",
  open: "live",
  closed: "переподключение...",
};

export default function TaskDetailPage() {
  const params = useParams();
  const taskId = String(params?.id ?? "");
  const router = useRouter();
  const [task, setTask] = useState<Task | null>(null);
  const [events, setEvents] = useState<EventItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [socketState, setSocketState] = useState<"connecting" | "open" | "closed">("connecting");
  const [busyApproval, setBusyApproval] = useState<string | null>(null);
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const refreshTask = useCallback(async () => {
    try {
      const fresh = await api.getTask(taskId);
      setTask(fresh);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setError("Задача не найдена");
      }
    }
  }, [taskId]);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/");
      return;
    }
    void refreshTask();
    const socket = openTaskSocket(taskSocketUrl(taskId), {
      onSnapshot: (snapshot, initialEvents) => {
        setTask(snapshot);
        setEvents(initialEvents);
      },
      onEvent: (event) => {
        setEvents((prev) => [...prev, event].slice(-300));
        if (REFRESH_KINDS.has(event.kind)) {
          if (refreshTimer.current) {
            clearTimeout(refreshTimer.current);
          }
          refreshTimer.current = setTimeout(() => void refreshTask(), 300);
        }
      },
      onStateChange: setSocketState,
    });
    return () => {
      socket.close();
      if (refreshTimer.current) {
        clearTimeout(refreshTimer.current);
      }
    };
  }, [refreshTask, router, taskId]);

  const pendingApproval: Approval | null = useMemo(() => {
    if (!task) {
      return null;
    }
    for (const step of task.steps) {
      if (step.approval?.status === "pending") {
        return step.approval;
      }
    }
    return null;
  }, [task]);

  const reversedEvents = useMemo(() => events.slice().reverse(), [events]);

  const decide = async (approved: boolean) => {
    if (!pendingApproval) {
      return;
    }
    setBusyApproval(pendingApproval.id);
    setError(null);
    try {
      await api.decideApproval(pendingApproval.id, approved);
      await refreshTask();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Не удалось отправить решение");
    } finally {
      setBusyApproval(null);
    }
  };

  const download = async (artifactId: string, filename: string) => {
    setError(null);
    try {
      await downloadArtifact(artifactId, filename);
    } catch {
      setError("Не удалось скачать артефакт");
    }
  };

  if (!task) {
    return (
      <div className="container">
        <header className="topbar">
          <Link href="/tasks">&larr; К задачам</Link>
        </header>
        <p className="muted">{error ?? "Загрузка задачи..."}</p>
      </div>
    );
  }

  return (
    <div className="container">
      <header className="topbar">
        <div>
          <Link href="/tasks">&larr; К задачам</Link>
          <h1>{task.title}</h1>
          <p className="muted">
            <span className="mono">{shortId(task.id)}</span>
            {" · "}
            создана {formatDateTime(task.created_at)}
          </p>
        </div>
        <div className="row">
          <span className="badge" data-status={task.status}>
            {taskStatusLabel(task.status)}
          </span>
          <span className={`socket socket-${socketState}`}>
            {SOCKET_LABELS[socketState] ?? socketState}
          </span>
        </div>
      </header>

      {error ? <p className="error-text">{error}</p> : null}

      {pendingApproval ? (
        <section className="card approval-card">
          <h2>Требуется подтверждение</h2>
          <pre className="code-block">{pendingApproval.command}</pre>
          {pendingApproval.match_reason ? (
            <p className="muted">Сработало правило: {pendingApproval.match_reason}</p>
          ) : null}
          {pendingApproval.expires_at ? (
            <p className="muted">Истекает: {formatDateTime(pendingApproval.expires_at)}</p>
          ) : null}
          <div className="row">
            <button
              className="btn btn-primary"
              type="button"
              disabled={busyApproval !== null}
              onClick={() => void decide(true)}
            >
              {busyApproval ? "Отправляем..." : "Разрешить"}
            </button>
            <button
              className="btn btn-danger"
              type="button"
              disabled={busyApproval !== null}
              onClick={() => void decide(false)}
            >
              Отклонить
            </button>
          </div>
        </section>
      ) : null}

      <section className="card">
        <h2>Задача</h2>
        <pre className="code-block">{task.prompt}</pre>
      </section>

      <section className="card">
        <div className="section-head">
          <h2>Шаги</h2>
          <span className="muted">{task.steps.length}</span>
        </div>
        {task.steps.length === 0 ? <p className="muted">План ещё не готов</p> : null}
        <ol className="step-list">
          {task.steps.map((step) => (
            <li key={step.id} className="step-item">
              <div className="step-head">
                <span className="badge" data-status={step.status}>
                  {stepStatusLabel(step.status)}
                </span>
                <strong>
                  {step.position + 1}. {step.title}
                </strong>
                <span className="muted mono">{step.tool}</span>
                {step.attempts > 1 ? (
                  <span className="muted">попыток: {step.attempts}</span>
                ) : null}
              </div>
              {step.description ? <p className="muted">{step.description}</p> : null}
              {step.error ? <pre className="code-block error-block">{step.error}</pre> : null}
              {step.output ? (
                <details>
                  <summary className="muted">Показать вывод</summary>
                  <pre className="code-block">{step.output}</pre>
                </details>
              ) : null}
            </li>
          ))}
        </ol>
      </section>

      {task.result ? (
        <section className="card">
          <h2>Результат</h2>
          <pre className="code-block">{task.result}</pre>
        </section>
      ) : null}

      {task.error ? (
        <section className="card">
          <h2>Ошибка</h2>
          <pre className="code-block error-block">{task.error}</pre>
        </section>
      ) : null}

      {task.artifacts.length > 0 ? (
        <section className="card">
          <h2>Артефакты</h2>
          <ul className="artifact-list">
            {task.artifacts.map((artifact) => (
              <li key={artifact.id}>
                <span className="mono">{artifact.filename}</span>
                <span className="muted">{formatBytes(artifact.size_bytes)}</span>
                <button
                  className="btn btn-ghost"
                  type="button"
                  onClick={() => void download(artifact.id, artifact.filename)}
                >
                  Скачать
                </button>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <section className="card">
        <h2>Журнал событий</h2>
        {reversedEvents.length === 0 ? <p className="muted">Событий пока нет</p> : null}
        <ul className="event-list">
          {reversedEvents.map((event) => (
            <li key={event.id} className="event-item">
              <span className="muted mono">{formatTime(event.created_at)}</span>
              <span className="event-kind">{event.kind}</span>
              <span>{event.message}</span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
