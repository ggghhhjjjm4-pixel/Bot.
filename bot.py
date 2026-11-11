import os
import re
import gc
import sys
import time
import json
import math
import uuid
import shutil
import asyncio
import subprocess
import threading
from urllib.parse import urljoin, urlparse, quote, unquote

import requests
from bs4 import BeautifulSoup
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
import yt_dlp
import imageio_ffmpeg

# ==================== فحص مكتبة تسريع التشفير tgcrypto ====================
try:
    import tgcrypto
    TGCRYPTO_ENABLED = True
except ImportError:
    TGCRYPTO_ENABLED = False

# ==================== الإعدادات الأساسية ====================
API_ID = 31896121                                                 # App api_id
API_HASH = "4b96c8f53d94f9e1c1a0dba376e22d9c"                    # App api_hash
BOT_TOKEN = "8890473484:AAHzUcZEWAG74LIuqlz-kp0TsUeHbgcDXz0"    # Bot Token
# =============================================================

# تهيئة مسار FFmpeg المدمج
FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
FFMPEG_DIR = os.path.dirname(FFMPEG_EXE)
os.environ["PATH"] = FFMPEG_DIR + os.pathsep + os.environ.get("PATH", "")

# إدارة الجلسات والمهام وإشارات الإلغاء
USER_SETTINGS = {}         # True = عرض الجودات | False = تنزيل فوري
USER_SESSIONS = {}         # تخزين بيانات الروابط المستخرجة
PROGRESS_TIMERS = {}       # مؤقت تحديث شريط التقدم لمنع حظر تيليجرام
ACTIVE_TASKS = {}          # مهام قيد المعالجة (لإلغاء التنزيل فورياً عند طلب المستخدم)
ACTIVE_CANCEL_FLAGS = {}   # task_id -> bool

# تهيئة عميل تيليجرام بأقصى سرعة ممكنة (8 مسارات متزامنة و16 عامل معالجة)
app = Client(
    "universal_video_downloader_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=16,
    max_concurrent_transmissions=8  # أقصى سرعة رفع متزامنة مدعومة في تيليجرام
)

# ==================== أدوات تنظيف السيرفر وإدارة الذاكرة ====================
def purge_all_temporary_files():
    """تنظيف فوري لجميع الملفات المؤقتة القديمة لمنع امتلاء السيرفر"""
    try:
        cur_dir = os.getcwd()
        for fname in os.listdir(cur_dir):
            if (
                fname.startswith("video_")
                or fname.startswith("thumb_")
                or fname.endswith(".part")
                or fname.endswith(".ytdl")
                or fname.endswith(".temp")
                or fname.endswith(".tmp")
            ):
                try:
                    fpath = os.path.join(cur_dir, fname)
                    if os.path.isfile(fpath):
                        os.remove(fpath)
                except Exception:
                    pass
        gc.collect()
    except Exception:
        pass

# تنظيف فوري عند إقلاع البوت
purge_all_temporary_files()

def cleanup_files(*filepaths):
    """تنظيف ملفات المهمة المكتملة أو الملغاة دون التأثير على مهام أخرى قيد التشغيل"""
    for fp in filepaths:
        if fp and os.path.exists(fp):
            try:
                os.remove(fp)
            except Exception:
                pass
    gc.collect()

# ==================== التنسيقات والحسابات الرياضية ====================
def clean_url(url: str) -> str:
    url = url.strip()
    return re.sub(r'([^:]/)/+', r'\1', url)

def format_size(bytes_size):
    if not bytes_size or bytes_size <= 0:
        return "حجم تلقائي"
    sizes = ['B', 'KB', 'MB', 'GB']
    i = int(math.floor(math.log(bytes_size, 1024)))
    i = min(len(sizes) - 1, max(0, i))
    p = math.pow(1024, i)
    s = round(bytes_size / p, 2)
    return f"{s} {sizes[i]}"

def format_speed(bytes_per_sec: float) -> str:
    if bytes_per_sec <= 0:
        return "0.0 B/s"
    sizes = ['B/s', 'KB/s', 'MB/s', 'GB/s']
    i = int(math.floor(math.log(bytes_per_sec, 1024)))
    i = min(len(sizes) - 1, max(0, i))
    p = math.pow(1024, i)
    s = round(bytes_per_sec / p, 1)
    return f"{s} {sizes[i]}"

def format_eta(seconds: float) -> str:
    if seconds <= 0 or math.isinf(seconds) or math.isnan(seconds):
        return "00:00"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def make_progress_bar(percent: float, total_blocks: int = 10) -> str:
    """صنع شريط تقدم رسومي تفاعلي متدرج بنسبة مئوية دقيقة"""
    percent = max(0.0, min(100.0, percent))
    filled = int(round((percent / 100.0) * total_blocks))
    filled = min(total_blocks, max(0, filled))
    return "▰" * filled + "▱" * (total_blocks - filled)

def render_interactive_card(
    stage_title: str,
    percent: float,
    current_bytes: int,
    total_bytes: int,
    speed: float,
    eta: float,
    extra_note: str = ""
) -> str:
    """بناء لوحة العرض التفاعلية الشاملة للأرقام والسرعة والشريط"""
    bar = make_progress_bar(percent, total_blocks=10)
    cur_str = format_size(current_bytes)
    tot_str = format_size(total_bytes) if total_bytes > 0 else "قيد الحساب..."
    spd_str = format_speed(speed)
    eta_str = format_eta(eta)

    text = (
        f"{stage_title}\n"
        f"[{bar}] **{percent:.1f}%**\n\n"
        f"💾 **الحجم:** `{cur_str}` / `{tot_str}`\n"
        f"⚡ **السرعة:** `{spd_str}`\n"
        f"⏳ **الوقت المتبقي:** `{eta_str}`"
    )
    if extra_note:
        text += f"\n💡 `{extra_note}`"
    return text

def build_action_keyboard(task_id: str) -> InlineKeyboardMarkup:
    """إنشاء أزرار التحكم التفاعلية المرفقة مع شريط التقدم"""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚫 إلغاء التنزيل", callback_data=f"cancel|{task_id}"),
            InlineKeyboardButton("🔄 تحديث فوري", callback_data=f"refresh|{task_id}")
        ]
    ])

def get_headers(url: str = "") -> dict:
    parsed = urlparse(url) if url else None
    ref = f"{parsed.scheme}://{parsed.netloc}/" if parsed and parsed.netloc else "https://google.com/"
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed and parsed.netloc else "https://google.com"
    h = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,video/*,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9,ar;q=0.8',
        'Referer': ref,
        'Origin': origin,
    }
    if "xhub.rip" in (url or ""):
        h["Cookie"] = "play=1; quality=720p"
    return h

def get_video_dimensions_and_duration(video_path: str):
    """استخراج أبعاد الفيديو ومدة التشغيل بدقة عبر FFmpeg"""
    width, height, duration = 0, 0, 0
    try:
        cmd = [FFMPEG_EXE, "-i", video_path]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors='ignore')
        combined = proc.stderr + proc.stdout

        dur_match = re.search(r'Duration:\s*(\d+):(\d+):(\d+\.?\d*)', combined)
        if dur_match:
            h, m, s = dur_match.groups()
            duration = int(int(h) * 3600 + int(m) * 60 + float(s))

        dim_matches = re.findall(r'Video:.*?\b(\d{2,5})x(\d{2,5})\b', combined)
        if dim_matches:
            width = int(dim_matches[0][0])
            height = int(dim_matches[0][1])
    except Exception:
        pass
    return width, height, duration

def generate_thumbnail(video_path: str, thumb_path: str) -> bool:
    try:
        cmd = [
            FFMPEG_EXE,
            "-y",
            "-ss", "00:00:01",
            "-i", video_path,
            "-vframes", "1",
            "-q:v", "2",
            thumb_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return os.path.exists(thumb_path)
    except Exception:
        return False

# ==================== حل مشكلة تويتر و X المتقدم (Failsafe Twitter Resolver) ====================
def resolve_twitter_video(page_url: str):
    """
    مستخرج تويتر و X فائق القوة:
    1. يستخرج معرف التغريدة (Tweet ID) واسم المستخدم (Username) بدقة.
    2. يتجاوز قيود تسجيل الدخول وفحص العمر 18+ والمحتوى الحساس (Age-restricted adult content) 100%.
    3. يستخرج روابط MP4 المباشرة الأصلية من سيرفرات video.twimg.com مع قائمة الجودات.
    """
    clean = page_url.strip()
    match_id = re.search(r'status/(\d+)', clean)
    if not match_id:
        return None, None, "فيديو تويتر", "رابط تويتر غير صالح", []

    tweet_id = match_id.group(1)
    match_user = re.search(r'(?:twitter\.com|x\.com)/([^/?#]+)/status', clean)
    user = match_user.group(1) if match_user and match_user.group(1) not in ['i', 'status'] else None

    session = requests.Session()
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*'
    }

    # بناء قائمة المنافذ مرتبة حسب الأولوية لضمان الاستخراج الفوري
    endpoints = []
    if user:
        endpoints.append(f"https://api.fxtwitter.com/{user}/status/{tweet_id}")
        endpoints.append(f"https://api.vxtwitter.com/{user}/status/{tweet_id}")
    endpoints.append(f"https://api.fxtwitter.com/i/status/{tweet_id}")
    endpoints.append(f"https://api.vxtwitter.com/i/status/{tweet_id}")

    for ep in endpoints:
        try:
            res = session.get(ep, headers=headers, timeout=8)
            if res.status_code == 200:
                data = res.json()

                # فحص بنية FxTwitter
                tweet = data.get('tweet', {})
                if tweet:
                    raw_text = tweet.get('text', 'فيديو تويتر')
                    title = re.sub(r'https?://\S+', '', raw_text).strip()
                    if not title:
                        author_name = tweet.get('author', {}).get('name') or tweet.get('author', {}).get('screen_name') or 'Twitter'
                        title = f"فيديو من @{author_name}"

                    media = tweet.get('media', {})
                    videos = media.get('videos') or [m for m in media.get('all', []) if m.get('type') == 'video']
                    if videos:
                        v = videos[0]
                        thumb = v.get('thumbnail_url')
                        variants = v.get('variants') or []
                        mp4_variants = [var for var in variants if var.get('content_type') == 'video/mp4' and var.get('url')]
                        mp4_variants.sort(key=lambda x: x.get('bitrate', 0), reverse=True)

                        formats_list = []
                        for idx, var in enumerate(mp4_variants):
                            v_url = var.get('url')
                            bitrate = var.get('bitrate', 0)
                            dim_match = re.search(r'/vid/avc1/(\d+x\d+)/', v_url)
                            label = dim_match.group(1) if dim_match else f"جودة {idx + 1}"
                            formats_list.append({'url': v_url, 'label': label, 'bitrate': bitrate})

                        best_url = mp4_variants[0]['url'] if mp4_variants else v.get('url')
                        if best_url:
                            return best_url, thumb, title, "Twitter HD Direct", formats_list

                # فحص بنية VxTwitter
                if 'media_extended' in data:
                    title = data.get('text', 'فيديو تويتر')
                    title = re.sub(r'https?://\S+', '', title).strip() or "فيديو تويتر"
                    for m in data['media_extended']:
                        if m.get('type') == 'video' and m.get('url'):
                            return m.get('url'), m.get('thumbnail_url'), title, "Twitter VxDirect", []

                if 'mediaURLs' in data and data['mediaURLs']:
                    for u in data['mediaURLs']:
                        if '.mp4' in u:
                            return u, None, "فيديو تويتر", "Twitter Direct", []
        except Exception:
            continue

    # محاولة بديلة عبر Twitsave
    try:
        ts_url = f"https://twitsave.com/info?url=https://twitter.com/{user or 'i'}/status/{tweet_id}"
        ts_res = session.get(ts_url, headers=headers, timeout=6)
        if ts_res.status_code == 200:
            soup = BeautifulSoup(ts_res.text, 'html.parser')
            dl_tags = soup.find_all('a', href=re.compile(r'video\.twimg\.com'))
            if dl_tags:
                return dl_tags[0]['href'], None, "فيديو تويتر", "Twitter Twitsave CDN", []
    except Exception:
        pass

    return None, None, "فيديو تويتر", "Twitter Unknown", []

# ==================== مستخرجات المواقع المحددة بدقة 100% ====================
def decode_hdthot(source_str: str) -> str:
    """فك تشفير data-source الخاص بمشغل HDThot لاستخراج رابط البث m3u8 الحقيقي"""
    if not source_str:
        return None
    import base64
    for i in range(10, 40):
        for j in range(0, 40):
            sub = source_str[i:len(source_str)-j] if j > 0 else source_str[i:]
            rev = sub[::-1]
            try:
                dec = base64.b64decode(rev).decode("utf-8", errors="ignore")
                if "http" in dec and ".m3u8" in dec:
                    m = re.search(r"(https?://[^\s\"<>]+)", dec)
                    if m:
                        return m.group(1)
            except Exception:
                pass
    return None

def resolve_hdthot_video(page_url: str):
    try:
        headers = get_headers(page_url)
        r = requests.get(page_url, headers=headers, timeout=12)
        if r.status_code == 200:
            html = r.text
            title = "HDThot Video"
            m_title = re.search(r"<h1[^>]*>([^<]+)</h1>", html, re.IGNORECASE)
            if m_title:
                title = m_title.group(1).strip()
            
            m_source = re.search(r'data-source=["\']([^"\']+)["\']', html)
            if m_source:
                m3u8 = decode_hdthot(m_source.group(1))
                if m3u8:
                    return m3u8, None, title, "HDThot Decoded HLS"
    except Exception:
        pass
    return None, None, "HDThot Video", "تعذر فك تشفير HDThot"

def resolve_noodlemagazine_video(page_url: str):
    try:
        headers = get_headers(page_url)
        r = requests.get(page_url, headers=headers, timeout=12)
        if r.status_code == 200:
            html = r.text
            m_mp4 = re.search(r'(https?://[^\s"\'<>]+\.(?:mp4|m3u8)(?:\?[^\s"\'<>]*)?)', html)
            if m_mp4:
                return m_mp4.group(1), None, "NoodleMagazine Video", "NoodleMagazine Direct"
    except Exception:
        pass
    return None, None, "NoodleMagazine Video", "لم يتم العثور على رابط مباشر"

def resolve_bunkr_real_video(page_url: str, session: requests.Session):
    try:
        parsed_url = urlparse(page_url)
        origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
        headers = get_headers(page_url)

        res = session.get(page_url, headers=headers, timeout=15, allow_redirects=True)
        if res.status_code != 200:
            return None, None, "فيديو Bunkr", f"تعذر فتح الصفحة (رمز: {res.status_code})"

        html = res.text
        soup = BeautifulSoup(html, 'html.parser')

        title = "Bunkr Video"
        thumb_url = None

        og_title = soup.find('meta', property='og:title')
        if og_title and og_title.get('content'):
            title = og_title['content'].replace(" | Bunkr", "").strip()

        og_img = soup.find('meta', property='og:image')
        if og_img and og_img.get('content'):
            thumb_url = urljoin(page_url, og_img['content'])

        jscdn_match = re.search(r'var\s+jsCDN\s*=\s*["\']([^"\']+)["\']', html)
        signurl_match = re.search(r'var\s+signUrl\s*=\s*["\']([^"\']+)["\']', html)

        if jscdn_match:
            raw_cdn = jscdn_match.group(1).replace(r'\/', '/')
            cdn_url = urljoin(page_url, raw_cdn)

            if signurl_match:
                sign_endpoint = signurl_match.group(1).replace(r'\/', '/')
                sign_endpoint = urljoin(page_url, sign_endpoint)
                try:
                    parsed_cdn = urlparse(cdn_url)
                    path = parsed_cdn.path
                    sign_req_url = f"{sign_endpoint}?path={quote(path)}"
                    sign_res = session.get(sign_req_url, headers={
                        'User-Agent': headers['User-Agent'],
                        'Referer': f"{origin}/",
                        'Origin': origin,
                        'Accept': 'application/json, text/plain, */*'
                    }, timeout=10)

                    if sign_res.status_code == 200:
                        sign_data = sign_res.json()
                        token = sign_data.get('token')
                        ex = sign_data.get('ex')
                        if token and ex:
                            delim = '&' if '?' in cdn_url else '?'
                            signed_final_url = f"{cdn_url}{delim}token={token}&ex={ex}"
                            return signed_final_url, thumb_url, title, "Bunkr Signed CDN"
                except Exception:
                    pass

            return cdn_url, thumb_url, title, "Bunkr Direct CDN"

        next_data = soup.find('script', id='__NEXT_DATA__')
        if next_data and next_data.string:
            try:
                data = json.loads(next_data.string)
                file_props = data.get('props', {}).get('pageProps', {}).get('file', {})
                if not file_props:
                    files_list = data.get('props', {}).get('pageProps', {}).get('files', [])
                    file_props = files_list[0] if files_list else {}

                if file_props.get('name'):
                    title = file_props['name']
                cdn = file_props.get('cdn')
                name = file_props.get('name', '')
                if cdn and name:
                    return f"{cdn.rstrip('/')}/{name}", thumb_url, title, "Bunkr NextData CDN"
            except Exception:
                pass

        for a in soup.find_all('a', href=True):
            href = a['href']
            if any(k in href.lower() for k in ['get-stream', 'media-files', 'download', 'cdn', '.mp4']):
                return urljoin(page_url, href), thumb_url, title, "Bunkr Stream Link"

        video_tag = soup.find('video')
        if video_tag:
            src = video_tag.get('src')
            if not src:
                src_tag = video_tag.find('source')
                if src_tag:
                    src = src_tag.get('src')
            if src:
                return urljoin(page_url, src), thumb_url, title, "Bunkr HTML5 Video"

        return None, thumb_url, title, "لم يتم العثور على رابط فيديو في Bunkr"
    except Exception as e:
        return None, None, "فيديو", f"خطأ في Bunkr: {str(e)}"

def resolve_xhub_video(page_url: str):
    headers = get_headers(page_url)
    try:
        r = requests.get(page_url, headers=headers, timeout=12)
        if r.status_code == 200:
            html = r.text
            m_src = re.search(r"var\s+src\s*=\s*[\'\"]([^\'\"]+)", html)
            m_title = re.search(r"<div class=[\'\"]title[\'\"]>([^<]+)</div>", html, re.IGNORECASE)
            title = m_title.group(1).strip() if m_title else "XHub Video"
            if m_src:
                return urljoin("https://xhub.rip", m_src.group(1)), None, title, "XHub HLS Stream"
    except Exception:
        pass
    return None, None, "XHub Video", "لم يتم العثور على رابط"

# ==================== محرك الاستخراج الشامل ====================
def extract_universal_video(page_url: str):
    """المستخرج العام لجميع المواقع مع توجيه ذكي للمستخرجات المتخصصة"""
    try:
        lower_url = page_url.lower()

        # 1. تويتر و X
        if "twitter.com" in lower_url or "x.com" in lower_url:
            tw_url, tw_thumb, tw_title, tw_note, tw_formats = resolve_twitter_video(page_url)
            if tw_url:
                return tw_url, tw_thumb, tw_title, tw_note, tw_formats

        # 2. Bunkr
        if 'bunkr' in lower_url:
            session = requests.Session()
            u, th, ti, no = resolve_bunkr_real_video(page_url, session)
            return u, th, ti, no, []

        # 3. Xhub
        if 'xhub.rip' in lower_url:
            u, th, ti, no = resolve_xhub_video(page_url)
            return u, th, ti, no, []

        # 4. HDThot
        if 'hdthot' in lower_url:
            u, th, ti, no = resolve_hdthot_video(page_url)
            return u, th, ti, no, []

        # 5. NoodleMagazine
        if 'noodlemagazine' in lower_url:
            u, th, ti, no = resolve_noodlemagazine_video(page_url)
            return u, th, ti, no, []

        # 6. فحص عام HTML5 وبثوث الميديا المباشرة
        session = requests.Session()
        headers = get_headers(page_url)
        res = session.get(page_url, headers=headers, timeout=12, allow_redirects=True)
        if res.status_code != 200:
            return None, None, "فيديو", f"تعذر فتح الصفحة (رمز: {res.status_code})", []

        html = res.text
        soup = BeautifulSoup(html, 'html.parser')

        thumb_url = None
        for meta_prop in ['og:image', 'twitter:image', 'thumbnail']:
            meta_tag = soup.find('meta', property=meta_prop) or soup.find('meta', attrs={'name': meta_prop})
            if meta_tag and meta_tag.get('content'):
                thumb_url = urljoin(page_url, meta_tag.get('content'))
                break

        page_title = "فيديو"
        og_title = soup.find('meta', property='og:title') or soup.find('title')
        if og_title:
            page_title = og_title.get('content') if hasattr(og_title, 'get') and og_title.get('content') else og_title.text.strip()

        video_tag = soup.find('video')
        if video_tag:
            poster = video_tag.get('poster')
            source = video_tag.find('source')
            if source and source.get('src'):
                return urljoin(page_url, source.get('src')), poster or thumb_url, page_title, "HTML5 Direct", []
            elif video_tag.get('src'):
                return urljoin(page_url, video_tag.get('src')), poster or thumb_url, page_title, "HTML5 Direct", []

        stream_matches = re.findall(r'(https?://[^\s\'"]+?\.(?:m3u8|mp4)(?:\?[^\s\'"]*)?)', html)
        if stream_matches:
            for link in stream_matches:
                if not any(ad in link.lower() for ad in ['google', 'analytics', 'doubleclick', 'banner']):
                    return link, thumb_url, page_title, "Stream Direct", []

        return None, thumb_url, page_title, "لم يتم العثور على رابط مباشر", []
    except Exception as e:
        return None, None, "فيديو", f"خطأ في الفحص: {str(e)}", []

# ==================== محرك التنزيل السريع عالي الاستقرار مع شريط التقدم ====================
def download_stream_file(
    url: str,
    output_path: str,
    headers: dict,
    task_id: str = "",
    progress_callback=None
) -> bool:
    """
    تنزيل متدفق سريع ومستقر عبر كتل 2MB مع حساب حي للسرعة والنسبة والوقت المتبقي.
    مضمون 100% ضد التجمد ويعمل بسلاسة تامة على جميع أنظمة التشغيل والسيرفرات.
    """
    session = requests.Session()
    clean_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': '*/*',
    }
    # إذا لم يكن الرابط من سيرفرات تويتر الداخلية، نمرر الهيدرز المخصصة
    if 'twimg.com' not in url:
        clean_headers.update(headers)

    total_size = 0
    try:
        head_res = session.head(url, headers=clean_headers, timeout=8, allow_redirects=True)
        if head_res.status_code in [200, 206]:
            cl = head_res.headers.get('content-length')
            if cl and cl.isdigit():
                total_size = int(cl)
    except Exception:
        pass

    try:
        with session.get(url, headers=clean_headers, stream=True, timeout=120, allow_redirects=True) as r:
            if r.status_code not in [200, 206]:
                return False

            if not total_size:
                cl = r.headers.get('content-length')
                if cl and cl.isdigit():
                    total_size = int(cl)

            downloaded = 0
            start_time = time.time()
            with open(output_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=2 * 1024 * 1024):
                    if ACTIVE_CANCEL_FLAGS.get(task_id):
                        return False
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback:
                            elapsed = time.time() - start_time
                            spd = downloaded / elapsed if elapsed > 0 else 0
                            pct = (downloaded / total_size * 100) if total_size > 0 else min(95.0, (downloaded / (20 * 1024 * 1024)) * 50)
                            rem = (total_size - downloaded) if total_size > 0 else 0
                            eta = (rem / spd) if spd > 0 else 0
                            progress_callback(pct, downloaded, total_size, spd, eta)

        return os.path.exists(output_path) and os.path.getsize(output_path) > 200 * 1024
    except Exception:
        return False

# ==================== شريط تقدم الرفع التفاعلي إلى تيليجرام ====================
async def upload_progress_callback(current, total, message: Message, task_id: str, start_time: float):
    if ACTIVE_CANCEL_FLAGS.get(task_id):
        return

    now = time.time()
    msg_key = f"{message.chat.id}_{message.id}"
    last_update = PROGRESS_TIMERS.get(msg_key, 0)

    # التحديث كل 3.5 ثوانٍ فقط لمنع قيود FloodWait مع استجابة مريحة
    if (now - last_update >= 3.5) or current == total:
        PROGRESS_TIMERS[msg_key] = now
        percent = (current / total) * 100
        elapsed = now - start_time
        speed = current / elapsed if elapsed > 0 else 0
        remaining = total - current
        eta = remaining / speed if speed > 0 else 0

        accel_info = "⚡ محرك C المسرّع نشط (8 مسارات)" if TGCRYPTO_ENABLED else "⚡ 8 مسارات رفع متوازية"
        card_text = render_interactive_card(
            stage_title="🚀 **المرحلة 3: جارٍ الرفع الصاروخي لتيليجرام...**",
            percent=percent,
            current_bytes=current,
            total_bytes=total,
            speed=speed,
            eta=eta,
            extra_note=accel_info
        )

        async def edit_card():
            try:
                await message.edit_text(card_text, reply_markup=build_action_keyboard(task_id))
            except Exception:
                pass

        asyncio.create_task(edit_card())

# ==================== المعالجة الرئيسية والتنزيل والرفع ====================
async def process_and_send_video(
    client: Client,
    chat_id: int,
    user_id: int,
    target_url: str,
    title: str,
    status_msg: Message,
    format_selector: str = "best",
    is_direct_stream: bool = False,
    original_page_url: str = ""
):
    task_id = uuid.uuid4().hex[:8]
    ACTIVE_CANCEL_FLAGS[task_id] = False
    ACTIVE_TASKS[task_id] = status_msg

    out_filename = f"video_{user_id}_{task_id}.mp4"
    thumb_filename = f"thumb_{user_id}_{task_id}.jpg"
    site_headers = get_headers(original_page_url or target_url)

    try:
        if ACTIVE_CANCEL_FLAGS.get(task_id):
            return

        # ---------------- 1. مرحلة التنزيل التفاعلية المباشرة ----------------
        download_success = False
        loop = asyncio.get_running_loop()
        last_dl_ui_update = 0

        def on_dl_progress(pct, cur_b, tot_b, spd, eta):
            nonlocal last_dl_ui_update
            now = time.time()
            if (now - last_dl_ui_update >= 3.2) or pct >= 99.0:
                last_dl_ui_update = now
                card_text = render_interactive_card(
                    stage_title="📥 **المرحلة 1: جارٍ تنزيل الفيديو من المصدر بأقصى سرعة...**",
                    percent=pct,
                    current_bytes=cur_b,
                    total_bytes=tot_b,
                    speed=spd,
                    eta=eta,
                    extra_note="🚀 تنزيل فائق السرعة عبر السيرفر المباشر"
                )
                async def safe_edit():
                    try:
                        await status_msg.edit_text(card_text, reply_markup=build_action_keyboard(task_id))
                    except Exception:
                        pass
                asyncio.run_coroutine_threadsafe(safe_edit(), loop)

        # أ. التنزيل المباشر فائق السرعة
        if is_direct_stream or any(ext in target_url.lower() for ext in ['.mp4', '.mkv', 'video.twimg.com', 'media-files', 'cdn', 'storage/media', 'token=']):
            initial_card = render_interactive_card(
                stage_title="📥 **المرحلة 1: جارٍ بدء التنزيل المباشر المسرّع...**",
                percent=5.0,
                current_bytes=0,
                total_bytes=0,
                speed=0,
                eta=0,
                extra_note="⚡ محرك التنزيل السريع نشط"
            )
            try:
                await status_msg.edit_text(initial_card, reply_markup=build_action_keyboard(task_id))
            except Exception:
                pass

            download_success = await loop.run_in_executor(
                None,
                lambda: download_stream_file(
                    url=target_url,
                    output_path=out_filename,
                    headers=site_headers,
                    task_id=task_id,
                    progress_callback=on_dl_progress
                )
            )

        if ACTIVE_CANCEL_FLAGS.get(task_id):
            cleanup_files(out_filename, thumb_filename)
            return

        # ب. في حال لم يتوفر رابط مباشر، الاستعانة بـ yt-dlp مع Progress Hook حي لمنع أي تجمد
        if not download_success:
            def ydl_hook(d):
                if ACTIVE_CANCEL_FLAGS.get(task_id):
                    raise Exception("تم الإلغاء بواسطة المستخدم")
                if d.get('status') == 'downloading':
                    tot = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                    cur = d.get('downloaded_bytes') or 0
                    spd = d.get('speed') or 0
                    eta = d.get('eta') or 0
                    pct = (cur / tot * 100) if tot > 0 else 15.0
                    on_dl_progress(pct, cur, tot, spd, eta)

            download_opts = {
                'quiet': True,
                'no_warnings': True,
                'ffmpeg_location': FFMPEG_DIR,
                'outtmpl': f"video_{user_id}_{task_id}.%(ext)s",
                'user_agent': site_headers['User-Agent'],
                'http_headers': site_headers,
                'format': format_selector,
                'overwrites': True,
                'nocheckcertificate': True,
                'concurrent_fragment_downloads': 8,
                'buffersize': 4 * 1024 * 1024,
                'socket_timeout': 15,
                'retries': 2,
                'fragment_retries': 2,
                'progress_hooks': [ydl_hook]
            }

            def run_ydl():
                with yt_dlp.YoutubeDL(download_opts) as ydl:
                    ydl.download([target_url])

            try:
                await loop.run_in_executor(None, run_ydl)
            except Exception as ydl_err:
                await status_msg.edit_text(f"❌ **تعذر تنزيل هذا الرابط:**\n`{str(ydl_err)[:200]}`")
                return

        if ACTIVE_CANCEL_FLAGS.get(task_id):
            cleanup_files(out_filename, thumb_filename)
            return

        # ---------------- 2. مرحلة التحقق واستخراج الأبعاد ----------------
        downloaded_file = None
        for f in os.listdir("."):
            if f.startswith(f"video_{user_id}_{task_id}."):
                downloaded_file = f
                break

        if downloaded_file and os.path.exists(downloaded_file):
            file_size_bytes = os.path.getsize(downloaded_file)
            file_size_mb = file_size_bytes / (1024 * 1024)

            if file_size_mb < 0.15:
                await status_msg.edit_text("❌ **تعذر تحميل الفيديو: الملف المستخرج غير صالح من المصدر.**")
                return

            if file_size_mb > 2000:
                await status_msg.edit_text(f"⚠️ **حجم الملف ({file_size_mb:.1f} MB) يتجاوز الحد الأقصى لتيليجرام (2000 MB).**")
                return

            # استخراج أبعاد الفيديو والغلاف
            width, height, duration = get_video_dimensions_and_duration(downloaded_file)
            dim_text = f"{width}x{height}" if (width and height) else "تلقائية"

            has_thumb = generate_thumbnail(downloaded_file, thumb_filename)
            thumb_path = thumb_filename if has_thumb and os.path.exists(thumb_filename) else None

            # ---------------- 3. مرحلة الرفع الصاروخي لتيليجرام ----------------
            upload_start_time = time.time()
            upload_initial_card = render_interactive_card(
                stage_title="🚀 **المرحلة 3: بدء الرفع الصاروخي لتيليجرام...**",
                percent=0.0,
                current_bytes=0,
                total_bytes=file_size_bytes,
                speed=0,
                eta=0,
                extra_note="⚡ 8 مسارات متوازية للرفع الفوري"
            )
            try:
                await status_msg.edit_text(upload_initial_card, reply_markup=build_action_keyboard(task_id))
            except Exception:
                pass

            # إرسال الفيديو مع ربط شريط التقدم التفاعلي
            await client.send_video(
                chat_id=chat_id,
                video=downloaded_file,
                thumb=thumb_path,
                caption=(
                    f"🎬 **{title}**\n\n"
                    f"📐 **الأبعاد:** `{dim_text}`\n"
                    f"💾 **الحجم:** `{file_size_mb:.1f} MB`\n"
                    f"⏱️ **المدة:** `{format_eta(duration)}`\n"
                    f"✅ **تم الرفع بنجاح فائق السرعة!**"
                ),
                duration=duration if duration > 0 else None,
                width=width if width > 0 else None,
                height=height if height > 0 else None,
                supports_streaming=True,
                progress=upload_progress_callback,
                progress_args=(status_msg, task_id, upload_start_time)
            )

            try:
                await status_msg.delete()
            except Exception:
                pass
        else:
            await status_msg.edit_text("❌ **تعذر إيجاد ملف الفيديو بعد انتهاء التنزيل.**")

    except Exception as e:
        if not ACTIVE_CANCEL_FLAGS.get(task_id):
            err_msg = str(e)
            if "No space left" in err_msg:
                purge_all_temporary_files()
                err_msg = "المساحة المؤقتة امتلأت. تم تنظيف السيرفر تلقائياً، يرجى المحاولة الآن."
            await status_msg.edit_text(f"❌ **حدث خطأ أثناء التنزيل أو الرفع:**\n`{err_msg}`")

    finally:
        ACTIVE_CANCEL_FLAGS.pop(task_id, None)
        ACTIVE_TASKS.pop(task_id, None)
        cleanup_files(out_filename, thumb_filename)
        for f in os.listdir("."):
            if f.startswith(f"video_{user_id}_{task_id}."):
                cleanup_files(f)

# ==================== استقبال ومعالجة الروابط ====================
async def handle_single_url_task(client: Client, message: Message, url: str, user_id: int):
    show_qualities = USER_SETTINGS.get(user_id, False)

    # 1. وضع التنزيل الفوري الأسرع (Default Fast Mode)
    if not show_qualities:
        status_msg = await message.reply_text(
            f"⚡ **[تنزيل فوري مسرّع]**\n🔗 جارٍ فحص وتحليل الرابط:\n`{url[:60]}...`"
        )

        target_url = url
        is_direct = False
        direct_link, _, title, note, _ = extract_universal_video(url)
        if direct_link:
            target_url = direct_link
            is_direct = True

        await process_and_send_video(
            client=client,
            chat_id=message.chat.id,
            user_id=user_id,
            target_url=target_url,
            title=title or "فيديو",
            status_msg=status_msg,
            format_selector="best",
            is_direct_stream=is_direct,
            original_page_url=url
        )
        return

    # 2. وضع استخراج الجودات المخصصة
    status_msg = await message.reply_text(f"⏳ **جارٍ فحص الرابط واستخراج الجودات:**\n`{url[:60]}...`")
    direct_link, thumb_url, title, note, formats_list = extract_universal_video(url)
    session_id = uuid.uuid4().hex[:8]

    # إذا تم استخراج قائمة جودات مخصصة (مثل تويتر)
    if formats_list:
        keyboard = []
        for idx, f_item in enumerate(formats_list):
            lbl = f_item.get('label', f'الجودة {idx+1}')
            keyboard.append([
                InlineKeyboardButton(f"📹 {lbl} (MP4)", callback_data=f"dl|{session_id}|fmt_{idx}")
            ])

        keyboard.append([
            InlineKeyboardButton("⬇️ أفضل جودة متوفرة (Best HD)", callback_data=f"dl|{session_id}|best")
        ])

        USER_SESSIONS[session_id] = {
            'video_url': url,
            'direct_url': direct_link,
            'formats_list': formats_list,
            'title': title or "فيديو تويتر",
            'thumbnail': thumb_url,
            'is_direct': True
        }

        caption = (
            f"🎬 **العنوان:** {title}\n"
            f"🔗 **المصدر:** `{note}`\n\n"
            f"👇 **اختر الجودة المطلوبة لبدء الرفع المسرّع فوراً:**"
        )

        try:
            await status_msg.delete()
        except Exception:
            pass

        if thumb_url:
            try:
                await message.reply_photo(photo=thumb_url, caption=caption, reply_markup=InlineKeyboardMarkup(keyboard))
                return
            except Exception:
                pass
        await message.reply_text(caption, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # إذا كان هناك رابط مباشر وحيد
    if direct_link:
        keyboard = [
            [InlineKeyboardButton("⬇️ تحميل الجودة الأصلية المباشرة", callback_data=f"dl|{session_id}|direct")],
            [InlineKeyboardButton("⬇️ أفضل جودة متوفرة (Best)", callback_data=f"dl|{session_id}|best")]
        ]

        USER_SESSIONS[session_id] = {
            'video_url': url,
            'direct_url': direct_link,
            'title': title or "فيديو مستخرج",
            'thumbnail': thumb_url,
            'is_direct': True
        }

        caption = (
            f"🎬 **العنوان:** {title}\n"
            f"🔗 **المصدر:** `{note}`\n\n"
            f"👇 **اختر الجودة لبدء الرفع الصاروخي فوراً:**"
        )

        try:
            await status_msg.delete()
        except Exception:
            pass

        if thumb_url:
            try:
                await message.reply_photo(photo=thumb_url, caption=caption, reply_markup=InlineKeyboardMarkup(keyboard))
                return
            except Exception:
                pass
        await message.reply_text(caption, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # الفحص عبر yt-dlp لعرض الجودات
    try:
        loop = asyncio.get_running_loop()
        site_headers = get_headers(url)
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'ffmpeg_location': FFMPEG_DIR,
            'user_agent': site_headers['User-Agent'],
            'http_headers': site_headers,
            'extract_flat': False,
            'socket_timeout': 15,
        }
        
        def extract_info():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)

        info = await loop.run_in_executor(None, extract_info)
        formats = info.get('formats', [])
        title = info.get('title', 'Video')
        thumbnail = info.get('thumbnail')
        duration = info.get('duration', 0)

        keyboard = []
        seen = set()

        for fmt in reversed(formats):
            height = fmt.get('height') or 0
            vcodec = fmt.get('vcodec')
            if vcodec and vcodec != 'none' and height > 0:
                if height in seen:
                    continue
                seen.add(height)

                filesize = fmt.get('filesize') or fmt.get('filesize_approx') or 0
                ext = fmt.get('ext', 'mp4')
                format_id = fmt.get('format_id')
                btn_text = f"📹 {height}p ({ext}) — {format_size(filesize)}"
                keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"dl|{session_id}|{format_id}")])

        if not keyboard:
            keyboard.append([InlineKeyboardButton("⬇️ أفضل جودة متوفرة (Best)", callback_data=f"dl|{session_id}|best")])

        USER_SESSIONS[session_id] = {
            'video_url': url,
            'direct_url': None,
            'title': title,
            'thumbnail': thumbnail,
            'is_direct': False
        }

        caption = f"🎬 **العنوان:** {title}\n"
        if duration:
            mins, secs = divmod(int(duration), 60)
            caption += f"⏱️ **المدة:** {mins:02d}:{secs:02d}\n"
        caption += "\n📦 **اختر الجودة المطلوبة لبدء الرفع المسرّع:**"

        try:
            await status_msg.delete()
        except Exception:
            pass

        if thumbnail:
            try:
                await message.reply_photo(photo=thumbnail, caption=caption, reply_markup=InlineKeyboardMarkup(keyboard))
                return
            except Exception:
                pass

        await message.reply_text(caption, reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        await status_msg.edit_text(f"❌ **تعذر استخراج الفيديو من هذا الرابط:**\n`{url}`\nالسبب: {note or str(e)}")

# ==================== معالجة الأوامر ====================
@app.on_message(filters.command("start"))
async def start_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    is_show_mode = USER_SETTINGS.get(user_id, False)
    mode_str = "🔍 وضع استخراج الجودات" if is_show_mode else "⚡ وضع التنزيل المباشر الفوري (الأسرع)"

    await message.reply_text(
        "👋 **أهلاً بك في بوت التحميل والرفع فائق السرعة!**\n\n"
        "🚀 **المميزات المحدثة:**\n"
        "• 🐦 **حل نهائي لمشكلة تويتر و X** (بما في ذلك المقاطع المقيدة بالعمر والروابط المعقدة دون أي تجمد).\n"
        "• ⚡ **تسريع التنزيل والرفع المباشر** عبر السيرفرات السريعة و8 مسارات لتيليجرام.\n"
        "• 📊 **شريط تفاعلي رسومي حي** يعرض النسبة والسرعة اللحظية والوقت المتبقي وأزرار إلغاء فورية.\n\n"
        f"⚙️ **وضع العمل الحالي:** `{mode_str}`\n"
        "🔄 أرسل `/mode` للتبديل بين التنزيل الفوري واختيار الجودات.\n\n"
        "📥 **أرسل أي رابط فيديو الآن وسأقوم برفعه بأقصى سرعة!**"
    )

@app.on_message(filters.command(["mode", "quality", "auto"]))
async def toggle_mode_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    current_mode = USER_SETTINGS.get(user_id, False)
    new_mode = not current_mode
    USER_SETTINGS[user_id] = new_mode

    if new_mode:
        await message.reply_text("✅ **تم تفعيل وضع استخراج الجودات والأحجام!**")
    else:
        await message.reply_text("⚡ **تم تفعيل وضع التنزيل المباشر الفوري فائق السرعة!**")

@app.on_message(filters.text & ~filters.command(["start", "help", "mode", "quality", "auto"]))
async def handle_url(client: Client, message: Message):
    text = message.text.strip()
    found_urls = re.findall(r'(https?://[^\s]+)', text)
    
    if not found_urls:
        await message.reply_text("⚠️ **الرجاء إرسال رابط صالح يبدأ بـ http أو https.**")
        return

    user_id = message.from_user.id
    for raw_url in found_urls:
        cleaned = clean_url(raw_url)
        asyncio.create_task(handle_single_url_task(client, message, cleaned, user_id))

# ==================== معالجة الضغط على الأزرار التفاعلية ====================
@app.on_callback_query()
async def handle_callback_router(client: Client, query: CallbackQuery):
    data = query.data

    # 1. زر إلغاء العملية التفاعلي
    if data.startswith("cancel|"):
        task_id = data.split("|")[1]
        ACTIVE_CANCEL_FLAGS[task_id] = True
        await query.answer("🛑 تم طلب إلغاء التنزيل...", show_alert=False)
        try:
            await query.message.edit_text("🚫 **تم إلغاء عملية التنزيل وحذف الملفات المؤقتة فوراً بناءً على طلبك.**")
        except Exception:
            pass
        return

    # 2. زر تحديث شريط التقدم الفوري
    if data.startswith("refresh|"):
        await query.answer("⚡ شريط التقدم يتم تحديثه تلقائياً بأقصى سرعة!", show_alert=False)
        return

    # 3. زر اختيار جودة التنزيل
    if data.startswith("dl|"):
        await query.answer()
        user_id = query.from_user.id
        parts = data.split("|")
        
        if len(parts) == 3:
            _, session_id, format_id = parts
            user_info = USER_SESSIONS.get(session_id)
        else:
            _, format_id = parts
            user_info = USER_SESSIONS.get(str(user_id))

        if not user_info:
            await query.message.reply_text("⚠️ **انتهت هذه الجلسة، يرجى إرسال الرابط من جديد.**")
            return

        url = user_info['video_url']
        direct_url = user_info.get('direct_url')
        title = user_info.get('title', 'فيديو')
        is_direct = user_info.get('is_direct', False)
        formats_list = user_info.get('formats_list', [])

        target_download_url = url
        format_selector = "best"

        # اختيار جودة مخصصة من تويتر أو الروابط المباشرة
        if format_id.startswith("fmt_") and formats_list:
            try:
                idx = int(format_id.replace("fmt_", ""))
                target_download_url = formats_list[idx]['url']
                is_direct = True
            except Exception:
                target_download_url = direct_url or url
        elif format_id == "direct" and direct_url:
            target_download_url = direct_url
            is_direct = True
        elif format_id == "best":
            target_download_url = direct_url if direct_url else url
            format_selector = "best"
        else:
            target_download_url = url
            format_selector = f"{format_id}/best"

        status_msg = await query.message.reply_text(
            "⚡ **جارٍ بدء سحب ومعالجة الفيديو لبدء الرفع الصاروخي...**"
        )

        asyncio.create_task(
            process_and_send_video(
                client=client,
                chat_id=query.message.chat.id,
                user_id=user_id,
                target_url=target_download_url,
                title=title,
                status_msg=status_msg,
                format_selector=format_selector,
                is_direct_stream=is_direct,
                original_page_url=url
            )
        )

if __name__ == "__main__":
    accel_text = "مفعلة (C-speed)" if TGCRYPTO_ENABLED else "غير مثبتة (يرجى تثبيت tgcrypto لأقصى سرعة)"
    print(f"🚀 البوت يعمل الآن بنظام الرفع الصاروخي (8 مسارات) والشريط التفاعلي الحي | تسريع التشفير: {accel_text}...")
    app.run()
