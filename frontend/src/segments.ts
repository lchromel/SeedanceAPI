import type { Chunk } from "./types";
export interface Segment {
  start: number;
  duration: number;
  state: string;
  chunks: Chunk[];
}
/** Viewing segments stay at 30 seconds even when a provider job crosses a boundary. */
export function segments(chunks: Chunk[]): Segment[] {
  const groups = new Map<
    number,
    { start: number; end: number; chunks: Chunk[] }
  >();
  for (const chunk of chunks) {
    const end = chunk.start + chunk.duration;
    for (let cursor = chunk.start; cursor < end;) {
      const boundary = Math.floor(cursor / 30);
      const stop = Math.min(end, (boundary + 1) * 30);
      const group = groups.get(boundary) || {
        start: cursor,
        end: stop,
        chunks: [],
      };
      group.start = Math.min(group.start, cursor);
      group.end = Math.max(group.end, stop);
      group.chunks.push(chunk);
      groups.set(boundary, group);
      cursor = stop;
    }
  }
  return [...groups.values()]
    .sort((a, b) => a.start - b.start)
    .map(({ start, end, chunks: items }) => ({
      start,
      duration: end - start,
      state: items.every((c) => c.state === "ready")
        ? "ready"
        : items.some((c) => c.state === "review")
          ? "review"
          : items.some((c) => c.state === "error")
            ? "error"
            : items.some((c) =>
                  ["generating", "submitting", "ready"].includes(c.state),
                )
              ? "generating"
              : "queued",
      chunks: items,
    }));
}
