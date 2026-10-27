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


_HTML = """<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,user-scalable=no">
<title>Лента</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{--bg:var(--tg-theme-bg-color,#fff);--tx:var(--tg-theme-text-color,#111);--hint:var(--tg-theme-hint-color,#8a8a8e);
--sec:var(--tg-theme-secondary-bg-color,#f1f1f4);--btn:var(--tg-theme-button-color,#2481cc);--btx:var(--tg-theme-button-text-color,#fff)}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.35 -apple-system,system-ui,Segoe UI,Roboto,sans-serif}
body{padding-bottom:env(safe-area-inset-bottom,0)}
.top{position:sticky;top:0;z-index:5;background:var(--bg);padding:8px 10px 6px}
.tabs{display:flex;gap:6px;margin-bottom:8px}
.tab{flex:1;padding:8px 4px;border:0;border-radius:10px;background:var(--sec);color:var(--tx);font:inherit;font-weight:600}
.tab.on{background:var(--btn);color:var(--btx)}
.q{width:100%;padding:9px 12px;border:0;border-radius:10px;background:var(--sec);color:var(--tx);font:inherit;outline:none}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:2px;padding:0 2px}
.cell{position:relative;aspect-ratio:1;background:var(--sec);overflow:hidden}
.cell img{width:100%;height:100%;object-fit:cover;display:block}
.cell .ph{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:26px;color:var(--hint)}
.cell .u{position:absolute;left:0;right:0;bottom:0;padding:14px 5px 3px;font-size:11px;color:#fff;
background:linear-gradient(transparent,rgba(0,0,0,.62));white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cell .v{position:absolute;top:4px;right:5px;font-size:13px;text-shadow:0 0 3px rgba(0,0,0,.7)}
.list{padding:0 10px}
.row{display:flex;align-items:center;gap:12px;padding:8px 0;border-bottom:1px solid var(--sec)}
.row .t{width:54px;height:54px;border-radius:12px;background:var(--sec);overflow:hidden;flex:none;position:relative}
.row .t img{width:100%;height:100%;object-fit:cover}
.row .n{font-weight:600}.row .c{color:var(--hint);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row .i{min-width:0}
.empty{padding:40px 24px;text-align:center;color:var(--hint)}
.toast{position:fixed;left:50%;bottom:calc(20px + env(safe-area-inset-bottom,0));transform:translateX(-50%) translateY(30px);
background:rgba(0,0,0,.82);color:#fff;padding:9px 16px;border-radius:20px;opacity:0;transition:.25s;pointer-events:none;z-index:9}
.toast.show{opacity:1;transform:translateX(-50%)}
</style></head><body>
<div class="top">
  <div class="tabs">
    <button class="tab on" data-t="feed">Лента</button>
    <button class="tab" data-t="bookmarks">Закладки</button>
    <button class="tab" data-t="subs">Подписки</button>
  </div>
  <input class="q" id="q" type="search" placeholder="Поиск по подписи и нику">
</div>
<div id="view"></div>
<div class="toast" id="toast"></div>
<script>
const tg = window.Telegram && Telegram.WebApp;
if (tg) { tg.ready(); tg.expand(); }
const BASE = location.pathname.replace(/\\/+$/, '');
let DATA = null, TAB = 'feed';
const $ = id => document.getElementById(id);

function toast(t){const e=$('toast');e.textContent=t;e.classList.add('show');clearTimeout(toast.t);toast.t=setTimeout(()=>e.classList.remove('show'),1800)}
async function api(path, extra){
  const r = await fetch(BASE + path, {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(Object.assign({initData: tg ? tg.initData : ''}, extra||{}))});
  if (!r.ok) throw new Error(r.status); return r.json();
}
function el(tag, cls, text){const e=document.createElement(tag); if(cls) e.className=cls; if(text!=null) e.textContent=text; return e}
function img(src){const i=el('img'); i.loading='lazy'; i.src=BASE+src; i.onerror=()=>i.remove(); return i}

async function openPost(sc){
  if(!sc){toast('Поста пока нет в кэше — открой профиль в боте');return}
  try{ await api('/api/open',{sc}); toast('Отправил в чат ✓'); tg&&tg.HapticFeedback&&tg.HapticFeedback.notificationOccurred('success') }
  catch(e){ toast('Не получилось отправить'); tg&&tg.HapticFeedback&&tg.HapticFeedback.notificationOccurred('error') }
}
const match = (it, q) => !q || (it.user+' '+(it.cap||'')).toLowerCase().includes(q);

function render(){
  const q = $('q').value.trim().toLowerCase(), v = $('view'); v.replaceChildren();
  if(!DATA){return}
  if(TAB==='subs'){
    const items = DATA.subs.filter(s=>match(s,q));
    if(!items.length){v.append(el('div','empty', DATA.subs.length?'Ничего не найдено':'Подписок пока нет — добавь аккаунт в боте'));return}
    const l = el('div','list');
    for(const s of items){
      const r = el('div','row'), t = el('div','t'); if(s.thumb) t.append(img(s.thumb)); else t.append(el('div','ph','📷'));
      const i = el('div','i'); i.append(el('div','n','@'+s.user), el('div','c', s.cap || 'Постов в кэше пока нет'));
      r.append(t,i); r.onclick=()=>openPost(s.sc); l.append(r);
    }
    v.append(l); return;
  }
  const items = DATA[TAB].filter(it=>match(it,q));
  if(!items.length){v.append(el('div','empty', DATA[TAB].length?'Ничего не найдено':
    (TAB==='feed'?'Лента пуста: посты появятся, когда бот пришлёт их по твоим подпискам':'Закладок пока нет — жми «📑 В закладки» под постами')));return}
  const g = el('div','grid');
  for(const it of items){
    const c = el('div','cell'); if(it.thumb) c.append(img(it.thumb)); else c.append(el('div','ph', it.video?'🎬':'🖼'));
    if(it.video) c.append(el('div','v','🎬')); c.append(el('div','u','@'+it.user));
    c.onclick=()=>openPost(it.sc); g.append(c);
  }
  v.append(g);
}
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('on',x===b)); TAB=b.dataset.t; render()});
$('q').oninput = render;
(async()=>{
  if(!tg || !tg.initData){$('view').append(el('div','empty','Открой эту страницу из Telegram — через команду /lenta в боте'));return}
  try{ DATA = await api('/api/feed'); render() }
  catch(e){ $('view').append(el('div','empty','Не удалось загрузить. Закрой и открой ленту ещё раз.')) }
})();
</script></body></html>
"""
