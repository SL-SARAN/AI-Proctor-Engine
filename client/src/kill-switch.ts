/**
 * Kill-switch handler.
 *
 * When the server sends `{ type: "kill_switch", payload: { reason, flag_id } }`,
 * this module:
 *   1. Locks the UI (disables form controls, overlays a message).
 *   2. Renders a "session ended, pending review" message.
 *      **NOT "terminated"** — the proctor can overturn via the
 *      ProctorReview fast-track undo (see docs/05, Path 3), and
 *      ExamSession.status can become `reinstated`.
 *   3. Drains the rolling buffer (returns all buffered entries).
 *   4. Sends the kill_switch_ack envelope.
 *   5. Notifies the host (e.g. for postMessage to the LMS iframe).
 */

import { KillSwitchPayload, buildEnvelope, KillSwitchAcknowledgePayload } from './envelope.js';
import { RollingBuffer, RollingBufferEntry } from './rolling-buffer.js';
import { WsClient } from './ws-client.js';

export interface KillSwitchResult {
  /** The buffered entries that should be uploaded as evidence. */
  bufferedEntries: RollingBufferEntry[];
  /** The flag_id from the kill-switch payload. */
  flagId: string;
  /** The reason string from the kill-switch payload. */
  reason: string;
}

export interface KillSwitchDeps {
  ws: WsClient;
  sessionId: string;
  rollingBuffer: RollingBuffer;
  /** DOM element where the lockout overlay is rendered. */
  overlayContainerId?: string;
}

/**
 * Human-facing messages for each kill-switch reason.
 *
 * Deliberately neutral and non-accusatory: the human review process,
 * not the client UI, is where a disputed termination gets adjudicated.
 */
const REASON_MESSAGES: Record<string, string> = {
  second_person: 'This session has ended and is pending review.',
  gaze_away_frequency: 'This session has ended and is pending review.',
  accumulated_score: 'This session has ended and is pending review.',
  liveness_check_failed: 'This session has ended and is pending review.',
  identity_mismatch: 'This session has ended and is pending review.',
};

const DEFAULT_MESSAGE = 'This session has ended and is pending review.';

/**
 * Track which flag IDs have already been handled.
 *
 * Prevents duplicate lockout overlays if the server retries a kill-switch
 * delivery because the first acknowledgement was lost (network issue,
 * race, etc.).  The set is module-scoped since a kill-switch is session-
 * terminal — there's no scenario where we need to "reset" it.
 */
const handledFlagIds = new Set<string>();

/**
 * Reset the dedup guard.  **Test-only** — production code never needs
 * this because a kill-switch is session-terminal and the set lives
 * for the entire page lifetime.
 */
export function _resetHandledFlagIds(): void {
  handledFlagIds.clear();
}

/**
 * Build and inject the lockout overlay into the page DOM.
 */
function showLockoutOverlay(containerId: string | undefined, reason: string): void {
  if (typeof document === 'undefined') return;

  const container = containerId
    ? document.getElementById(containerId)
    : document.body;
  if (!container) return;

  const message = REASON_MESSAGES[reason] ?? DEFAULT_MESSAGE;

  // Lock all inputs and buttons
  const inputs = document.querySelectorAll('input, button, textarea, select');
  inputs.forEach((el) => {
    (el as HTMLInputElement).disabled = true;
  });

  // Create overlay
  const overlay = document.createElement('div');
  overlay.id = 'proctoring-kill-switch-overlay';
  overlay.setAttribute('role', 'alert');
  overlay.setAttribute('aria-live', 'assertive');
  overlay.style.cssText = [
    'position: fixed',
    'top: 0',
    'left: 0',
    'width: 100%',
    'height: 100%',
    'background: rgba(0, 0, 0, 0.85)',
    'display: flex',
    'align-items: center',
    'justify-content: center',
    'z-index: 999999',
  ].join(';');

  const box = document.createElement('div');
  box.style.cssText = [
    'background: #fff',
    'padding: 2rem 3rem',
    'border-radius: 8px',
    'text-align: center',
    'max-width: 500px',
    'font-family: system-ui, sans-serif',
  ].join(';');

  const heading = document.createElement('h2');
  heading.textContent = 'Session Ended';
  heading.style.marginBottom = '1rem';
  box.appendChild(heading);

  const para = document.createElement('p');
  para.textContent = message;
  box.appendChild(para);

  overlay.appendChild(box);
  container.appendChild(overlay);
}

/**
 * Handle a kill-switch event. Returns the buffer contents for
 * evidence upload.
 *
 * If this `flag_id` has already been handled (e.g. the server retried
 * because the first ack was lost), the function still sends a fresh
 * ack so the server can advance its state machine, but does NOT
 * stack a second overlay or drain the (already-empty) buffer.
 */
export function handleKillSwitch(
  payload: KillSwitchPayload,
  deps: KillSwitchDeps
): KillSwitchResult {
  // Dedup guard: re-ack but don't re-lock on retry
  if (handledFlagIds.has(payload.flag_id)) {
    // Still send an ack — the server may not have received the first one
    const ackPayload: KillSwitchAcknowledgePayload = { flag_id: payload.flag_id };
    const ackEnvelope = buildEnvelope(
      'kill_switch_ack',
      deps.sessionId,
      ackPayload,
    );
    deps.ws.send(ackEnvelope);

    return {
      bufferedEntries: [],
      flagId: payload.flag_id,
      reason: payload.reason,
    };
  }

  // First time seeing this flag_id — handle normally
  handledFlagIds.add(payload.flag_id);

  // 1. Lock the UI
  showLockoutOverlay(deps.overlayContainerId, payload.reason);

  // 2. Drain the rolling buffer
  const bufferedEntries = deps.rollingBuffer.drain();

  // 3. Send kill_switch_ack back to the server
  const ackPayload: KillSwitchAcknowledgePayload = { flag_id: payload.flag_id };
  const ackEnvelope = buildEnvelope(
    'kill_switch_ack',
    deps.sessionId,
    ackPayload,
  );
  deps.ws.send(ackEnvelope);

  // 4. The caller (main.ts or test) handles the evidence upload
  return {
    bufferedEntries,
    flagId: payload.flag_id,
    reason: payload.reason,
  };
}
