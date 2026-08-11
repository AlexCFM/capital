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
  // Only handle same-origin requests (the app's own HTML/CSS/JS/images) —
  // leave cross-origin ones (iTunes artwork search, the metadata worker,
  // the icecast stream) completely alone. iOS Safari's service worker
  // implementation can fail cross-origin fetches re-issued from inside a
  // fetch handler ("FetchEvent.respondWith received an error: Load
  // failed"), which broke artwork lookups and could just as easily have
  // been interrupting the stream itself.
  var url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;
  event.respondWith(fetch(event.request, { cache: 'no-store' }));
});
