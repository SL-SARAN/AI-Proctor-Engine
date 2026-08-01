import { describe, it, expect, vi, beforeEach } from 'vitest';
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
});
