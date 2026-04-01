"""
Snitch Instagram Reel Analytics — Backend v8.0
Production-ready FastAPI server for cloud deployment (Railway / Render / Fly.io)

Cloud IP-blocking fix:
  Set SCRAPERAPI_KEY env var on Render/Railway → free tier: scraperapi.com
  Or set PROXY_URL  env var → http://user:pass@proxy-host:port
"""
from __future__ import annotations
import re, asyncio, json, os, datetime
from typing import Optional, Dict, List
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

app = FastAPI(title="Snitch Reel Analytics")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Env config ─────────────────────────────────────────────────────────────────
PROXY_URL      = os.environ.get("PROXY_URL", "").strip()       # e.g. http://user:pass@host:port
SCRAPERAPI_KEY = os.environ.get("SCRAPERAPI_KEY", "").strip()  # from scraperapi.com

# ── Serve frontend ─────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return HTMLResponse("<h1>Snitch API running</h1><p>Place index.html in the same directory.</p>")

# ── Shortcode extraction ───────────────────────────────────────────────────────
_SC_RE = re.compile(r"instagram\.com/(?:reel|p|tv)/([A-Za-z0-9_-]+)")

def _extract_sc(url: str) -> str:
    url = url.strip().rstrip("/")
    m = _SC_RE.search(url)
    if m:
        return m.group(1)
    parts = [p for p in url.split("/") if p]
    return parts[-1]

# ── View-count key priority ────────────────────────────────────────────────────
_VIEW_KEYS = (
    "play_count",
    "clips_aggregated_view_count",
    "ig_play_count",
    "video_play_count",
    "video_view_count",
    "view_count",
)

def _max_from_node(obj, depth: int = 0) -> Optional[int]:
    best: Optional[int] = None
    def _walk(o, d):
        nonlocal best
        if d > 14 or not isinstance(o, (dict, list)):
            return
        if isinstance(o, dict):
            for k in _VIEW_KEYS:
                v = o.get(k)
                if isinstance(v, (int, float)) and int(v) > 0:
                    best = max(best, int(v)) if best is not None else int(v)
            for v in o.values():
                if isinstance(v, (dict, list)):
                    _walk(v, d + 1)
        elif isinstance(o, list):
            for item in o:
                _walk(item, d + 1)
    _walk(obj, depth)
    return best

def _safe_int(val) -> Optional[int]:
    try:
        v = int(val)
        return v if v >= 0 else None
    except (TypeError, ValueError):
        return None

# ── Profile cache ──────────────────────────────────────────────────────────────
_profile_cache: Dict[str, dict] = {}

def _get_profile(L, username: str) -> dict:
    if username in _profile_cache:
        return _profile_cache[username]
    try:
        import instaloader
        prof = instaloader.Profile.from_username(L.context, username)
        data = {"followers": prof.followers, "is_verified": prof.is_verified}
    except Exception:
        data = {"followers": None, "is_verified": False}
    _profile_cache[username] = data
    return data

# ── View extractor ─────────────────────────────────────────────────────────────
def _get_views(post) -> Optional[int]:
    try:
        best = _max_from_node(post._node)
        if best is not None:
            return best
    except Exception:
        pass
    for attr in ("video_play_count", "video_view_count", "play_count"):
        try:
            v = getattr(post, attr, None)
            if v is not None and int(v) > 0:
                return int(v)
        except Exception:
            pass
    return None

# ── instaloader scraper ────────────────────────────────────────────────────────
def _scrape_with_loader(shortcodes: List[str], username: str = None, password: str = None) -> dict:
    try:
        import instaloader
    except ImportError:
        return {sc: {"error": "instaloader not installed"} for sc in shortcodes}

    L = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        quiet=True,
    )

    # ── Inject proxy into the underlying requests.Session ──────────────────────
    if PROXY_URL:
        try:
            L.context._session.proxies.update({
                "http":  PROXY_URL,
                "https": PROXY_URL,
            })
            print(f"[instaloader] using proxy: {PROXY_URL[:30]}…")
        except Exception as e:
            print(f"[instaloader] proxy setup failed: {e}")

    if username and password:
        try:
            L.login(username, password)
        except Exception as e:
            print(f"[login] failed: {e}")
    else:
        session_file = os.path.expanduser("~/.instaloader-session")
        try:
            if os.path.exists(session_file):
                L.load_session_from_file(open(session_file).read().strip(), session_file)
        except Exception:
            pass

    results = {}
    for sc in shortcodes:
        try:
            post = instaloader.Post.from_shortcode(L.context, sc)

            views = _get_views(post)
            try:
                a1 = L.context.get_json(f"p/{sc}/", params={"__a": "1", "__d": "dis"})
                pc = _max_from_node(a1)
                if pc and (views is None or pc > views):
                    views = pc
            except Exception:
                pass

            likes = None
            try:
                likes = _safe_int(post.likes)
            except Exception:
                pass

            comments = None
            try:
                comments = _safe_int(post.comments)
            except Exception:
                pass

            owner = "unknown"
            try:
                owner = post.owner_username
            except Exception:
                try:
                    owner = post.owner_profile.username
                except Exception:
                    pass

            prof      = _get_profile(L, owner)
            followers = prof.get("followers")
            is_verified = prof.get("is_verified", False)

            post_date = None
            try:
                post_date = post.date_utc.strftime("%Y-%m-%d %H:%M")
            except Exception:
                pass

            duration = None
            try:
                d = post.video_duration
                if d is not None:
                    duration = round(float(d), 1)
            except Exception:
                pass

            hashtags = []
            try:
                hashtags = list(post.caption_hashtags)[:15]
            except Exception:
                pass

            thumbnail = None
            try:
                thumbnail = post.url
            except Exception:
                pass

            caption = ""
            try:
                caption = (post.caption or "")[:200]
            except Exception:
                pass

            er = None
            if views and views > 0 and likes is not None and comments is not None:
                er = round((likes + comments) / views * 100, 2)

            view_rate = None
            if views and followers and followers > 0:
                view_rate = round(views / followers * 100, 2)

            results[sc] = {
                "views": views, "likes": likes, "comments": comments,
                "engagement_rate": er, "view_rate": view_rate,
                "author": owner, "handle": f"@{owner}",
                "is_verified": is_verified, "followers": followers,
                "post_date": post_date, "duration": duration,
                "hashtags": hashtags, "thumbnail": thumbnail,
                "caption": caption, "source": "instaloader",
            }
        except Exception as e:
            results[sc] = {"error": str(e), "source": "instaloader"}

    return results

# ── HTML parsing helpers ───────────────────────────────────────────────────────
_VIEW_PATTERNS   = [re.compile(r'"' + k + r'"\s*:\s*(\d+)') for k in _VIEW_KEYS]
_LIKES_PATTERN   = re.compile(r'"edge_media_preview_like"\s*:\s*\{"count"\s*:\s*(\d+)')
_COMMENT_PATTERN = re.compile(r'"edge_media_to_comment"\s*:\s*\{"count"\s*:\s*(\d+)')
_FOLLOW_PATTERN  = re.compile(r'"edge_followed_by"\s*:\s*\{"count"\s*:\s*(\d+)')
_OWNER_PATTERN   = re.compile(r'"username"\s*:\s*"([^"]+)"')
_DATE_PATTERN    = re.compile(r'"taken_at_timestamp"\s*:\s*(\d+)')
_DURATION_PATTERN= re.compile(r'"video_duration"\s*:\s*([\d.]+)')
_HASHTAG_PATTERN = re.compile(r'#(\w+)')

def _parse_html(html: str) -> dict:
    views = None
    for pat in _VIEW_PATTERNS:
        for m in pat.finditer(html):
            v = int(m.group(1))
            if v > 0:
                views = max(views, v) if views else v

    likes    = None
    m = _LIKES_PATTERN.search(html)
    if m: likes = _safe_int(m.group(1))

    comments = None
    m = _COMMENT_PATTERN.search(html)
    if m: comments = _safe_int(m.group(1))

    followers = None
    m = _FOLLOW_PATTERN.search(html)
    if m: followers = _safe_int(m.group(1))

    owner = None
    m = _OWNER_PATTERN.search(html)
    if m: owner = m.group(1)

    post_date = None
    m = _DATE_PATTERN.search(html)
    if m:
        post_date = datetime.datetime.utcfromtimestamp(int(m.group(1))).strftime("%Y-%m-%d %H:%M")

    duration = None
    m = _DURATION_PATTERN.search(html)
    if m: duration = round(float(m.group(1)), 1)

    hashtags = list(dict.fromkeys(_HASHTAG_PATTERN.findall(html)))[:15]

    er = None
    if views and views > 0 and likes is not None and comments is not None:
        er = round((likes + comments) / views * 100, 2)
    view_rate = None
    if views and followers and followers > 0:
        view_rate = round(views / followers * 100, 2)

    return {
        "views": views, "likes": likes, "comments": comments,
        "followers": followers, "author": owner,
        "handle": f"@{owner}" if owner else None,
        "post_date": post_date, "duration": duration,
        "hashtags": hashtags, "engagement_rate": er,
        "view_rate": view_rate, "is_verified": False,
        "source": "html_fallback",
    }

# ── Playwright fallback scraper ────────────────────────────────────────────────
async def _scrape_playwright(shortcodes: List[str]) -> dict:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {sc: {"error": "playwright not available on this server"} for sc in shortcodes}

    results = {}
    try:
        async with async_playwright() as pw:
            launch_args = [
                "--no-sandbox", "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
            ]
            launch_kwargs: dict = {"headless": True, "args": launch_args}
            if PROXY_URL:
                launch_kwargs["proxy"] = {"server": PROXY_URL}

            browser = await pw.chromium.launch(**launch_kwargs)
            ctx = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
                ),
                viewport={"width": 390, "height": 844},
            )
            page = await ctx.new_page()
            await page.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
            )
            for sc in shortcodes:
                try:
                    await page.goto(f"https://www.instagram.com/reel/{sc}/",
                                    wait_until="networkidle", timeout=30000)
                    await asyncio.sleep(2)
                    html = await page.content()
                    data = _parse_html(html)
                    scripts = await page.query_selector_all("script[type='application/json']")
                    for s in scripts:
                        try:
                            obj = json.loads(await s.inner_text())
                            v = _max_from_node(obj)
                            if v and (data["views"] is None or v > data["views"]):
                                data["views"] = v
                        except Exception:
                            pass
                    if data["views"] and data["likes"] is not None and data["comments"] is not None:
                        data["engagement_rate"] = round(
                            (data["likes"] + data["comments"]) / data["views"] * 100, 2
                        )
                    if data["views"] and data["followers"]:
                        data["view_rate"] = round(data["views"] / data["followers"] * 100, 2)
                    results[sc] = data
                except Exception as e:
                    results[sc] = {"error": str(e), "source": "playwright"}
            await browser.close()
    except Exception as e:
        for sc in shortcodes:
            if sc not in results:
                results[sc] = {"error": str(e), "source": "playwright_init"}
    return results

# ── ScraperAPI scraper (residential IP rotation — handles cloud IP blocking) ───
async def _scrape_scraperapi(shortcodes: List[str]) -> dict:
    """
    Uses ScraperAPI (scraperapi.com) to fetch Instagram pages via residential IPs.
    Requires SCRAPERAPI_KEY env var.  Free tier: 1,000 requests/month.
    """
    if not SCRAPERAPI_KEY:
        return {sc: {"error": "SCRAPERAPI_KEY not configured"} for sc in shortcodes}

    try:
        import httpx
    except ImportError:
        return {sc: {"error": "httpx not installed"} for sc in shortcodes}

    results = {}
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    async with httpx.AsyncClient(timeout=90.0, follow_redirects=True) as client:
        for sc in shortcodes:
            try:
                ig_url  = f"https://www.instagram.com/reel/{sc}/"
                api_url = (
                    f"http://api.scraperapi.com/"
                    f"?api_key={SCRAPERAPI_KEY}"
                    f"&url={ig_url}"
                    f"&render=true"          # JS rendering — required for Instagram
                    f"&device_type=mobile"   # mobile layout is lighter / more parseable
                )
                resp = await client.get(api_url, headers=headers)
                html = resp.text

                # Try JSON script tags first (most reliable)
                data = _parse_html(html)

                # Hunt for JSON blobs in <script> tags
                for blob in re.findall(r'<script[^>]*>(\{.*?\})</script>', html, re.DOTALL):
                    try:
                        obj = json.loads(blob)
                        v = _max_from_node(obj)
                        if v and (data["views"] is None or v > data["views"]):
                            data["views"] = v
                    except Exception:
                        pass

                data["source"] = "scraperapi"

                if not data.get("views") and not data.get("author"):
                    results[sc] = {
                        "error": f"ScraperAPI returned no usable data (HTTP {resp.status_code})",
                        "source": "scraperapi",
                    }
                else:
                    if data["views"] and data["likes"] is not None and data["comments"] is not None:
                        data["engagement_rate"] = round(
                            (data["likes"] + data["comments"]) / data["views"] * 100, 2
                        )
                    results[sc] = data
            except Exception as e:
                results[sc] = {"error": str(e), "source": "scraperapi"}

    return results

# ── API ────────────────────────────────────────────────────────────────────────
class ScrapeRequest(BaseModel):
    urls: List[str]
    username: Optional[str] = None
    password: Optional[str] = None

@app.post("/api/scrape")
async def scrape(req: ScrapeRequest):
    shortcodes = [_extract_sc(u) for u in req.urls if u.strip()]
    if not shortcodes:
        raise HTTPException(400, "No valid URLs provided")

    loop = asyncio.get_event_loop()

    # 1) instaloader (with proxy if configured)
    il_results = await loop.run_in_executor(
        None, _scrape_with_loader, shortcodes, req.username, req.password
    )

    # 2) Playwright for any that failed / missing views
    need_pw = [
        sc for sc in shortcodes
        if "error" in il_results.get(sc, {}) or il_results.get(sc, {}).get("views") is None
    ]
    pw_results = {}
    if need_pw:
        pw_results = await _scrape_playwright(need_pw)

    # Merge instaloader + playwright
    still_failed: List[str] = []
    merged: Dict[str, dict] = {}
    for sc in shortcodes:
        il = il_results.get(sc, {})
        pw = pw_results.get(sc, {})
        if "error" in il and "error" not in pw:
            merged[sc] = pw
        elif "error" not in il:
            m = dict(il)
            for k, v in pw.items():
                if m.get(k) is None and v is not None:
                    m[k] = v
            views = m.get("views"); likes = m.get("likes"); comments = m.get("comments")
            followers = m.get("followers")
            if views and views > 0 and likes is not None and comments is not None:
                m["engagement_rate"] = round((likes + comments) / views * 100, 2)
            if views and followers and followers > 0:
                m["view_rate"] = round(views / followers * 100, 2)
            merged[sc] = m
        else:
            still_failed.append(sc)
            merged[sc] = {"error": "instaloader + playwright failed"}

    # 3) ScraperAPI for any still failing
    if still_failed and SCRAPERAPI_KEY:
        sa_results = await _scrape_scraperapi(still_failed)
        for sc in still_failed:
            if sc in sa_results and "error" not in sa_results[sc]:
                merged[sc] = sa_results[sc]

    # Build final response with actionable hints on failure
    final: Dict[str, dict] = {}
    for sc in shortcodes:
        r = merged.get(sc, {})
        if "error" in r:
            hint = ""
            if not SCRAPERAPI_KEY and not PROXY_URL:
                hint = (
                    "Instagram is blocking this server's IP address. "
                    "Fix: add SCRAPERAPI_KEY env var on Render (free tier at scraperapi.com). "
                    "Or set PROXY_URL=http://user:pass@proxy:port to use a residential proxy."
                )
            final[sc] = {"shortcode": sc, "error": r.get("error", "scrape failed"), "hint": hint}
        else:
            final[sc] = r

    return {"results": final}

# ── Health check ───────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    scraperapi_ok = bool(SCRAPERAPI_KEY)
    proxy_ok      = bool(PROXY_URL)
    playwright_ok = False
    try:
        from playwright.async_api import async_playwright  # noqa
        playwright_ok = True
    except ImportError:
        pass

    status = "ok" if (scraperapi_ok or proxy_ok) else "degraded"
    return {
        "status":   status,
        "service":  "snitch-reel-analytics",
        "scrapers": {
            "instaloader": True,
            "playwright":  playwright_ok,
            "scraperapi":  scraperapi_ok,
        },
        "proxy_configured": proxy_ok,
        "note": (
            "Ready — ScraperAPI or proxy active." if (scraperapi_ok or proxy_ok)
            else "WARNING: No proxy/ScraperAPI configured. Instagram will likely block cloud requests. "
                 "Set SCRAPERAPI_KEY or PROXY_URL env var on Render."
        ),
    }

# ── Debug endpoint ─────────────────────────────────────────────────────────────
@app.get("/api/debug/{shortcode}")
async def debug(shortcode: str):
    try:
        import instaloader
        L = instaloader.Instaloader(quiet=True)
        if PROXY_URL:
            L.context._session.proxies.update({"http": PROXY_URL, "https": PROXY_URL})
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        node = post._node
        return {
            "shortcode": shortcode,
            "view_candidates": {k: node.get(k) for k in _VIEW_KEYS if k in node},
            "node_keys": list(node.keys())[:50],
            "views_result": _get_views(post),
        }
    except Exception as e:
        return {"error": str(e)}

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
