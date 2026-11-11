import {
  ClientEnvelope,
  parseServerMessage,
  KillSwitchPayload,
} from './envelope.js';

export type WSStatus = 'connecting' | 'connected' | 'reconnecting' | 'disconnected_terminal';

export interface WsClientOptions {
  url: string;
  sessionToken: string;
  onStatusChange?: (status: WSStatus) => void;
  onKillSwitch?: (payload: KillSwitchPayload) => void;
  onDisconnect?: (reason: string) => void;
  // Make these overridable for tests so we don't wait seconds
  reconnectBaseMs?: number;
  reconnectMaxMs?: number;
  pingIntervalMs?: number;
}

/**
 * Manages the authenticated WebSocket connection.
 * Sends the session token as a Sec-WebSocket-Protocol subprotocol value
 * rather than a query parameter, per the RFC 6455 auth pattern used by
 * the orchestrator.
 */
export class WsClient {
  private ws: WebSocket | null = null;
  private readonly options: Required<WsClientOptions>;
  private status: WSStatus = 'disconnected_terminal';
  private reconnectAttempt = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private pingTimer: ReturnType<typeof setInterval> | null = null;

  // Track if we purposefully disconnected or if it's considered terminal
  private isTerminal = false;

  constructor(options: WsClientOptions) {
    this.options = {
      onStatusChange: () => {},
      onKillSwitch: () => {},
      onDisconnect: () => {},
      reconnectBaseMs: 1000,
      reconnectMaxMs: 30000,
      pingIntervalMs: 15000,
      ...options,
    };
  }

  public connect() {
    if (this.isTerminal) return;
    this.setStatus('connecting');

    // Token goes into the subprotocol header. The server explicitly rejects
    // ?session_token=... on the query string.
    // Offer the bare 'proctoring-v1' marker alongside the token-bearing
    // subprotocol so the server can select only the bare marker in its
    // 101 response without echoing the JWT back.
    const subprotocols = ['proctoring-v1', `proctoring-v1.${this.options.sessionToken}`];

    try {
      this.ws = new WebSocket(this.options.url, subprotocols);
    } catch (err) {
      this.handleTerminalDisconnect(`WebSocket creation failed: ${(err as Error).message}`);
      return;
    }

    this.ws.onopen = this.handleOpen.bind(this);
    this.ws.onmessage = this.handleMessage.bind(this);
    this.ws.onclose = this.handleClose.bind(this);
    this.ws.onerror = this.handleError.bind(this);
  }

  public disconnect() {
    this.isTerminal = true;
    this.stopHeartbeat();
    this.clearReconnect();
    if (this.ws) {
      // 1000 = Normal closure
      this.ws.close(1000, 'Client intentionally disconnecting');
      this.ws = null;
    }
    this.setStatus('disconnected_terminal');
  }

  /**
   * Enqueue a message. If the session is not validated and connected,
   * it is synchronously dropped. (We don't queue messages offline; we're
   * sampling live data, and we must not emit telemetry before session_ready.)
   */
  public send(msg: ClientEnvelope) {
    if (this.ws?.readyState === WebSocket.OPEN && this.status === 'connected') {
      this.ws.send(JSON.stringify(msg));
    }
  }

  private handleOpen() {
    // Do NOT mark 'connected' yet: the server completes the 101 handshake
    // before checking auth, consent, and session status so that rejections
    // can travel as 4xxx RFC 6455 close frames. We wait for 'session_ready'
    // before transitioning to 'connected' and triggering camera capture.
  }

  private handleMessage(event: MessageEvent) {
    const rawData = event.data;

    if (rawData === '{"type":"ping"}') {
      // Server ping, we must reply pong so the server heartbeat doesn't timeout
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ type: 'pong' }));
      }
      return;
    }

    const msg = parseServerMessage(rawData);
    if (!msg) return; // Ignore unknown or malformed messages

    if (msg.type === 'session_ready') {
      this.reconnectAttempt = 0;
      this.setStatus('connected');
      this.startHeartbeat();
      return;
    }

    if (msg.type === 'kill_switch') {
      // We got the kill switch. This is structurally terminal.
      this.isTerminal = true; // Stop trying to reconnect if the connection subsequently drops
      this.options.onKillSwitch(msg.payload);
    }
  }

  private handleClose(event: CloseEvent) {
    this.stopHeartbeat();
    this.ws = null;

    if (this.isTerminal) {
      this.setStatus('disconnected_terminal');
      return;
    }

    // Server-defined 4000-level codes are terminal
    if (event.code >= 4000 && event.code < 5000) {
      this.handleTerminalDisconnect(`Authentication or session error (${event.code}): ${event.reason}`);
      return;
    }

    // Otherwise, we lost connection unexpectedly. Try to reconnect.
    this.scheduleReconnect();
  }

  private handleError(event: Event) {
    // We just log it; the onclose event will fire immediately after for
    // any underlying transport error.
    console.warn('WebSocket error:', event);
  }

  private handleTerminalDisconnect(reason: string) {
    this.isTerminal = true;
    this.setStatus('disconnected_terminal');
    this.options.onDisconnect(reason);
  }

  private scheduleReconnect() {
    if (this.isTerminal) return;
    this.setStatus('reconnecting');

    const base = this.options.reconnectBaseMs;
    const max = this.options.reconnectMaxMs;

    // Exponential backoff with jitter
    const delay = Math.min(max, base * Math.pow(2, this.reconnectAttempt));
    const jitter = delay * 0.2 * (Math.random() - 0.5); // +/- 10%
    const finalDelay = Math.max(0, delay + jitter);

    this.reconnectAttempt++;

    this.reconnectTimer = setTimeout(() => {
      this.connect();
    }, finalDelay);
  }

  private clearReconnect() {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private startHeartbeat() {
    this.stopHeartbeat();
    this.pingTimer = setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ type: 'ping' }));
      }
    }, this.options.pingIntervalMs);
  }

  private stopHeartbeat() {
    if (this.pingTimer !== null) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
  }

  private setStatus(newStatus: WSStatus) {
    if (this.status !== newStatus) {
      this.status = newStatus;
      this.options.onStatusChange(newStatus);
    }
  }

  public getStatus(): WSStatus {
    return this.status;
  }
}
