/**
 * Tests for media-capture module.
 *
 * Uses Vitest + jsdom with mocked MediaStream APIs.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  initMediaCapture,
  captureFrame,
  captureFrameAsJpeg,
  getVideoDimensions,
  MediaCaptureError,
} from '../src/media-capture.js';

// ============================================================================
// Mocks
// ============================================================================

const mockTrack = {
  stop: vi.fn(),
  getSettings: vi.fn(() => ({ width: 640, height: 480, frameRate: 30 })),
};

const mockStream = {
  getVideoTracks: vi.fn(() => [mockTrack]),
  getTracks: vi.fn(() => [mockTrack]),
};

const mockGetUserMedia = vi.fn();

// Mock canvas context
const mockContext = {
  drawImage: vi.fn(),
};

/** Create a mock video element with settable properties. */
function createMockVideo(overrides: {
  readyState?: number;
  videoWidth?: number;
  videoHeight?: number;
} = {}): HTMLVideoElement {
  return {
    srcObject: null,
    playsInline: false,
    muted: false,
    style: {} as CSSStyleDeclaration,
    readyState: overrides.readyState ?? 0,
    videoWidth: overrides.videoWidth ?? 640,
    videoHeight: overrides.videoHeight ?? 480,
    onloadedmetadata: null,
    onerror: null,
    play: vi.fn().mockResolvedValue(undefined),
    pause: vi.fn(),
  } as unknown as HTMLVideoElement;
}

/** Create a mock canvas element. */
function createMockCanvas(): HTMLCanvasElement {
  return {
    width: 0,
    height: 0,
    style: {} as CSSStyleDeclaration,
    getContext: vi.fn().mockReturnValue(mockContext),
    toDataURL: vi.fn().mockReturnValue('data:image/jpeg;base64,/9j/4AAQSkZJRg=='),
  } as unknown as HTMLCanvasElement;
}

// The play() mock for the video created during initMediaCapture
const mockPlay = vi.fn().mockResolvedValue(undefined);

function stubNavigatorWithGUM(): void {
  vi.stubGlobal('navigator', {
    mediaDevices: {
      getUserMedia: mockGetUserMedia,
    },
  });
}

vi.stubGlobal('document', {
  createElement: vi.fn((tag: string) => {
    if (tag === 'video') {
      return {
        srcObject: null,
        playsInline: false,
        muted: false,
        style: {},
        readyState: 2,
        videoWidth: 640,
        videoHeight: 480,
        onloadedmetadata: null,
        onerror: null,
        play: mockPlay,
        pause: vi.fn(),
      };
    }
    if (tag === 'canvas') {
      return createMockCanvas();
    }
    return {};
  }),
});

// ============================================================================
// Tests
// ============================================================================

describe('initMediaCapture', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    stubNavigatorWithGUM();
    mockGetUserMedia.mockResolvedValue(mockStream);
    mockPlay.mockResolvedValue(undefined);
  });

  it('requests camera with default constraints', async () => {
    const result = await initMediaCapture();

    expect(mockGetUserMedia).toHaveBeenCalledWith({
      video: {
        width: { ideal: 640 },
        height: { ideal: 480 },
        frameRate: { ideal: 30 },
        facingMode: 'user',
      },
      audio: false,
    });

    expect(result.track).toBe(mockTrack);
    expect(result.video).toBeDefined();
    expect(result.destroy).toBeInstanceOf(Function);
  });

  it('requests camera with custom constraints', async () => {
    await initMediaCapture({
      width: 1280,
      height: 720,
      frameRate: 60,
      mirror: false,
    });

    expect(mockGetUserMedia).toHaveBeenCalledWith({
      video: {
        width: { ideal: 1280 },
        height: { ideal: 720 },
        frameRate: { ideal: 60 },
        facingMode: 'user',
      },
      audio: false,
    });
  });

  it('throws MediaCaptureError when getUserMedia is not available', async () => {
    vi.stubGlobal('navigator', { mediaDevices: {} });

    await expect(initMediaCapture()).rejects.toThrow(MediaCaptureError);
    await expect(initMediaCapture()).rejects.toThrow('getUserMedia is not available');
  });

  it('throws MediaCaptureError with correct message for NotAllowedError', async () => {
    const error = new Error('Permission denied');
    error.name = 'NotAllowedError';
    mockGetUserMedia.mockRejectedValue(error);

    await expect(initMediaCapture()).rejects.toThrow('Camera access denied by user');
  });

  it('throws MediaCaptureError with correct message for NotFoundError', async () => {
    const error = new Error('No device');
    error.name = 'NotFoundError';
    mockGetUserMedia.mockRejectedValue(error);

    await expect(initMediaCapture()).rejects.toThrow('No camera device found');
  });

  it('throws MediaCaptureError with correct message for NotReadableError', async () => {
    const error = new Error('Device in use');
    error.name = 'NotReadableError';
    mockGetUserMedia.mockRejectedValue(error);

    await expect(initMediaCapture()).rejects.toThrow('Camera is in use by another application');
  });

  it('throws MediaCaptureError with correct message for OverconstrainedError', async () => {
    const error = new Error('Constraints not satisfied');
    error.name = 'OverconstrainedError';
    mockGetUserMedia.mockRejectedValue(error);

    await expect(initMediaCapture()).rejects.toThrow('Camera does not support the requested resolution');
  });

  it('destroy stops all tracks', async () => {
    const result = await initMediaCapture();
    result.destroy();

    expect(mockTrack.stop).toHaveBeenCalled();
  });
});

describe('captureFrame', () => {
  it('returns null when video not ready', () => {
    const video = createMockVideo({ readyState: 0 });
    const result = captureFrame(video);
    expect(result).toBeNull();
  });

  it('returns canvas when video is ready', () => {
    const video = createMockVideo({ readyState: 2, videoWidth: 640, videoHeight: 480 });
    const result = captureFrame(video);

    expect(result).not.toBeNull();
    expect(result?.width).toBe(640);
    expect(result?.height).toBe(480);
  });
});

describe('captureFrameAsJpeg', () => {
  it('returns null when video not ready', () => {
    const video = createMockVideo({ readyState: 0 });
    const result = captureFrameAsJpeg(video);
    expect(result).toBeNull();
  });

  it('returns base64 string when video is ready', () => {
    const video = createMockVideo({ readyState: 2 });
    const result = captureFrameAsJpeg(video);
    expect(result).toBe('/9j/4AAQSkZJRg==');
  });

  it('uses default quality of 0.85', () => {
    const video = createMockVideo({ readyState: 2 });
    const canvas = createMockCanvas();
    vi.spyOn(document, 'createElement').mockReturnValueOnce(canvas as unknown as HTMLElement);

    captureFrameAsJpeg(video);
    expect(canvas.toDataURL).toHaveBeenCalledWith('image/jpeg', 0.85);
  });

  it('uses custom quality when provided', () => {
    const video = createMockVideo({ readyState: 2 });
    const canvas = createMockCanvas();
    vi.spyOn(document, 'createElement').mockReturnValueOnce(canvas as unknown as HTMLElement);

    captureFrameAsJpeg(video, 0.5);
    expect(canvas.toDataURL).toHaveBeenCalledWith('image/jpeg', 0.5);
  });
});

describe('getVideoDimensions', () => {
  it('returns [0, 0] when video not ready', () => {
    const video = createMockVideo({ videoWidth: 0, videoHeight: 0 });
    expect(getVideoDimensions(video)).toEqual([0, 0]);
  });

  it('returns dimensions when video is ready', () => {
    const video = createMockVideo({ videoWidth: 1280, videoHeight: 720 });
    expect(getVideoDimensions(video)).toEqual([1280, 720]);
  });
});

describe('MediaCaptureError', () => {
  it('has correct name', () => {
    const error = new MediaCaptureError('test');
    expect(error.name).toBe('MediaCaptureError');
  });

  it('preserves cause', () => {
    const cause = new Error('underlying');
    const error = new MediaCaptureError('test', cause);
    expect(error.cause).toBe(cause);
  });
});
