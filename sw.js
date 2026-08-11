// Always serves fresh content from the network — no offline caching.
//
// Needed because iOS treats a Home Screen web app very differently from a
// regular Safari tab: without a service worker in the picture, it can keep
// running an old cached copy of index.html indefinitely and never notices
// new deployments. A service worker's own update-check lifecycle is what
// actually gets iOS to look for and activate a new version.
self.addEventListener('install', function(event){
  self.skipWaiting();
});

self.addEventListener('activate', function(event){
  event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', function(event){
  event.respondWith(fetch(event.request, { cache: 'no-store' }));
});
