/** Bounded workers stop taking queued items while allowing in-flight work to finish. */
export async function runPromptQueue<T>(
  items: T[],
  concurrency: number,
  work: (item: T) => Promise<void>,
  shouldStop: () => boolean,
): Promise<void> {
  let next = 0;
  const worker = async () => {
    while (!shouldStop() && next < items.length) {
      const item = items[next++];
      await work(item);
    }
  };
  await Promise.all(
    Array.from(
      {
        length: Math.min(
          items.length,
          Math.max(1, Math.min(4, Math.floor(concurrency) || 1)),
        ),
      },
      worker,
    ),
  );
}
