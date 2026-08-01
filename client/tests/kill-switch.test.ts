import { describe, it, expect, vi, beforeEach } from 'vitest';
import { handleKillSwitch, KillSwitchDeps } from '../src/kill-switch.js';
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
});
