// Install panel: the one-line command with Copy, Share, Star on GitHub and the directions link.
(function () {
  if (self.DemoInstall) return;
  var COMMAND = 'bash <(curl -fsSL https://get.llmsysmanager.com/install.sh)';
  var SHARE_URL = 'https://get.llmsysmanager.com/?ref=demo-share';
  var GITHUB_URL = 'https://github.com/llmsyscore/llm-systems-manager';
  var DIRECTIONS_URL = 'https://get.llmsysmanager.com/?ref=demo';
  var doc = self.document;

  function el(tag, cls, text) {
    var n = doc.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function say(noteEl, msg) { if (noteEl) noteEl.textContent = msg; }

  // Writes text to the clipboard; otherwise selects selectEl so the visitor can copy it.
  function copy(text, noteEl, selectEl) {
    function fallback() {
      try {
        if (selectEl) {
          var r = doc.createRange();
          r.selectNodeContents(selectEl);
          var s = self.getSelection && self.getSelection();
          if (s) { s.removeAllRanges(); s.addRange(r); }
        }
      } catch (e) { /* selection unavailable */ }
      say(noteEl, 'Press Ctrl+C / ⌘C to copy');
      return false;
    }
    var cb = self.navigator && self.navigator.clipboard;
    if (!cb || typeof cb.writeText !== 'function') return Promise.resolve(fallback());
    var p;
    try { p = Promise.resolve(cb.writeText(text)); } catch (e) { return Promise.resolve(fallback()); }
    return p.then(function () { say(noteEl, 'Copied'); return true; }, fallback);
  }

  // Opens the system share sheet, or copies the share link when there is none.
  function share(noteEl) {
    var nav = self.navigator;
    if (nav && typeof nav.share === 'function') {
      return Promise.resolve().then(function () {
        return nav.share({ title: 'LLM Systems Manager', text: 'Install LLM Systems Manager on your own machines', url: SHARE_URL });
      }).then(function () {}, function (err) {
        if (err && err.name === 'AbortError') return;
        return copy(SHARE_URL, null).then(function (ok) { say(noteEl, ok ? 'Link copied' : SHARE_URL); });
      });
    }
    return copy(SHARE_URL, null).then(function (ok) { say(noteEl, ok ? 'Link copied' : SHARE_URL); });
  }

  function shareButton(where) {
    var b = el('button', 'demo-btn demo-btn-ghost demo-install-share', 'Share');
    b.type = 'button';
    b.setAttribute('data-demo-track', 'share-' + where);
    return b;
  }
  function starLink(where) {
    var a = el('a', 'demo-btn demo-btn-ghost demo-install-star', 'Star on GitHub');
    a.href = GITHUB_URL;
    a.target = '_blank';
    a.rel = 'noopener';
    a.setAttribute('data-demo-track', 'star-' + where);
    return a;
  }

  function panel(where) {
    var root = el('div', 'demo-install');
    var code = el('code', 'demo-install-cmd', COMMAND);
    var copyBtn = el('button', 'demo-btn demo-install-copy', 'Copy');
    copyBtn.type = 'button';
    copyBtn.setAttribute('data-demo-track', 'copy-' + where);
    var shareBtn = shareButton(where);
    var dir = el('a', 'demo-install-directions', 'Installation directions');
    dir.href = DIRECTIONS_URL;
    dir.target = '_blank';
    dir.rel = 'noopener';
    dir.setAttribute('data-demo-track', 'directions-' + where);
    var note = el('p', 'demo-install-note');
    note.setAttribute('aria-live', 'polite');
    copyBtn.addEventListener('click', function () { copy(COMMAND, note, code); });
    shareBtn.addEventListener('click', function () { share(note); });
    var row = el('div', 'demo-install-actions');
    row.appendChild(copyBtn);
    row.appendChild(shareBtn);
    row.appendChild(starLink(where));
    root.appendChild(code);
    root.appendChild(row);
    root.appendChild(dir);
    root.appendChild(note);
    return root;
  }

  self.DemoInstall = { COMMAND: COMMAND, SHARE_URL: SHARE_URL, GITHUB_URL: GITHUB_URL, DIRECTIONS_URL: DIRECTIONS_URL,
    panel: panel, shareButton: shareButton, starLink: starLink, copy: copy, share: share };
})();
