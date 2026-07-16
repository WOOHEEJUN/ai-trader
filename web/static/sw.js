// PWA 설치용 최소 서비스 워커.
// 매매 데이터는 절대 캐시하지 않는다 — 오래된 잔고/수익률을 보여주면 위험하다.
// 정적 자산만 캐시하고 나머지는 항상 네트워크로 간다.
const CACHE = 'ai-trader-v1';
const ASSETS = ['/static/app.css', '/static/icon.svg', '/static/manifest.json'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return;
  if (!url.pathname.startsWith('/static/')) return;  // 페이지/API는 항상 네트워크
  e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
});
