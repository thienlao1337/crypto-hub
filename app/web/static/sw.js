/* Service worker веб-пуша.
 *
 * Отдаётся с корня сайта (/sw.js), а не из /static/: область действия
 * service worker'а ограничена каталогом, из которого он загружен, —
 * из /static/ он не смог бы открывать страницы панели по клику.
 */

self.addEventListener('push', function (event) {
  var data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (error) {
    // Полезная нагрузка не разобралась — покажем хоть что-то, вместо
    // того чтобы промолчать.
    data = { title: 'Crypto Hub', body: 'Есть новое уведомление.' };
  }

  var title = data.title || 'Crypto Hub';
  var options = {
    body: data.body || '',
    // tag склеивает повторные уведомления об одном событии в одно:
    // иначе пуш и повторная отправка легли бы двумя карточками.
    tag: data.tag || undefined,
    data: { url: data.url || '/notifications' },
    // Иконка обязательна для Android, иначе система рисует свою.
    icon: '/static/img/icon-192.png',
    badge: '/static/img/badge.png'
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function (event) {
  event.notification.close();
  var target = (event.notification.data && event.notification.data.url) || '/notifications';

  // Если панель уже открыта, переиспользуем вкладку, а не плодим новые.
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
