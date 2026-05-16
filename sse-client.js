// sse-client.js — Server-Sent Events client, drop-in replacement for ws-client.js.
// Contract: calls window.onStateUpdate(data) on every state push.
// Reconnects automatically with exponential backoff (1s → 2s → 4s → 30s max).
// pagehide closes the connection so OBS scene-switching frees the server thread.
(function () {
  'use strict';

  var RETRY_BASE = 1000;
  var RETRY_MAX  = 30000;
  var _retryMs   = RETRY_BASE;
  var _es        = null;
  var _retryTimer = null;

  function _dispatch(data) {
    if (typeof window.onStateUpdate === 'function') {
      try { window.onStateUpdate(data); } catch (e) {}
    }
  }

  // Fetch current state immediately so the overlay renders before the SSE
  // handshake completes. Mirrors ws-client.js behaviour on page load.
  fetch('/state.json?t=' + Date.now(), {cache: 'no-store'})
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) { if (d) _dispatch(d); })
    .catch(function () {});

  function connect() {
    if (_es) { _es.close(); _es = null; }
    _es = new EventSource('/stream');

    _es.onopen = function () {
      _retryMs = RETRY_BASE;  // reset backoff on successful connection
    };

    _es.onmessage = function (evt) {
      try { _dispatch(JSON.parse(evt.data)); } catch (e) {}
    };

    _es.onerror = function () {
      _es.close();
      _es = null;
      _retryTimer = setTimeout(connect, _retryMs);
      _retryMs = Math.min(_retryMs * 2, RETRY_MAX);
    };
  }

  function disconnect() {
    if (_retryTimer) { clearTimeout(_retryTimer); _retryTimer = null; }
    if (_es) { _es.close(); _es = null; }
  }

  function reconnectAndSync() {
    if (!_es) connect();
    fetch('/state.json?t=' + Date.now(), {cache: 'no-store'})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) _dispatch(d); })
      .catch(function () {});
  }

  // Release the server-side gthread when this page is hidden (OBS scene switch,
  // browser tab switch, or page navigation). Reconnects and syncs missed state
  // on pageshow (bfcache restore) or visibilitychange (OBS scene activation).
  window.addEventListener('pagehide', disconnect);
  window.addEventListener('pageshow', function (e) {
    if (e.persisted) reconnectAndSync();
  });
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) reconnectAndSync();
  });

  connect();
}());
