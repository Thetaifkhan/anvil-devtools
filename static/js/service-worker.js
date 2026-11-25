// Minimal offline support: cache the app shell (CSS/JS) and any tool pages
// the user has already visited, so purely client-side tools keep working
// without a network connection. Server-backed tools (XML/YAML/SQL/CSV
// conversion) still require connectivity.
const CACHE_NAME = "anvil-cache-v8";
const APP_SHELL = [
  "/",
  "/static/css/main.css",
  "/static/js/main.js",
  "/static/js/md5.js",
  "/static/js/command-palette.js",
  "/static/js/terminal-bg.js",
  "/static/js/workbench-drafts.js",
  "/static/js/workbench.js",
  "/static/js/tools/api_tool.js",
  "/static/js/tools/regex_worker.js",
  // PWA shell — manifest, icons, and the workhorse JS files for tools that
  // users are likely to revisit offline. Without these in the precache list
  // a cold offline launch can't paint the install button or show favicons.
  "/manifest.webmanifest",
  "/static/icons/favicon.svg",
  "/static/icons/icon-192.svg",
  "/static/icons/icon-512.svg",
  "/static/js/tools/formatter.js",
  "/static/js/tools/json_formatter.js",
  "/static/js/tools/json_validator.js",
  "/static/js/tools/json_diff.js",
  "/static/js/tools/json_tree_viewer.js",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  // Never cache API calls -- they're processed fresh server-side each time.
  if (event.request.url.includes("/api/")) return;

  event.respondWith(
    caches.match(event.request).then((cached) => {
      const networkFetch = fetch(event.request)
        .then((response) => {
          if (response.ok) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
          }
          return response;
        })
        .catch(() => cached);
      return cached || networkFetch;
    })
  );
});
