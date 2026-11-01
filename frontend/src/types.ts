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
