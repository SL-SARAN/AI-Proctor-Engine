/**
 * Type definitions matching the server-side Pydantic envelope definitions
 * in `src/proctoring_engine/websocket/client.py` and `server.py`.
 *
 * Every message over the WebSocket follows this exact envelope shape.
 */

// ============================================================================
// Client -> Server Messages
// ============================================================================

export type ClientMessageType =
  | 'telemetry_light'
  | 'telemetry_heavy_frame'
  | 'audio_chunk'
  | 'browser_event'
  | 'kill_switch_ack';

export interface Envelope<TType extends ClientMessageType, TPayload> {
  type: TType;
  session_id: string;
  captured_at: string; // ISO-8601
  payload: TPayload;
}

// ----------------------------------------------------------------------------
// Telemetry: Light (Face / Head pose points)
// ----------------------------------------------------------------------------

export interface TelemetryLightPayload {
  modality: 'face_presence' | 'head_pose_gaze';
  face_count?: number;
  confidence: number;
  bbox?: [number, number, number, number]; // [x, y, w, h] normalized 0-1

  // Specific to head_pose_gaze
  off_screen?: boolean;
}

export type TelemetryLightEnvelope = Envelope<'telemetry_light', TelemetryLightPayload>;

// ----------------------------------------------------------------------------
// Telemetry: Heavy Frame (JPEG upload)
// ----------------------------------------------------------------------------

export interface TelemetryHeavyFramePayload {
  frame: string; // Base64 jpeg
  resolution: [number, number]; // [w, h]
  encoding: 'jpeg';
}

export type TelemetryHeavyFrameEnvelope = Envelope<'telemetry_heavy_frame', TelemetryHeavyFramePayload>;

// ----------------------------------------------------------------------------
// Telemetry: Audio Chunk
// ----------------------------------------------------------------------------

export interface TelemetryAudioChunkPayload {
  audio: string; // Base64 PCM-16
  sample_rate_hz: 8000 | 16000 | 32000 | 48000;
  duration_ms: number;
}

export type TelemetryAudioChunkEnvelope = Envelope<'audio_chunk', TelemetryAudioChunkPayload>;

// ----------------------------------------------------------------------------
// Telemetry: Browser Events
// ----------------------------------------------------------------------------

export const VALID_BROWSER_EVENTS = [
  'visibilitychange',
  'blur',
  'focus',
  'fullscreenchange',
  'copy',
  'paste',
  'contextmenu'
] as const;

export type BrowserEventType = typeof VALID_BROWSER_EVENTS[number];

export interface TelemetryBrowserEventPayload {
  event_type: BrowserEventType;
  detail: Record<string, unknown>;
}

export type TelemetryBrowserEventEnvelope = Envelope<'browser_event', TelemetryBrowserEventPayload>;

// ----------------------------------------------------------------------------
// Kill Switch Acknowledge
// ----------------------------------------------------------------------------

export interface KillSwitchAcknowledgePayload {
  flag_id: string;
}

export type KillSwitchAcknowledgeEnvelope = Envelope<'kill_switch_ack', KillSwitchAcknowledgePayload>;


// Union of all possible outbound envelopes
export type ClientEnvelope =
  | TelemetryLightEnvelope
  | TelemetryHeavyFrameEnvelope
  | TelemetryAudioChunkEnvelope
  | TelemetryBrowserEventEnvelope
  | KillSwitchAcknowledgeEnvelope;


/**
 * Builder for outbound envelopes that figures out `captured_at` for us.
 */
export function buildEnvelope<TType extends ClientMessageType, TPayload>(
  type: TType,
  session_id: string,
  payload: TPayload,
  captured_at?: Date | null
): Envelope<TType, TPayload> {
  return {
    type,
    session_id,
    payload,
    captured_at: (captured_at ?? new Date()).toISOString()
  };
}


// ============================================================================
// Server -> Client Messages
// ============================================================================

export type ServerMessageType = 'ack' | 'kill_switch' | 'policy_update';

export interface ServerEnvelope<TType extends ServerMessageType, TPayload> {
  type: TType;
  payload: TPayload;
}

export interface SessionAcknowledgePayload {
  /** Monotonically increasing sequence number for this session (matches server's `seq`). */
  seq: number;
  /** Server-side wall-clock timestamp when the frame was processed (ISO-8601). */
  received_at: string;
}

export interface KillSwitchPayload {
  reason: 'second_person_detected' | 'gaze_frequency_exceeded' | 'accumulated_score_exceeded' | 'retry';
  flag_id: string;
}

export interface PolicyUpdatePayload {
  policy_config_id: string;
  config_dump: Record<string, unknown>;
}

export type SessionAcknowledgeServerEnvelope = ServerEnvelope<'ack', SessionAcknowledgePayload>;
export type KillSwitchServerEnvelope = ServerEnvelope<'kill_switch', KillSwitchPayload>;
export type PolicyUpdateServerEnvelope = ServerEnvelope<'policy_update', PolicyUpdatePayload>;

export type ServerMessage =
  | SessionAcknowledgeServerEnvelope
  | KillSwitchServerEnvelope
  | PolicyUpdateServerEnvelope;

/**
 * Validate an inbound JSON string meets the basic server envelope shape.
 */
export function parseServerMessage(rawText: string): ServerMessage | null {
  try {
    const raw = JSON.parse(rawText);
    if (!raw || typeof raw !== 'object') return null;

    // Both type and payload are required
    if (!('type' in raw) || !('payload' in raw)) return null;

    // Check type enum
    if (!['ack', 'kill_switch', 'policy_update'].includes(raw.type)) {
      return null;
    }

    // We do not exhaustively validate the payload shape here in TS since the
    // server is trusted and generating from strict Pydantic types, but we
    // downcast safely.
    return raw as ServerMessage;
  } catch {
    return null;
  }
}
