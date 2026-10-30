// Phone detection and the per-session "Desktop version" choice shared by the loading page, dashboard and tour.
(function () {
  if (self.DemoDevice) return;
  var DESKTOP_KEY = 'lsmDemoDesktop';

  // A phone: a narrow window with a touch screen or a mobile browser; tablets count as desktops.
  function isPhone() {
    var w = self.innerWidth || 0;
    if (!w || w > 640) return false;
    var coarse = false;
    try { coarse = !!(self.matchMedia && self.matchMedia('(pointer: coarse)').matches); } catch (e) { /* no matchMedia */ }
    return coarse || /Mobi|Android|iPhone/.test((self.navigator && self.navigator.userAgent) || '');
  }

  function choseDesktop() {
    try { return self.sessionStorage.getItem(DESKTOP_KEY) === '1'; } catch (e) { return false; }
  }

  function setDesktop(on) {
    try {
      if (on) self.sessionStorage.setItem(DESKTOP_KEY, '1'); else self.sessionStorage.removeItem(DESKTOP_KEY);
    } catch (e) { /* storage blocked */ }
  }

  function wantsPhone() { return isPhone() && !choseDesktop(); }

  self.DemoDevice = { isPhone: isPhone, choseDesktop: choseDesktop, setDesktop: setDesktop, wantsPhone: wantsPhone, DESKTOP_KEY: DESKTOP_KEY };
})();
