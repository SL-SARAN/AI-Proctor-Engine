/**
 * Tests for main.ts boot sequence & consent-gated capture startup.
 *
 * Verifies that opening the WebSocket (101 Switching Protocols) does NOT
 * trigger CaptureLoop or getUserMedia until the server sends `session_ready`.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

vi.hoisted(() => {
  (globalThis as Record<string, unknown>).__PROCTOR_TEST_NO_AUTOBOOT__ = true;
});

import { boot } from '../src/main.js';
import { CaptureLoop } from '../src/capture-loop.js';

class MockWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;

  readyState = MockWebSocket.CONNECTING;
  url: string;
  offeredProtocols: string[];
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;

  constructor(url: string, protocol?: string | string[]) {
    this.url = url;
    this.offeredProtocols = typeof protocol === 'string'
      ? [protocol]
      : (Array.isArray(protocol) ? protocol : []);
  }

  send(_data: string) {}
  close() {
    this.readyState = MockWebSocket.CLOSED;
  }

  simulateOpen() {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.(new Event('open'));
  }

  simulateMessage(data: string) {
    this.onmessage?.({ data } as MessageEvent);
  }

  simulateClose(code: number, reason: string) {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.({ code, reason } as CloseEvent);
  }
}

describe('main.ts boot consent & readiness gate', () => {
  let originalWebSocket: unknown;
  let originalMediaDevices: unknown;
  let lastWs: MockWebSocket | null = null;
  let getUserMediaSpy: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    lastWs = null;
    originalWebSocket = (globalThis as Record<string, unknown>).WebSocket;
    (globalThis as Record<string, unknown>).WebSocket = class extends MockWebSocket {
      constructor(url: string, protocol?: string | string[]) {
        super(url, protocol);
        lastWs = this;
      }
    };
    Object.assign(globalThis as object, {
      CONNECTING: 0,
      OPEN: 1,
      CLOSING: 2,
      CLOSED: 3,
    });

    getUserMediaSpy = vi.fn().mockRejectedValue(new Error('NotAllowedError'));
    originalMediaDevices = navigator.mediaDevices;
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia: getUserMediaSpy },
    });

    document.body.innerHTML = '<div id="error-display"></div><div id="status-dot"></div><div id="status-text"></div>';
    window.location.hash = '#session_token=test-jwt&session_id=11111111-1111-1111-1111-111111111111';
  });

  afterEach(() => {
    (globalThis as Record<string, unknown>).WebSocket = originalWebSocket;
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: originalMediaDevices,
    });
    vi.restoreAllMocks();
  });

  it('does NOT start CaptureLoop or call getUserMedia when WS opens and closes 4009 before session_ready', async () => {
    const startSpy = vi.spyOn(CaptureLoop.prototype, 'start');

    boot();
    expect(lastWs).not.toBeNull();

    // Server accepted 101 so it can send the 4009 close frame
    lastWs!.simulateOpen();
    expect(startSpy).not.toHaveBeenCalled();
    expect(getUserMediaSpy).not.toHaveBeenCalled();

    // Server closes with 4009 (no consent) before any session_ready
    lastWs!.simulateClose(4009, 'Consent has not been recorded for this session.');

    // Flush microtasks to verify no async start occurred
    await Promise.resolve();

    expect(startSpy).not.toHaveBeenCalled();
    expect(getUserMediaSpy).not.toHaveBeenCalled();
    expect(document.getElementById('error-display')?.textContent).toContain('4009');
  });

  it('starts CaptureLoop and requests getUserMedia once session_ready arrives', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const startSpy = vi.spyOn(CaptureLoop.prototype, 'start');

    boot();
    expect(lastWs).not.toBeNull();

    lastWs!.simulateOpen();
    expect(startSpy).not.toHaveBeenCalled();
    expect(getUserMediaSpy).not.toHaveBeenCalled();

    lastWs!.simulateMessage(JSON.stringify({
      type: 'session_ready',
      payload: {
        session_id: '11111111-1111-1111-1111-111111111111',
        status: 'active',
      },
    }));

    expect(startSpy).toHaveBeenCalledTimes(1);
    // CaptureLoop.start() synchronously advances to initMediaCapture -> getUserMedia
    expect(getUserMediaSpy).toHaveBeenCalledTimes(1);
  });
});
