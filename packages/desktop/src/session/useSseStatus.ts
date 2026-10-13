import { useSyncExternalStore } from "react";

let connected = false;
let lastSeq = 0;
let cache: { connected: boolean; lastSeq: number } | null = null;
const listeners = new Set<() => void>();

function snapshot(): { connected: boolean; lastSeq: number } {
  if (!cache || cache.connected !== connected || cache.lastSeq !== lastSeq) {
    cache = { connected, lastSeq };
  }
  return cache;
}

export function publishSseStatus(next: { connected: boolean; lastSeq: number }) {
  if (next.connected === connected && next.lastSeq === lastSeq) return;
  connected = next.connected;
  lastSeq = next.lastSeq;
  cache = { connected, lastSeq };
  listeners.forEach((l) => l());
}

export function useSseStatus(): { connected: boolean; lastSeq: number } {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => {
        listeners.delete(cb);
      };
    },
    snapshot,
    snapshot,
  );
}
