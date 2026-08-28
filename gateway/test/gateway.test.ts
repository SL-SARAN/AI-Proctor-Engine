import { describe, it, expect } from "vitest";
import { extractSessionId } from "../src/index.js";

describe("extractSessionId", () => {
  it("extracts session_id from query parameter ?session_id=UUID", () => {
    const req = new Request("https://gateway.example.com/ws?session_id=123e4567-e89b-12d3-a456-426614174000");
    expect(extractSessionId(req)).toBe("123e4567-e89b-12d3-a456-426614174000");
  });

  it("extracts session_id from URL path /ws/UUID", () => {
    const req = new Request("https://gateway.example.com/ws/123e4567-e89b-12d3-a456-426614174000");
    expect(extractSessionId(req)).toBe("123e4567-e89b-12d3-a456-426614174000");
  });

  it("extracts session_id from Sec-WebSocket-Protocol JWT payload", () => {
    // Construct dummy JWT header and payload: {"session_id":"sess-uuid-999","role":"learner"}
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

  it("returns null when no session_id can be found", () => {
    const req = new Request("https://gateway.example.com/ws");
    expect(extractSessionId(req)).toBeNull();
  });
});
