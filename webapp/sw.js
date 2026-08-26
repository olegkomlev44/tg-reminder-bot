// ════════════════════════════════════════════════════════════════════
//  Music App — Service Worker  (Cache-First Architecture)
//  Версия кэша: при изменении SW всегда меняй VERSION
// ════════════════════════════════════════════════════════════════════
const VERSION   = 'v1.0.0';
const SHELL     = `shell-${VERSION}`;   // App Shell — статика
const API_CACHE = `api-${VERSION}`;     // API-ответы (поиск, треки, волна)
const AUD_CACHE = `audio-${VERSION}`;   // Аудиопотоки (авто-кэш при прослушивании, LRU)
const OFFLINE_CACHE = `offline-${VERSION}`; // Явно сохранённые пользователем треки — не вытесняются автоматической LRU-очисткой AUD_CACHE

// App Shell — файлы, которые кэшируются при установке
const SHELL_FILES = [
    '/',
    '/index.html',
];

// Лимиты кэшей
const API_MAX   = 60;    // максимум API-записей
const AUD_MAX   = 15;    // максимум аудиофайлов (~15 × ~5MB = ~75MB)
const AUD_TTL   = 7 * 24 * 3600 * 1000;  // 7 дней
const OFFLINE_MAX = 40;  // явных офлайн-сохранений (~40 × ~5MB = ~200MB) — при превышении новое сохранение отклоняется, а не тихо вытесняет старое

// ── INSTALL ──────────────────────────────────────────────────────────
self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(SHELL).then(cache => cache.addAll(SHELL_FILES))
            .then(() => self.skipWaiting())
    );
});

// ── ACTIVATE — удаляем старые кэши ───────────────────────────────────
self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys().then(keys =>
            Promise.all(
                keys.filter(k => ![SHELL, API_CACHE, AUD_CACHE, OFFLINE_CACHE].includes(k))
                    .map(k => caches.delete(k))
            )
        ).then(() => self.clients.claim())
    );
});

// ── FETCH — стратегия по типу запроса ────────────────────────────────
self.addEventListener('fetch', event => {
    const url = new URL(event.request.url);

    // Только GET запросы
    if (event.request.method !== 'GET') return;

    // ── 1. Аудиопоток /api/stream/... ── Stale-While-Revalidate + офлайн
    if (url.pathname.startsWith('/api/stream/')) {
        event.respondWith(audioStrategy(event.request));
        return;
    }

    // ── 2. API-ответы /api/search, /api/wave, /api/tracks ── Network-First с офлайн-кэшем
    if (url.pathname.startsWith('/api/') && !url.pathname.startsWith('/api/stream/')) {
        event.respondWith(apiStrategy(event.request));
        return;
    }

    // ── 3. Обложки и внешние картинки ── Cache-First
    if (isCoverImage(url)) {
        event.respondWith(coverStrategy(event.request));
        return;
    }

    // ── 4. App Shell / index.html ── Cache-First
    if (url.pathname === '/' || url.pathname === '/index.html') {
        event.respondWith(shellStrategy(event.request));
        return;
    }
});

// ════════════════════════════════════════════════════════════════════
//  СТРАТЕГИИ
// ════════════════════════════════════════════════════════════════════

// ── Audio: сперва постоянный офлайн-кэш, потом Cache-First с TTL, фолбек на сеть ──
async function audioStrategy(request) {
    // Явно сохранённый пользователем офлайн-трек — отдаём всегда, без TTL
    // и без похода в сеть (это и есть смысл офлайн-режима).
    const offlineCache = await caches.open(OFFLINE_CACHE);
    const offlineHit = await offlineCache.match(request);
    if (offlineHit) return offlineHit;

    const cache = await caches.open(AUD_CACHE);
    const cached = await cache.match(request);

    if (cached) {
        // Проверяем TTL
        const dateHeader = cached.headers.get('sw-cached-at');
        if (dateHeader) {
            const age = Date.now() - parseInt(dateHeader, 10);
            if (age < AUD_TTL) return cached;
            // TTL истёк — удаляем и идём в сеть
            await cache.delete(request);
        } else {
            return cached;
        }
    }

    try {
        const response = await fetch(request.clone());
        if (response.ok && response.status === 200) {
            // Клонируем и добавляем заголовок времени кэширования
            const cloned = response.clone();
            const headers = new Headers(cloned.headers);
            headers.set('sw-cached-at', String(Date.now()));
            const body = await cloned.arrayBuffer();
            const toCache = new Response(body, { status: 200, headers });
            await cache.put(request, toCache);
            await trimCache(AUD_CACHE, AUD_MAX);
        }
        return response;
    } catch (_) {
        // Офлайн — возвращаем 503 с читаемым телом
        return new Response(JSON.stringify({ error: 'offline', cached: false }), {
            status: 503,
            headers: { 'Content-Type': 'application/json' },
        });
    }
}

// ── API: Network-First с кэш-фолбеком ─────────────────────────────────
async function apiStrategy(request) {
    const cache = await caches.open(API_CACHE);
    try {
        const response = await fetch(request.clone());
        if (response.ok) {
            // Кэшируем только search и wave (не колл-бэки, не истории)
            const url = new URL(request.url);
            if (url.pathname === '/api/search' || url.pathname === '/api/wave' || url.pathname === '/api/tracks') {
                await cache.put(request, response.clone());
                await trimCache(API_CACHE, API_MAX);
            }
        }
        return response;
    } catch (_) {
        // Офлайн — возвращаем кэшированный ответ если есть
        const cached = await cache.match(request);
        if (cached) return cached;
        return new Response(JSON.stringify({ error: 'offline', cached: false }), {
            status: 503,
            headers: { 'Content-Type': 'application/json' },
        });
    }
}

// ── Обложки: Cache-First ──────────────────────────────────────────────
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

// ── App Shell: Cache-First, обновляем в фоне ─────────────────────────
async function shellStrategy(request) {
    const cache = await caches.open(SHELL);
    const cached = await cache.match(request);
    // Обновляем в фоне независимо от результата
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
    // SoundCloud, LastFM, i.scdn.co (Spotify), и т.п.
    return (
        url.hostname.includes('sndcdn.com') ||
        url.hostname.includes('lastfm') ||
        url.hostname.includes('discogs') ||
        url.pathname.match(/\.(jpg|jpeg|png|webp|gif)$/i)
    );
}

async function trimCache(cacheName, maxEntries) {
    const cache = await caches.open(cacheName);
    const keys = await cache.keys();
    if (keys.length > maxEntries) {
        // Удаляем самые старые (первые в списке)
        const toDelete = keys.slice(0, keys.length - maxEntries);
        await Promise.all(toDelete.map(k => cache.delete(k)));
    }
}

// ── Слушаем сообщения от клиента ──────────────────────────────────────
self.addEventListener('message', event => {
    const { type, payload } = event.data || {};

    // Клиент просит прекэшировать аудио конкретного трека
    if (type === 'PRECACHE_AUDIO') {
        const { trackId } = payload || {};
        if (!trackId) return;
        const url = `/api/stream/${encodeURIComponent(trackId)}`;
        caches.open(AUD_CACHE).then(async cache => {
            const existing = await cache.match(url);
            if (existing) return;  // уже есть
            try {
                const resp = await fetch(url);
                if (resp.ok && resp.status === 200) {
                    const headers = new Headers(resp.headers);
                    headers.set('sw-cached-at', String(Date.now()));
                    const body = await resp.arrayBuffer();
                    await cache.put(url, new Response(body, { status: 200, headers }));
                    await trimCache(AUD_CACHE, AUD_MAX);
                    // Уведомляем клиента
                    event.source?.postMessage({ type: 'AUDIO_CACHED', trackId });
                }
            } catch (_) { /* нет сети — пропускаем */ }
        });
    }

    // Сброс кэша (НЕ трогает OFFLINE_CACHE — это явные сохранения
    // пользователя, "очистить кэш" не должно тихо удалять офлайн-музыку)
    if (type === 'CLEAR_CACHE') {
        caches.keys().then(keys => Promise.all(
            keys.filter(k => k !== OFFLINE_CACHE).map(k => caches.delete(k))
        )).then(() => event.source?.postMessage({ type: 'CACHE_CLEARED' }));
    }

    // ── Явное офлайн-сохранение трека (кнопка "Сохранить офлайн") ──────
    // В отличие от PRECACHE_AUDIO (временный кэш с LRU-вытеснением),
    // здесь трек кладётся в отдельный OFFLINE_CACHE, который не чистится
    // автоматически — только явным удалением или сверх лимита OFFLINE_MAX.
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
                const body = await resp.arrayBuffer();
                await cache.put(url, new Response(body, { status: 200, headers: resp.headers }));
                event.source?.postMessage({ type: 'OFFLINE_SAVED', trackId });
            } catch (_) {
                event.source?.postMessage({ type: 'OFFLINE_SAVE_FAILED', trackId, reason: 'error' });
            }
        })();
    }

    // Удалить трек из офлайн-кэша
    if (type === 'REMOVE_OFFLINE') {
        const { trackId } = payload || {};
        if (!trackId) return;
        const url = `/api/stream/${encodeURIComponent(trackId)}`;
        caches.open(OFFLINE_CACHE)
            .then(cache => cache.delete(url))
            .then(() => event.source?.postMessage({ type: 'OFFLINE_REMOVED', trackId }));
    }

    // Список id треков, реально сохранённых офлайн (для сверки со списком на клиенте)
    if (type === 'LIST_OFFLINE') {
        caches.open(OFFLINE_CACHE)
            .then(cache => cache.keys())
            .then(requests => requests.map(r => decodeURIComponent(r.url.split('/api/stream/')[1] || '')))
            .then(ids => event.source?.postMessage({ type: 'OFFLINE_LIST_RESULT', ids }));
    }

    // Запрос статуса кэша
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
