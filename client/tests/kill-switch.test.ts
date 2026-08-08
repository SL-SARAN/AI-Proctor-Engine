import { describe, it, expect, vi, beforeEach } from 'vitest';
import { handleKillSwitch, KillSwitchDeps, _resetHandledFlagIds } from '../src/kill-switch.js';
import { KillSwitchPayload } from '../src/envelope.js';
import { RollingBuffer } from '../src/rolling-buffer.js';
import { WsClient } from '../src/ws-client.js';

function makeMockWs(): WsClient {
  return {
    send: vi.fn(),
    disconnect: vi.fn(),
    getStatus: vi.fn().mockReturnValue('connected'),
    connect: vi.fn(),
  } as unknown as WsClient;
}

describe('handleKillSwitch', () => {
  let mockWs: WsClient;
  let buffer: RollingBuffer;
  let deps: KillSwitchDeps;

  beforeEach(() => {
    _resetHandledFlagIds();
    // Clean up any overlays from previous tests
    document.querySelectorAll('#proctoring-kill-switch-overlay').forEach(el => el.remove());
    mockWs = makeMockWs();
    buffer = new RollingBuffer({ windowMs: 10_000 });
    deps = {
      ws: mockWs,
      sessionId: 'session-123',
      rollingBuffer: buffer,
    };
  });

  it('drains the rolling buffer and returns the entries', () => {
    const now = Date.now();
    buffer.push({ data: 'frame-1', timestamp: now });
    buffer.push({ data: 'frame-2', timestamp: now + 100 });

    const payload: KillSwitchPayload = {
      reason: 'second_person_detected',
      flag_id: 'flag-abc',
    };

    const result = handleKillSwitch(payload, deps);
    expect(result.bufferedEntries).toHaveLength(2);
    expect(result.bufferedEntries[0]!.data).toBe('frame-1');
    expect(buffer.size()).toBe(0); // drained
  });

  it('sends a kill_switch_ack envelope via the WebSocket', () => {
    const payload: KillSwitchPayload = {
      reason: 'gaze_frequency_exceeded',
      flag_id: 'flag-xyz',
    };

    handleKillSwitch(payload, deps);

    expect(mockWs.send).toHaveBeenCalledTimes(1);
    const sentMsg = (mockWs.send as ReturnType<typeof vi.fn>).mock.calls[0]![0];
    expect(sentMsg.type).toBe('kill_switch_ack');
    expect(sentMsg.session_id).toBe('session-123');
    expect(sentMsg.payload.flag_id).toBe('flag-xyz');
  });

  it('returns the flag_id and reason', () => {
    const payload: KillSwitchPayload = {
      reason: 'accumulated_score_exceeded',
      flag_id: 'flag-999',
    };

    const result = handleKillSwitch(payload, deps);
    expect(result.flagId).toBe('flag-999');
    expect(result.reason).toBe('accumulated_score_exceeded');
  });

  it('handles empty rolling buffer gracefully', () => {
    const payload: KillSwitchPayload = {
      reason: 'second_person_detected',
      flag_id: 'flag-empty',
    };

    const result = handleKillSwitch(payload, deps);
    expect(result.bufferedEntries).toHaveLength(0);
    expect(mockWs.send).toHaveBeenCalledTimes(1);
  });

  it('creates a lockout overlay in the DOM', () => {
    const payload: KillSwitchPayload = {
      reason: 'second_person_detected',
      flag_id: 'flag-overlay',
    };

    handleKillSwitch(payload, deps);

    const overlay = document.getElementById('proctoring-kill-switch-overlay');
    expect(overlay).not.toBeNull();
    expect(overlay!.getAttribute('role')).toBe('alert');
    // Check the message is neutral
    expect(overlay!.textContent).toContain('pending review');
    expect(overlay!.textContent).not.toContain('terminated');
    expect(overlay!.textContent).not.toContain('cheating');
  });

  it('uses "pending review" message for all reason types', () => {
    for (const reason of ['second_person_detected', 'gaze_frequency_exceeded', 'accumulated_score_exceeded'] as const) {
      // Reset the dedup guard so each reason processes fresh
      _resetHandledFlagIds();
      // Reset the DOM
      const old = document.getElementById('proctoring-kill-switch-overlay');
      old?.remove();

      const payload: KillSwitchPayload = { reason, flag_id: `flag-${reason}` };
      handleKillSwitch(payload, deps);

      const overlay = document.getElementById('proctoring-kill-switch-overlay');
      expect(overlay).not.toBeNull();
      expect(overlay!.textContent).toContain('pending review');
    }
  });

  describe('dedup guard (item 8 fix)', () => {
    it('does not stack a second overlay for the same flag_id', () => {
      const payload: KillSwitchPayload = {
        reason: 'second_person_detected',
        flag_id: 'flag-dedup',
      };

      // First call — full handling
      const first = handleKillSwitch(payload, deps);
      expect(first.bufferedEntries).toHaveLength(0); // empty buffer
      expect(mockWs.send).toHaveBeenCalledTimes(1);

      // There should be exactly one overlay
      const overlays = document.querySelectorAll('#proctoring-kill-switch-overlay');
      expect(overlays).toHaveLength(1);

      // Add a buffer entry (this simulates data arriving between kills)
      buffer.push({ data: 'late-frame', timestamp: Date.now() });

      // Second call (retry) — should not stack overlay or drain buffer
      const retryPayload: KillSwitchPayload = {
        reason: 'retry',
        flag_id: 'flag-dedup', // same flag_id
      };
      const second = handleKillSwitch(retryPayload, deps);

      // Still returns empty (not the new buffer entry — it wasn't drained)
      expect(second.bufferedEntries).toHaveLength(0);

      // Ack is still sent (so server knows the retry was received)
      expect(mockWs.send).toHaveBeenCalledTimes(2);

      // Still only one overlay in the DOM
      const allOverlays = document.querySelectorAll('#proctoring-kill-switch-overlay');
      expect(allOverlays).toHaveLength(1);
    });

    it('sends ack on retry even when already handled', () => {
      const payload: KillSwitchPayload = {
        reason: 'gaze_frequency_exceeded',
        flag_id: 'flag-retry-ack',
      };

      handleKillSwitch(payload, deps);
      expect(mockWs.send).toHaveBeenCalledTimes(1);

      // Retry
      handleKillSwitch({ ...payload, reason: 'retry' }, deps);
      expect(mockWs.send).toHaveBeenCalledTimes(2);

      // Both acks carry the same flag_id
      const call1 = (mockWs.send as ReturnType<typeof vi.fn>).mock.calls[0]![0];
      const call2 = (mockWs.send as ReturnType<typeof vi.fn>).mock.calls[1]![0];
      expect(call1.payload.flag_id).toBe('flag-retry-ack');
      expect(call2.payload.flag_id).toBe('flag-retry-ack');
    });

    it('handles different flag_ids independently', () => {
      const payload1: KillSwitchPayload = {
        reason: 'second_person_detected',
        flag_id: 'flag-A',
      };
      const payload2: KillSwitchPayload = {
        reason: 'gaze_frequency_exceeded',
        flag_id: 'flag-B',
      };

      const first = handleKillSwitch(payload1, deps);
      const second = handleKillSwitch(payload2, deps);

      // Both should process fully (different flag_ids)
      expect(first.flagId).toBe('flag-A');
      expect(second.flagId).toBe('flag-B');
      expect(mockWs.send).toHaveBeenCalledTimes(2);
    });

    it('accepts "retry" as a valid reason type', () => {
      const payload: KillSwitchPayload = {
        reason: 'retry',
        flag_id: 'flag-retry-reason',
      };

      const result = handleKillSwitch(payload, deps);
      expect(result.reason).toBe('retry');
      expect(mockWs.send).toHaveBeenCalledTimes(1);
    });
  });
});
