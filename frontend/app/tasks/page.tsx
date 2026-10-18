"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import { ApiError, api, clearSession, getStoredUser, getToken } from "@/lib/api";
import { formatDateTime, shortId, taskStatusLabel } from "@/lib/format";
import type { TaskSummary, User } from "@/lib/types";

export default function TasksPage() {
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [prompt, setPrompt] = useState("");
  const [title, setTitle] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [loading, setLoading] = useState(true);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const items = await api.listTasks();
      setTasks(items);
      setError(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        return;
      }
      setError("Не удалось загрузить список задач");
    }
  }, []);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/");
      return;
    }
    setUser(getStoredUser());
    refresh().finally(() => setLoading(false));
    const timer = setInterval(() => void refresh(), 15000);
    return () => clearInterval(timer);
  }, [refresh, router]);

  const onCreate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (prompt.trim().length < 3) {
      setError("Опишите задачу подробнее (минимум 3 символа)");
      return;
    }
    setCreating(true);
    setError(null);
    try {
      const task = await api.createTask({
        prompt: prompt.trim(),
        title: title.trim() || null,
      });
      router.push(`/tasks/${task.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Не удалось создать задачу");
    } finally {
      setCreating(false);
    }
  };

  const onLogout = () => {
    clearSession();
    router.replace("/");
  };

  const onDelete = async (task: TaskSummary) => {
    if (!window.confirm(`Удалить задачу «${task.title}»? Действие необратимо.`)) {
      return;
    }
    setDeletingId(task.id);
    setError(null);
    try {
      await api.deleteTask(task.id);
      await refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Не удалось удалить задачу");
    } finally {
      setDeletingId(null);
    }
  };

  return (
    <div className="container">
      <header className="topbar">
        <div>
          <h1>Задачи</h1>
          <p className="muted">{user?.email ?? user?.display_name ?? "пользователь"}</p>
        </div>
        <button className="btn btn-ghost" type="button" onClick={onLogout}>
          Выйти
        </button>
      </header>

      <form className="card" onSubmit={onCreate}>
        <h2>Новая задача</h2>
        <label className="field">
          <span>Описание задачи</span>
          <textarea
            className="input textarea"
            rows={4}
            value={prompt}
            placeholder="Например: напиши скрипт, который считает статистику по CSV из репозитория"
            onChange={(event) => setPrompt(event.target.value)}
          />
        </label>
        <label className="field">
          <span>Заголовок (необязательно)</span>
          <input
            className="input"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
          />
        </label>
        {error ? <p className="error-text">{error}</p> : null}
        <button className="btn btn-primary" type="submit" disabled={creating}>
          {creating ? "Создаём..." : "Запустить"}
        </button>
      </form>

      <section className="card">
        <div className="section-head">
          <h2>Последние задачи</h2>
          <button className="btn btn-ghost" type="button" onClick={() => void refresh()}>
            Обновить
          </button>
        </div>
        {loading ? <p className="muted">Загрузка...</p> : null}
        {!loading && tasks.length === 0 ? <p className="muted">Задач пока нет</p> : null}
        <ul className="task-list">
          {tasks.map((task) => (
            <li key={task.id}>
              <div className="task-row-wrap">
                <Link className="task-row" href={`/tasks/${task.id}`}>
                  <span className="badge" data-status={task.status}>
                    {taskStatusLabel(task.status)}
                  </span>
                  <span className="task-title">{task.title}</span>
                  <span className="muted mono">{shortId(task.id)}</span>
                  <span className="muted">{formatDateTime(task.created_at)}</span>
                </Link>
                <button
                  className="btn btn-ghost"
                  type="button"
                  disabled={deletingId === task.id}
                  onClick={() => void onDelete(task)}
                >
                  {deletingId === task.id ? "Удаляем..." : "Удалить"}
                </button>
              </div>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
