/**
 * Tests for face-inference module.
 *
 * Uses Vitest with mocked MediaPipe Tasks Vision API.
 * vi.mock factories are hoisted — no references to outer variables.
 *
 * The FaceLandmarker tests were removed when client-side gaze was
 * stripped (2026-08-09) — gaze / head-pose inference is server-side
 * only.  See ``src/proctoring_engine/inference/head_pose_gaze.py``.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// vi.mock is hoisted above imports. The factory must NOT reference outer
// variables (they aren't initialised yet). Return inline objects instead.
vi.mock('@mediapipe/tasks-vision', () => {
  const detector = {
    detectForVideo: vi.fn(),
    close: vi.fn(),
  };
  return {
    FilesetResolver: {
      forVisionTasks: vi.fn().mockResolvedValue({}),
    },
    FaceDetector: {
      createFromOptions: vi.fn().mockResolvedValue(detector),
    },
    // Expose the mock instance for test assertions.
    __mockDetector: detector,
  };
});

import {
  FaceInferenceRunner,
  FaceInferenceError,
  normalizeBbox,
} from '../src/face-inference.js';

// Pull out the mock instance the factory exposed.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let mockDet: any;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let mockFileset: any;

beforeEach(async () => {
  const mod = await import('@mediapipe/tasks-vision');
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  mockDet = (mod as any).__mockDetector;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  mockFileset = (mod as any).FilesetResolver;
});

// Mock HTMLVideoElement
const mockVideo = {
  readyState: 2,
  videoWidth: 640,
  videoHeight: 480,
} as HTMLVideoElement;

// ============================================================================
// Tests
// ============================================================================

describe('FaceInferenceRunner', () => {
  let runner: FaceInferenceRunner;

  beforeEach(() => {
    vi.clearAllMocks();
    runner = new FaceInferenceRunner();
  });

  afterEach(() => {
    runner.destroy();
  });

  describe('initialize', () => {
    it('initializes successfully', async () => {
      await runner.initialize();

      expect(mockFileset.forVisionTasks).toHaveBeenCalledWith('/client/wasm/');
      expect(runner.isReady()).toBe(true);
    });

    it('is idempotent on repeated calls', async () => {
      await runner.initialize();
      await runner.initialize(); // second call — should not throw

      expect(runner.isReady()).toBe(true);
      // createFromOptions should only have been called once
      const { FaceDetector } = await import('@mediapipe/tasks-vision');
      expect(FaceDetector.createFromOptions).toHaveBeenCalledTimes(1);
    });

    it('handles initialization failure', async () => {
      const error = new Error('WASM load failed');
      mockFileset.forVisionTasks.mockRejectedValueOnce(error);

      await expect(runner.initialize()).rejects.toThrow(FaceInferenceError);
      expect(runner.isReady()).toBe(false);
      expect(runner.getInitError()).not.toBeNull();
    });

    it('uses custom paths', async () => {
      const customRunner = new FaceInferenceRunner({
        wasmPath: '/custom/wasm/',
        faceDetectorModelPath: '/custom/face.tflite',
      });

      await customRunner.initialize();

      expect(mockFileset.forVisionTasks).toHaveBeenCalledWith('/custom/wasm/');
      customRunner.destroy();
    });
  });

  describe('detectFaces', () => {
    it('returns null when not initialized', () => {
      const result = runner.detectFaces(mockVideo, performance.now());
      expect(result).toBeNull();
    });

    it('returns detection result with no faces', async () => {
      await runner.initialize();

      mockDet.detectForVideo.mockReturnValue({
        detections: [],
      });

      const result = runner.detectFaces(mockVideo, 1000);

      expect(result).toEqual({
        faceCount: 0,
        confidence: 0,
      });
    });

    it('returns detection result with one face', async () => {
      await runner.initialize();

      mockDet.detectForVideo.mockReturnValue({
        detections: [
          {
            categories: [{ score: 0.95, index: 0, categoryName: 'face', displayName: 'Face' }],
            boundingBox: { originX: 100, originY: 50, width: 200, height: 250, angle: 0 },
            keypoints: [],
          },
        ],
      });

      const result = runner.detectFaces(mockVideo, 1000);

      expect(result?.faceCount).toBe(1);
      expect(result?.confidence).toBe(0.95);
      expect(result?.bbox).toEqual([100, 50, 200, 250]);
    });

    it('returns detection result with multiple faces', async () => {
      await runner.initialize();

      mockDet.detectForVideo.mockReturnValue({
        detections: [
          {
            categories: [{ score: 0.9, index: 0, categoryName: 'face', displayName: 'Face' }],
            boundingBox: { originX: 100, originY: 50, width: 200, height: 250, angle: 0 },
            keypoints: [],
          },
          {
            categories: [{ score: 0.85, index: 0, categoryName: 'face', displayName: 'Face' }],
            boundingBox: { originX: 350, originY: 100, width: 180, height: 220, angle: 0 },
            keypoints: [],
          },
        ],
      });

      const result = runner.detectFaces(mockVideo, 1000);

      expect(result?.faceCount).toBe(2);
      expect(result?.confidence).toBe(0.9); // Max confidence
    });
  });

  describe('destroy', () => {
    it('closes the detector', async () => {
      await runner.initialize();
      runner.destroy();

      expect(mockDet.close).toHaveBeenCalled();
      expect(runner.isReady()).toBe(false);
    });

    it('clears initialization error', async () => {
      mockFileset.forVisionTasks.mockRejectedValueOnce(new Error('fail'));
      await expect(runner.initialize()).rejects.toThrow();
      expect(runner.getInitError()).not.toBeNull();

      runner.destroy();
      expect(runner.getInitError()).toBeNull();
    });
  });
});

describe('normalizeBbox', () => {
  it('normalizes bounding box correctly', () => {
    const bbox: [number, number, number, number] = [100, 50, 200, 150];
    const result = normalizeBbox(bbox, 640, 480);

    expect(result).toEqual([
      100 / 640,
      50 / 480,
      200 / 640,
      150 / 480,
    ]);
  });

  it('handles zero dimensions', () => {
    const bbox: [number, number, number, number] = [0, 0, 0, 0];
    const result = normalizeBbox(bbox, 640, 480);
    expect(result).toEqual([0, 0, 0, 0]);
  });

  it('handles equal image dimensions', () => {
    const bbox: [number, number, number, number] = [50, 50, 100, 100];
    const result = normalizeBbox(bbox, 200, 200);
    expect(result).toEqual([0.25, 0.25, 0.5, 0.5]);
  });
});

describe('FaceInferenceError', () => {
  it('has correct name', () => {
    const error = new FaceInferenceError('test');
    expect(error.name).toBe('FaceInferenceError');
  });

  it('preserves cause', () => {
    const cause = new Error('underlying');
    const error = new FaceInferenceError('test', cause);
    expect(error.cause).toBe(cause);
  });
});
