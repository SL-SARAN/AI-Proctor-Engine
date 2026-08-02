/**
 * In-memory bounded rolling buffer for the heavy-frame pre-roll context.
 *
 * Per `docs/proctoring-engine-v1-spec.md` §4, the buffer is bounded to
 * roughly 10-15 seconds (configurable). We don't use IndexedDB or OPFS —
 * the buffer is short-lived by design (it flushes on a confirmed flag),
 * and a page-refresh losing the buffer is bounded by the kill-switch
 * flush timing anyway.
 *
 * Eviction is by timestamp window, not by count, so older entries are
 * dropped as new ones arrive.
 */

export interface RollingBufferConfig {
  /** Duration of the window in milliseconds. */
  windowMs: number;
}

export interface RollingBufferEntry {
  /** The captured frame data (base64 JPEG, in N+9). */
  data: string;
  /** Capture timestamp (epoch ms). */
  timestamp: number;
  /** Optional modality tag for filtering. */
  modality?: 'heavy_frame' | 'audio_chunk';
}

/**
 * Extended entry format from capture loop (turn N+9).
 * Contains additional metadata for heavy frames.
 */
export interface HeavyFrameEntry {
  timestamp: number;
  jpegBase64: string;
  landmarks?: { x: number; y: number; z: number }[] | null;
  dimensions: [number, number];
}

export class RollingBuffer {
  private entries: RollingBufferEntry[] = [];
  private heavyFrameEntries: HeavyFrameEntry[] = [];
  private readonly windowMs: number;

  constructor(config: RollingBufferConfig) {
    this.windowMs = config.windowMs;
  }

  /**
   * Push a new entry into the buffer, evicting any older than `windowMs`.
   */
  public push(entry: RollingBufferEntry): void {
    this.entries.push(entry);
    this.evictOld();
  }

  /**
   * Add a heavy frame entry from the capture loop (turn N+9).
   * Stores the extended metadata alongside the base entry.
   */
  public add(entry: HeavyFrameEntry): void {
    this.heavyFrameEntries.push(entry);
    this.push({
      data: entry.jpegBase64,
      timestamp: entry.timestamp,
      modality: 'heavy_frame',
    });
  }

  /**
   * Returns a copy of all current entries, optionally filtered by modality.
   */
  public snapshot(modality?: 'heavy_frame' | 'audio_chunk'): RollingBufferEntry[] {
    return this.entries.filter(e => !modality || e.modality === modality).slice();
  }

  /**
   * Returns a copy of all current entries and clears the buffer.
   */
  public drain(): RollingBufferEntry[] {
    const snapshot = this.entries.slice();
    this.entries = [];
    this.heavyFrameEntries = [];
    return snapshot;
  }

  /**
   * Clear the buffer without returning anything.
   */
  public reset(): void {
    this.entries = [];
    this.heavyFrameEntries = [];
  }

  public size(): number {
    return this.entries.length;
  }

  /**
   * Returns the timestamp (epoch ms) of the oldest entry, or null if empty.
   */
  public oldestTimestamp(): number | null {
    if (this.entries.length === 0) return null;
    return this.entries[0]?.timestamp ?? null;
  }

  /**
   * Internal helper: drop everything older than `windowMs` from now.
   */
  private evictOld(): void {
    const now = Date.now();
    const cutoff = now - this.windowMs;
    // Since we push in time order, we can find the first index where ts >= cutoff
    // and slice from there.
    let startIndex = 0;
    while (startIndex < this.entries.length && (this.entries[startIndex]?.timestamp ?? 0) < cutoff) {
      startIndex++;
    }
    if (startIndex > 0) {
      this.entries = this.entries.slice(startIndex);
    }
  }
}
