import { readFileSync } from 'node:fs';
import os from 'node:os';
import { defineConfig } from 'vitest/config';

// Memory the kernel can hand out now (MemAvailable), falling back to os.freemem().
function availableMemoryMb() {
  try {
    const m = /^MemAvailable:\s+(\d+) kB/m.exec(readFileSync('/proc/meminfo', 'utf8'));
    if (m) return Math.floor(Number(m[1]) / 1024);
  } catch {}
  return Math.floor(os.freemem() / 1048576);
}

// Up to three jsdom workers, one per ~1 GB available, never more than the CPU count.
// VITEST_MAX_WORKERS=<n> sets the count explicitly.
function workerCount() {
  const override = Number.parseInt(process.env.VITEST_MAX_WORKERS ?? '', 10);
  if (override >= 1) return override;
  const cpus = os.availableParallelism?.() ?? os.cpus().length;
  const byMemory = Math.floor(availableMemoryMb() / 1024);
  return Math.max(1, Math.min(3, cpus, byMemory));
}

const maxWorkers = workerCount();
console.log(`vitest workers: ${maxWorkers} (${availableMemoryMb()} MB available, ${os.availableParallelism?.() ?? os.cpus().length} cpus)`);

// Run test/**/*.test.{js,mjs} in a jsdom environment.
export default defineConfig({
  test: {
    environment: 'jsdom',
    maxWorkers,
    include: ['test/**/*.test.js', 'test/**/*.test.mjs'],
  },
});
