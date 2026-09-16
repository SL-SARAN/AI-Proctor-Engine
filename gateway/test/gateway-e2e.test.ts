import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { spawn, type ChildProcess } from "node:child_process";
import { Miniflare, Log, LogLevel } from "miniflare";
import esbuild from "esbuild";
import path from "node:path";

interface ServerTokens {
  valid_session_id: string;
  valid_learner_token: string;
  instructor_token: string;
  no_consent_session_id: string;
  no_consent_token: string;
  terminated_session_id: string;
  terminated_token: string;
  participant_id: string;
}

let pythonProc: ChildProcess;
let serverTokens: ServerTokens;
let originPort: number = 8811;
let mf: Miniflare;
let gatewayWsUrl: string;
let gatewayHttpUrl: string;

beforeAll(async () => {
  // 1. Bundle Gateway Worker with esbuild
  const indexPath = path.resolve(__dirname, "../src/index.ts");
  const buildResult = await esbuild.build({
    entryPoints: [indexPath],
    bundle: true,
    format: "esm",
    target: "es2022",
    write: false,
  });
  const workerScript = buildResult.outputFiles[0].text;

  // 2. Spawn Python FastAPI test server
  const rootDir = path.resolve(__dirname, "../..");
  const pythonPath = path.join(rootDir, ".venv/Scripts/python.exe");
  const scriptPath = path.join(rootDir, "tests/integration/run_test_ws_server.py");

  pythonProc = spawn(pythonPath, [scriptPath, "--port", String(originPort)], {
    stdio: ["pipe", "pipe", "pipe"],
  });

  serverTokens = await new Promise<ServerTokens>((resolve, reject) => {
    pythonProc.stdout?.on("data", (chunk) => {
      const text = chunk.toString();
      console.log("[Python STDOUT]:", text);
      const match = text.match(/READY:(\{.*\})/);
      if (match) {
        resolve(JSON.parse(match[1]));
      }
    });
    pythonProc.stderr?.on("data", (chunk) => {
      console.error("[Python STDERR]:", chunk.toString());
    });
    pythonProc.on("error", reject);
    setTimeout(() => reject(new Error("Timeout waiting for FastAPI backend to start")), 10000);
  });

  // 3. Start Miniflare with real Cloudflare Workers runtime
  mf = new Miniflare({
    modules: true,
    script: workerScript,
    durableObjects: {
      SESSION_GATEWAY: "SessionGatewayDO",
    },
    bindings: {
      ORIGIN_WS_URL: `http://127.0.0.1:${originPort}/ws`,
      ENVIRONMENT: "test",
    },
    compatibilityDate: "2026-08-01",
    compatibilityFlags: ["nodejs_compat"],
    log: new Log(LogLevel.DEBUG),
  });

  const mfUrl = await mf.ready;
  gatewayHttpUrl = mfUrl.origin;
  gatewayWsUrl = `${mfUrl.origin.replace(/^http/, "ws")}/ws`;
}, 20000);

afterAll(async () => {
  if (mf) {
    await mf.dispose();
  }
  if (pythonProc) {
    pythonProc.kill();
  }
});

describe("Cloudflare Durable Objects Gateway E2E Integration Suite", () => {
  it("GET /health returns 200 with service metadata", async () => {
    const res = await fetch(`${gatewayHttpUrl}/health`);
    expect(res.status).toBe(200);
    const body = (await res.json()) as any;
    expect(body.status).toBe("healthy");
    expect(body.service).toBe("proctoring-engine-gateway");
  });

  it("GET /ws without token returns 400 session_id_missing", async () => {
    const res = await fetch(`${gatewayHttpUrl}/ws`);
    expect(res.status).toBe(400);
    const body = (await res.json()) as any;
    expect(body.error).toBe("session_id_missing");
  });

  it("rejects connection when token signature is invalid", async () => {
    // Generate token with tampered signature
    const header = btoa(JSON.stringify({ alg: "HS256", typ: "JWT" }))
      .replace(/=/g, "")
      .replace(/\+/g, "-")
      .replace(/\//g, "_");
    const payload = btoa(
      JSON.stringify({
        sub: serverTokens.participant_id,
        sid: serverTokens.valid_session_id,
        role: "learner",
        iss: "proctoring-engine",
        aud: "proctoring-client",
        iat: Math.floor(Date.now() / 1000),
        exp: Math.floor(Date.now() / 1000) + 3600,
        jti: "forged-token-jti",
      })
    )
      .replace(/=/g, "")
      .replace(/\+/g, "-")
      .replace(/\//g, "_");
    const forgedToken = `${header}.${payload}.forged_signature_here`;
    const subprotocol = `proctoring-v1.${forgedToken}`;

    const ws = new globalThis.WebSocket(gatewayWsUrl, subprotocol) as any;

    const closeResult = await new Promise<{ code: number; reason: string }>((resolve, reject) => {
      ws.addEventListener("close", (evt: any) => resolve({ code: evt.code, reason: evt.reason }));
      ws.addEventListener("error", () => {});
      setTimeout(() => reject(new Error("Timeout waiting for rejection close")), 5000);
    });

    console.log("[E2E Rejection Result - Invalid Token]:", closeResult);
    expect(closeResult.code).toBe(4001);
    expect(closeResult.reason).toContain("Invalid token");
  });

  it("rejects connection when connecting with non-learner token (instructor)", async () => {
    const subprotocol = `proctoring-v1.${serverTokens.instructor_token}`;
    const ws = new globalThis.WebSocket(gatewayWsUrl, subprotocol) as any;

    const closeResult = await new Promise<{ code: number; reason: string }>((resolve, reject) => {
      ws.addEventListener("close", (evt: any) => resolve({ code: evt.code, reason: evt.reason }));
      ws.addEventListener("error", () => {});
      setTimeout(() => reject(new Error("Timeout waiting for rejection close")), 5000);
    });

    console.log("[E2E Rejection Result - Non-Learner]:", closeResult);
    expect(closeResult.code).toBe(4005);
    expect(closeResult.reason).toContain("Only learner-role tokens may open a telemetry connection");
  });

  it("rejects connection when consent has not been recorded", async () => {
    const subprotocol = `proctoring-v1.${serverTokens.no_consent_token}`;
    const ws = new globalThis.WebSocket(gatewayWsUrl, subprotocol) as any;

    const closeResult = await new Promise<{ code: number; reason: string }>((resolve, reject) => {
      ws.addEventListener("close", (evt: any) => resolve({ code: evt.code, reason: evt.reason }));
      ws.addEventListener("error", () => {});
      setTimeout(() => reject(new Error("Timeout waiting for rejection close")), 5000);
    });

    console.log("[E2E Rejection Result - No Consent]:", closeResult);
    expect(closeResult.code).toBe(4009);
    expect(closeResult.reason).toContain("Consent has not been recorded for this session");
  });

  it("rejects connection when session is in terminal state", async () => {
    const subprotocol = `proctoring-v1.${serverTokens.terminated_token}`;
    const ws = new globalThis.WebSocket(gatewayWsUrl, subprotocol) as any;

    const closeResult = await new Promise<{ code: number; reason: string }>((resolve, reject) => {
      ws.addEventListener("close", (evt: any) => resolve({ code: evt.code, reason: evt.reason }));
      ws.addEventListener("error", () => {});
      setTimeout(() => reject(new Error("Timeout waiting for rejection close")), 5000);
    });

    console.log("[E2E Rejection Result - Terminated Session]:", closeResult);
    expect(closeResult.code).toBe(4003);
    expect(closeResult.reason).toContain("Session not found or in terminal state");
  });

  it("streams telemetry_light to origin and receives monotonic ACK over DO gateway", async () => {
    const subprotocol = `proctoring-v1.${serverTokens.valid_learner_token}`;
    const ws = new globalThis.WebSocket(gatewayWsUrl, subprotocol) as any;

    const receivedAcks: any[] = [];

    await new Promise<void>((resolve, reject) => {
      ws.addEventListener("open", () => {
        // Send first telemetry frame
        const msg = {
          type: "telemetry_light",
          session_id: serverTokens.valid_session_id,
          captured_at: new Date().toISOString(),
          payload: {
            modality: "face_presence",
            face_count: 1,
            confidence: 0.99,
            bbox: [0.1, 0.1, 0.4, 0.4],
          },
        };
        ws.send(JSON.stringify(msg));
      });

      ws.addEventListener("message", (evt: any) => {
        const data = JSON.parse(evt.data.toString());
        receivedAcks.push(data);
        if (receivedAcks.length === 1) {
          // Send second telemetry frame
          const msg2 = {
            type: "telemetry_light",
            session_id: serverTokens.valid_session_id,
            captured_at: new Date().toISOString(),
            payload: {
              modality: "face_presence",
              face_count: 1,
              confidence: 0.95,
              bbox: [0.1, 0.1, 0.4, 0.4],
            },
          };
          ws.send(JSON.stringify(msg2));
        } else if (receivedAcks.length >= 2) {
          resolve();
        }
      });

      ws.addEventListener("error", (err: any) => reject(err));
      ws.addEventListener("close", (evt: any) => {
        if (receivedAcks.length < 2) {
          reject(new Error(`WebSocket closed prematurely with code ${evt.code}: ${evt.reason}`));
        }
      });

      setTimeout(() => reject(new Error("Timeout waiting for ACKs")), 5000);
    });

    ws.close(1000, "Clean client close");

    expect(receivedAcks).toHaveLength(2);
    expect(receivedAcks[0].type).toBe("ack");
    expect(receivedAcks[0].payload.seq).toBe(0);
    expect(receivedAcks[1].type).toBe("ack");
    expect(receivedAcks[1].payload.seq).toBe(1);
  });

  it("handles 1:1 reconnection cleanly with verified fresh origin connection lifecycle and state teardown", async () => {
    const subprotocol = `proctoring-v1.${serverTokens.valid_learner_token}`;

    // Helper to query origin server stats
    async function getOriginStats() {
      const res = await fetch(`http://127.0.0.1:${originPort}/test/connection-stats`);
      return (await res.json()) as {
        total_connections: number;
        active_connections: number;
        connection_history: Array<{
          connection_id: string;
          started_at: string;
          ended_at: string | null;
          status: string;
        }>;
      };
    }

    const initialStats = await getOriginStats();

    // -------------------------------------------------------------
    // Connection 1: Open, verify active origin connection & seq=0 ACK
    // -------------------------------------------------------------
    const ws1 = new globalThis.WebSocket(gatewayWsUrl, subprotocol) as any;
    const ack1 = await new Promise<any>((resolve, reject) => {
      ws1.addEventListener("open", () => {
        ws1.send(
          JSON.stringify({
            type: "telemetry_light",
            session_id: serverTokens.valid_session_id,
            captured_at: new Date().toISOString(),
            payload: {
              modality: "face_presence",
              face_count: 1,
              confidence: 0.99,
              bbox: [0.2, 0.2, 0.3, 0.3],
            },
          })
        );
      });
      ws1.addEventListener("message", (evt: any) => resolve(JSON.parse(evt.data.toString())));
      ws1.addEventListener("error", reject);
      setTimeout(() => reject(new Error("Timeout on WS1")), 5000);
    });

    expect(ack1.type).toBe("ack");
    // Fresh connection 1 must start sequence at 0
    expect(ack1.payload.seq).toBe(0);

    const stats1 = await getOriginStats();
    expect(stats1.total_connections).toBe(initialStats.total_connections + 1);
    expect(stats1.active_connections).toBe(1);
    const conn1Record = stats1.connection_history[stats1.connection_history.length - 1];
    expect(conn1Record.status).toBe("active");
    expect(conn1Record.ended_at).toBeNull();
    const conn1Id = conn1Record.connection_id;

    // -------------------------------------------------------------
    // Teardown Connection 1: Close and verify full origin resource release
    // -------------------------------------------------------------
    ws1.close(1000, "Normal disconnect");

    // Wait for origin teardown to settle
    let statsAfterClose1: any;
    for (let i = 0; i < 20; i++) {
      await new Promise((r) => setTimeout(r, 50));
      statsAfterClose1 = await getOriginStats();
      if (statsAfterClose1.active_connections === 0) break;
    }

    expect(statsAfterClose1.active_connections).toBe(0);
    const conn1ClosedRecord = statsAfterClose1.connection_history.find(
      (c: any) => c.connection_id === conn1Id
    );
    expect(conn1ClosedRecord.status).toBe("closed");
    expect(conn1ClosedRecord.ended_at).not.toBeNull();

    // -------------------------------------------------------------
    // Connection 2: Reconnect with same token to active session
    // -------------------------------------------------------------
    const ws2 = new globalThis.WebSocket(gatewayWsUrl, subprotocol) as any;
    const ack2 = await new Promise<any>((resolve, reject) => {
      ws2.addEventListener("open", () => {
        ws2.send(
          JSON.stringify({
            type: "telemetry_light",
            session_id: serverTokens.valid_session_id,
            captured_at: new Date().toISOString(),
            payload: {
              modality: "face_presence",
              face_count: 1,
              confidence: 0.97,
              bbox: [0.2, 0.2, 0.3, 0.3],
            },
          })
        );
      });
      ws2.addEventListener("message", (evt: any) => resolve(JSON.parse(evt.data.toString())));
      ws2.addEventListener("error", reject);
      setTimeout(() => reject(new Error("Timeout on WS2 reconnect")), 5000);
    });

    expect(ack2.type).toBe("ack");
    // CRITICAL: Fresh origin connection has fresh DeliveryService sequence starting at 0, NOT continuing from connection 1
    expect(ack2.payload.seq).toBe(0);

    const stats2 = await getOriginStats();
    expect(stats2.total_connections).toBe(initialStats.total_connections + 2);
    expect(stats2.active_connections).toBe(1);

    const conn2Record = stats2.connection_history[stats2.connection_history.length - 1];
    expect(conn2Record.status).toBe("active");
    expect(conn2Record.ended_at).toBeNull();
    const conn2Id = conn2Record.connection_id;

    // CRITICAL: Origin connection ID MUST be distinctly different, proving complete teardown & rebuild
    expect(conn2Id).not.toBe(conn1Id);

    // Clean close Connection 2
    ws2.close(1000, "Clean reconnected close");

    let statsFinal: any;
    for (let i = 0; i < 20; i++) {
      await new Promise((r) => setTimeout(r, 50));
      statsFinal = await getOriginStats();
      if (statsFinal.active_connections === 0) break;
    }
    expect(statsFinal.active_connections).toBe(0);
  });
});
