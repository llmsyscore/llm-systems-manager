// #359: admin pool/pins cards are provider-parameterized, not llama-hardcoded.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const adminSrc = readFileSync(join(root, 'js/admin.js'), 'utf8');
const indexSrc = readFileSync(join(root, 'index.html'), 'utf8');

describe('provider-parameterized pool/pins admin UI', () => {
  it('stores pool_providers from /api/agents', () => {
    expect(adminSrc).toMatch(/pool_providers/);
  });
  it('builds pool/pins/models URLs from the selected provider', () => {
    expect(adminSrc).toMatch(/\$\{[^}]*\}-pool/);
    expect(adminSrc).toMatch(/\/api\/admin\/\$\{[^}]*\}-pins/);
    expect(adminSrc).toMatch(/\/api\/admin\/\$\{[^}]*\}-models/);
  });
  it('has no hardcoded llama-only pool/pins fetches left', () => {
    expect(adminSrc).not.toMatch(/fetch\((['"`])[^'"`$]*llama-pool/);
    expect(adminSrc).not.toMatch(/fetch\((['"`])[^'"`$]*llama-pins/);
    expect(adminSrc).not.toMatch(/fetch\((['"`])[^'"`$]*llama-models/);
  });
  it('index.html has provider chip containers on both cards', () => {
    expect(indexSrc).toMatch(/adminPoolProviderChips/);
    expect(indexSrc).toMatch(/adminPinsProviderChips/);
  });
  it('admin.js has a cache-bust query in index.html', () => {
    expect(indexSrc).toMatch(/js\/admin\.js\?v=[\w.-]+/);
  });
});
