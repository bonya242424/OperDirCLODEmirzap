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
