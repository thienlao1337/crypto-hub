/* Web push service worker.
 *
 * Served from the site root (/sw.js), not from /static/: a service worker's
 * scope is limited to the directory it was loaded from -
 * from /static/ it couldn't open panel pages on click.
 */

self.addEventListener('push', function (event) {
  var data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (error) {
    // The payload couldn't be parsed - show at least something instead
    // of staying silent.
    data = { title: 'Crypto Hub', body: 'Есть новое уведомление.' };
  }

  var title = data.title || 'Crypto Hub';
  var options = {
    body: data.body || '',
    // tag merges repeated notifications about one event into one:
    // otherwise the push and a resend would show up as two cards.
    tag: data.tag || undefined,
    data: { url: data.url || '/notifications' },
    // The icon is required on Android, otherwise the system draws its own.
    icon: '/static/img/icon-192.png',
    badge: '/static/img/badge.png'
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function (event) {
  event.notification.close();
  var target = (event.notification.data && event.notification.data.url) || '/notifications';

  // If the panel is already open, reuse the tab instead of opening new ones.
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (list) {
      for (var i = 0; i < list.length; i += 1) {
        var client = list[i];
        if ('focus' in client) {
          client.navigate(target);
          return client.focus();
        }
      }
      if (self.clients.openWindow) {
        return self.clients.openWindow(target);
      }
      return undefined;
    })
  );
});
