/**
 * Attaches DOM event listeners for the 6 valid browser events the engine
 * cares about. See `docs/02-ingestion-layer-design.md` §3.
 *
 * Returns a `detach()` function that removes the listeners. Detached
 * listeners are idempotent: calling detach twice is safe.
 */

import { BrowserEventType, VALID_BROWSER_EVENTS } from './envelope.js';

export type BrowserEventCallback = (
  eventType: BrowserEventType,
  detail: Record<string, unknown>
) => void;

interface AttachedListeners {
  [key: string]: EventListener;
}

export function attachBrowserEventListeners(callback: BrowserEventCallback): () => void {
  if (typeof document === 'undefined') {
    return () => {}; // No-op in non-browser environments (SSR or test runner)
  }

  const listeners: AttachedListeners = {};

  // 1. visibilitychange -> { hidden: boolean }
  listeners['visibilitychange'] = () => {
    callback('visibilitychange', { hidden: document.hidden });
  };

  // 2. blur -> { relatedTarget: string | null }
  listeners['blur'] = () => {
    callback('blur', { relatedTarget: null });
  };

  // 3. focus -> { relatedTarget: string | null }
  listeners['focus'] = () => {
    callback('focus', { relatedTarget: null });
  };

  // 4. fullscreenchange -> { is_fullscreen: boolean }
  listeners['fullscreenchange'] = () => {
    callback('fullscreenchange', { is_fullscreen: !!document.fullscreenElement });
  };

  // 5. copy / paste -> { clipboardData?: string } (we don't read clipboard contents due to permissions)
  listeners['copy'] = () => {
    callback('copy', {});
  };
  listeners['paste'] = () => {
    callback('paste', {});
  };

  // 6. contextmenu -> { target: string }
  listeners['contextmenu'] = (event) => {
    const mouseEvent = event as MouseEvent;
    callback('contextmenu', {
      target: (mouseEvent.target as HTMLElement)?.tagName ?? 'unknown',
    });
  };

  // Attach all
  for (const eventName of Object.keys(listeners)) {
    const listener = listeners[eventName];
    if (listener) {
      document.addEventListener(eventName, listener);
    }
  }

  // Return detach function
  let detached = false;
  return () => {
    if (detached) return;
    detached = true;
    for (const eventName of Object.keys(listeners)) {
      const listener = listeners[eventName];
      if (listener) {
        document.removeEventListener(eventName, listener);
      }
    }
  };
}

/**
 * Helper to assert a value is one of the valid browser event types.
 * Useful for tests and clients consuming an arbitrary DOM event name.
 */
export function isBrowserEventType(value: string): value is BrowserEventType {
  return (VALID_BROWSER_EVENTS as readonly string[]).includes(value);
}
