/**
 * Media capture module for webcam access and frame extraction.
 *
 * Provides getUserMedia wrapper with constraint validation, frame
 * extraction at configurable intervals, and cleanup handling.
 */

export interface MediaCaptureConfig {
  /** Video width constraint (default 640) */
  width?: number;
  /** Video height constraint (default 480) */
  height?: number;
  /** Frame rate constraint (default 30) */
  frameRate?: number;
  /** Whether to mirror the video horizontally (default true) */
  mirror?: boolean;
}

export interface MediaCaptureResult {
  /** The MediaStream track */
  track: MediaStreamTrack;
  /** The video element for frame capture */
  video: HTMLVideoElement;
  /** Cleanup function to stop tracks and remove elements */
  destroy: () => void;
}

export class MediaCaptureError extends Error {
  constructor(
    message: string,
    public readonly cause?: Error
  ) {
    super(message);
    this.name = 'MediaCaptureError';
  }
}

/**
 * Request camera access and return a video element ready for frame capture.
 *
 * @param config - Capture configuration
 * @returns Promise resolving to capture result with video element and cleanup
 * @throws MediaCaptureError if camera access is denied or unavailable
 */
export async function initMediaCapture(
  config: MediaCaptureConfig = {}
): Promise<MediaCaptureResult> {
  const {
    width = 640,
    height = 480,
    frameRate = 30,
    mirror = true,
  } = config;

  // Check for getUserMedia availability
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new MediaCaptureError(
      'getUserMedia is not available. HTTPS is required for camera access.'
    );
  }

  let stream: MediaStream;

  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: {
        width: { ideal: width },
        height: { ideal: height },
        frameRate: { ideal: frameRate },
        facingMode: 'user',
      },
      audio: false, // Audio is captured separately
    });
  } catch (err) {
    if (err instanceof Error) {
      if (err.name === 'NotAllowedError') {
        throw new MediaCaptureError('Camera access denied by user.', err);
      }
      if (err.name === 'NotFoundError') {
        throw new MediaCaptureError('No camera device found.', err);
      }
      if (err.name === 'NotReadableError') {
        throw new MediaCaptureError('Camera is in use by another application.', err);
      }
      if (err.name === 'OverconstrainedError') {
        throw new MediaCaptureError(
          'Camera does not support the requested resolution/frame rate.',
          err
        );
      }
      throw new MediaCaptureError(`Failed to access camera: ${err.message}`, err);
    }
    throw new MediaCaptureError('Failed to access camera due to unknown error.');
  }

  // Create video element for frame capture
  const video = document.createElement('video');
  video.srcObject = stream;
  video.playsInline = true;
  video.muted = true;

  if (mirror) {
    video.style.transform = 'scaleX(-1)';
  }

  // Wait for video to be ready
  try {
    await video.play();
  } catch (err) {
    // Clean up stream if video fails to play
    stream.getTracks().forEach(track => track.stop());
    throw new MediaCaptureError(
      'Failed to start video playback.',
      err instanceof Error ? err : undefined
    );
  }

  // Wait for video metadata to load (dimensions available)
  if (video.videoWidth === 0 || video.videoHeight === 0) {
    await new Promise<void>((resolve, reject) => {
      const timeout = setTimeout(() => {
        reject(new MediaCaptureError('Timeout waiting for video metadata.'));
      }, 5000);

      video.onloadedmetadata = () => {
        clearTimeout(timeout);
        resolve();
      };

      video.onerror = () => {
        clearTimeout(timeout);
        reject(new MediaCaptureError('Video element error.'));
      };
    });
  }

  const [track] = stream.getVideoTracks();

  if (!track) {
    stream.getTracks().forEach(t => t.stop());
    throw new MediaCaptureError('No video track in media stream.');
  }

  const destroy = () => {
    video.srcObject = null;
    video.pause();
    stream.getTracks().forEach(t => t.stop());
  };

  return { track, video, destroy };
}

/**
 * Capture a single frame from a video element as a canvas.
 *
 * @param video - The video element to capture from
 * @returns Canvas with the current frame, or null if video not ready
 */
export function captureFrame(video: HTMLVideoElement): HTMLCanvasElement | null {
  if (video.readyState < 2) {
    return null; // HAVE_CURRENT_DATA or less
  }

  const canvas = document.createElement('canvas');
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;

  const ctx = canvas.getContext('2d');
  if (!ctx) {
    return null;
  }

  ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
  return canvas;
}

/**
 * Capture a frame and encode as JPEG base64.
 *
 * @param video - The video element to capture from
 * @param quality - JPEG quality (0-1, default 0.85)
 * @returns Base64-encoded JPEG string (without data URL prefix), or null if not ready
 */
export function captureFrameAsJpeg(
  video: HTMLVideoElement,
  quality: number = 0.85
): string | null {
  const canvas = captureFrame(video);
  if (!canvas) {
    return null;
  }

  const dataUrl = canvas.toDataURL('image/jpeg', quality);
  // Strip "data:image/jpeg;base64," prefix
  const base64 = dataUrl.split(',')[1];
  return base64 ?? null;
}

/**
 * Get the video dimensions once metadata is loaded.
 *
 * @param video - The video element
 * @returns Tuple of [width, height], or [0, 0] if not ready
 */
export function getVideoDimensions(video: HTMLVideoElement): [number, number] {
  return [video.videoWidth, video.videoHeight];
}
