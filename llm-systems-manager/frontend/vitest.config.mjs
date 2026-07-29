import { defineConfig } from 'vitest/config';

// Run test/**/*.test.{js,mjs} in a jsdom environment.
export default defineConfig({
  test: {
    environment: 'jsdom',
    include: ['test/**/*.test.js', 'test/**/*.test.mjs'],
  },
});
