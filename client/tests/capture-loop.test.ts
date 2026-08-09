/**
 * Tests for capture-loop module.
 *
 * Tests the CaptureLoop coordinator, its configuration, and lifecycle.
 * Frame-processing tests verify the integration wiring — whether the
 * mocked capture + inference hooks are called — but not real RAF timing.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { CaptureLoop, CaptureLoopError } from '../src/capture-loop.js';
import { WsClient } from '../src/ws-client.js';
import { RollingBuffer } from '../src/rolling-buffer.js';

// ============================================================================
// Mocks
// ============================================================================

const mockWsSend = vi.fn();
const mockWs = {
  send: mockWsSend,
  isConnected: vi.fn(() => true),
} as unknown as WsClient;

const mockRollingBufferAdd = vi.fn();
const mockRollingBufferPush = vi.fn();
const mockRollingBuffer = {
  add: mockRollingBufferAdd,
  push: mockRollingBufferPush,
  drain: vi.fn(),
} as unknown as RollingBuffer;

// Mock media-capture
const mockDestroy = vi.fn();
const mockInitMediaCapture = vi.fn();

vi.mock('../src/media-capture.js', () => ({
  initMediaCapture: (...args: unknown[]) => mockInitMediaCapture(...args),
  captureFrameAsJpeg: vi.fn().mockReturnValue('base64jpegdata'),
  getVideoDimensions: vi.fn(() => [640, 480] as [number, number]),
}));

// Mock face-inference (FaceLandmarker removed — gaze is server-side only)
const mockDetectFaces = vi.fn().mockReturnValue({
  faceCount: 1,
  confidence: 0.95,
  bbox: [100, 50, 200, 250] as [number, number, number, number],
});

const mockFaceInferenceDestroy = vi.fn();

vi.mock('../src/face-inference.js', () => ({
  FaceInferenceRunner: vi.fn().mockImplementation(() => ({
    initialize: vi.fn().mockResolvedValue(undefined),
    isReady: vi.fn(() => true),
    detectFaces: (...args: unknown[]) => mockDetectFaces(...args),
    destroy: (...args: unknown[]) => mockFaceInferenceDestroy(...args),
  })),
  normalizeBbox: vi.fn((bbox: number[], w: number, h: number) => [
    bbox[0]! / w,
    bbox[1]! / h,
    bbox[2]! / w,
    bbox[3]! / h,
  ]),
}));

// Mock requestAnimationFrame — collect callbacks but don't auto-execute
vi.stubGlobal('requestAnimationFrame', vi.fn(() => 1));
vi.stubGlobal('cancelAnimationFrame', vi.fn());

// ============================================================================
// Tests
// ============================================================================

describe('CaptureLoop', () => {
  let loop: CaptureLoop;

  beforeEach(() => {
    vi.clearAllMocks();

    // Default: initMediaCapture succeeds
    mockInitMediaCapture.mockResolvedValue({
      track: { stop: vi.fn() },
      video: {
        readyState: 2,
        videoWidth: 640,
        videoHeight: 480,
        srcObject: {},
        playsInline: true,
        muted: true,
        style: {},
        play: vi.fn().mockResolvedValue(undefined),
        pause: vi.fn(),
      },
      destroy: mockDestroy,
    });
  });

  afterEach(() => {
    if (loop) {
      loop.stop();
    }
  });

  describe('start', () => {
    it('initializes and starts the loop', async () => {
      loop = new CaptureLoop({
        sessionId: 'test-session-id',
        ws: mockWs,
        rollingBuffer: mockRollingBuffer,
      });

      await loop.start();

      expect(loop.isRunning()).toBe(true);
      expect(loop.isInitializing()).toBe(false);
    });

    it('does not restart if already running', async () => {
      loop = new CaptureLoop({
        sessionId: 'test-session-id',
        ws: mockWs,
        rollingBuffer: mockRollingBuffer,
      });

      await loop.start();
      await loop.start(); // Second call is a no-op

      expect(loop.isRunning()).toBe(true);
      // initMediaCapture should only have been called once
      expect(mockInitMediaCapture).toHaveBeenCalledTimes(1);
    });

    it('throws CaptureLoopError on media capture failure', async () => {
      mockInitMediaCapture.mockRejectedValueOnce(new Error('Camera denied'));

      loop = new CaptureLoop({
        sessionId: 'test-session-id',
        ws: mockWs,
        rollingBuffer: mockRollingBuffer,
      });

      await expect(loop.start()).rejects.toThrow(CaptureLoopError);
      expect(loop.getInitError()).not.toBeNull();
    });
  });

  describe('stop', () => {
    it('stops the loop and cleans up resources', async () => {
      loop = new CaptureLoop({
        sessionId: 'test-session-id',
        ws: mockWs,
        rollingBuffer: mockRollingBuffer,
      });

      await loop.start();
      expect(loop.isRunning()).toBe(true);

      loop.stop();

      expect(loop.isRunning()).toBe(false);
      expect(mockFaceInferenceDestroy).toHaveBeenCalled();
      expect(mockDestroy).toHaveBeenCalled();
    });

    it('is safe to call multiple times', async () => {
      loop = new CaptureLoop({
        sessionId: 'test-session-id',
        ws: mockWs,
        rollingBuffer: mockRollingBuffer,
      });

      await loop.start();
      loop.stop();
      loop.stop(); // Second call is a no-op

      expect(loop.isRunning()).toBe(false);
    });
  });

  describe('getState', () => {
    it('returns current state when running', async () => {
      loop = new CaptureLoop({
        sessionId: 'test-session-id',
        ws: mockWs,
        rollingBuffer: mockRollingBuffer,
      });

      await loop.start();
      const state = loop.getState();

      expect(state.isRunning).toBe(true);
      expect(state.faceDetectorReady).toBe(true);
      expect(state.frameCount).toBe(0);
    });

    it('returns current state when not running', () => {
      loop = new CaptureLoop({
        sessionId: 'test-session-id',
        ws: mockWs,
        rollingBuffer: mockRollingBuffer,
      });

      const state = loop.getState();
      expect(state.isRunning).toBe(false);
      expect(state.faceDetectorReady).toBe(false);
    });
  });

  describe('configuration', () => {
    it('uses default config values', async () => {
      loop = new CaptureLoop({
        sessionId: 'test-session-id',
        ws: mockWs,
        rollingBuffer: mockRollingBuffer,
      });

      await loop.start();
      expect(loop.isRunning()).toBe(true);
    });

    it('accepts custom config', async () => {
      loop = new CaptureLoop({
        sessionId: 'test-session-id',
        ws: mockWs,
        rollingBuffer: mockRollingBuffer,
        config: {
          heavyFrameIntervalMs: 5000,
          maxLightFps: 10,
          lightFrameSendIntervalMs: 2000,
        },
      });

      await loop.start();
      expect(loop.isRunning()).toBe(true);
    });
  });

  describe('lifecycle', () => {
    it('calls requestAnimationFrame after start', async () => {
      loop = new CaptureLoop({
        sessionId: 'test-session-id',
        ws: mockWs,
        rollingBuffer: mockRollingBuffer,
      });

      await loop.start();

      expect(requestAnimationFrame).toHaveBeenCalled();
    });

    it('calls cancelAnimationFrame on stop', async () => {
      loop = new CaptureLoop({
        sessionId: 'test-session-id',
        ws: mockWs,
        rollingBuffer: mockRollingBuffer,
      });

      await loop.start();
      loop.stop();

      expect(cancelAnimationFrame).toHaveBeenCalled();
    });
  });
});

describe('CaptureLoopError', () => {
  it('has correct name', () => {
    const error = new CaptureLoopError('test');
    expect(error.name).toBe('CaptureLoopError');
  });

  it('preserves cause', () => {
    const cause = new Error('underlying');
    const error = new CaptureLoopError('test', cause);
    expect(error.cause).toBe(cause);
  });
});
