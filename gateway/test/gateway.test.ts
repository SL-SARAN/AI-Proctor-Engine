import { describe, it, expect, vi, beforeEach } from "vitest";
import { extractSessionId } from "../src/index.js";
import { SessionGatewayDO } from "../src/session-gateway.js";
import type { Env } from "../src/types.js";

// Mock WebSocket class for Node environment testing
class MockWebSocket {
  public readyState: number = 1; // 1 = OPEN
  public listeners: Record<string, ((event: any) => void)[]> = {};
  public sentMessages: any[] = [];
  public receivedMessages: any[] = [];
  public closedCode?: number;
  public closedReason?: string;

  accept() {}

  send(data: any) {
    this.sentMessages.push(data);
  }

  close(code?: number, reason?: string) {
    this.readyState = 3; // 3 = CLOSED
    this.closedCode = code;
    this.closedReason = reason;
    const closeListeners = this.listeners["close"] || [];
    for (const listener of closeListeners) {
      listener({ code: code || 1000, reason: reason || "" });
    }
  }

  addEventListener(event: string, callback: (event: any) => void) {
    if (!this.listeners[event]) {
      this.listeners[event] = [];
    }
    this.listeners[event].push(callback);
  }

  // Helper to simulate incoming event from peer
  simulateMessage(data: any) {
    this.receivedMessages.push(data);
    const msgListeners = this.listeners["message"] || [];
    for (const listener of msgListeners) {
      listener({ data });
    }
  }

  simulateClose(code: number, reason: string) {
    this.readyState = 3;
    this.closedCode = code;
    this.closedReason = reason;
    const closeListeners = this.listeners["close"] || [];
    for (const listener of closeListeners) {
      listener({ code, reason });
    }
  }
}

// Mock WebSocketPair global connecting pair endpoints
class MockWebSocketPair {
  public 0: MockWebSocket;
  public 1: MockWebSocket;
  constructor() {
    const a = new MockWebSocket();
    const b = new MockWebSocket();
    // Cross-wire send and close
    const originalSendA = a.send.bind(a);
    a.send = (data: any) => {
      originalSendA(data);
      b.simulateMessage(data);
    };
    const originalSendB = b.send.bind(b);
    b.send = (data: any) => {
      originalSendB(data);
      a.simulateMessage(data);
    };
    const originalCloseA = a.close.bind(a);
    a.close = (code?: number, reason?: string) => {
      originalCloseA(code, reason);
      b.simulateClose(code || 1000, reason || "");
    };
    const originalCloseB = b.close.bind(b);
    b.close = (code?: number, reason?: string) => {
      originalCloseB(code, reason);
      a.simulateClose(code || 1000, reason || "");
    };

    this[0] = a;
    this[1] = b;
  }
}

// Mock Response class to support Cloudflare Workers status 101 in Node test runtime
const OriginalResponse = globalThis.Response;
class MockWorkersResponse extends OriginalResponse {
  public webSocket: any = null;
  constructor(body?: any, init?: any) {
    if (init?.status === 101) {
      super(null, { status: 200, headers: init.headers });
      Object.defineProperty(this, "status", { value: 101 });
      this.webSocket = init.webSocket || null;
      return;
    }
    super(body, init);
    if (init?.webSocket) {
      this.webSocket = init.webSocket;
    }
  }
}

// Assign global WebSocket, WebSocketPair, and Response for test environment
(globalThis as any).WebSocketPair = MockWebSocketPair;
(globalThis as any).Response = MockWorkersResponse;
(globalThis as any).WebSocket = {
  CONNECTING: 0,
  OPEN: 1,
  CLOSING: 2,
  CLOSED: 3,
};

describe("extractSessionId", () => {
  it("extracts session_id from Sec-WebSocket-Protocol JWT payload in production", () => {
    const header = btoa(JSON.stringify({ alg: "HS256", typ: "JWT" }))
      .replace(/=/g, "")
      .replace(/\+/g, "-")
      .replace(/\//g, "_");
    const payload = btoa(JSON.stringify({ session_id: "sess-uuid-999", role: "learner" }))
      .replace(/=/g, "")
      .replace(/\+/g, "-")
      .replace(/\//g, "_");
    const fakeSignature = "sig123";
    const token = `${header}.${payload}.${fakeSignature}`;

    const req = new Request("https://gateway.example.com/ws", {
      headers: {
        "Sec-WebSocket-Protocol": `proctoring-v1.${token}`,
      },
    });

    expect(extractSessionId(req)).toBe("sess-uuid-999");
  });

  it("rejects query parameter in production mode", () => {
    const req = new Request("https://gateway.example.com/ws?session_id=123e4567-e89b-12d3-a456-426614174000");
    const prodEnv: Env = {
      SESSION_GATEWAY: {} as any,
      ENVIRONMENT: "production",
    };
    expect(extractSessionId(req, prodEnv)).toBeNull();
  });

  it("allows query parameter in development / test mode", () => {
    const req = new Request("https://gateway.example.com/ws?session_id=123e4567-e89b-12d3-a456-426614174000");
    const devEnv: Env = {
      SESSION_GATEWAY: {} as any,
      ENVIRONMENT: "development",
    };
    expect(extractSessionId(req, devEnv)).toBe("123e4567-e89b-12d3-a456-426614174000");
  });

  it("returns null on malformed or missing token", () => {
    const req = new Request("https://gateway.example.com/ws", {
      headers: {
        "Sec-WebSocket-Protocol": "proctoring-v1.invalid-token",
      },
    });
    expect(extractSessionId(req)).toBeNull();
  });
});

describe("SessionGatewayDO Rejection and Forwarding Lifecycle", () => {
  let mockState: any;
  let env: Env;

  beforeEach(() => {
    mockState = {};
    env = {
      SESSION_GATEWAY: {} as any,
      ORIGIN_WS_URL: "ws://origin.internal/ws",
      ENVIRONMENT: "test",
    };
    vi.restoreAllMocks();
  });

  it("returns 426 Upgrade Required for non-WebSocket HTTP requests", async () => {
    const gateway = new SessionGatewayDO(mockState, env);
    const req = new Request("https://gateway.example.com/ws", {
      method: "GET",
    });

    const res = await gateway.fetch(req);
    expect(res.status).toBe(426);
    expect(await res.text()).toContain("Expected WebSocket Upgrade");
  });

  it("returns 502 Bad Gateway when origin backend is completely unreachable", async () => {
    const gateway = new SessionGatewayDO(mockState, env);
    const req = new Request("https://gateway.example.com/ws", {
      headers: {
        Upgrade: "websocket",
        "Sec-WebSocket-Protocol": "proctoring-v1.dummy",
      },
    });

    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("Connection refused")));

    const res = await gateway.fetch(req);
    expect(res.status).toBe(502);
    expect(await res.text()).toBe("Origin backend unavailable");
    expect(res.webSocket).toBeNull();
  });

  it("returns origin HTTP error status directly if origin rejects handshake with HTTP error", async () => {
    const gateway = new SessionGatewayDO(mockState, env);
    const req = new Request("https://gateway.example.com/ws", {
      headers: {
        Upgrade: "websocket",
        "Sec-WebSocket-Protocol": "proctoring-v1.dummy",
      },
    });

    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("Unauthorized", {
          status: 401,
          headers: { "Content-Type": "text/plain" },
        })
      )
    );

    const res = await gateway.fetch(req);
    expect(res.status).toBe(401);
    expect(await res.text()).toBe("Unauthorized");
    expect(res.webSocket).toBeNull();
  });

  it("propagates origin WebSocket rejection close code 4009 (consent required) to client socket", async () => {
    const gateway = new SessionGatewayDO(mockState, env);
    const req = new Request("https://gateway.example.com/ws", {
      headers: {
        Upgrade: "websocket",
        "Sec-WebSocket-Protocol": "proctoring-v1.token",
      },
    });

    const originWsMock = new MockWebSocket();
    const originResponse = {
      status: 101,
      webSocket: originWsMock,
      headers: new Headers({ "Sec-WebSocket-Protocol": "proctoring-v1.token" }),
    };

    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(originResponse));

    const res = await gateway.fetch(req);
    expect(res.webSocket).toBeDefined();

    const clientSocket = (gateway as any).lastClientSocket || (res.webSocket as any);

    // Simulate origin FastAPI backend closing the WebSocket with 4009 (Consent Required)
    originWsMock.simulateClose(4009, "Consent has not been recorded for this session.");

    // Verify client socket is closed with matching code 4009 and reason
    expect(clientSocket.closedCode).toBe(4009);
    expect(clientSocket.closedReason).toBe("Consent has not been recorded for this session.");
  });

  it("propagates origin close code 4001 (auth failed) and 4005 (not learner)", async () => {
    const gateway = new SessionGatewayDO(mockState, env);
    const req = new Request("https://gateway.example.com/ws", {
      headers: {
        Upgrade: "websocket",
        "Sec-WebSocket-Protocol": "proctoring-v1.token",
      },
    });

    const originWsMock = new MockWebSocket();
    const originResponse = {
      status: 101,
      webSocket: originWsMock,
      headers: new Headers(),
    };

    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(originResponse));

    const res = await gateway.fetch(req);
    const clientSocket = (res.webSocket as any);

    // Simulate origin closing with 4001
    originWsMock.simulateClose(4001, "Token signature invalid.");
    expect(clientSocket.closedCode).toBe(4001);
    expect(clientSocket.closedReason).toBe("Token signature invalid.");
  });

  it("forwards bidirectional messages between client and origin", async () => {
    const gateway = new SessionGatewayDO(mockState, env);
    const req = new Request("https://gateway.example.com/ws", {
      headers: {
        Upgrade: "websocket",
        "Sec-WebSocket-Protocol": "proctoring-v1.token",
      },
    });

    const originWsMock = new MockWebSocket();
    const originResponse = {
      status: 101,
      webSocket: originWsMock,
      headers: new Headers(),
    };

    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(originResponse));

    const res = await gateway.fetch(req);
    const clientSocket = (res.webSocket as any);

    // 1. Simulate client sending message -> should arrive at originWs
    clientSocket.send(JSON.stringify({ type: "telemetry_light", seq: 1 }));
    expect(originWsMock.sentMessages).toHaveLength(1);
    expect(originWsMock.sentMessages[0]).toContain("telemetry_light");

    // 2. Simulate origin sending message (e.g. ack) -> should arrive at clientSocket
    originWsMock.simulateMessage(JSON.stringify({ type: "ack", payload: { seq: 1 } }));
    expect(clientSocket.receivedMessages).toHaveLength(1);
    expect(clientSocket.receivedMessages[0]).toContain("ack");
  });
});
