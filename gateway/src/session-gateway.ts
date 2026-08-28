import type { Env } from "./types.js";

/**
 * SessionGatewayDO is a session-scoped Durable Object proxy.
 *
 * Responsibilities:
 * - Deterministic edge session-affinity: Exactly one DO instance per session_id.
 * - Pure byte-forwarding proxy: Transparent bidirectional streaming (text & binary).
 * - Zero-trust auth passthrough: Passes Sec-WebSocket-Protocol (proctoring-v1.{token}) unchanged to FastAPI.
 * - 1:1 Connection Lifecycle: When client disconnects, origin socket is closed cleanly.
 *   When client reconnects, a fresh outbound origin connection is established.
 */
export class SessionGatewayDO {
  private state: DurableObjectState;
  private env: Env;

  constructor(state: DurableObjectState, env: Env) {
    this.state = state;
    this.env = env;
  }

  async fetch(request: Request): Promise<Response> {
    const upgradeHeader = request.headers.get("Upgrade");
    if (!upgradeHeader || upgradeHeader.toLowerCase() !== "websocket") {
      return new Response("Expected WebSocket Upgrade", {
        status: 426,
        headers: { Upgrade: "websocket" },
      });
    }

    const subprotocolHeader = request.headers.get("Sec-WebSocket-Protocol");
    const originBaseUrl = this.env.ORIGIN_WS_URL || "ws://localhost:8000/ws";

    // 1. Prepare outbound request to origin FastAPI backend
    const originHeaders: Record<string, string> = {
      Upgrade: "websocket",
      Connection: "Upgrade",
    };

    if (subprotocolHeader) {
      originHeaders["Sec-WebSocket-Protocol"] = subprotocolHeader;
    }

    const clientIp = request.headers.get("cf-connecting-ip") || request.headers.get("x-forwarded-for");
    if (clientIp) {
      originHeaders["X-Forwarded-For"] = clientIp;
    }

    const originReq = new Request(originBaseUrl, {
      headers: originHeaders,
    });

    let originResponse: Response;
    try {
      originResponse = await fetch(originReq);
    } catch (err) {
      console.error("Failed to reach origin WebSocket backend:", err);
      return new Response("Origin backend unavailable", { status: 502 });
    }

    // 2. If origin rejected the upgrade (e.g. non-101 HTTP status), pass the origin response directly
    const originWs = originResponse.webSocket;
    if (originResponse.status !== 101 || !originWs) {
      return new Response(originResponse.body, {
        status: originResponse.status,
        headers: originResponse.headers,
      });
    }

    // 3. Origin accepted! Create client WebSocket pair and bind 1:1 bidirectional forwarding
    const webSocketPair = new WebSocketPair();
    const [clientSocket, serverSocket] = Object.values(webSocketPair);

    serverSocket.accept();
    originWs.accept();

    // Bidirectional message forwarding
    serverSocket.addEventListener("message", (event: MessageEvent) => {
      try {
        if (originWs.readyState === WebSocket.OPEN) {
          originWs.send(event.data);
        }
      } catch (err) {
        console.error("Error forwarding client message to origin:", err);
      }
    });

    originWs.addEventListener("message", (event: MessageEvent) => {
      try {
        if (serverSocket.readyState === WebSocket.OPEN) {
          serverSocket.send(event.data);
        }
      } catch (err) {
        console.error("Error forwarding origin message to client:", err);
      }
    });

    // 1:1 Connection Lifecycle: Clean close propagation
    serverSocket.addEventListener("close", (event: CloseEvent) => {
      try {
        if (originWs.readyState === WebSocket.OPEN || originWs.readyState === WebSocket.CONNECTING) {
          originWs.close(event.code || 1000, event.reason || "Client disconnected");
        }
      } catch (err) {
        console.error("Error closing origin socket:", err);
      }
    });

    originWs.addEventListener("close", (event: CloseEvent) => {
      try {
        if (serverSocket.readyState === WebSocket.OPEN || serverSocket.readyState === WebSocket.CONNECTING) {
          serverSocket.close(event.code || 1000, event.reason || "Origin disconnected");
        }
      } catch (err) {
        console.error("Error closing server socket:", err);
      }
    });

    // Error propagation
    serverSocket.addEventListener("error", () => {
      try {
        if (originWs.readyState === WebSocket.OPEN) {
          originWs.close(1011, "Client socket error");
        }
      } catch {}
    });

    originWs.addEventListener("error", () => {
      try {
        if (serverSocket.readyState === WebSocket.OPEN) {
          serverSocket.close(1011, "Origin socket error");
        }
      } catch {}
    });

    const responseHeaders: Record<string, string> = {};
    const originSubprotocol = originResponse.headers.get("Sec-WebSocket-Protocol");
    if (originSubprotocol) {
      responseHeaders["Sec-WebSocket-Protocol"] = originSubprotocol;
    } else if (subprotocolHeader) {
      responseHeaders["Sec-WebSocket-Protocol"] = subprotocolHeader.split(",")[0].trim();
    }

    return new Response(null, {
      status: 101,
      webSocket: clientSocket,
      headers: responseHeaders,
    });
  }
}
