// Demo layer for the phone app at /phone: keeps the demo worker in charge and reports screen changes.
(function (self) {
  if (self.__demoPhoneInstalled) return;
  self.__demoPhoneInstalled = true;
  var doc = self.document;
  var sw = self.navigator && self.navigator.serviceWorker;
  var NO_PUSH = 'Not available in the demo';

  // The app's own worker and push sign-up are turned off; only the demo worker registers.
  if (sw && typeof sw.register === 'function') {
    var realRegister = sw.register.bind(sw);
    sw.register = function (url, opts) {
      if (/\/demo-sw\.js(\?|$)/.test(String(url))) return realRegister(url, opts);
      return Promise.reject(new Error(NO_PUSH));
    };
  }

  function tab(name) {
    if (!doc || typeof self.CustomEvent !== 'function') return;
    doc.dispatchEvent(new self.CustomEvent('lsm:tab', { detail: { tab: name } }));
  }

  // Reports 'phone-<screen>' each time the highlighted tab bar button changes.
  function watchScreens() {
    var bar = doc.getElementById('tabbar');
    if (!bar) return;
    var last = 'glance';
    var check = function () {
      var on = bar.querySelector('.tab.on');
      var name = on && on.getAttribute('data-tab');
      if (!name || name === last) return;
      last = name;
      tab('phone-' + name);
    };
    if (typeof self.MutationObserver === 'function') {
      new self.MutationObserver(check).observe(bar, { attributes: true, attributeFilter: ['class'], subtree: true });
    }
  }

  // Replaces the push status the app shows without its worker with the demo notice.
  function watchPush() {
    var st = doc.getElementById('pushStatus');
    if (!st || typeof self.MutationObserver !== 'function') return;
    var fix = function () { if (/service worker/.test(st.textContent)) st.textContent = NO_PUSH; };
    new self.MutationObserver(fix).observe(st, { childList: true, characterData: true, subtree: true });
    fix();
  }

  // Hides the app's Sign out row.
  function hideSignOut() {
    var a = doc.querySelector('a[href^="/logout"]');
    var row = a && (a.closest ? a.closest('.arow') : a.parentNode);
    if (row) row.style.display = 'none';
  }

  tab('phone');
  if (doc) {
    var ready = function () { watchScreens(); watchPush(); hideSignOut(); };
    if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', ready);
    else ready();
  }

  self.DemoPhone = { NO_PUSH: NO_PUSH };
})(self);
