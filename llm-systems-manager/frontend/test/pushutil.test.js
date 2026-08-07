// urlB64ToUint8Array — VAPID application-server-key decoding (#522).
import { describe, it, expect } from 'vitest';
import { urlB64ToUint8Array } from '../js/lib/pushutil.js';

describe('urlB64ToUint8Array', () => {
  it('decodes plain base64 without padding', () => {
    expect(Array.from(urlB64ToUint8Array('AQID'))).toEqual([1, 2, 3]);
  });

  it('decodes url-safe alphabet (- and _)', () => {
    // 0xfb 0xef 0xbe encodes to "----" in url-safe base64; "+" form would be "++++"
    expect(Array.from(urlB64ToUint8Array('--u-'))).toEqual([0xfb, 0xeb, 0xbe]);
    expect(Array.from(urlB64ToUint8Array('_v7-'))).toEqual([0xfe, 0xfe, 0xfe]);
  });

  it('handles unpadded lengths like a real VAPID key (87 chars)', () => {
    const key = 'B' + 'A'.repeat(86);
    const raw = urlB64ToUint8Array(key);
    expect(raw.length).toBe(65);
    expect(raw[0]).toBe(0x04);
  });

  it('round-trips arbitrary bytes', () => {
    const bytes = [0, 1, 127, 128, 255, 66];
    const b64 = Buffer.from(bytes).toString('base64url');
    expect(Array.from(urlB64ToUint8Array(b64))).toEqual(bytes);
  });
});
