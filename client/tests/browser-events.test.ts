import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { attachBrowserEventListeners, isBrowserEventType } from '../src/browser-events.js';
import type { BrowserEventType } from '../src/envelope.js';

describe('attachBrowserEventListeners', () => {
  let capturedEvents: Array<{ type: BrowserEventType; detail: Record<string, unknown> }>;
  let detach: () => void;

  beforeEach(() => {
    capturedEvents = [];
    detach = attachBrowserEventListeners((type, detail) => {
      capturedEvents.push({ type, detail });
    });
  });

  afterEach(() => {
    detach();
  });

  it('captures visibilitychange events', () => {
    document.dispatchEvent(new Event('visibilitychange'));
    expect(capturedEvents).toHaveLength(1);
    expect(capturedEvents[0]!.type).toBe('visibilitychange');
    expect(capturedEvents[0]!.detail).toHaveProperty('hidden');
  });

  it('captures blur events', () => {
    document.dispatchEvent(new Event('blur'));
    expect(capturedEvents).toHaveLength(1);
    expect(capturedEvents[0]!.type).toBe('blur');
  });

  it('captures focus events', () => {
    document.dispatchEvent(new Event('focus'));
    expect(capturedEvents).toHaveLength(1);
    expect(capturedEvents[0]!.type).toBe('focus');
  });

  it('captures fullscreenchange events', () => {
    document.dispatchEvent(new Event('fullscreenchange'));
    expect(capturedEvents).toHaveLength(1);
    expect(capturedEvents[0]!.type).toBe('fullscreenchange');
    expect(capturedEvents[0]!.detail).toHaveProperty('is_fullscreen');
  });

  it('captures copy events', () => {
    document.dispatchEvent(new Event('copy'));
    expect(capturedEvents).toHaveLength(1);
    expect(capturedEvents[0]!.type).toBe('copy');
  });

  it('captures paste events', () => {
    document.dispatchEvent(new Event('paste'));
    expect(capturedEvents).toHaveLength(1);
    expect(capturedEvents[0]!.type).toBe('paste');
  });

  it('captures contextmenu events', () => {
    document.dispatchEvent(new MouseEvent('contextmenu'));
    expect(capturedEvents).toHaveLength(1);
    expect(capturedEvents[0]!.type).toBe('contextmenu');
  });

  it('detach removes all listeners (idempotent)', () => {
    detach();
    document.dispatchEvent(new Event('blur'));
    expect(capturedEvents).toHaveLength(0);

    // Calling detach again is safe
    detach();
  });

  it('captures multiple different events in order', () => {
    document.dispatchEvent(new Event('blur'));
    document.dispatchEvent(new Event('focus'));
    document.dispatchEvent(new Event('copy'));
    expect(capturedEvents).toHaveLength(3);
    expect(capturedEvents[0]!.type).toBe('blur');
    expect(capturedEvents[1]!.type).toBe('focus');
    expect(capturedEvents[2]!.type).toBe('copy');
  });
});

describe('isBrowserEventType', () => {
  it('returns true for valid event types', () => {
    expect(isBrowserEventType('blur')).toBe(true);
    expect(isBrowserEventType('focus')).toBe(true);
    expect(isBrowserEventType('copy')).toBe(true);
    expect(isBrowserEventType('visibilitychange')).toBe(true);
    expect(isBrowserEventType('contextmenu')).toBe(true);
  });

  it('returns false for invalid event types', () => {
    expect(isBrowserEventType('click')).toBe(false);
    expect(isBrowserEventType('keydown')).toBe(false);
    expect(isBrowserEventType('')).toBe(false);
  });
});
