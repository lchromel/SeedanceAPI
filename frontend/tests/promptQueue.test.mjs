import test from 'node:test';
import assert from 'node:assert/strict';
import {runPromptQueue} from '../src/promptQueue.ts';

const deferred = () => {
  let resolve;
  const promise = new Promise((done) => {resolve = done;});
  return {promise, resolve};
};

test('prompts overlap while respecting the selected concurrency', async () => {
  const gates = Array.from({length: 5}, deferred);
  const started = [], finished = [];
  const running = runPromptQueue([0,1,2,3,4], 3, async (item) => {
    started.push(item);
    await gates[item].promise;
    finished.push(item);
  }, () => false);
  assert.deepEqual(started, [0,1,2]);
  gates[1].resolve();
  await new Promise(setImmediate);
  assert.deepEqual(started, [0,1,2,3]);
  assert.deepEqual(finished, [1]);
  gates.forEach((gate) => gate.resolve());
  await running;
  assert.deepEqual([...finished].sort(), [0,1,2,3,4]);
});

test('stop leaves queued prompts untouched and lets active prompts finish', async () => {
  const gate = deferred();
  let stopped = false;
  const started = [], finished = [];
  const running = runPromptQueue([0,1,2,3,4], 2, async (item) => {
    started.push(item);
    await gate.promise;
    finished.push(item);
  }, () => stopped);
  assert.deepEqual(started, [0,1]);
  stopped = true;
  gate.resolve();
  await running;
  assert.deepEqual(started, [0,1]);
  assert.deepEqual(finished, [0,1]);
});

test('a failed prompt can be collected without discarding other successful rows', async () => {
  const failed = [], ready = [];
  await runPromptQueue([0,1,2], 2, async (item) => {
    try {
      if (item === 1) throw new Error('Vision unavailable');
      ready.push(item);
    } catch { failed.push(item); }
  }, () => false);
  assert.deepEqual(ready, [0,2]);
  assert.deepEqual(failed, [1]);
  await runPromptQueue(failed, 2, async (item) => { ready.push(item); }, () => false);
  assert.deepEqual(ready, [0,2,1]);
});
