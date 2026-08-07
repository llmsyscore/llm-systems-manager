/* Shared interactions: copy button, screenshot gallery, contact form */
(function () {
  function copyText(text, done) {
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () { legacyCopy(text, done); });
    } else {
      legacyCopy(text, done);
    }
  }
  function legacyCopy(text, done) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (e) {}
    document.body.removeChild(ta);
    done();
  }

  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var text = document.querySelector(btn.getAttribute("data-copy")).textContent.trim();
      text = text.replace(/^\$ /gm, "");
      copyText(text, function () {
        var old = btn.textContent;
        btn.textContent = "copied";
        setTimeout(function () { btn.textContent = old; }, 1600);
      });
    });
  });

  document.querySelectorAll(".term-tabs button").forEach(function (tab) {
    tab.addEventListener("click", function () {
      tab.parentNode.querySelectorAll("button").forEach(function (x) {
        x.classList.toggle("active", x === tab);
        x.setAttribute("aria-selected", x === tab ? "true" : "false");
      });
      tab.closest(".term").querySelectorAll(".term-pane").forEach(function (p) {
        p.hidden = p.id !== tab.getAttribute("data-pane");
      });
      var note = document.getElementById("term-note");
      if (note && tab.getAttribute("data-note")) note.textContent = tab.getAttribute("data-note");
    });
  });

  var vers = document.querySelectorAll(".pkg-ver");
  if (vers.length) {
    fetch("https://api.github.com/repos/llmsyscore/llm-systems-manager/releases/latest")
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (rel) {
        if (!rel || !rel.tag_name) return;
        var v = rel.tag_name.replace(/^v/, "");
        vers.forEach(function (el) { el.textContent = v; });
      })
      .catch(function () {});
  }

  var main = document.getElementById("shot-main");
  var cap = document.getElementById("shot-caption");
  var thumbs = Array.prototype.slice.call(document.querySelectorAll(".thumbs button"));
  var current = 0;

  function show(i) {
    current = (i + thumbs.length) % thumbs.length;
    thumbs.forEach(function (x, n) { x.classList.toggle("active", n === current); });
    var t = thumbs[current];
    main.src = t.getAttribute("data-full");
    main.alt = t.getAttribute("data-caption");
    cap.textContent = t.getAttribute("data-caption");
  }

  if (main && thumbs.length) {
    thumbs.forEach(function (t, n) {
      t.addEventListener("click", function () { show(n); });
    });
    var prev = document.getElementById("gal-prev");
    var next = document.getElementById("gal-next");
    if (prev) prev.addEventListener("click", function () { show(current - 1); });
    if (next) next.addEventListener("click", function () { show(current + 1); });
  }

  var lb = document.getElementById("lightbox");
  if (main && lb) {
    var lbImg = lb.querySelector("img");
    main.addEventListener("click", function () {
      lbImg.src = main.src;
      lbImg.alt = main.alt;
      lb.classList.add("open");
    });
    lb.addEventListener("click", function () { lb.classList.remove("open"); });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") lb.classList.remove("open");
      if (main && lb.classList.contains("open")) {
        if (e.key === "ArrowLeft") { show(current - 1); lbImg.src = main.src; }
        if (e.key === "ArrowRight") { show(current + 1); lbImg.src = main.src; }
      }
    });
  }

  var form = document.getElementById("contact-form");
  if (form) {
    form.addEventListener("submit", function (e) {
      var action = form.getAttribute("action") || "";
      if (action.indexOf("YOUR_FORM_ID") !== -1) {
        e.preventDefault();
        var subject = encodeURIComponent("[" + form.topic.value + "] " + form.subject.value);
        var body = encodeURIComponent(form.message.value + "\n\n— " + form.name.value + " <" + form.email.value + ">");
        window.location.href = "mailto:support@llmsyscore.com?subject=" + subject + "&body=" + body;
        var st = document.getElementById("form-status");
        st.style.display = "block";
        st.textContent = "Opening your mail client to send this to support@llmsyscore.com.";
      }
    });
  }
})();
