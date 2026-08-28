import { SessionGatewayDO } from "./session-gateway.js";
import type { Env, SessionTokenPayload } from "./types.js";

export { SessionGatewayDO };

/**
 * IMPORTANT ARCHITECTURAL & SECURITY SPECIFICATION:
 *
 * 1. Edge-Side JWT Decode Purpose:
 *    The edge gateway decodes the unverified base64url payload segment of the
 *    `Sec-WebSocket-Protocol: proctoring-v1.{JWT}` header SOLELY to extract the
 *    `session_id` string for deterministic edge sharding (`idFromName(sessionId)`).
 *
 * 2. Zero Authority at Edge:
 *    The edge does NOT verify the cryptographic HMAC signature, check token expiration,
 *    or validate session state. The raw `Sec-WebSocket-Protocol` header is passed through
 *    completely unmodified to the FastAPI backend origin.
 *
 * 3. Authoritative Origin Verification:
 *    FastAPI remains the SOLE cryptographic and business-logic verifier. If an attacker
 *    forges or tampers with the `session_id` in the JWT payload, the connection is simply
 *    routed to an arbitrary edge DO shard, which forwards the invalid token to FastAPI
 *    where it is promptly rejected with close code 4001 (`WS_CLOSE_AUTH_FAILED`).
 *
 * 4. Production vs Dev Extraction:
 *    In production, the canonical client connects via WebSocket subprotocol
 *    (`proctoring-v1.{JWT}`). The query-param fallback (`?session_id=...`) is strictly
 *    scoped to development and testing environments.
 */
export function extractSessionId(request: Request, env?: Env): string | null {
  // 1. Primary & Canonical Production Path: Sec-WebSocket-Protocol: proctoring-v1.<jwt>
  const subprotocolHeader = request.headers.get("Sec-WebSocket-Protocol");
  if (subprotocolHeader) {
    const protocols = subprotocolHeader.split(",").map((p) => p.trim());
    for (const protocol of protocols) {
      if (protocol.startsWith("proctoring-v1.")) {
        const rawToken = protocol.slice("proctoring-v1.".length);
        const parts = rawToken.split(".");
        if (parts.length >= 2) {
          try {
            // Base64URL decode the JWT payload segment solely to extract session_id for DO sharding
            const base64Url = parts[1];
            const base64 = base64Url.replace(/-/g, "+").replace(/_/g, "/");
            const jsonPayload = atob(base64);
            const parsed = JSON.parse(jsonPayload) as SessionTokenPayload;
            if (parsed && typeof parsed.session_id === "string") {
              return parsed.session_id;
            }
          } catch {
            // If token payload cannot be parsed, return null
          }
        }
      }
    }
  }

  // 2. Explicit Development / Test Fallback: ?session_id=UUID
  if (env?.ENVIRONMENT === "development" || env?.ENVIRONMENT === "test") {
    const url = new URL(request.url);
    const querySessionId = url.searchParams.get("session_id");
    if (querySessionId) {
      return querySessionId;
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
    if (url.pathname === "/ws") {
      const sessionId = extractSessionId(request, env);

      if (!sessionId) {
        return new Response(
          JSON.stringify({
            error: "session_id_missing",
            message: "Could not resolve session_id from subprotocol token.",
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
