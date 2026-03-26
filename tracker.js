/**
 * ДепозитоПомогатор Tracker v2.0 (Refactored)
 * Lightweight session recording for affiliate landing pages
 * 
 * USAGE:
 * <script src="tracker.js" data-endpoint="https://your-server.com/api/track"></script>
 */

(function() {
  'use strict';

  // === Double-init guard ===
  if (window.__dp_initialized) return;
  window.__dp_initialized = true;

  // === Config ===
  // Capture currentScript ref BEFORE any async code (it becomes null after)
  var scriptTag = document.currentScript;
  var ENDPOINT = (scriptTag && scriptTag.getAttribute('data-endpoint'))
    || window.DP_ENDPOINT
    || '/api/track';

  var MOVE_THROTTLE   = 80;
  var SCROLL_THROTTLE = 200;
  var FLUSH_INTERVAL  = 5000;
  var MAX_BUFFER      = 500;
  var BEACON_LIMIT    = 60000; // sendBeacon practical limit ~64KB, leave margin
  var MAX_SECTIONS    = 30;    // limit IntersectionObserver targets

  // === Session ===
  var sessionId  = 'dp_' + Date.now().toString(36) + Math.random().toString(36).substring(2, 10);
  var pageUrl    = window.location.href;
  var pageTitle  = document.title;
  var referrer   = document.referrer;
  var startTime  = Date.now();

  var events           = [];
  var pendingRetry     = []; // events that failed to send
  var domSnapshot      = null;
  var maxScrollDepth   = 0;
  var totalClicks      = 0;
  var formInteractions = {};
  var rageClickBuffer  = [];
  var sessionEnded     = false;
  var flushTimer       = null;

  // === Helpers ===
  function ts() { return Date.now() - startTime; }

  function getViewport() {
    return { w: window.innerWidth, h: window.innerHeight };
  }

  function getDeviceType() {
    var w = window.innerWidth;
    return w <= 768 ? 'mobile' : w <= 1024 ? 'tablet' : 'desktop';
  }

  function sanitizeSid(id) {
    // Only allow alphanumeric, underscore, hyphen — prevent path traversal
    return String(id).replace(/[^a-zA-Z0-9_\-]/g, '').substring(0, 64);
  }

  function getSelector(el) {
    if (!el || el === document.body || el === document.documentElement) return 'body';
    if (el.id) return '#' + CSS.escape(el.id);

    // Build short path (max 4 levels, no querySelectorAll perf hit)
    var path = [];
    var current = el;
    var depth = 0;
    while (current && current !== document.body && depth < 4) {
      var part = current.tagName.toLowerCase();
      if (current.id) {
        path.unshift('#' + CSS.escape(current.id));
        break;
      }
      var parent = current.parentElement;
      if (parent) {
        var children = parent.children;
        var sameTag = 0, idx = 0;
        for (var i = 0; i < children.length; i++) {
          if (children[i].tagName === current.tagName) {
            sameTag++;
            if (children[i] === current) idx = sameTag;
          }
        }
        if (sameTag > 1) part += ':nth-of-type(' + idx + ')';
      }
      path.unshift(part);
      current = current.parentElement;
      depth++;
    }
    return path.join('>');
  }

  function getElementMeta(el) {
    if (!el) return {};
    var tag = '';
    try { tag = el.tagName ? el.tagName.toLowerCase() : ''; } catch(e) {}
    
    var text = '';
    try { text = (el.textContent || '').trim().substring(0, 80); } catch(e) {}
    
    var href = '';
    try { href = el.href || (el.closest && el.closest('a') ? el.closest('a').href : '') || ''; } catch(e) {}

    return {
      tag: tag,
      id: el.id || undefined,
      classes: (el.className && typeof el.className === 'string') ? el.className.trim().substring(0, 100) : undefined,
      text: text || undefined,
      href: href || undefined,
      type: el.type || undefined,
      name: el.name || undefined,
      selector: getSelector(el)
      // Removed getBoundingClientRect() — forced reflow on every event
    };
  }

  function getScrollPercent() {
    var docH = Math.max(
      document.body.scrollHeight || 0, document.documentElement.scrollHeight || 0,
      document.body.offsetHeight || 0, document.documentElement.offsetHeight || 0
    );
    var scrollTop = window.pageYOffset || document.documentElement.scrollTop || 0;
    var vpH = window.innerHeight;
    return docH > 0 ? Math.min(100, Math.round((scrollTop + vpH) / docH * 100)) : 0;
  }

  // === DOM Snapshot (sanitized — no form values, no hidden inputs) ===
  function captureDOM() {
    try {
      var clone = document.documentElement.cloneNode(true);
      // Remove scripts, tracking pixels, iframes
      var remove = clone.querySelectorAll('script,noscript,iframe,link[rel="preconnect"],meta[name="facebook"]');
      for (var i = 0; i < remove.length; i++) remove[i].parentNode.removeChild(remove[i]);
      // Clear form values (PII protection)
      var inputs = clone.querySelectorAll('input,textarea,select');
      for (var j = 0; j < inputs.length; j++) {
        inputs[j].removeAttribute('value');
        inputs[j].textContent = '';
      }
      var styles = [];
      var links = document.querySelectorAll('link[rel="stylesheet"]');
      for (var k = 0; k < links.length; k++) styles.push(links[k].href);

      var html = clone.outerHTML;
      // Cap at 200KB to fit within sendBeacon limits when combined with events
      domSnapshot = {
        html: html.substring(0, 200000),
        stylesheets: styles,
        docWidth: document.documentElement.scrollWidth,
        docHeight: document.documentElement.scrollHeight
      };
    } catch(e) {
      domSnapshot = { error: e.message };
    }
  }

  // === Event Recording ===
  function pushEvent(type, data) {
    var evt = { t: ts(), type: type };
    if (data) {
      var keys = Object.keys(data);
      for (var i = 0; i < keys.length; i++) evt[keys[i]] = data[keys[i]];
    }
    events.push(evt);
    if (events.length >= MAX_BUFFER) flush();
  }

  // --- Clicks ---
  document.addEventListener('click', function(e) {
    totalClicks++;
    var x = e.pageX, y = e.pageY, el = e.target;

    pushEvent('click', { x: x, y: y, vx: e.clientX, vy: e.clientY, el: getElementMeta(el) });

    // Rage click detection
    var now = Date.now();
    rageClickBuffer.push({ x: x, y: y, t: now });
    // Keep only recent clicks
    var filtered = [];
    for (var i = 0; i < rageClickBuffer.length; i++) {
      if (now - rageClickBuffer[i].t < 1500) filtered.push(rageClickBuffer[i]);
    }
    rageClickBuffer = filtered;

    if (rageClickBuffer.length >= 3) {
      var inArea = true;
      for (var j = 0; j < rageClickBuffer.length; j++) {
        if (Math.abs(rageClickBuffer[j].x - x) > 50 || Math.abs(rageClickBuffer[j].y - y) > 50) {
          inArea = false; break;
        }
      }
      if (inArea) {
        pushEvent('rage_click', { x: x, y: y, count: rageClickBuffer.length, el: getElementMeta(el) });
        rageClickBuffer = [];
      }
    }
  }, true);

  // --- Mouse movement ---
  var lastMoveTime = 0;
  document.addEventListener('mousemove', function(e) {
    var now = Date.now();
    if (now - lastMoveTime < MOVE_THROTTLE) return;
    lastMoveTime = now;
    pushEvent('move', { x: e.pageX, y: e.pageY });
  }, { passive: true });

  // --- Touch movement ---
  document.addEventListener('touchmove', function(e) {
    var now = Date.now();
    if (now - lastMoveTime < MOVE_THROTTLE) return;
    lastMoveTime = now;
    var touch = e.touches[0];
    if (touch) pushEvent('move', { x: Math.round(touch.pageX), y: Math.round(touch.pageY) });
  }, { passive: true });

  // --- Scroll ---
  var lastScrollTime = 0;
  var scrollDebounce;
  window.addEventListener('scroll', function() {
    var now = Date.now();
    var pct = getScrollPercent();
    if (pct > maxScrollDepth) maxScrollDepth = pct;

    if (now - lastScrollTime < SCROLL_THROTTLE) {
      clearTimeout(scrollDebounce);
      scrollDebounce = setTimeout(function() {
        pushEvent('scroll', { y: window.pageYOffset || 0, pct: getScrollPercent() });
      }, SCROLL_THROTTLE);
      return;
    }
    lastScrollTime = now;
    pushEvent('scroll', { y: window.pageYOffset || 0, pct: pct });
  }, { passive: true });

  // --- Section visibility (capped at MAX_SECTIONS) ---
  function trackSections() {
    if (!window.IntersectionObserver) return;

    // Prefer explicit hp-sections, then section tags, then forms — cap total
    var candidates = document.querySelectorAll('[data-hp-section],.hp-section,section,form');
    var targets = [];
    for (var i = 0; i < candidates.length && targets.length < MAX_SECTIONS; i++) {
      targets.push(candidates[i]);
    }
    if (!targets.length) return;

    var observer = new IntersectionObserver(function(entries) {
      for (var i = 0; i < entries.length; i++) {
        var entry = entries[i];
        var el = entry.target;
        var id = el.getAttribute('data-hp-section') || el.id || getSelector(el);
        if (entry.isIntersecting) {
          el._dpEnter = Date.now();
          pushEvent('section_enter', { section: id });
        } else if (el._dpEnter) {
          pushEvent('section_leave', { section: id, duration: Date.now() - el._dpEnter });
          el._dpEnter = null;
        }
      }
    }, { threshold: 0.3 });

    for (var j = 0; j < targets.length; j++) observer.observe(targets[j]);
  }

  // --- Form tracking ---
  document.addEventListener('focus', function(e) {
    var el = e.target;
    if (!(el.name || el.id)) return;
    if (el.tagName !== 'INPUT' && el.tagName !== 'SELECT' && el.tagName !== 'TEXTAREA') return;
    var fid = el.name || el.id;
    if (!formInteractions[fid]) formInteractions[fid] = { focuses: 0, changes: 0 };
    formInteractions[fid].focuses++;
    pushEvent('form_focus', { field: fid, fieldType: el.type || el.tagName.toLowerCase(), el: getElementMeta(el) });
  }, true);

  document.addEventListener('blur', function(e) {
    var el = e.target;
    if (!(el.name || el.id)) return;
    if (el.tagName !== 'INPUT' && el.tagName !== 'SELECT' && el.tagName !== 'TEXTAREA') return;
    var fid = el.name || el.id;
    var filled = (el.type === 'checkbox' || el.type === 'radio') ? el.checked : !!el.value;
    pushEvent('form_blur', { field: fid, filled: filled });
  }, true);

  document.addEventListener('change', function(e) {
    var el = e.target;
    var fid = el.name || el.id;
    if (fid && formInteractions[fid]) formInteractions[fid].changes++;
  }, true);

  document.addEventListener('submit', function(e) {
    var form = e.target;
    pushEvent('form_submit', { formId: form.id || form.name || getSelector(form), fields: Object.keys(formInteractions).length });
    flush();
  }, true);

  // === Data Transmission ===
  function buildPayload(evts) {
    return {
      sid: sanitizeSid(sessionId),
      url: pageUrl,
      title: pageTitle,
      ref: referrer,
      ua: navigator.userAgent,
      lang: navigator.language,
      screen: { w: screen.width, h: screen.height },
      viewport: getViewport(),
      device: getDeviceType(),
      ts: startTime,
      duration: ts(),
      maxScroll: maxScrollDepth,
      totalClicks: totalClicks,
      events: evts,
      forms: Object.keys(formInteractions).length > 0 ? formInteractions : undefined
    };
  }

  function send(payload) {
    var data = JSON.stringify(payload);
    var byteLen = data.length; // approximate, close enough for ASCII-heavy JSON

    if (navigator.sendBeacon && byteLen < BEACON_LIMIT) {
      var ok = navigator.sendBeacon(ENDPOINT, new Blob([data], { type: 'application/json' }));
      if (!ok) sendXHR(data);
    } else {
      sendXHR(data);
    }
  }

  function sendXHR(data) {
    try {
      var xhr = new XMLHttpRequest();
      xhr.open('POST', ENDPOINT, true);
      xhr.setRequestHeader('Content-Type', 'application/json');
      xhr.onload = function() {
        if (xhr.status >= 400) {
          // Store failed events for retry on next flush
          try {
            var parsed = JSON.parse(data);
            if (parsed.events) pendingRetry = pendingRetry.concat(parsed.events);
          } catch(e) {}
        }
      };
      xhr.onerror = function() {
        try {
          var parsed = JSON.parse(data);
          if (parsed.events) pendingRetry = pendingRetry.concat(parsed.events);
        } catch(e) {}
      };
      xhr.send(data);
    } catch(e) {}
  }

  function flush() {
    // Combine pending retry + current events
    var toSend = pendingRetry.concat(events.splice(0));
    pendingRetry = [];

    if (toSend.length === 0 && !domSnapshot) return;

    var payload = buildPayload(toSend);

    // Include DOM in the payload directly (server handles it)
    if (domSnapshot) {
      payload.dom = domSnapshot;
      domSnapshot = null;
    }

    // Always use XHR when DOM is included (can be large)
    var data = JSON.stringify(payload);
    if (payload.dom || data.length > BEACON_LIMIT) {
      sendXHR(data);
    } else {
      send(payload);
    }
  }

  // === Lifecycle ===
  flushTimer = setInterval(flush, FLUSH_INTERVAL);

  window.addEventListener('beforeunload', function() {
    if (sessionEnded) return;
    sessionEnded = true;
    clearInterval(flushTimer);
    pushEvent('session_end', { duration: ts(), maxScroll: maxScrollDepth, totalClicks: totalClicks });
    flush();
  });

  document.addEventListener('visibilitychange', function() {
    if (document.visibilityState === 'hidden') flush();
  });

  // === Init ===
  function init() {
    captureDOM();
    trackSections();
    pushEvent('session_start', { url: pageUrl, ref: referrer, device: getDeviceType() });
    setTimeout(flush, 2000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // === Public API ===
  window.DepositoPomogator = {
    track: function(name, data) { pushEvent('custom', { name: name, data: data }); },
    flush: flush,
    sessionId: sessionId
  };

})();
