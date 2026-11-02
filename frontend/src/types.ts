export interface User {
  id: number;
  email: string;
  full_name: string;
  position: string;
  department: string;
  role: "owner" | "employee";
  can_apply_changes: boolean;
  bitrix_user_id: number | null;
  is_active: boolean;
}

export type IntegrationStatus = "ok" | "warning" | "error" | "not_configured" | "unknown";

export interface Integration {
  key: string;
  name: string;
  description: string;
  category: string;
  status: IntegrationStatus;
  message: string;
  has_sync: boolean;
  last_check_at: string | null;
  last_sync_at: string | null;
}

export interface IntegrationsResponse {
  check_interval_minutes: number;
  items: Integration[];
}

export interface ChatMessage {
  id: number;
  role: "user" | "assistant";
  content: string;
  created_at: string;
}

export interface ConnectionField {
  name: string;
  label: string;
  secret: boolean;
  required: boolean;
  default: string;
}

export interface ConnectionKind {
  key: string;
  name: string;
  description: string;
  fields: ConnectionField[];
}

export interface Connection {
  id: number;
  kind: string;
  kind_name: string;
  title: string;
  scope: "company" | "personal";
  owner_id: number | null;
  owner_name: string;
  status: IntegrationStatus;
  message: string;
  last_check_at: string | null;
  last_sync_at: string | null;
  values: Record<string, string>;
  secrets_set: string[];
  can_edit: boolean;
}

export interface Folder {
  id: number;
  name: string;
  description: string;
  source_key: string | null;
  files_count: number;
  last_upload_at: string | null;
  can_edit: boolean;
}

export interface StoredFile {
  id: number;
  folder_id: number;
  name: string;
  size: number;
  content_type: string;
  uploaded_by: string;
  uploaded_at: string;
  can_delete: boolean;
}

export interface TaskItem {
  id: number;
  title: string;
  responsible: string;
  status: number;
  deadline: string | null;
  overdue: boolean;
  days_left: number | null;
}

export interface TasksResponse {
  total_open: number;
  overdue: number;
  due_today: number;
  due_soon: number;
  no_deadline: number;
  people: { name: string; open: number; overdue: number; due_soon: number }[];
  items: TaskItem[];
}

export interface BoardCard {
  id: string;
  source: "local" | "bitrix";
  title: string;
  description: string;
  status: "new" | "in_progress" | "review" | "done";
  priority: "low" | "normal" | "high";
  assignee_id: number | null;
  assignee: string;
  deadline: string | null;
  overdue: boolean;
  editable: boolean;
}

export interface BoardColumn {
  key: BoardCard["status"];
  title: string;
  cards: BoardCard[];
}

export interface Board {
  columns: BoardColumn[];
}

export interface Metric {
  id: number;
  direction: string;
  name: string;
  unit: string;
  description: string;
  better: "up" | "down";
  warn_threshold: number | null;
  bad_threshold: number | null;
  expected_days: number;
  latest: number | null;
  latest_period: string | null;
  previous: number | null;
  change: number | null;
  status: "ok" | "warning" | "bad" | "no_data";
  stale: boolean;
  history: number[];
  source_folder_id: number | null;
  source_folder: string;
  source_prompt: string;
  calc_status: "none" | "running" | "ok" | "error";
  calc_message: string;
  calc_at: string | null;
  calc_file: string;
}

export interface MetricValue {
  period: string;
  value: number;
  note: string;
  created_at: string;
}
