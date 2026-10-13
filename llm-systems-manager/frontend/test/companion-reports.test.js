// Companion Settings → Reports: three switches and a PUT per toggle.
import { describe, it, expect } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const html = fs.readFileSync(path.join(root, 'companion.html'), 'utf8');
const js = fs.readFileSync(path.join(root, 'js/companion.js'), 'utf8');

describe('companion reports card', () => {
  it('has the container and the settings code targets it', () => {
    expect(html).toContain('id="settingsReports"');
    expect(js).toMatch(/\$\('settingsReports'\)/);
  });
  it('toggles PUT the allowed keys only', () => {
    expect(js).toMatch(/jpost\('\/api\/admin\/telemetry', \{ \[key\]: next \}, 'PUT'\)/);
    expect(js).toMatch(/data-tele="\$\{key\}"/);
    for (const k of ['install_report', 'crash_reports', 'heartbeat']) expect(js).toContain(`'${k}'`);
  });
});
