import { describe, it, expect, vi } from 'vitest';
import { consumeRedirectFragment, RedirectError } from '../src/redirect.js';

function makeWindow(hash: string): Window {
  return {
    location: {
      hash,
      pathname: '/client/',
      search: '',
    },
    history: {
      replaceState: vi.fn(),
    },
  } as unknown as Window;
}

describe('consumeRedirectFragment', () => {
  it('extracts session_token and session_id from a valid fragment', () => {
    const w = makeWindow('#session_token=abc123&session_id=sess-456');
    const result = consumeRedirectFragment(w);
    expect(result.sessionToken).toBe('abc123');
    expect(result.sessionId).toBe('sess-456');
  });

  it('calls history.replaceState to strip the fragment', () => {
    const w = makeWindow('#session_token=tok&session_id=sid');
    consumeRedirectFragment(w);
    expect(w.history.replaceState).toHaveBeenCalledWith(null, '', '/client/');
  });

  it('throws RedirectError when fragment is missing', () => {
    const w = makeWindow('');
    expect(() => consumeRedirectFragment(w)).toThrow(RedirectError);
    expect(() => consumeRedirectFragment(w)).toThrow('Missing URL fragment');
  });

  it('throws RedirectError when fragment is just "#"', () => {
    const w = makeWindow('#');
    expect(() => consumeRedirectFragment(w)).toThrow(RedirectError);
  });

  it('throws RedirectError when session_token is missing from fragment', () => {
    const w = makeWindow('#session_id=sid');
    expect(() => consumeRedirectFragment(w)).toThrow('Missing session_token');
  });

  it('throws RedirectError when session_id is missing from fragment', () => {
    const w = makeWindow('#session_token=tok');
    expect(() => consumeRedirectFragment(w)).toThrow('Missing session_id');
  });

  it('handles fragment with extra parameters gracefully', () => {
    const w = makeWindow('#session_token=tok&session_id=sid&extra=val');
    const result = consumeRedirectFragment(w);
    expect(result.sessionToken).toBe('tok');
    expect(result.sessionId).toBe('sid');
  });

  it('handles URL-encoded values in fragment', () => {
    const w = makeWindow('#session_token=eyJ%3D&session_id=abc-123');
    const result = consumeRedirectFragment(w);
    expect(result.sessionToken).toBe('eyJ=');
    expect(result.sessionId).toBe('abc-123');
  });
});
