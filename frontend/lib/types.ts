export type TaskStatus =
  | "pending"
  | "planning"
  | "running"
  | "awaiting_approval"
  | "completed"
  | "failed"
  | "cancelled";

export type StepStatus =
  | "pending"
  | "running"
  | "awaiting_approval"
  | "succeeded"
  | "failed"
  | "skipped";

export type ApprovalStatus = "pending" | "approved" | "rejected" | "expired";

export type ToolName =
  | "read_file"
  | "write_file"
  | "list_dir"
  | "run_shell"
  | "http_request"
  | "finish_task";

export interface User {
  id: number;
  email: string | null;
  display_name: string | null;
  telegram_id: number | null;
  is_active: boolean;
  is_admin: boolean;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  user: User;
}

export interface Approval {
  id: string;
  task_id: string;
  step_id: string;
  command: string;
  match_reason: string | null;
  status: ApprovalStatus;
  decision_reason: string | null;
  expires_at: string | null;
  decided_at: string | null;
  created_at: string;
}

export interface Step {
  id: string;
  position: number;
  title: string;
  description: string;
  tool: ToolName;
  status: StepStatus;
  requires_approval: boolean;
  attempts: number;
  output: string | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  updated_at: string;
  approval: Approval | null;
}

export interface Artifact {
  id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string | null;
  created_at: string;
}

export interface TaskSummary {
  id: string;
  title: string;
  status: TaskStatus;
  source: string;
  created_at: string;
  finished_at: string | null;
}

export interface Task {
  id: string;
  title: string;
  prompt: string;
  source: string;
  status: TaskStatus;
  result: string | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  updated_at: string;
  steps: Step[];
  artifacts: Artifact[];
}

export interface EventItem {
  id: number;
  task_id: string;
  step_id: string | null;
  level: string;
  kind: string;
  message: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export type WsMessage =
  | { type: "snapshot"; task: Task; events: EventItem[] }
  | { type: "event"; event: EventItem }
  | { type: "ping" }
  | { type: "error"; message: string };
