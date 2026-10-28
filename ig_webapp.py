"""
Telegram Mini App «Лента»: недавние посты подписок, закладки и подписки сеткой с превью.

Как это устроено:
  • превью берутся из Telegram (file_id уже отправленных ботом постов) и отдаются через /thumb —
    CDN Instagram не нужен, ссылки не протухают;
  • каждый запрос из аппы проверяется по initData (HMAC с токеном бота) — человек видит только своё;
  • тап по плитке присылает пост в чат бота (из кэша file_id, без запросов к Instagram).

Подключение:
  A) Свой маленький сервер рядом с ботом (если web_server.py не трогаем):
        IG_WEBAPP_STANDALONE=1, IG_WEBAPP_PORT=8081, IG_WEBAPP_URL=https://<домен>/ig-app
     (домен/порт должны смотреть наружу по https — проверь, как BotHost пробрасывает порты)
  B) Добавить маршруты в уже работающий aiohttp-сервер (web_server.py):
        from ig_webapp import setup_ig_webapp_routes
        setup_ig_webapp_routes(app)            # app — ваш web.Application
     и задать IG_WEBAPP_URL=https://<домен>/ig-app

В main.py сразу после создания бота:  set_webapp_bot(bot); await start_ig_webapp_standalone()
"""
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
from collections import OrderedDict
from urllib.parse import parse_qsl

from aiohttp import web

from instagram_viewer import IG_DB, ig_bm_list, ig_cache_get, ig_get_subscriptions

logger = logging.getLogger(__name__)

PREFIX = os.getenv("IG_WEBAPP_PREFIX", "/ig-app").rstrip("/") or "/ig-app"
INITDATA_MAX_AGE = 24 * 3600
_BOT = None
_THUMBS: "OrderedDict[str, bytes]" = OrderedDict()
_THUMBS_MAX = 300
_LAST_OPEN: dict[int, float] = {}


def set_webapp_bot(bot) -> None:
    global _BOT
    _BOT = bot


# ── авторизация ──────────────────────────────────────────────────────────────

def verify_init_data(init_data: str, token: str, max_age: int = INITDATA_MAX_AGE) -> dict | None:
    """Проверка Telegram WebApp initData. Возвращает user-dict или None."""
    if not init_data or not token:
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        got = parsed.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not got or not hmac.compare_digest(calc, got):
            return None
        if time.time() - int(parsed.get("auth_date", "0")) > max_age:
            return None
        user = json.loads(parsed.get("user", "{}"))
        return user if user.get("id") else None
    except Exception:
        return None


def _thumb_token(uid: int, sc: str) -> str:
    key = (_BOT.token if _BOT else "").encode()
    return hmac.new(key, f"thumb:{uid}:{sc}".encode(), hashlib.sha256).hexdigest()[:24]


async def _auth(request: web.Request) -> tuple[int | None, dict]:
    try:
        body = await request.json()
    except Exception:
        return None, {}
    user = verify_init_data(str(body.get("initData", "")), _BOT.token if _BOT else "")
    return (int(user["id"]) if user else None), body


# ── данные ───────────────────────────────────────────────────────────────────

def _thumb_ref(row: dict | None) -> tuple[str, bool]:
    """(file_id превью | '', это видео?) из записи кэша."""
    if not row or not row.get("items"):
        return "", False
    it = row["items"][0]
    is_video = it.get("kind") == "video"
    if it.get("thumb"):
        return it["thumb"], is_video
    return ("" if is_video else it.get("file_id", "")), is_video


def _plain_caption(text: str) -> str:
    import html
    import re
    t = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", t).strip()[:140]


def _item(uid: int, sc: str, user: str, caption: str, row: dict | None) -> dict:
    fid, is_video = _thumb_ref(row)
    return {
        "sc": sc, "user": user, "cap": _plain_caption(caption), "video": is_video,
        "n": len(row["items"]) if row and row.get("items") else 1,
        "thumb": f"/thumb/{sc}?u={uid}&t={_thumb_token(uid, sc)}" if fid else "",
    }


def build_feed(uid: int) -> dict:
    subs = sorted({s["ig_username"] for s in ig_get_subscriptions(str(uid))})

    # лента: недавние кэшированные посты аккаунтов, на которые подписан
    feed = []
    if subs:
        q = ",".join("?" * len(subs))
        conn = sqlite3.connect(IG_DB, timeout=10)
        try:
            rows = conn.execute(
                f"SELECT shortcode, username, caption FROM ig_file_cache WHERE username IN ({q}) "
                f"ORDER BY created DESC LIMIT 90", subs).fetchall()
        finally:
            conn.close()
        for sc, user, cap in rows:
            feed.append(_item(uid, sc, user, cap, ig_cache_get(sc)))

    bms = [_item(uid, b["shortcode"], b["username"], b["caption"], ig_cache_get(b["shortcode"]))
           for b in ig_bm_list(str(uid), 0, 300)]

    latest: dict[str, dict] = {}
    for it in feed:                                  # feed уже отсортирован — первый и есть свежий
        latest.setdefault(it["user"], it)
    sub_items = [{"user": u, "sc": latest[u]["sc"] if u in latest else "",
                  "thumb": latest[u]["thumb"] if u in latest else "",
                  "cap": latest[u]["cap"] if u in latest else ""} for u in subs]
    return {"feed": feed, "bookmarks": bms, "subs": sub_items}


# ── хендлеры ─────────────────────────────────────────────────────────────────

async def api_feed(request: web.Request) -> web.Response:
    uid, _ = await _auth(request)
    if uid is None:
        return web.json_response({"error": "auth"}, status=401)
    return web.json_response(build_feed(uid))


async def api_open(request: web.Request) -> web.Response:
    uid, body = await _auth(request)
    if uid is None:
        return web.json_response({"error": "auth"}, status=401)
    sc = str(body.get("sc", ""))[:40]
    if not sc or not sc.replace("_", "").replace("-", "").isalnum():
        return web.json_response({"error": "bad"}, status=400)
    if time.time() - _LAST_OPEN.get(uid, 0) < 1.0:
        return web.json_response({"error": "slow"}, status=429)
    _LAST_OPEN[uid] = time.time()

    from aiogram.types import InlineKeyboardMarkup
    from instagram_handlers import _act_row, _out_chat, _send_cached
    row = ig_cache_get(sc)
    try:
        if row:
            ar = _act_row(sc, row.get("username", ""), uid) if len(row["items"]) == 1 else []
            kb = InlineKeyboardMarkup(inline_keyboard=[ar]) if ar else None
            await _send_cached(_out_chat(_BOT, uid), row, kb=kb)
        else:
            await _BOT.send_message(uid, f"https://www.instagram.com/p/{sc}/")
    except Exception as e:
        logger.warning(f"webapp open {sc} → {uid}: {e}")
        return web.json_response({"error": "send"}, status=502)
    return web.json_response({"ok": True})


async def thumb(request: web.Request) -> web.Response:
    sc = request.match_info["sc"]
    try:
        uid = int(request.query.get("u", "0"))
    except ValueError:
        return web.Response(status=403)
    if not hmac.compare_digest(_thumb_token(uid, sc), request.query.get("t", "")):
        return web.Response(status=403)
    fid, _ = _thumb_ref(ig_cache_get(sc))
    if not fid:
        return web.Response(status=404)
    data = _THUMBS.get(fid)
    if data is None:
        try:
            f = await _BOT.get_file(fid)
            buf = await _BOT.download_file(f.file_path)
            data = buf.read() if hasattr(buf, "read") else bytes(buf)
        except Exception as e:
            logger.info(f"webapp thumb {sc}: {e}")
            return web.Response(status=404)
        _THUMBS[fid] = data
        while len(_THUMBS) > _THUMBS_MAX:
            _THUMBS.popitem(last=False)
    else:
        _THUMBS.move_to_end(fid)
    return web.Response(body=data, content_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})


async def page(request: web.Request) -> web.Response:
    return web.Response(text=_HTML, content_type="text/html", charset="utf-8",
                        headers={"Cache-Control": "no-cache"})


def setup_ig_webapp_routes(app: web.Application, prefix: str | None = None) -> None:
    p = (prefix or PREFIX).rstrip("/")
    app.router.add_get(p, page)
    app.router.add_get(p + "/", page)
    app.router.add_post(p + "/api/feed", api_feed)
    app.router.add_post(p + "/api/open", api_open)
    app.router.add_get(p + "/thumb/{sc}", thumb)


async def start_ig_webapp_standalone():
    """Поднять отдельный aiohttp-сервер, если IG_WEBAPP_STANDALONE=1. Иначе ничего не делает."""
    if os.getenv("IG_WEBAPP_STANDALONE", "0") != "1":
        return None
    app = web.Application()
    setup_ig_webapp_routes(app)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("IG_WEBAPP_PORT", "8081"))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info(f"🖼 Мини-апп «Лента»: http://0.0.0.0:{port}{PREFIX}")
    return runner


_HTML = r"""<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,user-scalable=no">
<title>Лента</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{--bg:var(--tg-theme-bg-color,#fff);--tx:var(--tg-theme-text-color,#111);--hint:var(--tg-theme-hint-color,#8a8a8e);
--sec:var(--tg-theme-secondary-bg-color,#f1f1f5);--btn:var(--tg-theme-button-color,#2481cc);--btx:var(--tg-theme-button-text-color,#fff);
--ig:linear-gradient(45deg,#feda75,#fa7e1e 28%,#d62976 60%,#962fbf 82%,#4f5bd5);--r:14px}
@media(prefers-color-scheme:dark){:root{--bg:var(--tg-theme-bg-color,#0e0e11);--tx:var(--tg-theme-text-color,#f2f2f5);--sec:var(--tg-theme-secondary-bg-color,#1b1b20)}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.35 -apple-system,system-ui,"SF Pro Text",Segoe UI,Roboto,sans-serif;-webkit-font-smoothing:antialiased}
body{padding-bottom:calc(24px + env(safe-area-inset-bottom,0px))}
button{font:inherit;color:inherit;border:0;background:none;padding:0;cursor:pointer}
.top{position:sticky;top:0;z-index:5;padding:10px 12px 8px;background:var(--bg);background:color-mix(in srgb,var(--bg) 84%,transparent);-webkit-backdrop-filter:blur(16px) saturate(1.6);backdrop-filter:blur(16px) saturate(1.6)}
.hd{display:flex;align-items:center;justify-content:space-between;margin:2px 2px 10px}
.hd h1{margin:0;font-size:26px;font-weight:800;letter-spacing:-.5px;background:var(--ig);-webkit-background-clip:text;background-clip:text;color:transparent}
.rf{width:36px;height:36px;border-radius:50%;background:var(--sec);display:grid;place-items:center;transition:transform .15s}
.rf:active{transform:scale(.88)}.rf svg{width:18px;height:18px;transition:transform .6s}.rf.spin svg{transform:rotate(360deg)}
.seg{position:relative;display:grid;grid-template-columns:repeat(3,1fr);padding:3px;border-radius:13px;background:var(--sec);--idx:0}
.seg i{position:absolute;top:3px;bottom:3px;left:3px;width:calc((100% - 6px)/3);border-radius:10px;background:var(--bg);box-shadow:0 1px 6px rgba(0,0,0,.14);transform:translateX(calc(var(--idx)*100%));transition:transform .28s cubic-bezier(.3,.9,.3,1)}
.tab{position:relative;z-index:1;padding:8px 2px;font-weight:600;font-size:14px;color:var(--hint);transition:color .2s}
.tab.on{color:var(--tx)}.tab b{margin-left:5px;font-size:11px;font-weight:700;padding:1px 6px;border-radius:9px;background:var(--sec);color:var(--hint)}
.tab.on b{background:var(--ig);color:#fff}
.sw{position:relative;margin-top:9px}.sw svg{position:absolute;left:12px;top:50%;width:16px;height:16px;transform:translateY(-50%);color:var(--hint);pointer-events:none}
.q{width:100%;padding:10px 36px 10px 36px;border:0;border-radius:12px;background:var(--sec);color:var(--tx);font:inherit;outline:none;-webkit-appearance:none}
.q::-webkit-search-cancel-button{display:none}.q::placeholder{color:var(--hint)}
.x{position:absolute;right:6px;top:50%;transform:translateY(-50%);width:26px;height:26px;border-radius:50%;color:var(--hint);display:none;place-items:center;font-size:17px}
.sw.has .x{display:grid}
.st{display:flex;gap:14px;overflow-x:auto;padding:12px 14px 6px;scrollbar-width:none}.st::-webkit-scrollbar{display:none}
.s{flex:none;width:64px;text-align:center;transition:transform .15s}.s:active{transform:scale(.92)}
.ring{width:64px;height:64px;border-radius:50%;padding:2.5px;background:var(--ig);transition:transform .25s,box-shadow .25s}
.s.on .ring{transform:scale(1.07);box-shadow:0 0 0 3px var(--bg),0 0 0 5px #d62976}
.ring>div{width:100%;height:100%;border-radius:50%;border:2.5px solid var(--bg);background:var(--sec);overflow:hidden;display:grid;place-items:center;font-weight:700;font-size:21px;color:#fff;position:relative}
.ring img{width:100%;height:100%;object-fit:cover;position:absolute;inset:0}
.s span{display:block;margin-top:5px;font-size:11px;color:var(--hint);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.s.on span{color:var(--tx);font-weight:600}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:4px;padding:8px 8px 0}
.cell{position:relative;aspect-ratio:1;border-radius:12px;overflow:hidden;background:var(--sec);transform:translateZ(0);animation:up .45s both;animation-delay:calc(var(--i,0)*28ms);transition:transform .15s}
.cell:active{transform:scale(.95)}
.cell img{width:100%;height:100%;object-fit:cover;display:block;opacity:0;transition:opacity .4s}.cell img.ld{opacity:1}
.ph{position:absolute;inset:0;display:grid;place-items:center;font-size:26px;color:var(--hint);background:linear-gradient(135deg,var(--sec),color-mix(in srgb,var(--hint) 18%,var(--sec)))}
.cell .u{position:absolute;left:0;right:0;bottom:0;padding:22px 7px 5px;font-size:11px;font-weight:600;color:#fff;background:linear-gradient(transparent,rgba(0,0,0,.66));white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bd{position:absolute;top:6px;right:6px;width:22px;height:22px;border-radius:50%;display:grid;place-items:center;background:rgba(0,0,0,.42);-webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px)}
.bd svg{width:12px;height:12px;fill:#fff}
.cell.busy::after,.cell.ok::after{content:"";position:absolute;inset:0;background:rgba(0,0,0,.45);display:grid;place-items:center}
.cell.busy::before{content:"";position:absolute;z-index:2;left:50%;top:50%;width:26px;height:26px;margin:-13px 0 0 -13px;border-radius:50%;border:3px solid rgba(255,255,255,.35);border-top-color:#fff;animation:sp .7s linear infinite}
.cell.ok::after{content:"✓";color:#fff;font-size:30px;font-weight:700;animation:pop .35s}
.list{padding:6px 12px}
.row{display:flex;align-items:center;gap:13px;padding:10px 12px;margin-bottom:8px;border-radius:var(--r);background:var(--sec);animation:up .45s both;animation-delay:calc(var(--i,0)*35ms);transition:transform .15s}
.row:active{transform:scale(.98)}.row .ring{width:56px;height:56px;flex:none}.row .i{min-width:0;flex:1}
.row .n{font-weight:700}.row .c{margin-top:1px;color:var(--hint);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row .k{flex:none;font-size:12px;font-weight:700;padding:3px 9px;border-radius:10px;background:var(--bg);color:var(--hint)}
.empty{padding:56px 28px;text-align:center;color:var(--hint);animation:up .5s both}
.empty .e{width:84px;height:84px;margin:0 auto 16px;border-radius:50%;background:var(--sec);display:grid;place-items:center;font-size:38px}
.empty h3{margin:0 0 6px;color:var(--tx);font-size:17px}.empty p{margin:0;font-size:14px}
.sk{border-radius:12px;aspect-ratio:1;background:linear-gradient(100deg,var(--sec) 30%,color-mix(in srgb,var(--hint) 22%,var(--sec)) 50%,var(--sec) 70%);background-size:300% 100%;animation:sh 1.3s linear infinite}
.toast{position:fixed;left:50%;bottom:calc(22px + env(safe-area-inset-bottom,0px));transform:translate(-50%,30px);background:rgba(20,20,24,.9);color:#fff;padding:10px 18px;border-radius:22px;font-weight:500;box-shadow:0 8px 24px rgba(0,0,0,.28);opacity:0;transition:.28s;pointer-events:none;z-index:9;max-width:90%;text-align:center}
.toast.show{opacity:1;transform:translate(-50%,0)}
@keyframes up{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
@keyframes sp{to{transform:rotate(360deg)}}@keyframes sh{to{background-position:-300% 0}}
@keyframes pop{from{transform:scale(.4);opacity:0}to{transform:none;opacity:1}}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body>
<div class="top">
  <div class="hd"><h1>Лента</h1>
    <button class="rf" id="rf" aria-label="Обновить"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg></button></div>
  <div class="seg" id="seg"><i></i>
    <button class="tab on" data-t="feed">Лента<b id="c0"></b></button>
    <button class="tab" data-t="bookmarks">Закладки<b id="c1"></b></button>
    <button class="tab" data-t="subs">Подписки<b id="c2"></b></button></div>
  <div class="sw" id="sw"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>
    <input class="q" id="q" type="search" placeholder="Поиск по подписи и нику" autocomplete="off"><button class="x" id="x" aria-label="Очистить">✕</button></div>
</div>
<div id="view"></div>
<div class="toast" id="toast"></div>
<script>
const tg = window.Telegram && Telegram.WebApp;
if (tg) { tg.ready(); tg.expand(); try { tg.setHeaderColor('bg_color'); } catch (e) {} }
const BASE = location.pathname.replace(/\/+$/, '');
const TABS = ['feed', 'bookmarks', 'subs'];
let DATA = null, TAB = 'feed', USER = '';
const $ = id => document.getElementById(id);
const hap = (t) => { try { tg.HapticFeedback.impactOccurred(t || 'light'); } catch (e) {} };
const note = (t) => { try { tg.HapticFeedback.notificationOccurred(t); } catch (e) {} };
const VID = '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>';
const CAR = '<svg viewBox="0 0 24 24"><path d="M4 7h12v12H4zM8 3h12v12h-2V5H8z"/></svg>';

function toast(t) { const e = $('toast'); e.textContent = t; e.classList.add('show'); clearTimeout(toast.t); toast.t = setTimeout(() => e.classList.remove('show'), 1900); }
async function api(path, extra) {
  const r = await fetch(BASE + path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(Object.assign({ initData: tg ? tg.initData : '' }, extra || {})) });
  if (!r.ok) throw new Error(r.status); return r.json();
}
function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
function img(src) { const i = el('img'); i.loading = 'lazy'; i.decoding = 'async'; i.onload = () => i.classList.add('ld'); i.onerror = () => i.remove(); i.src = BASE + src; return i; }
function badge(svg) { const b = el('div', 'bd'); b.innerHTML = svg; return b; }
function ring(user, thumb) {
  const r = el('div', 'ring'), c = el('div');
  const h = [...user].reduce((a, ch) => a + ch.charCodeAt(0), 0) * 37 % 360;
  c.style.background = 'hsl(' + h + ' 55% 48%)'; c.textContent = (user[0] || '?').toUpperCase();
  if (thumb) c.append(img(thumb)); r.append(c); return r;
}
function empty(icon, title, text) {
  const d = el('div', 'empty'), e = el('div', 'e', icon); d.append(e, el('h3', null, title), el('p', null, text)); return d;
}
async function openPost(sc, cell) {
  if (!sc) { toast('Поста пока нет в кэше — открой профиль в боте'); return; }
  hap(); if (cell) cell.classList.add('busy');
  try {
    await api('/api/open', { sc });
    if (cell) { cell.classList.remove('busy'); cell.classList.add('ok'); setTimeout(() => cell.classList.remove('ok'), 900); }
    toast('Отправил в чат ✓'); note('success');
  } catch (e) { if (cell) cell.classList.remove('busy'); toast('Не получилось отправить'); note('error'); }
}
const match = (it, q) => (!q || (it.user + ' ' + (it.cap || '')).toLowerCase().includes(q)) && (!USER || it.user === USER);

function render() {
  const q = $('q').value.trim().toLowerCase(), v = $('view'); v.replaceChildren();
  $('sw').classList.toggle('has', !!q);
  if (!DATA) return;
  $('c0').textContent = DATA.feed.length || ''; $('c1').textContent = DATA.bookmarks.length || ''; $('c2').textContent = DATA.subs.length || '';

  if (TAB === 'subs') {
    const items = DATA.subs.filter(s => !q || s.user.toLowerCase().includes(q));
    if (!items.length) { v.append(empty('👥', DATA.subs.length ? 'Ничего не найдено' : 'Подписок пока нет', DATA.subs.length ? 'Попробуй другой запрос' : 'Добавь аккаунт в боте — он появится здесь')); return; }
    const l = el('div', 'list'); let n = 0;
    for (const s of items) {
      const r = el('div', 'row'); r.style.setProperty('--i', Math.min(n++, 12));
      const i = el('div', 'i'); i.append(el('div', 'n', '@' + s.user), el('div', 'c', s.cap || 'Постов в кэше пока нет'));
      const k = DATA.feed.filter(f => f.user === s.user).length;
      r.append(ring(s.user, s.thumb), i); if (k) r.append(el('div', 'k', k));
      r.onclick = () => { hap(); USER = s.user; setTab('feed'); }; l.append(r);
    }
    v.append(l); return;
  }

  if (TAB === 'feed' && DATA.subs.length) {
    const st = el('div', 'st');
    for (const s of DATA.subs) {
      const b = el('button', 's' + (USER === s.user ? ' on' : ''));
      b.append(ring(s.user, s.thumb), el('span', null, s.user));
      b.onclick = () => { hap(); USER = USER === s.user ? '' : s.user; render(); };
      st.append(b);
    }
    v.append(st);
  }
  const items = DATA[TAB].filter(it => match(it, q));
  if (!items.length) {
    v.append(DATA[TAB].length ? empty('🔍', 'Ничего не найдено', 'Измени запрос или сбрось фильтр')
      : TAB === 'feed' ? empty('🖼', 'Лента пуста', 'Посты появятся, когда бот пришлёт их по твоим подпискам')
      : empty('📑', 'Закладок пока нет', 'Жми «📑 В закладки» под постами в боте'));
    return;
  }
  const g = el('div', 'grid'); let n = 0;
  for (const it of items) {
    const c = el('div', 'cell'); c.style.setProperty('--i', Math.min(n++, 15));
    if (it.thumb) c.append(el('div', 'ph', it.video ? '🎬' : '🖼'), img(it.thumb)); else c.append(el('div', 'ph', it.video ? '🎬' : '🖼'));
    if (it.video) c.append(badge(VID)); else if (it.n > 1) c.append(badge(CAR));
    c.append(el('div', 'u', '@' + it.user));
    c.onclick = () => openPost(it.sc, c); g.append(c);
  }
  v.append(g);
}
function setTab(t) {
  if (t !== TAB && t !== 'feed') USER = '';
  TAB = t; $('seg').style.setProperty('--idx', TABS.indexOf(t));
  document.querySelectorAll('.tab').forEach(x => x.classList.toggle('on', x.dataset.t === t));
  render(); window.scrollTo({ top: 0 });
}
function skeleton() { const g = el('div', 'grid'); for (let i = 0; i < 12; i++) g.append(el('div', 'sk')); $('view').replaceChildren(g); }
async function load(manual) {
  if (!tg || !tg.initData) { $('view').replaceChildren(empty('✈️', 'Открой из Telegram', 'Запусти ленту командой /lenta в боте')); return; }
  if (!DATA || manual) skeleton();
  $('rf').classList.add('spin');
  try { DATA = await api('/api/feed'); render(); if (manual) toast('Обновлено'); }
  catch (e) { $('view').replaceChildren(empty('😕', 'Не удалось загрузить', 'Закрой и открой ленту ещё раз')); }
  setTimeout(() => $('rf').classList.remove('spin'), 600);
}
document.querySelectorAll('.tab').forEach(b => b.onclick = () => { hap(); setTab(b.dataset.t); });
$('q').oninput = render;
$('x').onclick = () => { $('q').value = ''; render(); $('q').focus(); };
$('rf').onclick = () => { hap(); load(true); };
load();
</script></body></html>
"""
