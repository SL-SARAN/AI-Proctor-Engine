/**
 * Handles extraction of the LTI session token and exam session ID
 * from the URL fragment (the "hash").
 *
 * See `docs/02-ingestion-layer-design.md` update: the token travels
 * in the fragment (`#session_token=...&session_id=...`) to avoid
 * leaking in reverse-proxy logs, `Referer` headers, and browser
 * history. We extract it here and immediately strip it from the
 * visible URL.
 */

export class RedirectError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'RedirectError';
  }
}

export interface LaunchParams {
  sessionToken: string;
  sessionId: string;
}

/**
 * Extracts and consumes the launch parameters from the URL fragment.
 *
 * @param window The browser window (passed explicitly for testability).
 * @throws RedirectError if the fragment is missing or malformed.
 */
export function consumeRedirectFragment(windowObj: Window = window): LaunchParams {
  const hash = windowObj.location.hash;

  // No hash at all
  if (!hash || hash.length <= 1) {
    throw new RedirectError('Missing URL fragment');
  }

  // Strip the leading '#'
  const hashContent = hash.substring(1);

  // URLSearchParams can parse key=value&key2=value2 just fine,
  // we don't need to write a custom splitter.
  const params = new URLSearchParams(hashContent);

  const sessionToken = params.get('session_token');
  const sessionId = params.get('session_id');

  if (!sessionToken) {
    throw new RedirectError('Missing session_token in fragment');
  }
  if (!sessionId) {
    throw new RedirectError('Missing session_id in fragment');
  }

  // Immediately strip the fragment from the visible URL so it
  // doesn't linger for screen shares or casual observation.
  // We use replaceState instead of setting hash='' to avoid leaving a
  // trailing '#' on the URL.
  const newUrl = windowObj.location.pathname + windowObj.location.search;
  windowObj.history.replaceState(null, '', newUrl);

  return { sessionToken, sessionId };
}
