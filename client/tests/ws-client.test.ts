import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { WsClient, WSStatus } from '../src/ws-client.js';

/**
 * Lightweight mock WebSocket for unit tests.
 * jsdom does not provide WebSocket; we inject our own.
 */
class MockWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;

  readyState = MockWebSocket.CONNECTING;
  url: string;
  protocol: string;
  sentMessages: string[] = [];

  onopen: ((this: MockWebSocket, ev: Event) => void) | null = null;
  onmessage: ((this: MockWebSocket, ev: MessageEvent) => void) | null = null;
  onclose: ((this: MockWebSocket, ev: CloseEvent) => void) | null = null;
  onerror: ((this: MockWebSocket, ev: Event) => void) | null = null;

  constructor(url: string, protocol?: string | string[]) {
    this.url = url;
    this.protocol = typeof protocol === 'string' ? protocol : (Array.isArray(protocol) ? protocol[0] ?? '' : '');
  }

  send(data: string) {
    this.sentMessages.push(data);
  }

  close(_code?: number, _reason?: string) {
    this.readyState = MockWebSocket.CLOSED;
  }

  // Test helpers
  simulateOpen() {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.call(this, new Event('open'));
  }

  simulateMessage(data: string) {
    this.onmessage?.call(this, { data } as MessageEvent);
  }

  simulateClose(code = 1000, reason = '') {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.call(this, { code, reason } as CloseEvent);
  }

  simulateError() {
    this.onerror?.call(this, new Event('error'));
  }
}

describe('WsClient', () => {
  let originalWebSocket: unknown;
  let lastCreatedWs: MockWebSocket | null;

  beforeEach(() => {
    lastCreatedWs = null;
    originalWebSocket = (globalThis as Record<string, unknown>).WebSocket;
    (globalThis as Record<string, unknown>).WebSocket = class extends MockWebSocket {
      constructor(url: string, protocol?: string | string[]) {
        super(url, protocol);
        lastCreatedWs = this;
      }
    } as unknown as typeof WebSocket;
    // Also need OPEN/CLOSED constants on the global
    Object.assign(globalThis as object, {
      CONNECTING: 0,
      OPEN: 1,
      CLOSING: 2,
      CLOSED: 3,
    });
  });

  afterEach(() => {
    (globalThis as Record<string, unknown>).WebSocket = originalWebSocket;
    vi.restoreAllMocks();
  });

  function makeClient(overrides?: Record<string, unknown>) {
    return new WsClient({
      url: 'wss://example.com/ws',
      sessionToken: 'test-jwt-token',
      reconnectBaseMs: 10,
      reconnectMaxMs: 100,
      ...overrides,
    } as ConstructorParameters<typeof WsClient>[0]);
  }

  it('sends the token via the subprotocol header, not a query param', () => {
    const client = makeClient();
    client.connect();

    expect(lastCreatedWs).not.toBeNull();
    expect(lastCreatedWs!.url).toBe('wss://example.com/ws');
    // Token must be in the protocol, not the URL
    expect(lastCreatedWs!.url).not.toContain('token=');
    expect(lastCreatedWs!.protocol).toBe('proctoring-v1.test-jwt-token');
  });

  it('transitions to "connected" on open', () => {
    const statuses: WSStatus[] = [];
    const client = makeClient({ onStatusChange: (s: WSStatus) => statuses.push(s) });
    client.connect();
    lastCreatedWs!.simulateOpen();
    expect(statuses).toContain('connected');
    expect(client.getStatus()).toBe('connected');
  });

  it('transitions to "disconnected_terminal" on 4xxx close code', () => {
    const statuses: WSStatus[] = [];
    const client = makeClient({ onStatusChange: (s: WSStatus) => statuses.push(s) });
    client.connect();
    lastCreatedWs!.simulateOpen();
    lastCreatedWs!.simulateClose(4001, 'Auth failed');
    expect(client.getStatus()).toBe('disconnected_terminal');
  });

  it('transitions to "reconnecting" on non-terminal close', () => {
    vi.useFakeTimers();
    const statuses: WSStatus[] = [];
    const client = makeClient({ onStatusChange: (s: WSStatus) => statuses.push(s) });
    client.connect();
    lastCreatedWs!.simulateOpen();
    lastCreatedWs!.simulateClose(1006, 'Abnormal');
    expect(statuses).toContain('reconnecting');
    client.disconnect(); // Prevent timer leaks
    vi.useRealTimers();
  });

  it('responds to server pings with pong', () => {
    const client = makeClient();
    client.connect();
    lastCreatedWs!.simulateOpen();
    lastCreatedWs!.simulateMessage('{"type":"ping"}');
    const sent = lastCreatedWs!.sentMessages;
    expect(sent[sent.length - 1]).toBe('{"type":"pong"}');
  });

  it('calls onKillSwitch when a kill_switch message arrives', () => {
    const killSwitchHandler = vi.fn();
    const client = makeClient({ onKillSwitch: killSwitchHandler });
    client.connect();
    lastCreatedWs!.simulateOpen();
    lastCreatedWs!.simulateMessage(JSON.stringify({
      type: 'kill_switch',
      payload: { reason: 'second_person_detected', flag_id: 'flag-1' },
    }));
    expect(killSwitchHandler).toHaveBeenCalledWith({
      reason: 'second_person_detected',
      flag_id: 'flag-1',
    });
  });

  it('marks connection as terminal after kill_switch', () => {
    const client = makeClient();
    client.connect();
    lastCreatedWs!.simulateOpen();
    lastCreatedWs!.simulateMessage(JSON.stringify({
      type: 'kill_switch',
      payload: { reason: 'gaze_frequency_exceeded', flag_id: 'flag-2' },
    }));
    // After a kill_switch, a subsequent close should not trigger reconnect
    lastCreatedWs!.simulateClose(1000, 'Normal');
    expect(client.getStatus()).toBe('disconnected_terminal');
  });

  it('send() drops messages when WS is not open', () => {
    const client = makeClient();
    client.connect();
    // WS is still CONNECTING, not OPEN
    client.send({ type: 'browser_event', session_id: 's', captured_at: '', payload: { event_type: 'blur', detail: {} } });
    expect(lastCreatedWs!.sentMessages).toHaveLength(0);
  });

  it('send() delivers messages when WS is open', () => {
    const client = makeClient();
    client.connect();
    lastCreatedWs!.simulateOpen();
    client.send({ type: 'browser_event', session_id: 's', captured_at: '', payload: { event_type: 'blur', detail: {} } });
    expect(lastCreatedWs!.sentMessages).toHaveLength(1);
  });

  it('disconnect() stops reconnection and marks terminal', () => {
    vi.useFakeTimers();
    const client = makeClient();
    client.connect();
    lastCreatedWs!.simulateOpen();
    client.disconnect();
    expect(client.getStatus()).toBe('disconnected_terminal');
    vi.useRealTimers();
  });

  it('ignores unknown server message types', () => {
    const killSwitchHandler = vi.fn();
    const client = makeClient({ onKillSwitch: killSwitchHandler });
    client.connect();
    lastCreatedWs!.simulateOpen();
    lastCreatedWs!.simulateMessage(JSON.stringify({ type: 'unknown_type', payload: {} }));
    expect(killSwitchHandler).not.toHaveBeenCalled();
  });

  it('ignores malformed JSON messages', () => {
    const client = makeClient();
    client.connect();
    lastCreatedWs!.simulateOpen();
    // Should not throw
    lastCreatedWs!.simulateMessage('not valid json{');
  });

  it('routes close code 4008 (identity backend unavailable) to handleTerminalDisconnect', () => {
    // Regression test: the server-side identity verification handshake
    // can close the WebSocket with code 4008 when the identity backend
    // could not be constructed AND no valid override exists. The
    // client must surface this through the existing 4000–4999
    // terminal-disconnect path so the application's "session
    // unavailable" page renders — the entire reason Option 1 was
    // chosen over failing at LTI launch time.
    //
    // The 4000–4999 handling is shared (no per-code branching in
    // ws-client.ts). This test confirms that 4008 in particular lands
    // in handleTerminalDisconnect, NOT the reconnect path.
    let disconnectReason = '';
    const statuses: WSStatus[] = [];
    const client = makeClient({
      onStatusChange: (s: WSStatus) => statuses.push(s),
      onDisconnect: (reason: string) => { disconnectReason = reason; },
    });
    client.connect();
    lastCreatedWs!.simulateOpen();
    lastCreatedWs!.simulateClose(4008, 'Identity backend unavailable');

    // Status must be 'disconnected_terminal', not 'reconnecting'.
    expect(client.getStatus()).toBe('disconnected_terminal');
    expect(statuses).toContain('disconnected_terminal');
    expect(statuses).not.toContain('reconnecting');

    // The disconnect reason must include the close code so the UI
    // layer can render the right page.
    expect(disconnectReason).toContain('4008');
    expect(disconnectReason).toContain('Identity backend unavailable');
  });
});
