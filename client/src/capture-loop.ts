/**
 * Capture loop coordinator for client-side inference.
 *
 * Manages the frame capture loop with:
 * - Light frame inference (face presence via FaceDetector) on every frame
 * - Heavy frame capture (JPEG upload) at configurable intervals
 * - Integration with RollingBuffer and WsClient
 *
 * **Gaze / head-pose computation is server-side only.**  The server runs
 * the full MediaPipe FaceLandmarker (478 landmarks, blendshapes, EAR
 * blink filtering) on the raw heavy frame JPEG — see
 * ``src/proctoring_engine/inference/head_pose_gaze.py``.  An earlier
 * version of this module ran a client-side FaceLandmarker and computed
 * a crude iris-offset heuristic, but it was dead code: the server's
 * ``TelemetryLightPayload`` Pydantic model only accepts
 * ``modality: "face_presence"`` (with a required ``face_count`` field),
 * so the client's ``head_pose_gaze`` messages were rejected at
 * validation.  Removed 2026-08-09.
 */

import { WsClient } from './ws-client.js';
import { RollingBuffer } from './rolling-buffer.js';
import { buildEnvelope, TelemetryLightPayload, TelemetryHeavyFramePayload } from './envelope.js';
import {
  initMediaCapture,
  MediaCaptureResult,
  captureFrameAsJpeg,
  getVideoDimensions,
} from './media-capture.js';
import {
  FaceInferenceRunner,
  FaceDetection,
  normalizeBbox,
} from './face-inference.js';

// ============================================================================
// Types
// ============================================================================

export interface CaptureLoopConfig {
  /** Heavy frame interval in ms (default 2500 = 2.5s) */
  heavyFrameIntervalMs?: number;
  /** Maximum frames per second for light detection (default 15) */
  maxLightFps?: number;
  /** Minimum time between light frame sends in ms (default 1000 = 1s) */
  lightFrameSendIntervalMs?: number;
}

export interface CaptureLoopDeps {
  sessionId: string;
  ws: WsClient;
  rollingBuffer: RollingBuffer;
  config?: CaptureLoopConfig;
}

export interface CaptureLoopState {
  isRunning: boolean;
  faceDetectorReady: boolean;
  lastHeavyFrameTimestamp: number;
  frameCount: number;
}

export class CaptureLoopError extends Error {
  constructor(message: string, public readonly cause?: unknown) {
    super(message);
    this.name = 'CaptureLoopError';
  }
}

// ============================================================================
// Capture Loop
// ============================================================================

/**
 * Coordinates webcam capture, face detection, and telemetry sending.
 *
 * Light frames (face presence) are processed on every frame but only
 * sent to the server at `lightFrameSendIntervalMs` intervals.
 * Heavy frames (JPEG) are captured at `heavyFrameIntervalMs`
 * intervals and stored in the rolling buffer.  Server-side inference
 * (gaze, identity, object detection) runs on the heavy frame JPEG.
 */
export class CaptureLoop {
  private sessionId: string;
  private ws: WsClient;
  private rollingBuffer: RollingBuffer;
  private config: Required<CaptureLoopConfig>;

  private mediaCapture: MediaCaptureResult | null = null;
  private faceInference: FaceInferenceRunner | null = null;
  private animationFrameId: number | null = null;
  private lastFrameTime: number = 0;
  private lastLightSendTime: number = 0;
  private lastHeavyFrameTime: number = 0;
  private frameCount: number = 0;
  private isRunningFlag: boolean = false;
  private isInitializingFlag: boolean = false;
  private initError: Error | null = null;

  constructor(deps: CaptureLoopDeps) {
    this.sessionId = deps.sessionId;
    this.ws = deps.ws;
    this.rollingBuffer = deps.rollingBuffer;
    this.config = {
      heavyFrameIntervalMs: deps.config?.heavyFrameIntervalMs ?? 2500,
      maxLightFps: deps.config?.maxLightFps ?? 15,
      lightFrameSendIntervalMs: deps.config?.lightFrameSendIntervalMs ?? 1000,
    };
  }

  /**
   * Start the capture loop. Initializes media and inference asynchronously.
   */
  async start(): Promise<void> {
    if (this.isRunningFlag || this.isInitializingFlag) {
      return;
    }

    this.isInitializingFlag = true;
    this.initError = null;

    try {
      // Initialize media capture
      this.mediaCapture = await initMediaCapture({
        width: 640,
        height: 480,
        frameRate: 30,
        mirror: true,
      });

      // Initialize face inference
      this.faceInference = new FaceInferenceRunner();
      await this.faceInference.initialize();

      this.isRunningFlag = true;
      this.isInitializingFlag = false;

      // Start the frame loop
      this.lastFrameTime = performance.now();
      this.lastLightSendTime = this.lastFrameTime;
      this.lastHeavyFrameTime = this.lastFrameTime;
      this.scheduleNextFrame();
    } catch (err) {
      this.isInitializingFlag = false;
      this.initError = err instanceof Error ? err : new Error(String(err));
      throw new CaptureLoopError('Failed to start capture loop', err);
    }
  }

  /**
   * Stop the capture loop and release resources.
   */
  stop(): void {
    this.isRunningFlag = false;

    if (this.animationFrameId !== null) {
      cancelAnimationFrame(this.animationFrameId);
      this.animationFrameId = null;
    }

    if (this.faceInference) {
      this.faceInference.destroy();
      this.faceInference = null;
    }

    if (this.mediaCapture) {
      this.mediaCapture.destroy();
      this.mediaCapture = null;
    }
  }

  /**
   * Check if the loop is running.
   */
  isRunning(): boolean {
    return this.isRunningFlag;
  }

  /**
   * Check if initialization is in progress.
   */
  isInitializing(): boolean {
    return this.isInitializingFlag;
  }

  /**
   * Get the initialization error, if any.
   */
  getInitError(): Error | null {
    return this.initError;
  }

  /**
   * Get current state for UI display.
   */
  getState(): CaptureLoopState {
    return {
      isRunning: this.isRunningFlag,
      faceDetectorReady: this.faceInference?.isReady() ?? false,
      lastHeavyFrameTimestamp: this.lastHeavyFrameTime,
      frameCount: this.frameCount,
    };
  }

  private scheduleNextFrame(): void {
    if (!this.isRunningFlag) {
      return;
    }

    this.animationFrameId = requestAnimationFrame(timestamp => this.processFrame(timestamp));
  }

  private processFrame(timestamp: number): void {
    if (!this.isRunningFlag || !this.mediaCapture || !this.faceInference) {
      return;
    }

    const video = this.mediaCapture.video;

    // Check if video is ready
    if (video.readyState < 2) {
      this.scheduleNextFrame();
      return;
    }

    // Frame rate limiting for light inference
    const elapsed = timestamp - this.lastFrameTime;
    const minFrameInterval = 1000 / this.config.maxLightFps;

    if (elapsed < minFrameInterval) {
      this.scheduleNextFrame();
      return;
    }

    this.lastFrameTime = timestamp;
    this.frameCount++;

    const [videoWidth, videoHeight] = getVideoDimensions(video);

    // Run face detection (light inference) on every frame
    const detection = this.faceInference.detectFaces(video, timestamp);

    if (detection) {
      // Send light telemetry at configured interval
      if (timestamp - this.lastLightSendTime >= this.config.lightFrameSendIntervalMs) {
        this.sendLightTelemetry(detection, videoWidth, videoHeight);
        this.lastLightSendTime = timestamp;
      }
    }

    // Check if it's time for a heavy frame
    if (timestamp - this.lastHeavyFrameTime >= this.config.heavyFrameIntervalMs) {
      this.processHeavyFrame(video, timestamp, videoWidth, videoHeight);
      this.lastHeavyFrameTime = timestamp;
    }

    this.scheduleNextFrame();
  }

  private sendLightTelemetry(
    detection: FaceDetection,
    videoWidth: number,
    videoHeight: number
  ): void {
    const payload: TelemetryLightPayload = {
      modality: 'face_presence',
      face_count: detection.faceCount,
      confidence: detection.confidence,
    };

    // Normalize bounding box if present
    if (detection.bbox && videoWidth > 0 && videoHeight > 0) {
      payload.bbox = normalizeBbox(detection.bbox, videoWidth, videoHeight);
    }

    const envelope = buildEnvelope('telemetry_light', this.sessionId, payload);
    this.ws.send(envelope);
  }

  private processHeavyFrame(
    video: HTMLVideoElement,
    timestamp: number,
    videoWidth: number,
    videoHeight: number
  ): void {
    // Capture JPEG
    const jpegBase64 = captureFrameAsJpeg(video, 0.85);
    if (!jpegBase64) {
      return;
    }

    // Add to rolling buffer (JPEG only — the server runs its own
    // FaceLandmarker on the raw frame for gaze/identity/object
    // detection, so no client-side landmarks are stored).
    const bufferEntry = {
      timestamp,
      jpegBase64,
      landmarks: null,
      dimensions: [videoWidth, videoHeight] as [number, number],
    };
    this.rollingBuffer.add(bufferEntry);

    // Send heavy frame envelope
    const payload: TelemetryHeavyFramePayload = {
      frame: jpegBase64,
      resolution: [videoWidth, videoHeight],
      encoding: 'jpeg',
    };

    const envelope = buildEnvelope('telemetry_heavy_frame', this.sessionId, payload);
    this.ws.send(envelope);
  }
}

/**
 * Create and start a capture loop.
 *
 * @param deps - Dependencies (sessionId, ws, rollingBuffer, config)
 * @returns Started capture loop
 */
export async function createCaptureLoop(deps: CaptureLoopDeps): Promise<CaptureLoop> {
  const loop = new CaptureLoop(deps);
  await loop.start();
  return loop;
}
