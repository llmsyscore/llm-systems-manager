// Links to other sites open in a new tab and carry ref=demo.
(function (self) {
  if (self.DemoLinks) return;
  var doc = self.document;
  var loc = self.location;

  // Returns the URL with ref=demo added when it points to another http(s) site; otherwise unchanged.
  function tag(url) {
    var u;
    try { u = new self.URL(String(url), loc.href); } catch (e) { return url; }
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return url;
    if (u.host === loc.host) return url;
    if (u.searchParams.has('ref')) return u.href;
    u.searchParams.append('ref', 'demo');
    return u.href;
  }

  function external(a) {
    if (!a || !a.getAttribute || a.hasAttribute('download')) return false;
    var raw = a.getAttribute('href');
    if (!raw) return false;
    var u;
    try { u = new self.URL(raw, loc.href); } catch (e) { return false; }
    return (u.protocol === 'http:' || u.protocol === 'https:') && u.host !== loc.host;
  }

  function fix(a) {
    if (!external(a)) return;
    var raw = a.getAttribute('href');
    var t = tag(raw);
    if (t !== raw) a.setAttribute('href', t);
    a.setAttribute('target', '_blank');
    var rel = (a.getAttribute('rel') || '').split(/\s+/).filter(Boolean);
    if (rel.indexOf('noopener') < 0) rel.push('noopener');
    a.setAttribute('rel', rel.join(' '));
  }

  function sweep() {
    var all = doc.querySelectorAll('a[href^="http"], a[href^="//"]');
    for (var i = 0; i < all.length; i++) fix(all[i]);
  }

  if (doc) {
    doc.addEventListener('click', function (e) {
      for (var n = e.target; n && n.nodeType === 1; n = n.parentElement) {
        if (n.tagName === 'A') { fix(n); return; }
      }
    }, true);

    var timer = null;
    var later = function () {
      if (timer) return;
      timer = self.setTimeout(function () { timer = null; sweep(); }, 150);
    };
    var start = function () {
      sweep();
      if (typeof self.MutationObserver === 'function' && doc.body) {
        new self.MutationObserver(later).observe(doc.body, { childList: true, subtree: true });
      }
    };
    if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', start);
    else start();
  }

  // window.open to another site gets the tag and always opens a new tab.
  if (typeof self.open === 'function') {
    var realOpen = self.open;
    self.open = function (url, target, features) {
      if (url && tag(url) !== String(url)) return realOpen.call(self, tag(url), '_blank', features);
      if (url) {
        try {
          var u = new self.URL(String(url), loc.href);
          if ((u.protocol === 'http:' || u.protocol === 'https:') && u.host !== loc.host) return realOpen.call(self, u.href, '_blank', features);
        } catch (e) { /* fall through */ }
      }
      return realOpen.apply(self, arguments);
    };
  }

  self.DemoLinks = { tag: tag };
})(self);
