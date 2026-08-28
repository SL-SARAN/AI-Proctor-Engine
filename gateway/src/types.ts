export interface Env {
  SESSION_GATEWAY: DurableObjectNamespace;
  ORIGIN_WS_URL?: string;
  ENVIRONMENT?: string;
}

export interface SessionTokenPayload {
  session_id: string;
  sub?: string;
  role?: string;
  exp?: number;
  [key: string]: unknown;
}
