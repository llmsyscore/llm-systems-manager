// Web-push helpers for the PWA companion (#522). Dual-mode: classic-script
// global (window.PushUtil) + CommonJS export for vitest.

// Decode a base64url (unpadded) string — e.g. a VAPID application server
// key — into the Uint8Array shape pushManager.subscribe() requires.
function urlB64ToUint8Array(s) {
  const pad = '='.repeat((4 - (s.length % 4)) % 4);
  const b64 = (s + pad).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(b64);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

if (typeof window !== 'undefined')
  window.PushUtil = { urlB64ToUint8Array };
if (typeof module !== 'undefined' && module.exports)
  module.exports = { urlB64ToUint8Array };
