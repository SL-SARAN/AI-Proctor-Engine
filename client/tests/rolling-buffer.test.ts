import { describe, it, expect, beforeEach } from 'vitest';
import { RollingBuffer } from '../src/rolling-buffer.js';

describe('RollingBuffer', () => {
  let buffer: RollingBuffer;

  beforeEach(() => {
    buffer = new RollingBuffer({ windowMs: 10_000 }); // 10s window
  });

  it('starts empty', () => {
    expect(buffer.size()).toBe(0);
    expect(buffer.snapshot()).toEqual([]);
    expect(buffer.oldestTimestamp()).toBeNull();
  });

  it('push adds an entry', () => {
    buffer.push({ data: 'frame1', timestamp: Date.now() });
    expect(buffer.size()).toBe(1);
  });

  it('snapshot returns a copy', () => {
    const now = Date.now();
    buffer.push({ data: 'frame1', timestamp: now });
    const snap = buffer.snapshot();
    expect(snap).toHaveLength(1);
    expect(snap[0]!.data).toBe('frame1');
    // Mutating snapshot should not affect buffer
    snap.pop();
    expect(buffer.size()).toBe(1);
  });

  it('drain returns entries and empties the buffer', () => {
    const now = Date.now();
    buffer.push({ data: 'frame1', timestamp: now });
    buffer.push({ data: 'frame2', timestamp: now + 100 });
    const drained = buffer.drain();
    expect(drained).toHaveLength(2);
    expect(buffer.size()).toBe(0);
  });

  it('reset clears the buffer', () => {
    buffer.push({ data: 'frame1', timestamp: Date.now() });
    buffer.reset();
    expect(buffer.size()).toBe(0);
  });

  it('evicts entries older than windowMs', () => {
    const base = Date.now();
    // old entry (12 seconds ago)
    buffer.push({ data: 'old', timestamp: base - 12_000 });
    // recent entry (1 second ago)
    buffer.push({ data: 'recent', timestamp: base - 1_000 });
    // The eviction happens on push, triggered by the current timestamp
    // being Date.now() which is ~base
    expect(buffer.size()).toBe(1);
    expect(buffer.snapshot()[0]!.data).toBe('recent');
  });

  it('keeps entries exactly at the boundary (not older than windowMs)', () => {
    const now = Date.now();
    // Entry at exactly windowMs ago should be evicted (strictly <)
    buffer.push({ data: 'boundary', timestamp: now - 10_001 });
    buffer.push({ data: 'just-inside', timestamp: now - 9_999 });
    // After push, eviction runs with Date.now() >= now
    // boundary is at now - 10_001, cutoff is now - 10_000 => evicted
    // just-inside is at now - 9_999, cutoff is now - 10_000 => kept
    expect(buffer.size()).toBe(1);
    expect(buffer.snapshot()[0]!.data).toBe('just-inside');
  });

  it('oldestTimestamp returns the timestamp of the first entry', () => {
    const now = Date.now();
    buffer.push({ data: 'a', timestamp: now - 5_000 });
    buffer.push({ data: 'b', timestamp: now - 2_000 });
    expect(buffer.oldestTimestamp()).toBe(now - 5_000);
  });

  it('snapshot filters by modality', () => {
    const now = Date.now();
    buffer.push({ data: 'frame', timestamp: now, modality: 'heavy_frame' });
    buffer.push({ data: 'audio', timestamp: now + 100, modality: 'audio_chunk' });
    buffer.push({ data: 'frame2', timestamp: now + 200, modality: 'heavy_frame' });

    const frames = buffer.snapshot('heavy_frame');
    expect(frames).toHaveLength(2);
    expect(frames.every(e => e.modality === 'heavy_frame')).toBe(true);

    const audio = buffer.snapshot('audio_chunk');
    expect(audio).toHaveLength(1);
  });

  it('handles multiple pushes within the window without eviction', () => {
    const now = Date.now();
    for (let i = 0; i < 50; i++) {
      buffer.push({ data: `frame-${i}`, timestamp: now - (9_000 - i * 100) });
    }
    // All entries are within 10s window
    expect(buffer.size()).toBe(50);
  });

  describe('heavyFrameEntries eviction (item 5 fix)', () => {
    it('evicts old heavy frame entries beyond the time window', () => {
      const now = Date.now();
      // old heavy frame (12 seconds ago)
      buffer.add({
        timestamp: now - 12_000,
        jpegBase64: 'old-frame',
        landmarks: null,
        dimensions: [640, 480],
      });
      // recent heavy frame
      buffer.add({
        timestamp: now - 1_000,
        jpegBase64: 'recent-frame',
        landmarks: null,
        dimensions: [640, 480],
      });

      // The base entries array should have evicted the old one
      expect(buffer.size()).toBe(1);

      // Drain should return only the recent entry (both base + heavy cleared)
      const drained = buffer.drain();
      expect(drained).toHaveLength(1);
      expect(drained[0]!.data).toBe('recent-frame');
    });

    it('drain clears both entries and heavyFrameEntries', () => {
      const now = Date.now();
      buffer.add({
        timestamp: now,
        jpegBase64: 'frame-data',
        landmarks: [{ x: 0, y: 0, z: 0 }],
        dimensions: [640, 480],
      });

      expect(buffer.size()).toBe(1);
      const drained = buffer.drain();
      expect(drained).toHaveLength(1);
      expect(buffer.size()).toBe(0);

      // A second drain should return nothing
      expect(buffer.drain()).toHaveLength(0);
    });

    it('does not accumulate heavy frames indefinitely in a long session', () => {
      // Simulate a session that's been running for 60 seconds, adding
      // frames spaced 2.5s apart. Each frame's timestamp is anchored to
      // (now - 60s) and increments forward — so the eviction logic
      // (which uses Date.now() as "now") correctly identifies the early
      // frames as outside the 10s window.
      const now = Date.now();
      const sessionStart = now - 60_000; // 60 seconds ago
      for (let i = 0; i < 24; i++) {
        buffer.add({
          timestamp: sessionStart + i * 2500,
          jpegBase64: `frame-${i}`,
          landmarks: null,
          dimensions: [640, 480],
        });
      }
      // Only the last ~10s of frames survive in a 10s window. With 24
      // frames spaced 2.5s apart starting 60s ago, the last ~5 frames
      // (those within the last 10s) should remain.
      expect(buffer.size()).toBeLessThanOrEqual(6);
      expect(buffer.size()).toBeGreaterThan(0);
    });
  });
});
