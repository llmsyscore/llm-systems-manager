// #1395: Builds row — selection circles, start-body payload, switch note and progress lines.
import { describe, it, expect, beforeEach } from 'vitest';
import BP from '../js/lib/buildpick.js';

const VK = { path: '/opt/llama.cpp/build/bin/llama-server', dir: '/opt/llama.cpp/build/bin', name: 'llama.cpp', active: true, backend: 'Vulkan', ok: true };
const ROCM = { path: '/opt/llama-rocm/build/bin/llama-server', dir: '/opt/llama-rocm/build/bin', name: 'llama-rocm', active: false, backend: 'ROCm', ok: true };
const CPU = { path: '/opt/llama-cpu/bin/llama-server', dir: '/opt/llama-cpu/bin', name: 'llama-cpu', active: false, backend: 'CPU', ok: true };
const BAD = { path: '/opt/broken/bin/llama-server', dir: '/opt/broken/bin', name: 'broken', active: false, backend: 'CPU', ok: false, error: 'does not run' };

let el;
beforeEach(() => { document.body.innerHTML = '<div class="bp-row" id="xBuilds"></div>'; el = document.getElementById('xBuilds'); });
const pick = (path, on) => {
  const inp = [...el.querySelectorAll('.bp-list input')].find(i => i.value === path);
  inp.checked = on; inp.dispatchEvent(new Event('change'));
};

describe('BUILDPICK', () => {
  it('payload null for active only', () => {
    BP.render(el, [VK, ROCM], {});
    expect(el.style.display).toBe('');
    expect(BP.selected(el)).toEqual([VK.path]);
    expect(BP.payload(el)).toBeNull();
  });

  it('payload lists picked paths in list order', () => {
    BP.render(el, [VK, ROCM, CPU], {});
    pick(CPU.path, true); pick(ROCM.path, true);
    expect(BP.payload(el)).toEqual([VK.path, ROCM.path, CPU.path]);
    pick(VK.path, false);
    expect(BP.payload(el)).toEqual([ROCM.path, CPU.path]);
  });

  it('keeps the last picked circle on', () => {
    BP.render(el, [VK, ROCM], {});
    pick(VK.path, false);
    expect(BP.selected(el)).toEqual([VK.path]);
  });

  it('row hidden with one build', () => {
    BP.render(el, [VK, BAD], {});
    expect(el.style.display).toBe('none');
    expect(el.innerHTML).toBe('');
    expect(BP.payload(el)).toBeNull();
    BP.render(el, [], {});
    expect(el.style.display).toBe('none');
  });

  it('non-admin live sees no non-active circles', () => {
    BP.render(el, [VK, ROCM, CPU], { needsSwitchNote: true, admin: false });
    expect(el.querySelectorAll('.bp-list input').length).toBe(0);
    expect(el.style.display).toBe('none');
    BP.render(el, [VK, ROCM, CPU], { needsSwitchNote: true, admin: true });
    expect(el.querySelectorAll('.bp-list input').length).toBe(3);
    BP.render(el, [VK, ROCM], { needsSwitchNote: false, admin: false });
    expect(el.querySelectorAll('.bp-list input').length).toBe(2);
  });

  it('switch note text exact', () => {
    BP.render(el, [VK, ROCM], { needsSwitchNote: true, admin: true });
    const note = el.querySelector('.bp-note');
    expect(note.style.display).toBe('none');
    pick(ROCM.path, true);
    expect(note.style.display).toBe('');
    expect(note.textContent).toBe('The server switches to each picked build and back to llama.cpp when the run ends.');
    BP.render(el, [VK, ROCM], { needsSwitchNote: false });
    pick(ROCM.path, true);
    expect(el.querySelector('.bp-note').textContent).toBe('');
  });

  it('labels each circle with the build name, backend and the active tag', () => {
    BP.render(el, [VK, ROCM], {});
    const labels = [...el.querySelectorAll('.bp-pick')].map(l => l.textContent);
    expect(labels).toEqual(['llama.cppVulkanActive', 'llama-rocmROCm']);
    expect(el.querySelector('.bp-h').textContent).toBe('Builds');
    expect(el.querySelector('.bp-pick input').type).toBe('checkbox');
    expect(el.querySelector('.bp-pick .dot')).not.toBeNull();
  });

  it('nameOf strips trailing bin and build folders when the row has no name', () => {
    expect(BP.nameOf({ dir: '/opt/llama-rocm/build/bin' })).toBe('llama-rocm');
    expect(BP.nameOf({ path: '/opt/llama-cpu/bin/llama-server' })).toBe('llama-cpu');
    expect(BP.nameOf({ name: 'given', dir: '/x/bin' })).toBe('given');
  });

  it('progress lines for the build events', () => {
    expect(BP.eventText({ type: 'build_start', build: 'llama-rocm', index: 1, total: 3 })).toBe('Build 2 of 3: llama-rocm');
    expect(BP.eventText({ phase: 'build_failed', build: 'llama-rocm', error: 'did not start' })).toBe('llama-rocm could not be used: did not start');
    expect(BP.eventText({ type: 'switch_back', ok: true }, 'llama.cpp')).toBe('Switched back to llama.cpp');
    expect(BP.eventText({ type: 'switch_back', ok: false, error: 'timed out' }, 'llama.cpp')).toBe('Could not switch back: timed out');
    expect(BP.eventText({ type: 'line' })).toBeNull();
  });

  it('load hides the row when the request fails', async () => {
    globalThis.fetch = () => Promise.reject(new Error('down'));
    await BP.load(el, '/api/llm/builds', {});
    expect(el.style.display).toBe('none');
    globalThis.fetch = () => Promise.resolve({ json: () => Promise.resolve({ ok: true, builds: [VK, ROCM] }) });
    await BP.load(el, '/api/llm/builds', {});
    expect(el.style.display).toBe('');
  });
});
