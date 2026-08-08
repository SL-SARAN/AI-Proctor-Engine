/**
 * Client entry point.
 *
 * Wires the redirect-fragment reader, the WebSocket client, the
 * browser-event listeners, the rolling buffer, the kill-switch
 * handler, and the capture loop (client-side inference) together.
 *
 * Turn N+8: WS connection + browser events + rolling buffer + kill-switch UI.
 * Turn N+9: Client-side inference (FaceDetector, FaceLandmarker) + capture loop.
 */

import { consumeRedirectFragment, RedirectError } from './redirect.js';
import { WsClient, WSStatus } from './ws-client.js';
import { attachBrowserEventListeners } from './browser-events.js';
import { buildEnvelope, BrowserEventType, KillSwitchPayload } from './envelope.js';
import { RollingBuffer } from './rolling-buffer.js';
import { handleKillSwitch } from './kill-switch.js';
import { CaptureLoop } from './capture-loop.js';

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

  // 7. Capture loop — created here, but started only once the WebSocket
  //    is in the OPEN state. WsClient.send() silently drops messages
  //    before readyState === OPEN, so starting the capture loop before
  //    the connection is established would cause real data loss at the
  //    start of every session.
  let captureLoop: CaptureLoop | null = null;
  let captureLoopStarted = false;

  function startCaptureLoopOnce(): void {
    if (captureLoopStarted) return;
    captureLoopStarted = true;

    captureLoop = new CaptureLoop({
      sessionId,
      ws,
      rollingBuffer,
      config: {
        heavyFrameIntervalMs: 2500,    // Heavy frame every 2.5s
        maxLightFps: 15,                // Light inference at 15fps max
        lightFrameSendIntervalMs: 1000, // Send light telemetry every 1s
      },
    });

    captureLoop.start().catch((err) => {
      showError(`Camera/init error: ${err.message}. Reload the page or check camera permissions.`);
      console.error('Capture loop failed:', err);
    });
  }

  // 4. Create WebSocket client
  const ws = new WsClient({
    url: wsUrl,
    sessionToken,
    onStatusChange: (status: WSStatus) => {
      updateStatusUI(status);

      // Start capture loop once the WebSocket connection is established.
      // This ensures no telemetry is silently dropped during the
      // connecting / reconnecting window.
      if (status === 'connected') {
        startCaptureLoopOnce();
      }
    },
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
  attachBrowserEventListeners(
    (eventType: BrowserEventType, detail: Record<string, unknown>) => {
      const envelope = buildEnvelope('browser_event', sessionId, {
        event_type: eventType,
        detail,
      });
      ws.send(envelope);
    }
  );

  // 6. Connect — the capture loop starts via the onStatusChange callback
  //    once the connection reaches 'connected' state.
  ws.connect();

  // Clean up capture loop on page unload
  window.addEventListener('beforeunload', () => {
    if (captureLoop) {
      captureLoop.stop();
    }
  });
}

// Boot on DOMContentLoaded if document is still loading, otherwise
// run directly (the script is type="module" so it's deferred).
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
