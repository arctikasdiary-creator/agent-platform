import type { StepStatus, TaskStatus } from "./types";

const TASK_STATUS_LABELS: Record<TaskStatus, string> = {
  pending: "в очереди",
  planning: "планируется",
  running: "выполняется",
  awaiting_approval: "ждёт подтверждения",
  completed: "завершена",
  failed: "ошибка",
  cancelled: "отменена",
};

const STEP_STATUS_LABELS: Record<StepStatus, string> = {
  pending: "ожидает",
  running: "выполняется",
  awaiting_approval: "ждёт подтверждения",
  succeeded: "выполнен",
  failed: "ошибка",
  skipped: "пропущен",
};

export function taskStatusLabel(status: string): string {
  return TASK_STATUS_LABELS[status as TaskStatus] ?? status;
}

export function stepStatusLabel(status: string): string {
  return STEP_STATUS_LABELS[status as StepStatus] ?? status;
}

export function shortId(id: string): string {
  return id.split("-")[0];
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) {
    return "—";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "—";
  }
  return date.toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  return date.toLocaleTimeString("ru-RU");
}

export function formatBytes(size: number): string {
  if (size < 1024) {
    return `${size} Б`;
  }
  if (size < 1024 * 1024) {
    return `${(size / 1024).toFixed(1)} КБ`;
  }
  return `${(size / (1024 * 1024)).toFixed(1)} МБ`;
}
