import type { Approval, Task, TaskSummary, TokenResponse, User } from "./types";

const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/$/, "");
const WS_URL = (process.env.NEXT_PUBLIC_WS_URL ?? "ws://localhost:8000").replace(/\/$/, "");

const TOKEN_KEY = "agent_platform_token";
const USER_KEY = "agent_platform_user";

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export function getToken(): string | null {
  if (typeof window === "undefined") {
    return null;
  }
  return window.localStorage.getItem(TOKEN_KEY);
}

export function getStoredUser(): User | null {
  if (typeof window === "undefined") {
    return null;
  }
  const raw = window.localStorage.getItem(USER_KEY);
  if (!raw) {
    return null;
  }
  try {
    return JSON.parse(raw) as User;
  } catch {
    return null;
  }
}

export function storeSession(token: string, user: User): void {
  window.localStorage.setItem(TOKEN_KEY, token);
  window.localStorage.setItem(USER_KEY, JSON.stringify(user));
}

export function clearSession(): void {
  window.localStorage.removeItem(TOKEN_KEY);
  window.localStorage.removeItem(USER_KEY);
}

async function extractDetail(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body?.detail === "string") {
      return body.detail;
    }
    if (body?.detail) {
      return JSON.stringify(body.detail);
    }
  } catch {
    return `HTTP ${response.status}`;
  }
  return `HTTP ${response.status}`;
}

async function request<T>(path: string, options: RequestInit = {}, auth = true): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body) {
    headers.set("Content-Type", "application/json");
  }
  if (auth) {
    const token = getToken();
    if (token) {
      headers.set("Authorization", `Bearer ${token}`);
    }
  }
  const response = await fetch(`${API_URL}${path}`, { ...options, headers });
  if (response.status === 401 && auth) {
    clearSession();
    if (typeof window !== "undefined") {
      window.location.href = "/";
    }
    throw new ApiError(401, "unauthorized");
  }
  if (!response.ok) {
    throw new ApiError(response.status, await extractDetail(response));
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export const api = {
  login(email: string, password: string): Promise<TokenResponse> {
    return request<TokenResponse>(
      "/api/auth/login",
      { method: "POST", body: JSON.stringify({ email, password }) },
      false,
    );
  },

  me(): Promise<User> {
    return request<User>("/api/auth/me");
  },

  createTask(payload: { prompt: string; title?: string | null }): Promise<Task> {
    return request<Task>("/api/tasks", { method: "POST", body: JSON.stringify(payload) });
  },

  listTasks(): Promise<TaskSummary[]> {
    return request<TaskSummary[]>("/api/tasks");
  },

  getTask(taskId: string): Promise<Task> {
    return request<Task>(`/api/tasks/${taskId}`);
  },

  decideApproval(approvalId: string, approved: boolean, reason?: string): Promise<Approval> {
    return request<Approval>(`/api/approvals/${approvalId}/decision`, {
      method: "POST",
      body: JSON.stringify({ approved, reason: reason ?? null }),
    });
  },
};

export function taskSocketUrl(taskId: string): string {
  return `${WS_URL}/api/ws/tasks/${taskId}?token=${encodeURIComponent(getToken() ?? "")}`;
}

export async function downloadArtifact(artifactId: string, filename: string): Promise<void> {
  const token = getToken();
  const response = await fetch(`${API_URL}/api/artifacts/${artifactId}/download`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!response.ok) {
    throw new ApiError(response.status, "download failed");
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename.split("/").pop() ?? filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}
