// Run-to-run spread formatting for bench results (#1301).
// IIFE-scoped, exposed as window.SPREAD.
(function (root, factory) {
  const api = factory();
  if (typeof root !== 'undefined') root.SPREAD = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {

// "lo–hi": p25–p75 at 8+ samples, min–max at 2–7, "" otherwise.
function rangeText(sp, digits) {
  if (!sp || !(sp.n >= 2)) return '';
  const d = digits == null ? 1 : digits;
  const iqr = sp.n >= 8 && sp.p25 != null && sp.p75 != null;
  const lo = iqr ? sp.p25 : sp.min;
  const hi = iqr ? sp.p75 : sp.max;
  if (lo == null || hi == null) return '';
  return Number(lo).toFixed(d) + '–' + Number(hi).toFixed(d);
}

function rangeLabel(sp, unit, digits, countWord) {
  const t = rangeText(sp, digits);
  return t ? `${t} ${unit} · ${sp.n} ${countWord}` : '';
}

return { rangeText, rangeLabel };
});
