/* Live refresh (General Comments #6 and #7).
 * - Any element with data-live-url is re-fetched every few seconds and its
 *   contents replaced (admin notifications, transaction history).
 * - The sidebar notification badge polls the unread-count endpoint.
 * Polling is used instead of websockets so it works with plain Flask. */
(function () {
  var script = document.currentScript || document.querySelector('script[data-unread-url]');

  function fetchText(url) {
    return fetch(url, { credentials: 'same-origin', headers: { 'X-Requested-With': 'fetch' } })
      .then(function (r) {
        if (r.redirected) { window.location.href = r.url; return null; }  // session expired
        return r.ok ? r.text() : null;
      });
  }

  document.querySelectorAll('[data-live-url]').forEach(function (el) {
    var url = el.getAttribute('data-live-url');
    var every = parseInt(el.getAttribute('data-live-interval') || '4000', 10);
    var last = null;
    setInterval(function () {
      if (document.hidden) return;
      fetchText(url).then(function (html) {
        if (html !== null && html !== last) {
          if (last !== null) { el.innerHTML = html; }
          last = html;
        }
      }).catch(function () {});
    }, every);
  });

  var badges = document.querySelectorAll('[data-unread-badge]');
  var countUrl = script && script.getAttribute('data-unread-url');
  if (badges.length && countUrl) {
    setInterval(function () {
      if (document.hidden) return;
      fetch(countUrl, { credentials: 'same-origin' }).then(function (r) { return r.json(); })
        .then(function (d) {
          badges.forEach(function (b) { b.textContent = d.count; b.hidden = !d.count; });
        }).catch(function () {});
    }, 5000);
  }
})();
