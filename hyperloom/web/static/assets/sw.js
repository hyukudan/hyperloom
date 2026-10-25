/* Hyperloom installable shell worker (F50), served at /sw.js with scope "/".
   It answers only GET requests for the shell's static scripts, styles and icons: network first, the cached copy
   only when the network fails. Navigations (the per-process CSRF page), /hyperloom/api/*, this file and every
   other request are not intercepted at all, so they always go to the network and never enter a cache.
   When the portal stops serving this file (404/410), the worker unregisters itself and deletes its caches. */
"use strict";
const CACHE = "hyperloom-shell-v1";
const PREFIX = "hyperloom-shell-";
const STATIC = /^(?:\/hyperloom)?\/(?:app\.js|app\.css|recipes\.js|pages\/[a-z0-9][a-z0-9_-]*(?:\.[a-z0-9_-]+)*\.(?:js|css)|assets\/[a-z0-9][a-z0-9_-]*(?:\.[a-z0-9_-]+)*\.(?:svg|png|webmanifest))$/;
const CHECK_EVERY_MS = 10 * 60 * 1000;
let checkedAt = 0;
let retired = false;        // set once this worker is being removed: it stops writing to its cache

async function dropCaches(keep) {
  for (const name of await caches.keys()) if (name.startsWith(PREFIX) && name !== keep) await caches.delete(name);
}

async function checkSelf() {
  if (Date.now() - checkedAt < CHECK_EVERY_MS) return;
  checkedAt = Date.now();
  try {
    const response = await fetch(self.location.href, {method: "HEAD", cache: "no-store", credentials: "same-origin"});
    if (response.status === 404 || response.status === 410) {
      retired = true;
      await self.registration.unregister();
      await dropCaches(null);
    }
  } catch (_) { /* portal not reachable: decide on a later navigation */ }
}

self.addEventListener("install", () => self.skipWaiting());
// The page removes this worker when the portal no longer serves it: it says so first, so that requests still in
// flight do not write the cache again after the page deleted it.
self.addEventListener("message", event => {
  if (event.data === "hyperloom-shell-retire") { retired = true; event.waitUntil(dropCaches(null)); }
});
self.addEventListener("activate", event => event.waitUntil((async () => {
  await dropCaches(CACHE);
  await self.clients.claim();
  await checkSelf();
})()));

self.addEventListener("fetch", event => {
  const request = event.request;
  if (request.mode === "navigate") {
    event.waitUntil(checkSelf());       // not answered here: navigations always go to the network
    return;
  }
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || url.search || !STATIC.test(url.pathname)) return;
  event.respondWith((async () => {
    try {
      const response = await fetch(request);
      if (response.ok && response.type === "basic" && !retired) {
        const cache = await caches.open(CACHE);
        await cache.put(url.pathname, response.clone());
      }
      return response;
    } catch (error) {
      const cached = await caches.match(url.pathname, {cacheName: CACHE});     // never creates the cache
      if (cached) return cached;
      throw error;
    }
  })());
});
