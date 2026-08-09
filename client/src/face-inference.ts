/**
 * Face inference module using MediaPipe Tasks Vision API.
 *
 * Wraps FaceDetector (face presence/count) with lazy async initialization.
 * The FaceLandmarker (gaze, blendshapes) is **not** loaded client-side —
 * gaze / head-pose computation is server-side only, running the full
 * MediaPipe FaceLandmarker on the raw heavy-frame JPEG.  See
 * ``src/proctoring_engine/inference/head_pose_gaze.py``.
 *
 * Model files are served from the backend at /client/models/ and
 * loaded on first use. Initialization is async; detection methods
 * return null if not ready.
 */

import {
  FilesetResolver,
  FaceDetector,
  FaceDetectorResult,
} from '@mediapipe/tasks-vision';

// ============================================================================
// Types
// ============================================================================

export interface FaceDetectorConfig {
  /** Minimum detection confidence threshold (0-1, default 0.5) */
  minDetectionConfidence?: number;
  /** Minimum suppression threshold for non-max suppression (0-1, default 0.3) */
  minSuppressionThreshold?: number;
}

export interface FaceDetection {
  /** Number of faces detected */
  faceCount: number;
  /** Detection confidence (max of all detections) */
  confidence: number;
  /** Bounding box of the primary face [x, y, w, h] normalized 0-1 */
  bbox?: [number, number, number, number] | undefined;
}

export class FaceInferenceError extends Error {
  constructor(message: string, public readonly cause?: unknown) {
    super(message);
    this.name = 'FaceInferenceError';
  }
}

// ============================================================================
// Constants
// ============================================================================

/** Default WASM path relative to client bundle */
const DEFAULT_WASM_PATH = '/client/wasm/';

/** Default model paths relative to client bundle */
const FACE_DETECTOR_MODEL = '/client/models/blaze_face_short_range.tflite';

// ============================================================================
// Face Inference Runner
// ============================================================================

/**
 * Manages the MediaPipe FaceDetector instance for light face-presence
 * detection.
 *
 * Initialization is lazy and async. Call `initialize()` before use,
 * or check `isReady()` before calling detection methods.
 *
 * The FaceLandmarker is **not** loaded here — gaze / head-pose
 * inference runs server-side on the raw heavy frame JPEG.  Keeping it
 * out of the client saves ~2 MB of WASM + model download per session.
 */
export class FaceInferenceRunner {
  private wasmPath: string;
  private faceDetectorModelPath: string;
  private faceDetectorConfig: FaceDetectorConfig;

  private faceDetector: FaceDetector | null = null;
  private initPromise: Promise<void> | null = null;
  private initError: Error | null = null;

  constructor(config?: {
    wasmPath?: string;
    faceDetectorModelPath?: string;
    faceDetectorConfig?: FaceDetectorConfig;
  }) {
    this.wasmPath = config?.wasmPath ?? DEFAULT_WASM_PATH;
    this.faceDetectorModelPath = config?.faceDetectorModelPath ?? FACE_DETECTOR_MODEL;
    this.faceDetectorConfig = config?.faceDetectorConfig ?? {};
  }

  /**
   * Initialize the face detector. Safe to call multiple times;
   * returns the same promise if already initializing.
   */
  async initialize(): Promise<void> {
    if (this.initPromise) {
      return this.initPromise;
    }

    if (this.faceDetector) {
      return; // Already initialized
    }

    this.initPromise = this._doInitialize();
    return this.initPromise;
  }

  private async _doInitialize(): Promise<void> {
    try {
      // Load WASM runtime
      const vision = await FilesetResolver.forVisionTasks(this.wasmPath);

      // Initialize FaceDetector for light frames (every frame)
      this.faceDetector = await FaceDetector.createFromOptions(vision, {
        baseOptions: {
          modelAssetPath: this.faceDetectorModelPath,
          delegate: 'GPU',
        },
        runningMode: 'VIDEO',
        minDetectionConfidence: this.faceDetectorConfig.minDetectionConfidence ?? 0.5,
        minSuppressionThreshold: this.faceDetectorConfig.minSuppressionThreshold ?? 0.3,
      });
    } catch (err) {
      this.initError = err instanceof Error ? err : new Error(String(err));
      this.initPromise = null;
      throw new FaceInferenceError('Failed to initialize face inference', err);
    }
  }

  /** Check if the detector is ready */
  isReady(): boolean {
    return this.faceDetector !== null;
  }

  /** Get the initialization error, if any */
  getInitError(): Error | null {
    return this.initError;
  }

  /**
   * Run face detection (light frame) for face count.
   *
   * @param video - Video element to detect from
   * @param timestamp - Frame timestamp in ms (performance.now())
   * @returns Face detection result, or null if not ready
   */
  detectFaces(video: HTMLVideoElement, timestamp: number): FaceDetection | null {
    if (!this.faceDetector) {
      return null;
    }

    const result: FaceDetectorResult = this.faceDetector.detectForVideo(video, timestamp);
    return this._processDetectionResult(result);
  }

  private _processDetectionResult(result: FaceDetectorResult): FaceDetection {
    const faceCount = result.detections.length;

    if (faceCount === 0) {
      return { faceCount: 0, confidence: 0 };
    }

    // Get max confidence across all detections
    let maxConfidence = 0;
    let primaryBbox: [number, number, number, number] | undefined;

    for (const detection of result.detections) {
      const score = detection.categories[0]?.score ?? 0;
      if (score > maxConfidence) {
        maxConfidence = score;
        // Convert bounding box to normalized [x, y, w, h]
        const bb = detection.boundingBox;
        if (bb) {
          // Note: BoundingBox is in pixels; we need normalized coords
          // This will be normalized by the caller using video dimensions
          primaryBbox = [bb.originX, bb.originY, bb.width, bb.height];
        }
      }
    }

    return {
      faceCount,
      confidence: maxConfidence,
      bbox: primaryBbox,
    };
  }

  /**
   * Clean up and release resources.
   */
  destroy(): void {
    if (this.faceDetector) {
      this.faceDetector.close();
      this.faceDetector = null;
    }
    this.initPromise = null;
    this.initError = null;
  }
}

/**
 * Normalize a pixel bounding box to 0-1 range.
 *
 * @param bbox - [x, y, w, h] in pixels
 * @param width - Image width
 * @param height - Image height
 * @returns Normalized [x, y, w, h]
 */
export function normalizeBbox(
  bbox: [number, number, number, number],
  width: number,
  height: number
): [number, number, number, number] {
  return [
    bbox[0] / width,
    bbox[1] / height,
    bbox[2] / width,
    bbox[3] / height,
  ];
}
