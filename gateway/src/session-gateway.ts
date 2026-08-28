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

    // Setup client-side WebSocket pair
    const webSocketPair = new WebSocketPair();
    const [clientSocket, serverSocket] = Object.values(webSocketPair);

    // Accept the server end of the pair inside the Durable Object
    serverSocket.accept();

    // Establish outbound WebSocket to the FastAPI backend
    try {
      const originHeaders: Record<string, string> = {
        Upgrade: "websocket",
        Connection: "Upgrade",
      };

      if (subprotocolHeader) {
        originHeaders["Sec-WebSocket-Protocol"] = subprotocolHeader;
      }

      // Forward client origin / IP headers for backend audit logging if present
      const clientIp = request.headers.get("cf-connecting-ip") || request.headers.get("x-forwarded-for");
      if (clientIp) {
        originHeaders["X-Forwarded-For"] = clientIp;
      }

      const originReq = new Request(originBaseUrl, {
        headers: originHeaders,
      });

      const originResponse = await fetch(originReq);
      const originWs = originResponse.webSocket;

      if (!originWs) {
        serverSocket.close(4003, "Origin did not accept WebSocket upgrade");
        return new Response(null, {
          status: 101,
          webSocket: clientSocket,
        });
      }

      originWs.accept();

      // Setup bidirectional message forwarding
      serverSocket.addEventListener("message", (event: MessageEvent) => {
        try {
          if (originWs.readyState === WebSocket.OPEN) {
            originWs.send(event.data);
          }
        } catch (err) {
          console.error("Error forwarding message from client to origin:", err);
        }
      });

      originWs.addEventListener("message", (event: MessageEvent) => {
        try {
          if (serverSocket.readyState === WebSocket.OPEN) {
            serverSocket.send(event.data);
          }
        } catch (err) {
          console.error("Error forwarding message from origin to client:", err);
        }
      });

      // Handle close events: 1:1 lifecycle teardown
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

      // Handle error events
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
    } catch (err) {
      console.error("Failed to connect to origin WebSocket:", err);
      serverSocket.close(4003, "Origin backend unavailable");
      return new Response(null, {
        status: 101,
        webSocket: clientSocket,
      });
    }
  }
}
