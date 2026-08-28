import { SessionGatewayDO } from "./session-gateway.js";
import type { Env, SessionTokenPayload } from "./types.js";

export { SessionGatewayDO };

/**
 * Extracts the session_id from request URL or JWT subprotocol header.
 *
 * Supported formats:
 * 1. Query parameter: /ws?session_id=UUID
 * 2. URL path: /ws/UUID
 * 3. Subprotocol header: Sec-WebSocket-Protocol: proctoring-v1.{JWT}
 */
export function extractSessionId(request: Request): string | null {
  const url = new URL(request.url);

  // 1. Check query parameter
  const querySessionId = url.searchParams.get("session_id");
  if (querySessionId) {
    return querySessionId;
  }

  // 2. Check path /ws/:sessionId
  const pathMatch = url.pathname.match(/^\/ws\/([a-zA-Z0-9_-]+)$/);
  if (pathMatch && pathMatch[1]) {
    return pathMatch[1];
  }

  // 3. Check Sec-WebSocket-Protocol: proctoring-v1.<jwt>
  const subprotocolHeader = request.headers.get("Sec-WebSocket-Protocol");
  if (subprotocolHeader) {
    const protocols = subprotocolHeader.split(",").map((p) => p.trim());
    for (const protocol of protocols) {
      if (protocol.startsWith("proctoring-v1.")) {
        const rawToken = protocol.slice("proctoring-v1.".length);
        const parts = rawToken.split(".");
        if (parts.length >= 2) {
          try {
            // Base64URL decode the JWT payload segment
            const base64Url = parts[1];
            const base64 = base64Url.replace(/-/g, "+").replace(/_/g, "/");
            const jsonPayload = atob(base64);
            const parsed = JSON.parse(jsonPayload) as SessionTokenPayload;
            if (parsed && typeof parsed.session_id === "string") {
              return parsed.session_id;
            }
          } catch {
            // If token payload cannot be parsed, fallback
          }
        }
      }
    }
  }

  return null;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    // Health check endpoint
    if (url.pathname === "/health") {
      return new Response(
        JSON.stringify({
          status: "healthy",
          service: "proctoring-engine-gateway",
          timestamp: new Date().toISOString(),
        }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }
      );
    }

    // Handle WebSocket upgrade routes
    if (url.pathname === "/ws" || url.pathname.startsWith("/ws/")) {
      const sessionId = extractSessionId(request);

      if (!sessionId) {
        return new Response(
          JSON.stringify({
            error: "session_id_missing",
            message: "Could not resolve session_id from query params, path, or subprotocol token.",
          }),
          {
            status: 400,
            headers: { "Content-Type": "application/json" },
          }
        );
      }

      // Route deterministically to the session's designated Durable Object
      const doId = env.SESSION_GATEWAY.idFromName(sessionId);
      const stub = env.SESSION_GATEWAY.get(doId);

      return stub.fetch(request);
    }

    return new Response("Not Found", { status: 404 });
  },
};
