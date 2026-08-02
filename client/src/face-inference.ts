/**
 * Face inference module using MediaPipe Tasks Vision API.
 *
 * Wraps FaceDetector (face presence/count) and FaceLandmarker
 * (landmarks for gaze detection) with lazy async initialization.
 *
 * Model files are served from the backend at /client/models/ and
 * loaded on first use. Initialization is async; detection methods
 * return null if not ready.
 */

import {
  FilesetResolver,
  FaceDetector,
  FaceLandmarker,
  FaceDetectorResult,
  FaceLandmarkerResult,
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

export interface FaceLandmarkerConfig {
  /** Maximum number of faces to detect (default 1) */
  numFaces?: number;
  /** Minimum face detection confidence (0-1, default 0.5) */
  minFaceDetectionConfidence?: number;
  /** Minimum face presence confidence (0-1, default 0.5) */
  minFacePresenceConfidence?: number;
  /** Minimum tracking confidence (0-1, default 0.5) */
  minTrackingConfidence?: number;
  /** Whether to output blendshapes (default false for v1) */
  outputBlendshapes?: boolean;
}

export interface FaceDetection {
  /** Number of faces detected */
  faceCount: number;
  /** Detection confidence (max of all detections) */
  confidence: number;
  /** Bounding box of the primary face [x, y, w, h] normalized 0-1 */
  bbox?: [number, number, number, number] | undefined;
}

export interface FaceLandmarks {
  /** 478 normalized landmarks (x, y, z) per face */
  landmarks: Array<{ x: number; y: number; z: number }>;
  /** Whether blendshapes are included */
  hasBlendshapes: boolean;
  /** 52 blendshape coefficients (if enabled) */
  blendshapes?: Map<string, number> | undefined;
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
const FACE_LANDMARKER_MODEL = '/client/models/face_landmarker.task';

// ============================================================================
// Face Inference Runner
// ============================================================================

/**
 * Manages MediaPipe FaceDetector and FaceLandmarker instances.
 *
 * Initialization is lazy and async. Call `initialize()` before use,
 * or check `isReady()` before calling detection methods.
 */
export class FaceInferenceRunner {
  private wasmPath: string;
  private faceDetectorModelPath: string;
  private faceLandmarkerModelPath: string;
  private faceDetectorConfig: FaceDetectorConfig;
  private faceLandmarkerConfig: FaceLandmarkerConfig;

  private faceDetector: FaceDetector | null = null;
  private faceLandmarker: FaceLandmarker | null = null;
  private initPromise: Promise<void> | null = null;
  private initError: Error | null = null;

  constructor(config?: {
    wasmPath?: string;
    faceDetectorModelPath?: string;
    faceLandmarkerModelPath?: string;
    faceDetectorConfig?: FaceDetectorConfig;
    faceLandmarkerConfig?: FaceLandmarkerConfig;
  }) {
    this.wasmPath = config?.wasmPath ?? DEFAULT_WASM_PATH;
    this.faceDetectorModelPath = config?.faceDetectorModelPath ?? FACE_DETECTOR_MODEL;
    this.faceLandmarkerModelPath = config?.faceLandmarkerModelPath ?? FACE_LANDMARKER_MODEL;
    this.faceDetectorConfig = config?.faceDetectorConfig ?? {};
    this.faceLandmarkerConfig = config?.faceLandmarkerConfig ?? {};
  }

  /**
   * Initialize both detectors. Safe to call multiple times;
   * returns the same promise if already initializing.
   */
  async initialize(): Promise<void> {
    if (this.initPromise) {
      return this.initPromise;
    }

    if (this.faceDetector && this.faceLandmarker) {
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

      // Initialize FaceLandmarker for heavy frames (every 2-3s)
      this.faceLandmarker = await FaceLandmarker.createFromOptions(vision, {
        baseOptions: {
          modelAssetPath: this.faceLandmarkerModelPath,
          delegate: 'GPU',
        },
        runningMode: 'VIDEO',
        numFaces: this.faceLandmarkerConfig.numFaces ?? 1,
        minFaceDetectionConfidence:
          this.faceLandmarkerConfig.minFaceDetectionConfidence ?? 0.5,
        minFacePresenceConfidence:
          this.faceLandmarkerConfig.minFacePresenceConfidence ?? 0.5,
        minTrackingConfidence: this.faceLandmarkerConfig.minTrackingConfidence ?? 0.5,
        outputFaceBlendshapes: this.faceLandmarkerConfig.outputBlendshapes ?? false,
        outputFacialTransformationMatrixes: false,
      });
    } catch (err) {
      this.initError = err instanceof Error ? err : new Error(String(err));
      this.initPromise = null;
      throw new FaceInferenceError('Failed to initialize face inference', err);
    }
  }

  /** Check if both detectors are ready */
  isReady(): boolean {
    return this.faceDetector !== null && this.faceLandmarker !== null;
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

  /**
   * Run face landmark detection (heavy frame) for gaze.
   *
   * @param video - Video element to detect from
   * @param timestamp - Frame timestamp in ms (performance.now())
   * @returns Face landmarks result, or null if not ready
   */
  detectLandmarks(video: HTMLVideoElement, timestamp: number): FaceLandmarks | null {
    if (!this.faceLandmarker) {
      return null;
    }

    const result: FaceLandmarkerResult = this.faceLandmarker.detectForVideo(
      video,
      timestamp
    );

    if (!result.faceLandmarks || result.faceLandmarks.length === 0) {
      return {
        landmarks: [],
        hasBlendshapes: false,
      };
    }

    // Get first face's landmarks (478 points)
    const firstFace = result.faceLandmarks[0];
    if (!firstFace) {
      return {
        landmarks: [],
        hasBlendshapes: false,
      };
    }

    const landmarks = firstFace.map(lm => ({
      x: lm.x,
      y: lm.y,
      z: lm.z,
    }));

    // Extract blendshapes if available
    let blendshapes: Map<string, number> | undefined;
    const firstBlendshape = result.faceBlendshapes?.[0];
    if (firstBlendshape) {
      blendshapes = new Map();
      for (const category of firstBlendshape.categories) {
        blendshapes.set(category.categoryName, category.score);
      }
    }

    return {
      landmarks,
      hasBlendshapes: blendshapes !== undefined,
      blendshapes,
    };
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
    if (this.faceLandmarker) {
      this.faceLandmarker.close();
      this.faceLandmarker = null;
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
