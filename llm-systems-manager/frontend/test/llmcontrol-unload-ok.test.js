// #730: Load/Unload/Reload surface a 200 {ok:false} agent reply instead of
// reporting success. Real source in jsdom via extraction.
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { fnSrc as sharedFnSrc, srcFile } from './helpers/harness.js';

const src = srcFile('js/llmcontrol-models.js');
const fnSrc = (name) => {
  const m = sharedFnSrc(src, name);
  expect(m, `${name} not found`).toBeTruthy();
  return m;
};

const resp = (body) => ({ ok: true, status: 200, json: async () => body,
                          headers: { get: () => null } });

beforeEach(() => {
  window._fetchT = vi.fn();
  window.alert = vi.fn();
  window.refreshLLMTab = vi.fn(async () => {});
  window._actionClaim = () => true;
  window._actionRelease = () => {};
  window._notePinOverride = () => {};
  window.CSS = { escape: (s) => s };
  (0, eval)([fnSrc('unloadModel'), fnSrc('loadModel'), fnSrc('reloadModel'),
             'window.unloadModel = unloadModel; window.loadModel = loadModel;',
             'window.reloadModel = reloadModel;'].join('\n'));
});

describe('unloadModel (#730)', () => {
  it('alerts on a 200 {ok:false} reply', async () => {
    window._fetchT.mockResolvedValue(resp({ ok: false, error: 'model instance did not unload in time' }));
    await window.unloadModel('m1');
    expect(window.alert).toHaveBeenCalledWith('Unload failed: model instance did not unload in time');
    expect(window.refreshLLMTab).toHaveBeenCalled();
  });
  it('stays quiet on {ok:true}', async () => {
    window._fetchT.mockResolvedValue(resp({ ok: true }));
    await window.unloadModel('m1');
    expect(window.alert).not.toHaveBeenCalled();
  });
});

describe('loadModel (#730)', () => {
  it('alerts on a 200 {ok:false} reply', async () => {
    window._fetchT.mockResolvedValue(resp({ ok: false, error: 'previous model instance did not unload in time' }));
    await window.loadModel('m1');
    expect(window.alert).toHaveBeenCalledWith('Load failed: previous model instance did not unload in time');
  });
});

describe('reloadModel (#730)', () => {
  it('stops after a refused unload and never issues the load', async () => {
    window._fetchT.mockResolvedValue(resp({ ok: false, error: 'llama-server returned HTTP 400' }));
    await window.reloadModel('m1');
    expect(window._fetchT).toHaveBeenCalledTimes(1);
    expect(window.alert).toHaveBeenCalledWith('Reload error: unload refused: llama-server returned HTTP 400');
    expect(window.refreshLLMTab).toHaveBeenCalled();
  });
});
