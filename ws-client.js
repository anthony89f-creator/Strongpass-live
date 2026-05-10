// ws-client.js — polls /state.json every 500ms
(function() {
  let lastJson = null;

  async function poll() {
    try {
      const res = await fetch('/state.json?t=' + Date.now(), {cache: 'no-store'});
      if (!res.ok) return;
      const text = await res.text();
      if (text !== lastJson) {
        lastJson = text;
        try {
          const data = JSON.parse(text);
          if (typeof window.onStateUpdate === 'function') {
            window.onStateUpdate(data);
          }
        } catch(e) {}
      }
    } catch(e) {}
  }

  poll();
  setInterval(poll, 500);
})();
