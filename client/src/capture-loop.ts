/**
 * Capture loop coordinator for client-side inference.
 *
 * Manages the frame capture loop with:
 * - Light frame inference (face presence) on every frame
 * - Heavy frame capture (JPEG upload + landmarks) at configurable intervals
 * - Integration with RollingBuffer and WsClient
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
  FaceLandmarks,
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
  faceLandmarkerReady: boolean;
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
 * Coordinates webcam capture, face inference, and telemetry sending.
 *
 * Light frames (face presence) are processed on every frame but only
 * sent to the server at `lightFrameSendIntervalMs` intervals.
 * Heavy frames (JPEG + landmarks) are captured at `heavyFrameIntervalMs`
 * intervals and stored in the rolling buffer.
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
      faceLandmarkerReady: this.faceInference?.isReady() ?? false,
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
    if (!this.faceInference) {
      return;
    }

    // Capture JPEG
    const jpegBase64 = captureFrameAsJpeg(video, 0.85);
    if (!jpegBase64) {
      return;
    }

    // Run landmark detection for gaze
    const landmarks = this.faceInference.detectLandmarks(video, timestamp);

    // Add to rolling buffer
    const bufferEntry = {
      timestamp,
      jpegBase64,
      landmarks: landmarks?.landmarks ?? null,
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

    // Also send head_pose_gaze light telemetry if landmarks available
    if (landmarks && landmarks.landmarks.length > 0) {
      this.sendGazeTelemetry(landmarks);
    }
  }

  private sendGazeTelemetry(landmarks: FaceLandmarks): void {
    // Simple off-screen detection based on iris position
    // Landmark indices for iris center (from MediaPipe FaceLandmarker spec)
    // Left iris center: ~468, Right iris center: ~473
    const LEFT_IRIS_CENTER = 468;
    const RIGHT_IRIS_CENTER = 473;
    const LEFT_EYE_CORNER = 33;  // Left eye outer corner
    const RIGHT_EYE_CORNER = 263; // Right eye outer corner

    if (landmarks.landmarks.length < RIGHT_IRIS_CENTER + 1) {
      return;
    }

    const leftIris = landmarks.landmarks[LEFT_IRIS_CENTER]!;
    const rightIris = landmarks.landmarks[RIGHT_IRIS_CENTER]!;
    const leftCorner = landmarks.landmarks[LEFT_EYE_CORNER]!;
    const rightCorner = landmarks.landmarks[RIGHT_EYE_CORNER]!;

    // Simple heuristic: if both iris centers are very close to the eye corners,
    // the person is likely looking away
    const leftOffset = Math.abs(leftIris.x - leftCorner.x);
    const rightOffset = Math.abs(rightIris.x - rightCorner.x);

    // Threshold for "looking away" (tuned empirically)
    const THRESHOLD = 0.02;
    const offScreen = leftOffset < THRESHOLD && rightOffset < THRESHOLD;

    const payload: TelemetryLightPayload = {
      modality: 'head_pose_gaze',
      confidence: landmarks.hasBlendshapes ? 0.9 : 0.7,
      off_screen: offScreen,
    };

    const envelope = buildEnvelope('telemetry_light', this.sessionId, payload);
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
