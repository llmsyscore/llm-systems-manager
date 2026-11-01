// #1302: run-conditions block rows, comparison and rendering.
import { describe, it, expect } from 'vitest';
import COND from '../js/lib/conditions.js';
const A = { schema: 1, captured_start: '2026-10-18T20:00:00Z', captured_end: '2026-10-18T20:03:00Z',
  hardware: { gpu: '7900 XTX', gpu_count: 1, driver: 'amdgpu 6.12.0', cpu: 'Ryzen 7 9700X', cpu_cores: 8, cpu_threads: 16, ram_bytes: 68719476736 },
  os: { name: 'Debian GNU/Linux 13 (trixie)', kernel: '6.12.0-amd64' },
  backend: { name: 'llama.cpp', build: 'b1234-abc', server_cmd: 'llama-server -m x.gguf  -ngl 99' },
  model: { id: 'org/m', file: 'x.gguf', size_bytes: 4831838208, hash: 'ht-sha256:0123456789ab', quantization: 'Q4_K_M', shards: null },
  context: 32768, sampling: { temperature: 0.8, top_k: 40 },
  snapshot: { start: { temp_c: 41, sclk_mhz: 2100, power_w: 48, power_cap_w: 300, performance_level: 'auto', cpu_governor: 'performance', ram_available_bytes: 51539607552 },
              end: { temp_c: 72, sclk_mhz: 2400, power_w: 290, power_cap_w: 300, performance_level: 'auto', cpu_governor: 'performance', ram_available_bytes: 49392123904 } } };
describe('COND.rows', () => {
  it('flattens the block with the spec labels and formats sizes and start→end pairs', () => {
    const r = Object.fromEntries(COND.rows(A).map(x => [x.label, x.value]));
    expect(r['GPU']).toBe('7900 XTX'); expect(r['Driver']).toBe('amdgpu 6.12.0'); expect(r['Cores']).toBe('8 cores · 16 threads');
    expect(r['RAM']).toBe('64.0 GiB'); expect(r['Size']).toBe('4.5 GiB'); expect(r['File id']).toBe('ht-sha256:0123456789ab');
    expect(r['GPU temp start→end']).toBe('41 → 72 °C'); expect(r['Clock start→end']).toBe('2100 → 2400 MHz'); expect(r['Power start→end']).toBe('48 → 290 W');
    expect(r['RAM free start→end']).toBe('48.0 → 46.0 GiB'); expect(r['Server command']).toBe('llama-server -m x.gguf  -ngl 99');
    expect(COND.rows(null)).toEqual([]); expect(COND.rows({ schema: 1 })).toEqual([]);
  });
});
describe('COND.diff', () => {
  it('applies the thresholds and normalises server-command whitespace', () => {
    const B = JSON.parse(JSON.stringify(A));
    B.backend.server_cmd = 'llama-server -m x.gguf -ngl 99'; B.snapshot.start.temp_c = 45; B.snapshot.start.power_w = 60; B.model.hash = 'ht-sha256:ffffffffffff';
    const d = Object.fromEntries(COND.diff(A, B).map(x => [x.label, x]));
    expect(d['Server command'].changed).toBe(false);
    expect(d['GPU temp start→end'].changed).toBe(false);
    expect(d['Power start→end'].changed).toBe(true);
    expect(d['File id']).toMatchObject({ a: 'ht-sha256:0123456789ab', b: 'ht-sha256:ffffffffffff', changed: true });
    expect(COND.diff(null, A).every(x => x.a == null)).toBe(true);
  });
});
describe('COND.html', () => {
  it('renders a details block that escapes the command and wraps it', () => {
    const h = COND.html({ ...A, backend: { ...A.backend, server_cmd: 'a <b> & "c"' } });
    expect(h).toContain('<details class="cond-block">'); expect(h).toContain('a &lt;b&gt; &amp; &quot;c&quot;');
    expect(h).toMatch(/<td class="wrap">a &lt;b&gt;/); expect(COND.html(null)).toBe('');
  });
});
describe('COND install row', () => {
  it('renders the installed build and flags a change', () => {
    const B = JSON.parse(JSON.stringify(A));
    B.backend.install = { name: 'rocm-build', backend: 'ROCm' };
    const r = Object.fromEntries(COND.rows(B).map(x => [x.label, x.value]));
    expect(r['Installed build']).toBe('rocm-build \u00b7 ROCm');
    const C = JSON.parse(JSON.stringify(B)); C.backend.install = { name: 'vk-build', backend: 'Vulkan' };
    const d = Object.fromEntries(COND.diff(B, C).map(x => [x.label, x]));
    expect(d['Installed build'].changed).toBe(true);
  });
});
