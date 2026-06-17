self.addEventListener('install', event => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('push', event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = { title: 'ProcureFlow Alert', body: event.data ? event.data.text() : 'New procurement notification' }; }
  const title = data.title || 'ProcureFlow Alert';
  const options = { body: data.body || data.message || 'New procurement notification', icon: data.icon || undefined, data: data.url || '/' };
  event.waitUntil(self.registration.showNotification(title, options));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = event.notification.data || '/';
  event.waitUntil(clients.openWindow(url));
});
