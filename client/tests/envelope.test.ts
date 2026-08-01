import { describe, it, expect } from 'vitest';
import {
  buildEnvelope,
  parseServerMessage,
  VALID_BROWSER_EVENTS,
} from '../src/envelope.js';

describe('buildEnvelope', () => {
  it('builds a telemetry_light envelope with the correct shape', () => {
    const env = buildEnvelope('telemetry_light', 'session-1', {
      modality: 'face_presence',
      face_count: 1,
      confidence: 0.97,
    });
    expect(env.type).toBe('telemetry_light');
    expect(env.session_id).toBe('session-1');
    expect(env.payload.modality).toBe('face_presence');
    expect(typeof env.captured_at).toBe('string');
    // ISO-8601 should parse back
    expect(new Date(env.captured_at).toISOString()).toBe(env.captured_at);
  });

  it('uses a custom captured_at when provided', () => {
    const d = new Date('2026-07-31T12:00:00.000Z');
    const env = buildEnvelope('browser_event', 'session-1', { event_type: 'blur', detail: {} }, d);
    expect(env.captured_at).toBe('2026-07-31T12:00:00.000Z');
  });

  it('builds a kill_switch_ack envelope', () => {
    const env = buildEnvelope('kill_switch_ack', 'session-1', { flag_id: 'flag-abc' });
    expect(env.type).toBe('kill_switch_ack');
    expect(env.payload.flag_id).toBe('flag-abc');
  });

  it('builds an audio_chunk envelope with required fields', () => {
    const env = buildEnvelope('audio_chunk', 'session-1', {
      audio: 'base64data',
      sample_rate_hz: 16000,
      duration_ms: 2000,
    });
    expect(env.type).toBe('audio_chunk');
    expect(env.payload.sample_rate_hz).toBe(16000);
  });

  it('builds a telemetry_heavy_frame envelope', () => {
    const env = buildEnvelope('telemetry_heavy_frame', 'session-1', {
      frame: 'base64jpeg',
      resolution: [640, 480],
      encoding: 'jpeg',
    });
    expect(env.type).toBe('telemetry_heavy_frame');
    expect(env.payload.resolution).toEqual([640, 480]);
  });
});

describe('parseServerMessage', () => {
  it('parses a valid ack message', () => {
    const raw = JSON.stringify({
      type: 'ack',
      payload: { sequence_number: 1, accepted_events: 1 },
    });
    const msg = parseServerMessage(raw);
    expect(msg).not.toBeNull();
    expect(msg!.type).toBe('ack');
  });

  it('parses a valid kill_switch message', () => {
    const raw = JSON.stringify({
      type: 'kill_switch',
      payload: { reason: 'second_person_detected', flag_id: 'flag-123' },
    });
    const msg = parseServerMessage(raw);
    expect(msg).not.toBeNull();
    expect(msg!.type).toBe('kill_switch');
    if (msg!.type === 'kill_switch') {
      expect(msg!.payload.flag_id).toBe('flag-123');
    }
  });

  it('parses a valid policy_update message', () => {
    const raw = JSON.stringify({
      type: 'policy_update',
      payload: { policy_config_id: 'p-1', config_dump: {} },
    });
    const msg = parseServerMessage(raw);
    expect(msg).not.toBeNull();
    expect(msg!.type).toBe('policy_update');
  });

  it('returns null for invalid JSON', () => {
    expect(parseServerMessage('not json{')).toBeNull();
  });

  it('returns null for unknown type', () => {
    const raw = JSON.stringify({ type: 'unknown', payload: {} });
    expect(parseServerMessage(raw)).toBeNull();
  });

  it('returns null when type field is missing', () => {
    const raw = JSON.stringify({ payload: {} });
    expect(parseServerMessage(raw)).toBeNull();
  });

  it('returns null when payload field is missing', () => {
    const raw = JSON.stringify({ type: 'ack' });
    expect(parseServerMessage(raw)).toBeNull();
  });

  it('returns null for non-object input', () => {
    expect(parseServerMessage('"just a string"')).toBeNull();
  });

  it('returns null for null input', () => {
    expect(parseServerMessage('null')).toBeNull();
  });
});

describe('VALID_BROWSER_EVENTS', () => {
  it('contains exactly 7 event types', () => {
    expect(VALID_BROWSER_EVENTS).toHaveLength(7);
  });

  it('includes the documented 6 events + contextmenu', () => {
    const expected = ['visibilitychange', 'blur', 'focus', 'fullscreenchange', 'copy', 'paste', 'contextmenu'];
    for (const e of expected) {
      expect(VALID_BROWSER_EVENTS).toContain(e);
    }
  });
});
