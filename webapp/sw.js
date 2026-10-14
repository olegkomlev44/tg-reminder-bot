// ════════════════════════════════════════════════════════════════════
//  Music App — Service Worker  (Cache-First Architecture)
//  Версия кэша: при изменении SW всегда меняй VERSION
// ════════════════════════════════════════════════════════════════════
const VERSION   = 'v1.4.0';
const SHELL     = `shell-${VERSION}`;   // App Shell — статика
const API_CACHE = `api-${VERSION}`;     // API-ответы (поиск, треки, волна, тексты песен)
const AUD_CACHE = `audio-${VERSION}`;   // Аудиопотоки (авто-кэш при прослушивании, LRU)
const OFFLINE_CACHE = `offline-${VERSION}`; // Явно сохранённые пользователем треки

const SHELL_FILES = [
    '/',
    '/index.html',
];

const API_MAX     = 80;
const AUD_MAX     = 15;
const AUD_TTL     = 7 * 24 * 3600 * 1000;  // 7 дней
const OFFLINE_MAX = 40;

// ── INSTALL ──────────────────────────────────────────────────────────
self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(SHELL).then(cache => cache.addAll(SHELL_FILES))
            .then(() => self.skipWaiting())
    );
});

// ── ACTIVATE — удаляем старые кэши (сохраняя OFFLINE_CACHE при минорных апдейтах) ──
self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys().then(keys =>
            Promise.all(
                keys.filter(k => ![SHELL, API_CACHE, AUD_CACHE, OFFLINE_CACHE].includes(k) && !k.startsWith('offline-'))
                    .map(k => caches.delete(k))
            )
        ).then(() => self.clients.claim())
    );
});

// ── FETCH — стратегия по типу запроса ────────────────────────────────
self.addEventListener('fetch', event => {
    const url = new URL(event.request.url);

    if (event.request.method !== 'GET') return;

    // 1. Аудиопоток /api/stream/...
    if (url.pathname.startsWith('/api/stream/')) {
        event.respondWith(audioStrategy(event.request));
        return;
    }

    // 2. API-ответы (/api/search, /api/wave, /api/tracks, /api/lyrics)
    if (url.pathname.startsWith('/api/') && !url.pathname.startsWith('/api/stream/')) {
        event.respondWith(apiStrategy(event.request));
        return;
    }

    // 3. Обложки и внешние картинки
    if (isCoverImage(url)) {
        event.respondWith(coverStrategy(event.request));
        return;
    }

    // 4. App Shell / index.html
    if (url.pathname === '/' || url.pathname === '/index.html') {
        event.respondWith(shellStrategy(event.request));
        return;
    }
});

// ════════════════════════════════════════════════════════════════════
//  СТРАТЕГИИ
// ════════════════════════════════════════════════════════════════════

async function audioStrategy(request) {
    const offlineCache = await caches.open(OFFLINE_CACHE);
    const offlineHit = await offlineCache.match(request, { ignoreSearch: true });
    if (offlineHit) return offlineHit;

    const cache = await caches.open(AUD_CACHE);
    const cached = await cache.match(request, { ignoreSearch: true });

    if (cached) {
        const dateHeader = cached.headers.get('sw-cached-at');
        if (dateHeader) {
            const age = Date.now() - parseInt(dateHeader, 10);
            if (age < AUD_TTL) return cached;
            await cache.delete(request);
        } else {
            return cached;
        }
    }

    try {
        const response = await fetch(request.clone());
        if (response.ok && response.status === 200) {
            const cloned = response.clone();
            (async () => {
                try {
                    const headers = new Headers(cloned.headers);
                    headers.set('sw-cached-at', String(Date.now()));
                    const body = await cloned.arrayBuffer();
                    await cache.put(request, new Response(body, { status: 200, headers }));
                    await trimCache(AUD_CACHE, AUD_MAX);
                } catch (_) {}
            })();
        }
        return response;
    } catch (_) {
        return new Response(JSON.stringify({ error: 'offline', cached: false }), {
            status: 503,
            headers: { 'Content-Type': 'application/json' },
        });
    }
}

async function apiStrategy(request) {
    const cache = await caches.open(API_CACHE);
    try {
        const response = await fetch(request.clone());
        if (response.ok) {
            const url = new URL(request.url);
            if (
                url.pathname === '/api/search' ||
                url.pathname === '/api/wave' ||
                url.pathname === '/api/tracks' ||
                url.pathname === '/api/lyrics' ||
                url.pathname.startsWith('/api/track/')
            ) {
                await cache.put(request, response.clone());
                await trimCache(API_CACHE, API_MAX);
            }
        }
        return response;
    } catch (_) {
        const cached = await cache.match(request);
        if (cached) return cached;
        return new Response(JSON.stringify({ error: 'offline', cached: false }), {
            status: 503,
            headers: { 'Content-Type': 'application/json' },
        });
    }
}

async function coverStrategy(request) {
    const cache = await caches.open(SHELL);
    const cached = await cache.match(request);
    if (cached) return cached;
    try {
        const response = await fetch(request.clone());
        if (response.ok) await cache.put(request, response.clone());
        return response;
    } catch (_) {
        return new Response('', { status: 404 });
    }
}

async function shellStrategy(request) {
    const cache = await caches.open(SHELL);
    const cached = await cache.match(request);
    const networkFetch = fetch(request.clone()).then(resp => {
        if (resp.ok) cache.put(request, resp.clone());
        return resp;
    }).catch(() => null);
    return cached || networkFetch;
}

// ════════════════════════════════════════════════════════════════════
//  УТИЛИТЫ
// ════════════════════════════════════════════════════════════════════

function isCoverImage(url) {
    return (
        url.hostname.includes('sndcdn.com') ||
        url.hostname.includes('lastfm') ||
        url.hostname.includes('discogs') ||
        url.hostname.includes('ytimg.com') ||
        url.pathname.match(/\.(jpg|jpeg|png|webp|gif)$/i)
    );
}

async function trimCache(cacheName, maxEntries) {
    const cache = await caches.open(cacheName);
    const keys = await cache.keys();
    if (keys.length > maxEntries) {
        const toDelete = keys.slice(0, keys.length - maxEntries);
        await Promise.all(toDelete.map(k => cache.delete(k)));
    }
}

// ── Слушаем сообщения от клиента ──────────────────────────────────────
self.addEventListener('message', event => {
    const { type, payload } = event.data || {};

    if (type === 'PRECACHE_AUDIO') {
        const { trackId } = payload || {};
        if (!trackId) return;
        const url = `/api/stream/${encodeURIComponent(trackId)}`;
        caches.open(AUD_CACHE).then(async cache => {
            const existing = await cache.match(url);
            if (existing) return;
            try {
                const resp = await fetch(url);
                if (resp.ok && resp.status === 200) {
                    const headers = new Headers(resp.headers);
                    headers.set('sw-cached-at', String(Date.now()));
                    const body = await resp.arrayBuffer();
                    await cache.put(url, new Response(body, { status: 200, headers }));
                    await trimCache(AUD_CACHE, AUD_MAX);
                    event.source?.postMessage({ type: 'AUDIO_CACHED', trackId });
                }
            } catch (_) {}
        });
    }

    if (type === 'CLEAR_CACHE') {
        caches.keys().then(keys => Promise.all(
            keys.filter(k => k !== OFFLINE_CACHE).map(k => caches.delete(k))
        )).then(() => event.source?.postMessage({ type: 'CACHE_CLEARED' }));
    }

    // ── Явное офлайн-сохранение трека с прогрессом скачивания (OFFLINE_PROGRESS) ──
    if (type === 'SAVE_OFFLINE') {
        const { trackId } = payload || {};
        if (!trackId) return;
        const url = `/api/stream/${encodeURIComponent(trackId)}`;
        (async () => {
            try {
                const cache = await caches.open(OFFLINE_CACHE);
                const existing = await cache.match(url);
                if (existing) {
                    event.source?.postMessage({ type: 'OFFLINE_SAVED', trackId });
                    return;
                }
                const keys = await cache.keys();
                if (keys.length >= OFFLINE_MAX) {
                    event.source?.postMessage({ type: 'OFFLINE_SAVE_FAILED', trackId, reason: 'limit' });
                    return;
                }
                const resp = await fetch(url);
                if (!resp.ok || resp.status !== 200) {
                    event.source?.postMessage({ type: 'OFFLINE_SAVE_FAILED', trackId, reason: 'network' });
                    return;
                }

                const contentLength = parseInt(resp.headers.get('Content-Length') || '0', 10);
                let bodyBuffer;

                if (resp.body && typeof resp.body.getReader === 'function') {
                    const reader = resp.body.getReader();
                    const chunks = [];
                    let received = 0;
                    let lastSentPct = -1;
                    while (true) {
                        const { done, value } = await reader.read();
                        if (done) break;
                        if (value) {
                            chunks.push(value);
                            received += value.byteLength;
                            const pct = contentLength > 0
                                ? Math.min(99, Math.round((received / contentLength) * 100))
                                : Math.min(95, Math.round((received / (4 * 1024 * 1024)) * 100));
                            if (pct - lastSentPct >= 5) {
                                lastSentPct = pct;
                                event.source?.postMessage({ type: 'OFFLINE_PROGRESS', trackId, pct, bytes: received });
                            }
                        }
                    }
                    const merged = new Uint8Array(received);
                    let offset = 0;
                    for (const ch of chunks) {
                        merged.set(ch, offset);
                        offset += ch.byteLength;
                    }
                    bodyBuffer = merged.buffer;
                } else {
                    bodyBuffer = await resp.arrayBuffer();
                }

                if (bodyBuffer.byteLength < 15000) {
                    event.source?.postMessage({ type: 'OFFLINE_SAVE_FAILED', trackId, reason: 'empty' });
                    return;
                }
                await cache.put(url, new Response(bodyBuffer, { status: 200, headers: resp.headers }));
                event.source?.postMessage({ type: 'OFFLINE_SAVED', trackId });
            } catch (_) {
                event.source?.postMessage({ type: 'OFFLINE_SAVE_FAILED', trackId, reason: 'error' });
            }
        })();
    }

    if (type === 'REMOVE_OFFLINE') {
        const { trackId } = payload || {};
        if (!trackId) return;
        const url = `/api/stream/${encodeURIComponent(trackId)}`;
        caches.open(OFFLINE_CACHE)
            .then(cache => cache.delete(url))
            .then(() => event.source?.postMessage({ type: 'OFFLINE_REMOVED', trackId }));
    }

    if (type === 'LIST_OFFLINE') {
        caches.open(OFFLINE_CACHE)
            .then(cache => cache.keys())
            .then(requests => requests.map(r => decodeURIComponent(r.url.split('/api/stream/')[1] || '')))
            .then(ids => event.source?.postMessage({ type: 'OFFLINE_LIST_RESULT', ids }));
    }

    if (type === 'CACHE_STATUS') {
        Promise.all([
            caches.open(SHELL).then(c => c.keys()).then(k => k.length),
            caches.open(API_CACHE).then(c => c.keys()).then(k => k.length),
            caches.open(AUD_CACHE).then(c => c.keys()).then(k => k.length),
            caches.open(OFFLINE_CACHE).then(c => c.keys()).then(k => k.length),
        ]).then(([shell, api, audio, offline]) => {
            event.source?.postMessage({ type: 'CACHE_STATUS_RESULT', shell, api, audio, offline });
        });
    }
});
