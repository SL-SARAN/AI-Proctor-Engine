/**
 * Client entry point.
 *
 * Wires the redirect-fragment reader, the WebSocket client, the
 * browser-event listeners, the rolling buffer, and the kill-switch
 * handler together.
 *
 * NOTE: client-side inference (FaceDetector, FaceLandmarker) is turn N+9.
 * This turn (N+8) only handles WS connection + browser events + rolling
 * buffer + kill-switch UI.
 */

import { consumeRedirectFragment, RedirectError } from './redirect.js';
import { WsClient, WSStatus } from './ws-client.js';
import { attachBrowserEventListeners } from './browser-events.js';
import { buildEnvelope, BrowserEventType, KillSwitchPayload } from './envelope.js';
import { RollingBuffer } from './rolling-buffer.js';
import { handleKillSwitch } from './kill-switch.js';

function showError(msg: string): void {
  const el = document.getElementById('error-display');
  if (el) {
    el.textContent = msg;
    el.style.display = 'block';
  }
}

function updateStatusUI(status: WSStatus): void {
  const dot = document.getElementById('status-dot');
  const text = document.getElementById('status-text');
  if (dot) {
    dot.className = `dot ${status}`;
  }
  if (text) {
    const labels: Record<WSStatus, string> = {
      connecting: 'Connecting…',
      connected: 'Connected',
      reconnecting: 'Reconnecting…',
      disconnected_terminal: 'Disconnected',
    };
    text.textContent = labels[status];
  }
}

function boot(): void {
  // 1. Extract launch params from URL fragment
  let sessionToken: string;
  let sessionId: string;

  try {
    const params = consumeRedirectFragment(window);
    sessionToken = params.sessionToken;
    sessionId = params.sessionId;
  } catch (err) {
    if (err instanceof RedirectError) {
      showError(`Launch error: ${err.message}. Please relaunch from your LMS.`);
    } else {
      showError('Unexpected error during launch.');
    }
    return;
  }

  // 2. Create rolling buffer (10 seconds window)
  const rollingBuffer = new RollingBuffer({ windowMs: 10_000 });

  // 3. Resolve the WebSocket URL relative to the current page
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${protocol}//${window.location.host}/ws`;

  // 4. Create WebSocket client
  const ws = new WsClient({
    url: wsUrl,
    sessionToken,
    onStatusChange: updateStatusUI,
    onKillSwitch: (payload: KillSwitchPayload) => {
      handleKillSwitch(payload, {
        ws,
        sessionId,
        rollingBuffer,
      });
    },
    onDisconnect: (reason: string) => {
      showError(`Disconnected: ${reason}`);
    },
  });

  // 5. Attach browser event listeners
  const _detachBrowserEvents = attachBrowserEventListeners(
    (eventType: BrowserEventType, detail: Record<string, unknown>) => {
      const envelope = buildEnvelope('browser_event', sessionId, {
        event_type: eventType,
        detail,
      });
      ws.send(envelope);
    }
  );

  // 6. Connect
  ws.connect();
}

// Boot on DOMContentLoaded if document is still loading, otherwise
// run directly (the script is type="module" so it's deferred).
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
