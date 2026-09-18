import { defineConfig } from 'vitest/config';

// Run test/**/*.test.{js,mjs} in a jsdom environment.
export default defineConfig({
  test: {
    environment: 'jsdom',
    // Cap parallel test workers at three.
    maxWorkers: 3,
    include: ['test/**/*.test.js', 'test/**/*.test.mjs'],
  },
});
